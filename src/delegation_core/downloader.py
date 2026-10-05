"""
downloader.py — Downloads GGUF models and the llama.cpp binary.
All downloads show a live progress bar via rich.
"""

import os
import platform
import shutil
import tarfile
import tempfile
import zipfile
from pathlib import Path

import requests
from rich.console import Console
from rich.progress import (
    BarColumn,
    DownloadColumn,
    Progress,
    SpinnerColumn,
    TimeRemainingColumn,
    TransferSpeedColumn,
)

console = Console()

# ── model catalog ─────────────────────────────────────────────────────────────
# Curated Q4_K_M quants from bartowski — stable, well-maintained HF repo.
MODELS = [
    {
        "name": "Llama 3.2 1B",
        "size": "0.8 GB",
        "ram": "2 GB",
        "description": "Ultra-lightweight · best for basic tasks on low-RAM machines",
        "filename": "Llama-3.2-1B-Instruct-Q4_K_M.gguf",
        "url": (
            "https://huggingface.co/bartowski/Llama-3.2-1B-Instruct-GGUF"
            "/resolve/main/Llama-3.2-1B-Instruct-Q4_K_M.gguf"
        ),
        "recommended": False,
    },
    {
        "name": "Llama 3.2 3B",
        "size": "2.0 GB",
        "ram": "4 GB",
        "description": "Fast and capable · recommended for most users",
        "filename": "Llama-3.2-3B-Instruct-Q4_K_M.gguf",
        "url": (
            "https://huggingface.co/bartowski/Llama-3.2-3B-Instruct-GGUF"
            "/resolve/main/Llama-3.2-3B-Instruct-Q4_K_M.gguf"
        ),
        "recommended": True,
    },
    {
        "name": "Phi-3.5 Mini",
        "size": "2.2 GB",
        "ram": "4 GB",
        "description": "Microsoft · excellent reasoning in a compact size",
        "filename": "Phi-3.5-mini-instruct-Q4_K_M.gguf",
        "url": (
            "https://huggingface.co/bartowski/Phi-3.5-mini-instruct-GGUF"
            "/resolve/main/Phi-3.5-mini-instruct-Q4_K_M.gguf"
        ),
        "recommended": False,
    },
    {
        "name": "Llama 3.1 8B",
        "size": "4.7 GB",
        "ram": "8 GB",
        "description": "High quality · best results, needs more RAM",
        "filename": "Meta-Llama-3.1-8B-Instruct-Q4_K_M.gguf",
        "url": (
            "https://huggingface.co/bartowski/Meta-Llama-3.1-8B-Instruct-GGUF"
            "/resolve/main/Meta-Llama-3.1-8B-Instruct-Q4_K_M.gguf"
        ),
        "recommended": False,
    },
]


# ── public API ────────────────────────────────────────────────────────────────

def download_model(model: dict, models_dir: Path) -> Path | None:
    dest = models_dir / model["filename"]
    if dest.exists():
        console.print(f"  [green]Already downloaded:[/green] {dest.name}")
        return dest
    models_dir.mkdir(parents=True, exist_ok=True)
    # Download to a *.part sibling and only rename onto `dest` once the
    # transfer completes successfully. If the process is killed mid-download
    # (crash, OOM, lid closed) rather than raising a catchable exception,
    # writing straight to `dest` would leave a truncated GGUF file that the
    # `dest.exists()` check above would treat as a fully valid model on the
    # next run. A partial file at a different, non-matching name can never
    # be mistaken for a complete download.
    partial = dest.with_name(dest.name + ".part")
    ok = _download_file(model["url"], partial, f"Downloading {model['name']}")
    if not ok:
        return None
    partial.rename(dest)
    return dest


