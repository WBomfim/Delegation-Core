"""
embeddings.py: BGE embedding function factory and text chunking utilities.

Isolated from vault.py so the embedding backend can be swapped without
touching vault logic. normalize_embeddings=True is mandatory for BGE
cosine similarity to be correct.

New in v0.2 (previously embedded in vault.py).
"""

import logging
import os
from pathlib import Path

logger = logging.getLogger("embeddings")


#: Per-model retrieval calibration, measured on a real vault rather than guessed.
#:
#: Cosine thresholds are NOT portable between embedding models: a short query
#: against a long note scores on a different scale for each one. Switching this
#: vault from bge-base-en-v1.5 to bge-m3 moved the whole distribution down, and
#: carrying the old numbers over reported every result as "low confidence" while
#: the old 0.55 floor silently cut three of four known-good answers.
#:
#: Measured head-to-head over the same 1608 indexed rows, 9 known-answer queries:
#:   bge-base-en-v1.5: sharper when it hits (0.75-0.82 at rank 1), but returned
#:                      nothing at all for 4 of 9, three of them Portuguese.
#:   bge-m3          : found the answer in all 9, at a compressed scale
#:                      (irrelevant p95 0.480 / max 0.524; correct 0.518-0.677).
MODEL_PROFILES: dict[str, dict] = {
    "BAAI/bge-base-en-v1.5": {
        "collection": "vault_bge",          # historical name: do not rename, it holds existing data
        "dim": 768,
        "max_seq": 512,
        "search_threshold": 0.55,
        "confidence": (0.80, 0.65),
        "languages": "inglês",
        "summary": "menor e mais rápido, ranqueia com mais contraste: mas monolíngue",
    },
    "BAAI/bge-m3": {
        "collection": "vault_bge_m3",
        "dim": 1024,
        "max_seq": 8192,
        "search_threshold": 0.45,
        "confidence": (0.62, 0.50),
        "languages": "multilíngue (100+)",
        "summary": "acha o que o base não vê, sobretudo em português; escala comprimida",
    },
}

#: Used for any model without a measured profile. Deliberately conservative:
#: an unmeasured model gets the safest floor rather than another model's numbers.
DEFAULT_PROFILE = {
    "collection": "vault_custom",
    "dim": None,
    "max_seq": None,
    "search_threshold": 0.50,
    "confidence": (0.80, 0.65),
    "languages": "desconhecido",
    "summary": "sem calibração medida: limiares são chute conservador",
}


def profile_for(model_name: str) -> dict:
    """Return the retrieval profile for an embedding model.

    Matching is exact first, then case-insensitive on the bare model name, so
    "bge-m3" and "BAAI/bge-m3" resolve to the same profile.
    """
    if model_name in MODEL_PROFILES:
        return MODEL_PROFILES[model_name]
    bare = (model_name or "").rsplit("/", 1)[-1].lower()
    for known, profile in MODEL_PROFILES.items():
        if known.rsplit("/", 1)[-1].lower() == bare:
            return profile
    return DEFAULT_PROFILE


def collection_name_for(model_name: str) -> str:
    """ChromaDB collection holding this model's vectors.

    One collection per model, because their vectors have different dimensions and
    ChromaDB rejects a mismatched insert. Keeping them separate is also what makes
    switching models cheap: both indexes can coexist, so going back is immediate
    instead of a full reindex. Unknown models are slugged rather than sharing a
    bucket, which would reintroduce the dimension clash.
    """
    profile = profile_for(model_name)
    if profile is not DEFAULT_PROFILE:
        return profile["collection"]
    slug = "".join(c if c.isalnum() else "_" for c in (model_name or "custom").lower())
    return f"vault_{slug.strip('_')[:48]}"


def _model_is_cached(model_name: str) -> bool:
    """True when the model's weights are already on disk.

    Checks the Hugging Face hub cache layout (`models--org--name`) under whichever
    root is configured, plus the sentence-transformers cache some versions use.
    """
    roots = []
    for env in ("HF_HUB_CACHE", "HUGGINGFACE_HUB_CACHE"):
        if os.environ.get(env):
            roots.append(Path(os.environ[env]))
    hf_home = os.environ.get("HF_HOME")
    roots.append(Path(hf_home) / "hub" if hf_home else Path.home() / ".cache" / "huggingface" / "hub")
    roots.append(Path.home() / ".cache" / "torch" / "sentence_transformers")

    slug = "models--" + model_name.replace("/", "--")
    alt = model_name.replace("/", "_")
    for root in roots:
        try:
            if (root / slug).is_dir() or (root / alt).is_dir():
                return True
        except (OSError, ValueError):
            # ValueError: a malformed value (embedded null byte) in one of the cache
            # env vars. A bad override must not take startup down with it.
            continue
    return False


