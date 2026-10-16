"""
Clean shutdown on SIGINT *and* SIGTERM for the long-running tools.

The launcher (`./mdp3 up`) and systemd stop their children with SIGTERM, and a
process started in the background of a non-interactive shell inherits SIGINT
as *ignored*. Either way the tool would die without printing its summary or
flushing its last pcap window. `stop_on_signals()` routes both signals to
KeyboardInterrupt, which every tool already handles as "finish up and report".
"""
from __future__ import annotations

import signal


def _interrupt(_signum, _frame):
    raise KeyboardInterrupt


def stop_on_signals() -> None:
    signal.signal(signal.SIGINT, _interrupt)
    signal.signal(signal.SIGTERM, _interrupt)