def download_llama_binary(llama_dir: Path) -> Path | None:
    """Download the latest llama-server binary from GitHub releases."""
    system = platform.system()
    binary_name = "llama-server.exe" if system == "Windows" else "llama-server"
    dest = llama_dir / binary_name

    if dest.exists():
        return dest

    asset = _get_release_asset(system, platform.machine().lower())
    if not asset:
        return None

    url, filename = asset
    console.print(f"  Asset: [bold]{filename}[/bold]")

    # llama.cpp packages Windows releases as .zip but Linux/macOS as .tar.gz —
    # suffix and extraction method must follow the actual asset, not assume zip.
    is_targz = filename.endswith(".tar.gz")
    suffix = ".tar.gz" if is_targz else ".zip"
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        tmp_path = Path(tmp.name)

    # Extract into a scratch staging dir first, not llama_dir directly. If the
    # process is killed mid-extraction (crash, power loss), llama_dir must not
    # end up containing the main binary alongside missing/truncated sibling
    # .so/.dll files — find_llama_binary() only checks whether the binary
    # itself exists, so a partial extraction there would be picked up on the
    # next run as if it were a complete, working install. Only merge into
    # llama_dir once every file has been extracted and the binary verified
    # present in the staging dir.
    llama_dir.mkdir(parents=True, exist_ok=True)
    stage_dir = Path(tempfile.mkdtemp(prefix="llama_dl_", dir=str(llama_dir.parent)))

    try:
        if not _download_file(url, tmp_path, "Downloading llama.cpp"):
            return None

        # llama-server.exe / llama-server is a thin binary dynamically linked
        # against ~10-50 sibling .dll/.so files in the same archive
        # (llama-server-impl, ggml-*, llama-common, per-CPU-microarch ggml-cpu-*,
        # etc.) — extracting only the named binary produces a file that exists
        # but can't launch (missing shared library). Extract the whole archive
        # instead, flattening the release's single top-level folder (e.g.
        # "llama-b9941/") so everything lands directly in the staging dir.
        if is_targz:
            with tarfile.open(tmp_path, "r:gz") as t:
                # Include symlinks (m.issym()), not just regular files: the
                # release tarball ships versioned real libs (libggml.so.0.15.3)
                # plus unversioned SONAME symlinks (libggml.so.0 -> ...) that
                # the dynamic linker actually looks up by name. Dropping them
                # produces a binary that "exists" but segfaults or falls back
                # to mismatched system libraries with the same SONAME.
                members = [m for m in t.getmembers() if m.isfile() or m.issym()]
                if not any(m.name.endswith(binary_name) for m in members):
                    console.print(f"[red]  {binary_name} not found inside release archive.[/red]")
                    return None
                prefix = _common_dir_prefix([m.name for m in members])
                # Extract real files first, symlinks second (a symlink's target
                # must already exist in the flattened layout for relative links
                # within the same directory to resolve correctly after the fact
                # — though since it's just recreating the link, order mainly
                # keeps intent clear here).
                for m in sorted(members, key=lambda m: m.issym()):
                    rel = m.name[len(prefix):] if prefix else m.name
                    if not rel:
                        continue
                    out_path = stage_dir / rel
                    out_path.parent.mkdir(parents=True, exist_ok=True)
                    if m.issym():
                        out_path.unlink(missing_ok=True)
                        os.symlink(m.linkname, out_path)
                        continue
                    src = t.extractfile(m)
                    if src is None:
                        continue
                    with src, open(out_path, "wb") as out:
                        shutil.copyfileobj(src, out)
                    out_path.chmod(m.mode)
        else:
            with zipfile.ZipFile(tmp_path) as z:
                members = [m for m in z.infolist() if not m.is_dir()]
                if not any(m.filename.endswith(binary_name) for m in members):
                    console.print(f"[red]  {binary_name} not found inside release zip.[/red]")
                    return None
                prefix = _common_dir_prefix([m.filename for m in members])
                for m in members:
                    rel = m.filename[len(prefix):] if prefix else m.filename
                    if not rel:
                        continue
                    out_path = stage_dir / rel
                    out_path.parent.mkdir(parents=True, exist_ok=True)
                    with z.open(m) as src, open(out_path, "wb") as out:
                        shutil.copyfileobj(src, out)

        staged_binary = stage_dir / binary_name
        if not staged_binary.exists():
            console.print(f"[red]  Extraction completed but {binary_name} is missing from the archive.[/red]")
            return None
        if system != "Windows":
            staged_binary.chmod(0o755)

        # Everything extracted successfully — merge the staging dir's contents
        # into llama_dir, overwriting any stale/partial files from a prior
        # interrupted attempt.
        for item in stage_dir.iterdir():
            target = llama_dir / item.name
            if target.is_dir() and not target.is_symlink():
                shutil.rmtree(target, ignore_errors=True)
            else:
                target.unlink(missing_ok=True)
            shutil.move(str(item), str(target))

        if not dest.exists():
            console.print(f"[red]  Extraction completed but {binary_name} is missing from {llama_dir}.[/red]")
            return None
        return dest

    finally:
        tmp_path.unlink(missing_ok=True)
        shutil.rmtree(stage_dir, ignore_errors=True)


def _common_dir_prefix(names: list[str]) -> str:
    """If every entry shares a leading 'top_folder/' segment, return it (else '')."""
    if not names:
        return ""
    first = names[0]
    if "/" not in first:
        return ""
    prefix = first.split("/", 1)[0] + "/"
    return prefix if all(n.startswith(prefix) for n in names) else ""


def find_llama_binary(llama_dir: Path) -> Path | None:
    """Return an existing llama-server binary, or None."""
    system = platform.system()
    binary_name = "llama-server.exe" if system == "Windows" else "llama-server"

    managed = llama_dir / binary_name
    if managed.exists():
        return managed

    in_path = shutil.which(binary_name)
    if in_path:
        return Path(in_path)

    return None


# ── internals ─────────────────────────────────────────────────────────────────