def prefer_offline(model_name: str) -> bool:
    """Enable HF offline mode, but only when the weights are actually cached.

    Offline mode exists to stop a hub round-trip on every startup once the model is
    local. Setting it unconditionally is a trap: on a machine where the model was
    never downloaded it forbids the one download that would fix the situation, so
    startup fails identically forever instead of self-healing once.

    Returns True when offline mode was enabled.
    """
    if os.environ.get("HF_HUB_OFFLINE") is not None:
        return os.environ["HF_HUB_OFFLINE"] == "1"   # explicit operator choice wins
    if not model_name:
        return False        # nothing to look for: stay online rather than lock out
    if _model_is_cached(model_name):
        os.environ["HF_HUB_OFFLINE"] = "1"
        return True
    logger.info(
        "Embedding model %s is not cached locally: leaving HF online for this run "
        "so it can be fetched once. Later runs will start offline.", model_name,
    )
    return False


def detect_device() -> str:
    """Return 'cuda', 'mps', or 'cpu' depending on available hardware accelerators."""
    try:
        import torch
        if torch.cuda.is_available():
            return "cuda"
        if hasattr(torch.backends, "mps") and torch.backends.mps.is_available():
            return "mps"
        return "cpu"
    except Exception:
        return "cpu"


def _effective_max_seq_length(model_name: str, requested: int | None) -> int | None:
    """Resolve the sequence-length cap to apply, or None for "leave the model alone".

    MODEL_PROFILES already records each model's real input window, and until now that
    field was display-only: cli.py prints it in the model table and nothing else read
    it. It is the one number on hand that can catch a config asking for more than the
    weights can do: raising max_seq_length past a model's trained window does not
    extend the window, it hands the encoder positions its embeddings cannot represent.
    So a request above a *measured* ceiling is clamped down to it. An unmeasured model
    carries max_seq=None (DEFAULT_PROFILE), and a ceiling nobody has verified is not a
    ceiling worth enforcing, so those pass through exactly as configured.
    """
    if not requested or requested <= 0:
        return None
    ceiling = profile_for(model_name).get("max_seq")
    if ceiling and requested > ceiling:
        logger.warning(
            "Configured max_seq_length=%d exceeds %s's %d-token window: clamping to %d",
            requested, model_name, ceiling, ceiling,
        )
        return ceiling
    return requested


#: Chars per token to assume when converting a token ceiling into a character
#: budget. Deliberately a FLOOR, not an average: for the ratio to protect
#: anything it has to hold for the densest text in the corpus, and dense text
#: tokenizes at fewer characters per token. Portuguese and code-heavy markdown
#: both sit below the ~4.0 that English prose averages, so 3.0 leaves margin.
_CHARS_PER_TOKEN_FLOOR = 3.0


def effective_chunk_chars(model_name: str, requested_chars: int,
                          max_seq_length: int | None) -> int:
    """Clamp a character-sized chunk to what the model's token window can hold.

    Chunk size is configured in characters because chunk_text splits on
    characters; the model's limit is in tokens. Nothing reconciled the two, so a
    4000-character chunk: around 1000 English tokens, more in Portuguese: was
    fine under bge-m3's window and silently cut in half under bge-base-en-v1.5,
    whose window is 512. That is the very bug chunking exists to fix, returning
    at a smaller scale: the tail of every chunk simply never reaches the index.

    Returns the character budget to actually chunk with.
    """
    ceiling = _effective_max_seq_length(model_name, max_seq_length)
    if not ceiling or requested_chars <= 0:
        return requested_chars
    budget = int(ceiling * _CHARS_PER_TOKEN_FLOOR)
    if budget < requested_chars:
        logger.info(
            "Chunk size %d chars exceeds what %s can embed at %d tokens: "
            "chunking at %d chars instead",
            requested_chars, model_name, ceiling, budget,
        )
        return budget
    return requested_chars


