"""delegation-core — local MCP delegation server."""

# v5.1 patch: this docstring previously read "v0.2.0" while pyproject.toml
# declared 0.5.0 — a stale copy-paste. Kept in lockstep with pyproject going
# forward so `python -c "import delegation_core; print(delegation_core.__doc__)"`
# and `pip show delegation-core` never disagree.
#
# v0.7.0: this drifted again (docstring/__version__ said 0.6.3 while
# pyproject.toml said 0.6.4, then 0.7.0) — the exact regression the 2026-07-22
# upgrade session on the other machine caught and patched. Re-synced here.
#
# v0.11.0: and again, three ways at once — the docstring said 0.10.0,
# __version__ said 0.9.0, pyproject said 0.10.0, and the dashboard header dutifully
# showed 0.9.0 to anyone looking at it. Two comments above this line promised to
# keep them in lockstep by hand and neither survived contact with a release.
# The version now lives here and nowhere else in the source: the docstring no
# longer carries a copy, and test_version_consistency.py fails the suite when
# this and pyproject.toml disagree. Deriving it from importlib.metadata was the
# obvious alternative and is wrong here — an editable install freezes its
# metadata at install time, and this machine's reported 0.7.0 while the source
# said 0.10.0.
__version__ = "0.15.0"
