"""
Python version floor for the simulator.

This project targets **Python 3.14+**. The floor is not cosmetic: the feed
handler's timestamping and threading paths assume a modern interpreter, and
3.14 is where free-threaded builds became officially supported, so the receive
thread scales instead of merely interleaving.

Every entry point calls `require_python()` on import, so running on an older
interpreter fails immediately with a clear message rather than deep inside a
traceback about some unrelated syntax or API.
"""
from __future__ import annotations

import sys

MIN_PYTHON = (3, 14)


def require_python(minimum: tuple = MIN_PYTHON) -> None:
    """Exit with a readable message if the interpreter is too old."""
    if sys.version_info < minimum:
        want = ".".join(str(n) for n in minimum)
        have = ".".join(str(n) for n in sys.version_info[:3])
        raise SystemExit(
            f"This project requires Python {want} or newer; this is {have}\n"
            f"  ({sys.executable})\n"
            f"Install a newer interpreter, then recreate the venv:\n"
            f"  python3.14 -m venv .venv && .venv/bin/pip install -r requirements.txt")


def gil_enabled() -> bool:
    """False on a free-threaded (no-GIL) build, where threads run in parallel."""
    probe = getattr(sys, "_is_gil_enabled", None)
    return True if probe is None else bool(probe())


def runtime_banner() -> str:
    build = "free-threaded" if not gil_enabled() else "GIL"
    return f"Python {'.'.join(str(n) for n in sys.version_info[:3])} ({build})"