def _is_out_of_memory(exc: BaseException) -> bool:
    """True for an accelerator out-of-memory failure.

    Three tiers, narrowest first, because a false positive here is expensive:
    the caller's response is to move the model to cpu permanently, so anything
    this wrongly accepts leaves the process quietly ~20x slower with only a log
    line to explain it.

    1. isinstance against torch's real exception, but only if torch is ALREADY
       imported. sys.modules is consulted rather than `import torch` so the
       module keeps its lazy-import discipline and still works where torch is
       absent: and if we are far enough along to be encoding on a GPU, torch is
       loaded by definition.
    2. Class name, for accelerator backends that define their own.
    3. Message text: needed because older torch raised a plain RuntimeError
       ("CUDA out of memory. Tried to allocate ..."): but requiring an
       accelerator to be named alongside the phrase. A bare "out of memory" from
       anywhere else in the stack used to be enough to trigger the fallback.
    """
    import sys
    torch = sys.modules.get("torch")
    if torch is not None:
        for name in ("OutOfMemoryError",):
            exc_type = getattr(torch.cuda, name, None) if hasattr(torch, "cuda") else None
            if exc_type is not None and isinstance(exc, exc_type):
                return True
    if type(exc).__name__ in ("OutOfMemoryError", "CudaOutOfMemoryError"):
        return True
    text = str(exc).lower()
    if "out of memory" not in text:
        return False
    return any(tag in text for tag in ("cuda", "gpu", "hip", "mps", "device"))


#: Cache of the generated subclass, keyed by the base class it was built from.
_LIMITED_EF_CLASSES: dict[type, type] = {}


def _limited_embedding_function_class(base: type) -> type:
    """Subclass SentenceTransformerEmbeddingFunction so the execution limits actually
    reach the model. Neither limit can be passed through the documented route:

      * STEF forwards **kwargs straight into SentenceTransformer.__init__, which
        (sentence-transformers 5.5.1) accepts neither `max_seq_length` nor
        `batch_size` and has no **kwargs of its own: both are a TypeError, not a
        silent no-op. max_seq_length is a *post-construction* property on the model.
      * STEF.__call__ never passes batch_size to .encode() at all, so the model's own
        default of 32 applies no matter what anyone configured.

    Built lazily and cached per base class instead of declared at module scope,
    because the chromadb import is deliberately inside make_bge_embedding_function
    (importing chromadb at module import time costs seconds on every CLI invocation)
    and the base has to be whatever that import returned.

    A subclass rather than a delegating wrapper, so name()/get_config()/
    default_space()/build_from_config() stay byte-for-byte the base's. chromadb 1.5.9
    serialises the embedding function into the collection config and validates it
    against schemas/embedding_functions/sentence_transformer.json, which declares
    additionalProperties=false: a get_config() carrying two extra keys would raise
    inside _serialize_config's try, and the except there quietly rewrites the whole
    entry to {"type": "legacy"}. The cost of inheriting build_from_config unchanged
    is that an EF chromadb rehydrates from that stored config comes back as a plain
    STEF without the limits; nothing in this project does that (vault.py always hands
    get_or_create_collection an EF it built itself), but it is the reason not to
    depend on the stored config for these values.
    """
    cached = _LIMITED_EF_CLASSES.get(base)
    if cached is not None:
        return cached

    import numpy as np

    class LimitedSentenceTransformerEmbeddingFunction(base):  # type: ignore[misc, valid-type]
        """STEF plus a sequence-length cap and a batch-size cap."""

        def __init__(self, model_name, device, normalize_embeddings,
                     max_seq_length=None, batch_size=None, **kwargs):
            super().__init__(model_name=model_name, device=device,
                             normalize_embeddings=normalize_embeddings, **kwargs)
            self.max_seq_length = max_seq_length or None
            self.batch_size = batch_size or None
            model = getattr(self, "_model", None) if self.max_seq_length else None
            if self.max_seq_length and model is None:
                logger.warning(
                    "Embedding backend %s exposes no _model: max_seq_length=%d not applied",
                    type(self).__mro__[1].__name__, self.max_seq_length,
                )
            elif model is not None:
                # STEF caches SentenceTransformer instances in a class-level dict keyed
                # by model name, so this mutates the one shared model rather than a copy.
                # That is the behaviour we want: every EF over this model should honour
                # the cap: but it does mean the last cap constructed wins if two ever
                # disagree, which is why the value is resolved once, up in the factory.
                model.max_seq_length = self.max_seq_length

        def _encode(self, documents: list[str]):
            # Reimplements the base's __call__ body rather than delegating to it: the
            # EmbeddingFunction protocol's __init_subclass__ wraps every __call__ it
            # sees in validate/normalize, so super().__call__() would run that wrapper
            # a second time: and .encode() is the only seam batch_size can enter by.
            encode_kwargs = {}
            if self.batch_size:
                encode_kwargs["batch_size"] = self.batch_size
            # Re-assert the cap here, not only at construction. STEF keys cached
            # SentenceTransformer instances by model name in a class-level dict,
            # so every EF over one model shares an instance: and setting the cap
            # only in __init__ meant the last EF constructed silently decided the
            # sequence length for all of them. Applying it per encode makes each
            # EF get its own cap whoever built last, and costs an attribute
            # compare on a path that is about to run a transformer.
            if self.max_seq_length and getattr(self._model, "max_seq_length", None) != self.max_seq_length:
                self._model.max_seq_length = self.max_seq_length
            vectors = self._model.encode(
                documents,
                convert_to_numpy=True,
                normalize_embeddings=self.normalize_embeddings,
                **encode_kwargs,
            )
            return [np.array(v, dtype=np.float32) for v in vectors]

        def __call__(self, input):
            try:
                return self._encode(list(input))
            except Exception as e:
                # make_bge_embedding_function's fallback only covers OOM while the
                # weights are being *placed*. The OOM that motivated these limits hit
                # mid-reindex: inside encode, where nothing caught it and the reindex
                # died with the vault half indexed. The caps above are the actual fix;
                # this is the net under them, because how much VRAM is free depends on
                # what else is running (llama.cpp shares this GPU) and no static cap is
                # right on every machine. One move to cpu, permanent for this process:
                # a retry on the same device is the same failure, and a model that
                # bounces back to the GPU per call would just OOM again on the next one.
                if self.device == "cpu" or not _is_out_of_memory(e):
                    raise
                # ERROR, not WARNING, and it says what the consequence is. This
                # is the one branch that permanently changes how fast the whole
                # process runs; a line that reads as routine is how a machine
                # ends up mysteriously an order of magnitude slower with nobody
                # able to say when it started.
                logger.error(
                    "Embedding encode ran out of memory on %s (%s). Moving the model to "
                    "cpu PERMANENTLY for this process: embedding will be far slower "
                    "until it restarts. Lower embed_batch_size or embed_max_seq_length, "
                    "or free the accelerator, to avoid this.", self.device, e,
                )
                self._model.to("cpu")
                self.device = "cpu"
                try:
                    import torch
                    torch.cuda.empty_cache()
                except Exception:
                    pass    # freeing the cache is opportunistic; failing to is not fatal
                return self._encode(list(input))

    _LIMITED_EF_CLASSES[base] = LimitedSentenceTransformerEmbeddingFunction
    return LimitedSentenceTransformerEmbeddingFunction