def _download_file(url: str, dest: Path, label: str) -> bool:
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        with requests.get(url, stream=True, timeout=60) as r:
            r.raise_for_status()
            total = int(r.headers.get("content-length", 0))
            written = 0
            with Progress(
                SpinnerColumn(),
                "[progress.description]{task.description}",
                BarColumn(),
                DownloadColumn(),
                TransferSpeedColumn(),
                TimeRemainingColumn(),
                console=console,
            ) as progress:
                task = progress.add_task(f"  {label}", total=total or None)
                with open(dest, "wb") as f:
                    for chunk in r.iter_content(chunk_size=65536):
                        f.write(chunk)
                        written += len(chunk)
                        progress.advance(task, len(chunk))
        # A server/proxy that closes the connection cleanly but early won't
        # always surface as an exception from iter_content — verify the byte
        # count against Content-Length (when the server sent one) so a
        # silently truncated response is treated as a failure, not a valid
        # model/binary.
        if total and written != total:
            console.print(f"  [red]Download incomplete:[/red] got {written} of {total} bytes.")
            dest.unlink(missing_ok=True)
            return False
        return True
    except Exception as e:
        console.print(f"  [red]Download failed:[/red] {e}")
        dest.unlink(missing_ok=True)
        return False


# Backend-specific variants that need extra runtime deps (CUDA toolkit, ROCm, etc.)
# we don't want to grab as a generic CPU-inference fallback — plus the `cudart-`
# prefix marks CUDA-runtime helper packages that don't even contain a llama-server
# binary at all. Verified against a live llama.cpp release (b9941): without this
# exclusion, sorted()[0] alphabetically picks "cudart-llama-bin-win-cuda-*.zip"
# before "llama-*-bin-win-cpu-x64.zip".
_GPU_BACKEND_KEYWORDS = ("cuda", "hip", "vulkan", "sycl", "opencl", "openvino", "rocm")


def _is_plain_cpu_asset(name: str) -> bool:
    lname = name.lower()
    if lname.startswith("cudart-"):
        return False
    return not any(k in lname for k in _GPU_BACKEND_KEYWORDS)


def _get_release_asset(system: str, machine: str) -> tuple[str, str] | None:
    try:
        r = requests.get(
            "https://api.github.com/repos/ggml-org/llama.cpp/releases/latest",
            headers={"Accept": "application/vnd.github.v3+json"},
            timeout=15,
        )
        r.raise_for_status()
        assets = {a["name"]: a["browser_download_url"] for a in r.json().get("assets", [])}
    except Exception as e:
        console.print(f"  [yellow]GitHub API unavailable:[/yellow] {e}")
        return None

    # llama.cpp currently ships Windows as .zip and Linux/macOS as .tar.gz, but
    # that's changed before — accept either rather than assuming one.
    def _archive(k: str) -> bool:
        return k.endswith(".zip") or k.endswith(".tar.gz")

    if system == "Linux":
        candidates = [k for k in assets if "ubuntu" in k and "x64" in k and _archive(k) and _is_plain_cpu_asset(k)]
    elif system == "Darwin":
        arch = "arm64" if machine in ("arm64", "aarch64") else "x64"
        candidates = [k for k in assets if "macos" in k and arch in k and _archive(k) and _is_plain_cpu_asset(k)]
    elif system == "Windows":
        # Prefer an avx2 build when upstream still names one that way (best perf),
        # but llama.cpp has renamed the Windows CPU asset before (e.g. to
        # win-cpu-x64.zip) — fall back to any win+x64 archive so a naming change
        # upstream doesn't silently break the installer again. _is_plain_cpu_asset
        # guards BOTH branches, not just the fallback: it was on the fallback
        # alone until 2026-09-03, and the preferred branch was correct only by
        # alphabetical luck. No llama.cpp GPU build carries "avx2" in its name
        # today, so `sorted(...)[0]` happened to land on the CPU asset. Given an
        # asset list where the GPU build sorts first, as
        # "llama-...-bin-win-cuda-avx2-x64.zip" does against
        # "llama-...-bin-win-x64-avx2.zip", the preferred branch selected the
        # CUDA build and the installer put a GPU binary on a machine that may
        # have no GPU. The comment already claimed this protection; now it is
        # true. Upstream has renamed this asset before, which is the whole
        # reason the fallback exists, so depending on their naming order is
        # depending on the thing that already broke once.
        candidates = [k for k in assets if "win" in k and "avx2" in k and "x64" in k
                      and _archive(k) and _is_plain_cpu_asset(k)]
        if not candidates:
            candidates = [k for k in assets if "win" in k and "x64" in k and _archive(k) and _is_plain_cpu_asset(k)]
    else:
        return None

    if not candidates:
        console.print(
            f"[yellow]  No release asset matches system={system!r} machine={machine!r}.[/yellow]"
        )
        console.print(f"[dim]  Available assets: {', '.join(sorted(assets)) or 'none'}[/dim]")
        return None
    filename = sorted(candidates)[0]
    console.print(f"[dim]  Selected release asset: {filename}[/dim]")
    return assets[filename], filename
