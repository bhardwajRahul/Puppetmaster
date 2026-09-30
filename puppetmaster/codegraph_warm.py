"""Warm ``codegraph explore`` helpers, one per workspace.

``codegraph_context`` runs on every chat turn of a host like Marionette, and
the CLI pays Node start, module load and a graph open each time (about 60 ms
of start-up plus the open, ~270 ms per call on a mid-size repo). A helper
(``codegraph_warm.mjs``) keeps the graph open and runs the same
``ToolHandler.execute('codegraph_explore')`` call the CLI makes, so output is
identical and a call costs only the query.

Any doubt falls back to the CLI: unknown install layout, helper start failure,
timeout, or an error reply. A helper restarts when its index files change
on disk, idles out after ``IDLE_SECONDS``, and exits with its owner (stdin EOF).
Set ``PUPPETMASTER_CODEGRAPH_WARM=0`` to disable.
"""

from __future__ import annotations

import atexit
import json
import os
import queue
import subprocess
import threading
import time
from pathlib import Path
from typing import Optional

IDLE_SECONDS = 300.0
MAX_HELPERS = 4
START_TIMEOUT_SECONDS = 15.0
_HELPER_JS = Path(__file__).with_name("codegraph_warm.mjs")

_lock = threading.Lock()
_helpers: dict = {}


class Unavailable(Exception):
    """The warm path cannot answer; use the CLI."""


def enabled() -> bool:
    return os.environ.get("PUPPETMASTER_CODEGRAPH_WARM", "1").strip().lower() not in ("0", "false", "no", "off")


def locate(invocation: list) -> Optional[tuple[str, str]]:
    """``(node, codegraph lib dist dir)`` for a known install layout, else None."""
    scripts = [Path(a) for a in invocation if str(a).endswith((".js", ".cjs", ".mjs"))]
    if not scripts:
        return None
    entry = scripts[-1]
    candidates = []
    if entry.name == "npm-shim.js":
        scope = entry.parent / "node_modules" / "@colbymchenry"
        try:
            candidates += [child for child in scope.iterdir() if child.name.startswith("codegraph-")]
        except OSError:
            pass
        candidates = [(bundle / "lib" / "dist", bundle) for bundle in candidates]
    elif entry.parent.name == "bin" and entry.parent.parent.name == "dist":
        lib = entry.parent.parent
        candidates = [(lib, lib.parent.parent)]
    for lib, bundle in candidates:
        if not (lib / "mcp" / "tools.js").is_file() or not (lib / "index.js").is_file():
            continue
        for name in ("node.exe", "node") if os.name == "nt" else ("node",):
            bundled = bundle / name
            if bundled.is_file():
                return str(bundled), str(lib)
        first = Path(str(invocation[0]))
        if first.is_file() and first.name.lower().startswith("node"):
            return str(first), str(lib)
    return None


def index_signature(root: Path) -> tuple:
    sig = []
    for name in ("codegraph.db", "codegraph.db-wal"):
        try:
            st = (root / ".codegraph" / name).stat()
            sig.append((st.st_mtime_ns, st.st_size))
        except OSError:
            sig.append(None)
    return tuple(sig)


class _Helper:
    def __init__(self, node: str, lib: str, cwd: str):
        kwargs = {}
        if os.name == "nt":
            kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        self.proc = subprocess.Popen(
            [node, str(_HELPER_JS), lib, cwd],
            cwd=cwd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
            **kwargs,
        )
        self.lines: "queue.Queue[Optional[str]]" = queue.Queue()
        threading.Thread(target=self._pump, name="codegraph-warm", daemon=True).start()
        self.busy = threading.Lock()
        self.last_used = time.monotonic()
        self.next_id = 0
        hello = self._reply(START_TIMEOUT_SECONDS)
        if not hello.get("ready"):
            self.close()
            raise Unavailable(hello.get("error") or "helper not ready")
        self.root = Path(hello["root"])
        self.signature = index_signature(self.root)

    def _pump(self) -> None:
        try:
            for line in self.proc.stdout:
                self.lines.put(line)
        finally:
            self.lines.put(None)

    def _reply(self, timeout: float) -> dict:
        try:
            line = self.lines.get(timeout=timeout)
        except queue.Empty:
            self.close()
            raise Unavailable("helper timed out")
        if line is None:
            self.close()
            raise Unavailable("helper exited")
        try:
            return json.loads(line)
        except ValueError:
            self.close()
            raise Unavailable("helper sent an unreadable reply")

    def alive(self) -> bool:
        return self.proc.poll() is None

    def explore(self, query: str, max_files: int, timeout: float) -> str:
        with self.busy:
            self.next_id += 1
            request = {"id": self.next_id, "query": query, "maxFiles": max_files}
            try:
                self.proc.stdin.write(json.dumps(request) + "\n")
                self.proc.stdin.flush()
            except OSError:
                self.close()
                raise Unavailable("helper pipe closed")
            reply = self._reply(timeout)
            self.last_used = time.monotonic()
            if reply.get("id") != request["id"]:
                self.close()
                raise Unavailable("helper reply out of order")
            if not reply.get("ok"):
                raise Unavailable(reply.get("error") or "explore failed")
            return reply.get("text") or ""

    def close(self) -> None:
        try:
            self.proc.stdin.close()
        except Exception:
            pass
        try:
            self.proc.wait(2)
        except Exception:
            self.proc.kill()
            try:
                self.proc.wait(2)
            except Exception:
                pass
        try:
            self.proc.stdout.close()
        except Exception:
            pass


def _reap_locked(now: float) -> None:
    for key, helper in list(_helpers.items()):
        if not helper.alive() or now - helper.last_used > IDLE_SECONDS:
            _helpers.pop(key).close()
    while len(_helpers) >= MAX_HELPERS:
        oldest = min(_helpers, key=lambda k: _helpers[k].last_used)
        _helpers.pop(oldest).close()


def explore(invocation: list, cwd: str, query: str, max_files: int, timeout: float) -> str:
    """Run ``codegraph explore`` warm; raise :class:`Unavailable` to use the CLI."""
    if not enabled():
        raise Unavailable("disabled")
    located = locate(invocation)
    if located is None:
        raise Unavailable("unknown CodeGraph layout")
    key = (cwd, located)
    with _lock:
        helper = _helpers.get(key)
        if helper is not None and (not helper.alive() or index_signature(helper.root) != helper.signature):
            _helpers.pop(key).close()
            helper = None
        if helper is None:
            _reap_locked(time.monotonic())
            try:
                helper = _Helper(located[0], located[1], cwd)
            except (OSError, ValueError) as exc:
                raise Unavailable(str(exc))
            _helpers[key] = helper
    try:
        return helper.explore(query, max_files, timeout)
    except Unavailable:
        with _lock:
            if _helpers.get(key) is helper and not helper.alive():
                _helpers.pop(key, None)
        raise


def prewarm(invocation: list, cwd: str) -> bool:
    """Start the helper for ``cwd`` ahead of the first query (best effort)."""
    if not enabled():
        return False
    located = locate(invocation)
    if located is None:
        return False
    key = (cwd, located)
    with _lock:
        helper = _helpers.get(key)
        if helper is not None and helper.alive():
            return True
        _reap_locked(time.monotonic())
        try:
            _helpers[key] = _Helper(located[0], located[1], cwd)
        except (Unavailable, OSError, ValueError):
            return False
    return True


def shutdown() -> None:
    with _lock:
        helpers = list(_helpers.values())
        _helpers.clear()
    for helper in helpers:
        helper.close()


atexit.register(shutdown)