#: Valores aceitos em `embed_device`. "auto" e o comportamento historico:
#: usar o acelerador que existir.
DISPOSITIVOS = ("auto", "cpu", "cuda", "mps")


def resolver_dispositivo(preferencia: str | None = "auto") -> str:
    """Traduz a preferencia de config no dispositivo a usar.

    `detect_device()` responde o que EXISTE na maquina. Esta funcao responde o
    que se QUER usar, que e outra pergunta, e a separacao importa: pedir CPU
    numa maquina com CUDA e uma escolha legitima, nao um engano a ser corrigido.

    ## Por que isto existe

    Nesta maquina o BGE-m3 e o llama-server nao cabem juntos nos 16 GB da
    placa: o BGE segurava 3364 MiB e o segundo a carregar morria em
    `cudaMalloc`. A solucao em uso desde 09/09/2026 e um drop-in de systemd com
    `CUDA_VISIBLE_DEVICES=`, e o comentario dele explica por que foi preciso
    chegar a esse ponto: *"detect_device() escolhe cuda sempre que
    torch.cuda.is_available(), e nao le nenhuma variavel propria, entao esconder
    a placa e o unico jeito sem tocar no codigo"*.

    Esconder a placa do processo inteiro e um instrumento grosso: apaga a GPU
    para tudo que rodar ali, nao so para o encoder. Um campo de config faz a
    mesma coisa com a pontaria certa.

    Preferencia desconhecida cai em "auto" com aviso, em vez de levantar: um
    erro de digitacao no config nao pode deixar a busca sem embedder.
    """
    escolha = (preferencia or "auto").strip().lower()
    if escolha not in DISPOSITIVOS:
        logger.warning("embed_device=%r nao e um valor conhecido %s; usando auto",
                       preferencia, list(DISPOSITIVOS))
        escolha = "auto"
    if escolha == "auto":
        return detect_device()
    return escolha


