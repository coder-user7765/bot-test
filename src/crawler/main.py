"""Entry point: `python -m crawler <command>`."""

from __future__ import annotations

import sys

from crawler.cli import run_cli


def main() -> None:
    # Windows consoles often default to a legacy code page; never crash on Arabic text.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except (AttributeError, ValueError):
            pass
    sys.exit(run_cli(sys.argv[1:]))


if __name__ == "__main__":
    main()
