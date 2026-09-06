"""Process-tree teardown and bounded subprocess ownership cleanup.

POSIX process groups cover descendants that remain in the original session;
inherited ownership markers also identify descendants that escape it.
Windows has no process-group SIGKILL equivalent, so timeout paths that only
call ``Popen.kill()`` leave agent-CLI grandchildren alive.

This module prefers ``taskkill /F /T`` (tree kill) and falls back to a
``CreateToolhelp32Snapshot`` walk + ``TerminateProcess``. Every step is
best-effort: missing ``taskkill``, denied handles, or unavailable Toolhelp
APIs never raise to the caller.
"""
from __future__ import annotations

import os
import shutil
import signal
import subprocess
import time
from typing import Iterable, Optional


def kill_process_tree(pid: int) -> bool:
    """Kill ``pid`` and its descendants on Windows.

    Returns True when at least one kill method was attempted successfully
    enough to consider the tree addressed (taskkill ran, or Toolhelp
    terminated the root). Returns False when no method was available or
    the pid is invalid — callers should fall back to ``Popen.kill()``.
    """
    if os.name != "nt" or pid is None or int(pid) <= 0:
        return False
    pid = int(pid)
    if _taskkill_process_tree(pid):
        return True
    return _toolhelp_kill_process_tree(pid)


def _taskkill_creationflags() -> int:
    """Hide the taskkill console under console-less hosts when possible."""
    try:
        from puppetmaster.win_console import effective_creationflags

        return int(effective_creationflags(0))
    except Exception:
        return getattr(subprocess, "CREATE_NO_WINDOW", 0) or 0


def _taskkill_process_tree(pid: int, timeout: float = 15) -> bool:
    """Invoke ``taskkill /F /T /PID`` when the binary is on PATH."""
    taskkill = shutil.which("taskkill")
    if not taskkill:
        return False
    try:
        completed = subprocess.run(
            [taskkill, "/F", "/T", "/PID", str(pid)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=timeout,
            creationflags=_taskkill_creationflags(),
        )
    except (OSError, subprocess.SubprocessError):
        return False
    # 0 = killed, 128 = process not found (already gone) — both fine.
    return completed.returncode in (0, 128)


def _toolhelp_kill_process_tree(pid: int, *, deadline: Optional[float] = None) -> bool:
    """Enumerate descendants via Toolhelp and TerminateProcess each one."""
    try:
        targets = _toolhelp_tree_pids(pid)
    except Exception:
        return False
    if not targets:
        return False
    killed_any = False
    for target in targets:
        if deadline is not None and time.monotonic() >= deadline:
            return False
        try:
            if _terminate_pid(target):
                killed_any = True
        except Exception:
            continue
    return killed_any


def _toolhelp_tree_pids(root_pid: int) -> list[int]:
    """Return descendants (deepest-first) followed by ``root_pid``."""
    children_by_parent = _snapshot_children_by_parent()
    if children_by_parent is None:
        return []
    return _descendant_pids_from_map(root_pid, children_by_parent)


def _snapshot_children_by_parent() -> Optional[dict[int, list[int]]]:
    import ctypes
    from ctypes import wintypes

    TH32CS_SNAPPROCESS = 0x00000002

    class PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.POINTER(wintypes.ULONG)),
            ("th32ModuleID", wintypes.DWORD),
            ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase", ctypes.c_long),
            ("dwFlags", wintypes.DWORD),
            ("szExeFile", wintypes.WCHAR * 260),
        ]

    kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
    invalid = ctypes.c_void_p(-1).value
    snapshot = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if not snapshot or int(snapshot) == int(invalid):  # type: ignore[arg-type]
        return None

    children_by_parent: dict[int, list[int]] = {}
    try:
        entry = PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
        if not kernel32.Process32FirstW(snapshot, ctypes.byref(entry)):
            return None
        while True:
            parent = int(entry.th32ParentProcessID)
            child = int(entry.th32ProcessID)
            children_by_parent.setdefault(parent, []).append(child)
            if not kernel32.Process32NextW(snapshot, ctypes.byref(entry)):
                break
    finally:
        kernel32.CloseHandle(snapshot)
    return children_by_parent


def _terminate_pid(pid: int) -> bool:
    import ctypes

    PROCESS_TERMINATE = 0x0001
    kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
    handle = kernel32.OpenProcess(PROCESS_TERMINATE, False, int(pid))
    if not handle:
        return False
    try:
        return bool(kernel32.TerminateProcess(handle, 1))
    finally:
        kernel32.CloseHandle(handle)


def _descendant_pids_from_map(
    root_pid: int, children_by_parent: dict[int, Iterable[int]]
) -> list[int]:
    """Descendants deepest-first, then ``root_pid``. Cycle-safe under PID reuse."""
    root = int(root_pid)
    descendants: list[int] = []
    seen = {root}
    stack = [int(child) for child in children_by_parent.get(root, ())]
    while stack:
        child = stack.pop()
        if child in seen:
            # Parent/child cycles appear under PID reuse; skip already-walked
            # nodes so Toolhelp fallback cannot hang the timeout path.
            continue
        seen.add(child)
        descendants.append(child)
        stack.extend(int(next_child) for next_child in children_by_parent.get(child, ()))
    descendants.reverse()
    descendants.append(root)
    return descendants


def _owned_posix_pids(owner: str, timeout: float) -> list[int]:
    """Find inherited ownership even after setsid and parent reparenting.

    Both macOS and Linux ps expose the initial environment with eww. Keep
    output private: it contains environments of other processes as well.
    """
    result = subprocess.run(["ps", "eww", "-ax", "-o", "pid=", "-o", "command="],
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                            text=True, timeout=timeout, check=True)
    marker = "PUPPETMASTER_PROCESS_OWNER=" + owner
    return [int(fields[0]) for line in result.stdout.splitlines()
            if (fields := line.split()) and fields[0].isdigit() and marker in fields[1:]]


def stop_owned_process(process: subprocess.Popen, owner: str, deadline: float) -> None:
    """Bound teardown by an absolute deadline, including after leader exit.

    POSIX ownership is inherited through the environment, independently of
    session and parent IDs. Windows Toolhelp retains descendant parent IDs
    after leader exit; use it before taskkill's already-gone fast path.
    """
    if os.name == "nt":
        try:
            _toolhelp_kill_process_tree(process.pid, deadline=deadline)
        except Exception:
            pass
        remaining = deadline - time.monotonic()
        if remaining > 0:
            try:
                _taskkill_process_tree(process.pid, timeout=remaining)
            except Exception:
                pass
    else:
        # Freeze the original group while enumerating escaped descendants.
        try:
            os.killpg(process.pid, signal.SIGSTOP)
        except Exception:
            pass
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                targets = _owned_posix_pids(owner, remaining)
            except Exception:
                # Discovery is strict before launch, but best-effort at teardown.
                break
            if not targets:
                break
            for pid in targets:
                if time.monotonic() >= deadline:
                    break
                try:
                    os.kill(pid, signal.SIGKILL)
                except Exception:
                    continue
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except Exception:
            pass
    try:
        process.kill()
    except Exception:
        pass
    remaining = deadline - time.monotonic()
    if remaining > 0:
        try:
            process.wait(timeout=remaining)
        except Exception:
            pass