def make_bge_embedding_function(model_name: str, max_seq_length: int | None = None,
                                batch_size: int | None = None,
                                device: str | None = "auto"):
    """Build a chromadb-compatible BGE embedding function.

    Uses SentenceTransformerEmbeddingFunction with normalize_embeddings=True,
    which is required for BGE models to produce correct cosine similarities.
    Automatically selects CUDA when available.

    max_seq_length and batch_size cap what a single encode call asks of the device.
    Transformer attention costs batch x seq^2, so bge-m3's advertised 8192-token
    window at .encode()'s default batch of 32 is enough to exhaust a 16 GB card
    partway through a reindex: observed in production. Both default to None,
    meaning "leave the model's own default alone", so every caller that predates
    them behaves exactly as before.
    """
    from chromadb.utils.embedding_functions import SentenceTransformerEmbeddingFunction

    max_seq_length = _effective_max_seq_length(model_name, max_seq_length)
    batch_size = batch_size or None

    def build(on_device: str):
        """One construction attempt. Both call sites below go through here so the
        cpu fallback cannot drift into loading the model without the limits: an
        unlimited cpu encode is slower than the failure it replaced, not safer."""
        if max_seq_length is None and batch_size is None:
            return SentenceTransformerEmbeddingFunction(
                model_name=model_name,
                device=on_device,
                normalize_embeddings=True,
            )
        limited = _limited_embedding_function_class(SentenceTransformerEmbeddingFunction)
        return limited(
            model_name=model_name,
            device=on_device,
            normalize_embeddings=True,
            max_seq_length=max_seq_length,
            batch_size=batch_size,
        )

    device = resolver_dispositivo(device)
    if device == "mps":
        # Apple Silicon MPS safety guard (DC-47): cap sequence length to 1024 and batch to 16
        # when not explicitly configured, preventing buffer allocation faults in unified memory.
        if max_seq_length is None:
            max_seq_length = 1024
        if batch_size is None:
            batch_size = 16

    logger.info("Loading BGE model: %s (device=%s, max_seq_length=%s, batch_size=%s)",
                model_name, device, max_seq_length or "model default",
                batch_size or "model default")
    try:
        return build(device)
    except Exception as e:
        # detect_device() only checks whether CUDA/MPS *exists*, not whether
        # there's free memory for it right now: a concurrently-running
        # llama.cpp server (this project's own local-LLM mode) can leave next
        # to nothing free, and every retry would hit the exact same OOM
        # forever since _ensure_ready() just calls this again unchanged. Fall
        # back to CPU once rather than leaving search permanently broken.
        if device == "cpu":
            raise
        # Vale tambem para quem PEDIU um dispositivo que a maquina nao tem: o
        # config e do usuario e pode viajar entre maquinas, entao um
        # `embed_device: cuda` num laptop sem placa cai para cpu com aviso em
        # vez de deixar a busca sem embedder.
        logger.warning("BGE load failed on %s (%s): falling back to cpu", device, e)
        return build("cpu")


def chunk_text(text: str, max_chars: int = 4000, overlap: int = 200) -> list[str]:
    """Split text into overlapping chunks for better embedding coverage of long documents.

    Used by IngestManager for external files that exceed the effective embedding window.
    Short texts (≤ max_chars) are returned as a single-element list unchanged.

    Neither size is trusted, because both arrive from user-editable config
    (ingest_chunk_size/ingest_chunk_overlap, vault_chunk_size/vault_chunk_overlap).
    An overlap at or above max_chars makes the stride `max_chars - overlap` zero or
    negative and the loop then appends forever: chunk_text("x" * 10000, 100, 100)
    grew until the OOM killer took the process (exit 137). A typo in a config file
    must not be able to do that, so the overlap is clamped to half of max_chars : 
    half rather than max_chars-1 because a stride of 1 character is barely less
    ruinous than a stride of 0, just slower to notice.
    """
    if max_chars <= 0:
        logger.warning("chunk_text got max_chars=%d: returning the text unsplit", max_chars)
        return [text]
    if len(text) <= max_chars:
        return [text]
    if not 0 <= overlap <= max_chars // 2:
        clamped = max(0, min(overlap, max_chars // 2))
        logger.warning(
            "chunk_text overlap=%d is out of range for max_chars=%d: clamping to %d",
            overlap, max_chars, clamped,
        )
        overlap = clamped
    chunks: list[str] = []
    start = 0
    while start < len(text):
        chunks.append(text[start:start + max_chars])
        # Stop as soon as a chunk reaches the end of the text. Without this the loop
        # takes one more stride and emits a tail that lies wholly inside the previous
        # chunk's overlap: chunk_text("x" * 7601) returned lengths [4000, 3801, 1],
        # and that 1-character duplicate became its own ChromaDB row with its own
        # embedding: a full vector of index spent on content its predecessor
        # already covers, and a spurious near-empty result competing in search.
        if start + max_chars >= len(text):
            break
        start += max_chars - overlap
    return chunks
