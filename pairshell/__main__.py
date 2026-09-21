"""Allow ``python -m pairshell``."""

from .cli import main

if __name__ == "__main__":  # pragma: no cover - thin wrapper
    raise SystemExit(main())
