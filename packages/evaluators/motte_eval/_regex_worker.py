"""Out-of-process regex matcher: a genuinely terminable backtracking guard.

CPython ``re`` has no match timeout, so a catastrophic pattern can wedge a
worker thread forever. The matcher runs in a spawned child process that the
caller can terminate when the deadline expires. This module must stay
stdlib-only so the spawned child never imports the application stack.
"""
from __future__ import annotations

import re
from multiprocessing.connection import Connection
from queue import Empty


def run_search(pattern: str, flags: int, text: str) -> tuple[str, object]:
    compiled = re.compile(pattern, flags)
    return ("ok", compiled.search(text) is not None)


def worker_main(conn: Connection, pattern: str, flags: int, text: str) -> None:
    try:
        conn.send(run_search(pattern, flags, text))
    except BaseException as error:  # noqa: BLE001 - report every failure to the parent
        conn.send(("error", f"{type(error).__name__}: {error}"))
    finally:
        conn.close()
