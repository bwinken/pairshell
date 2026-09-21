"""pairshell - pair-program in a remote shell with your AI agent.

You and your agent drive the same tmux session over SSH or Telnet; every
command the agent runs shows up live in your terminal.
"""

from __future__ import annotations

__version__ = "0.1.0"


def build_info() -> str:
    """Commit and origin of this copy, so `--version` can tell updates apart.

    The version number does not change between commits, so this looks at,
    in order: the ``_build`` module a wheel built by ``tools/build_wheel.py``
    carries, pip's ``direct_url.json`` for ``pip install git+...`` installs,
    and ``.git/HEAD`` when running from a checkout.
    """
    try:
        from . import _build  # type: ignore[attr-defined]

        return f"commit {_build.commit}, built {_build.built}"
    except ImportError:
        pass
    try:
        import json
        from importlib import metadata

        raw = metadata.distribution("pairshell").read_text("direct_url.json")
        if raw:
            info = json.loads(raw)
            commit = info.get("vcs_info", {}).get("commit_id")
            if commit:
                return f"commit {commit[:12]}, from {info.get('url', '?')}"
    except Exception:  # noqa: BLE001 - metadata is optional
        pass
    try:
        from pathlib import Path

        git = Path(__file__).resolve().parent.parent / ".git"
        head = (git / "HEAD").read_text(encoding="utf-8").strip()
        if head.startswith("ref: "):
            ref = head[5:]
            ref_file = git / ref
            if ref_file.exists():
                commit = ref_file.read_text(encoding="utf-8").strip()
            else:
                commit = "?"
                packed = git / "packed-refs"
                if packed.exists():
                    for line in packed.read_text(encoding="utf-8").splitlines():
                        parts = line.split()
                        if len(parts) == 2 and parts[1] == ref:
                            commit = parts[0]
            return f"commit {commit[:12]}, checkout on {ref.rsplit('/', 1)[-1]}"
        return f"commit {head[:12]}, checkout"
    except OSError:
        return "source unknown"


def version_string() -> str:
    return f"pairshell {__version__} ({build_info()})"
