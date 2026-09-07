"""Fenced, non-mutating SQLite reads in an isolated descriptor owner.

No database copies. Live sidecars are explicitly unavailable. The helper holds
SQLite's shared lock range while an immutable connection reads the checkpointed
source. Isolation matters: closing *any* source fd in the caller could release
locks held by another SQLite connection in that process.
"""
from __future__ import annotations

import json
import os
import queue
import sqlite3
import subprocess
import sys
import threading
import time
import weakref
from pathlib import Path
from subprocess import Popen as ReaderProcess

from puppetmaster.readonly_worker import source_stamp


class ReadUnavailable(sqlite3.OperationalError):
    retry_after_ms = 100


def _source_stamp(store):
    path = store.root / ('state.sqlite3' if store.backend_name == 'sqlite' else 'metadata.sqlite3')
    result = []
    for source in (store.root, *(Path(str(path) + suffix) for suffix in ('', '-wal', '-shm', '-journal'))):
        try:
            result.append(source_stamp(source))
        except FileNotFoundError:
            result.append(None)
    return tuple(result)


def selection(store):
    path = store.root / ('state.sqlite3' if store.backend_name == 'sqlite' else 'metadata.sqlite3')
    def identity(path):
        try:
            stat = path.stat()
            return stat.st_dev, stat.st_ino
        except FileNotFoundError:
            return None
    return identity(store.root), identity(path)


class ReadRow(tuple):
    def __new__(cls, values, names):
        row = super().__new__(cls, values)
        row.names = names
        return row

    def __getitem__(self, key):
        return super().__getitem__(self.names.index(key) if isinstance(key, str) else key)

    def keys(self):
        return self.names


class ReadCursor:
    def __init__(self, rows, names):
        self.rows = iter(ReadRow(row, names) for row in rows)

    def fetchone(self):
        return next(self.rows, None)

    def fetchmany(self, size=1):
        from itertools import islice
        return list(islice(self.rows, size))

    def fetchall(self):
        return list(self.rows)

    def __iter__(self):
        return self.rows


class _Transport:
    """Store-lifetime helper; idle helpers own pipes, never source descriptors."""
    def __init__(self, path):
        self.closed = False
        self.pid = os.getpid()
        self.busy = threading.Lock()
        self.process = ReaderProcess(
            [sys.executable, '-I', '-S', str(Path(__file__).with_name('readonly_worker.py')), str(path)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, encoding='utf-8')
        self.responses = queue.Queue(maxsize=1)
        def receive():
            try:
                while True:
                    line = self.process.stdout.readline(8 * 1024 * 1024 + 1)
                    self.responses.put(line)
                    if not line or len(line) > 8 * 1024 * 1024:
                        break
            except (OSError, ValueError):
                pass
        self.reader = threading.Thread(target=receive, daemon=True)
        self.reader.start()

    def close(self):
        if self.closed or self.pid != os.getpid():
            return
        self.closed = True
        # Terminate before closing the pipe: a stuck query must not hold locks.
        if self.process.poll() is None:
            self.process.terminate()
        try:
            self.process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=1)
        self.process.stdin.close()
        # Drain the single-slot queue so the reader can publish EOF and exit.
        try:
            self.responses.get_nowait()
        except queue.Empty:
            pass
        self.reader.join(timeout=1)
        self.process.stdout.close()


class ReadConnection:
    in_transaction = True
    row_factory = sqlite3.Row

    def __init__(self, store, timeout, *, reuse=False, attach_binding=False, launch_binding=False):
        self.store = store
        self.selected = selection(store)
        self._opened = False
        self.closed = False
        self._authorizer = self._progress = self._trace = None
        self.timeout = max(0.001, timeout)
        open_deadline = time.monotonic() + self.timeout
        write_deadline = open_deadline if attach_binding or launch_binding else min(open_deadline, time.monotonic() + (0.1 if reuse else 1.0))
        path = store.root / ('state.sqlite3' if store.backend_name == 'sqlite' else 'metadata.sqlite3')
        try:
            weakref.ref(store)
            cacheable = reuse
        except TypeError:
            cacheable = False
        cached = getattr(store, '_readonly_transport', None) if cacheable else None
        reuse = (cached is not None and cached.pid == os.getpid() and
                 not cached.closed and cached.busy.acquire(blocking=False))
        if reuse:
            self.transport = cached
        else:
            self.transport = _Transport(path)
            self.transport.busy.acquire()
            # Concurrent sessions get independent descriptor owners. Only the
            # first is cached; temporary sessions are reaped on close.
            if cacheable and (cached is None or cached.closed or cached.pid != os.getpid()):
                store._readonly_transport = self.transport
                weakref.finalize(store, self.transport.close)
        self._cached = getattr(store, '_readonly_transport', None) is self.transport
        self.process = self.transport.process
        self.responses = self.transport.responses
        try:
            try:
                retry_source = source_stamp(path)
            except FileNotFoundError:
                retry_source = None
            stamp = _source_stamp(store) if reuse else None
            if reuse:
                self.process.stdin.write(json.dumps(dict(open=str(path))) + '\n')
                self.process.stdin.flush()
            while True:
                if attach_binding:
                    self.timeout = max(.001, open_deadline - time.monotonic())
                try:
                    self.source_journal_mode = self._receive()['journal']
                    break
                except sqlite3.OperationalError as exc:
                    write_race = getattr(exc, 'same_store_write', False)
                    topology_race = attach_binding and getattr(exc, 'launch_topology_change', False)
                    if not (write_race or topology_race or attach_binding and _locked(exc)):
                        raise
                    retry_deadline = write_deadline if write_race else open_deadline
                    remaining = retry_deadline - time.monotonic()
                    if remaining <= 0:
                        raise
                    # Retry in the same descriptor owner, after main() has
                    # closed its failed session. Do not fork a startup herd.
                    time.sleep(min(.01 if write_race else .05, remaining))
                    remaining = retry_deadline - time.monotonic()
                    if remaining <= 0:
                        raise
                    self._fence()
                    current_source = source_stamp(path)
                    _metadata_fence(retry_source, current_source)
                    retry_source = current_source
                    self.process.stdin.write(json.dumps(dict(open=str(path))) + '\n')
                    self.process.stdin.flush()
            self._opened = True
        except BaseException as exc:
            self._abort()
            if (reuse and isinstance(exc, ReadUnavailable) and
                    str(exc) == 'unable to open database: source changed' and
                    _source_stamp(store) == stamp):
                exc.stable_helper_stamp = stamp
            raise

    def _fence(self):
        from puppetmaster.identity import StoreIdentityError
        if selection(self.store) != self.selected or self.selected != self.store._read_selection:
            raise StoreIdentityError('store removed or replaced since selection; explicitly reopen')

    def _receive(self):
        while True:
            try:
                line = self.responses.get(timeout=self.timeout)
            except queue.Empty as exc:
                raise ReadUnavailable('unable to open database: reader timed out') from exc
            if not self._opened:
                self._fence()
            if not line or len(line) > 8 * 1024 * 1024:
                raise ReadUnavailable('unable to open database: reader unavailable')
            result = json.loads(line)
            if 'event' in result:
                callback = {'authorize': self._authorizer, 'progress': self._progress,
                            'trace': self._trace}[result['event']]
                answer = callback(*result['args']) if callback is not None else 0
                self.process.stdin.write(json.dumps(answer or 0) + '\n')
                self.process.stdin.flush()
                continue
            if 'error' in result:
                kind = result.get('kind')
                error = (ReadUnavailable if kind == 'unavailable' else
                         sqlite3.OperationalError if kind == 'OperationalError' else sqlite3.DatabaseError)(result['error'])
                error.launch_topology_change = result.get('launch_topology_change') is True
                error.same_store_write = (kind == 'unavailable' and
                    result['error'] == 'unable to open database: source changed' and
                    result.get('same_store_write') is True)
                if result.get('code') is not None:
                    error.sqlite_errorcode = result['code']
                raise error
            return result

    def execute(self, sql, parameters=()):
        try:
            self.process.stdin.write(json.dumps(dict(sql=sql, parameters=parameters)) + '\n')
            self.process.stdin.flush()
            result = self._receive()
            return ReadCursor(result['rows'], result['names'])
        except BaseException:
            self._abort()
            raise

    def _control(self, name, value):
        try:
            self.process.stdin.write(json.dumps(dict(control=name, value=value)) + '\n')
            self.process.stdin.flush()
            self._receive()
        except BaseException:
            self._abort()
            raise

    def set_authorizer(self, callback):
        self._authorizer = callback
        self._control('authorizer', callback is not None)

    def set_progress_handler(self, callback, instructions):
        self._progress = callback
        self._control('progress', instructions if callback is not None else 0)

    def set_trace_callback(self, callback):
        self._trace = callback
        self._control('trace', callback is not None)

    def _abort(self):
        if not self.closed:
            self.closed = True
            self.transport.close()
            self.transport.busy.release()

    def close(self):
        if self.closed:
            return
        if not self._cached or any((self._authorizer, self._progress, self._trace)):
            self._abort()
            return
        try:
            self._control('release', True)
        except BaseException:
            self._abort()
            raise
        self.closed = True
        self.transport.busy.release()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


def _locked(exc):
    code = getattr(exc, 'sqlite_errorcode', None)
    return ((isinstance(code, int) and (code & 0xff) in (5, 6)) if code is not None else
            str(exc) in ('database is locked', 'database table is locked', 'database schema is locked'))


def _metadata_fence(before, after):
    from puppetmaster.identity import StoreIdentityError
    if (before is not None and after is not None and
            before[2:4] == after[2:4] and before[4] != after[4]):
        raise StoreIdentityError('store source metadata changed during binding')


def connect(store, *, timeout=5, reuse=False, launch_binding=False, attach_binding=False):
    from puppetmaster.identity import StoreIdentityError
    if selection(store) != store._read_selection:
        raise StoreIdentityError('store removed or replaced since selection; explicitly reopen')
    started = time.monotonic()
    deadline = started + max(0, timeout)
    if launch_binding:
        reuse = False
    # Ordinary reads keep their short availability window.
    unavailable_deadline = min(deadline, started + (0.1 if reuse else 1.0))
    refresh_stamp = None
    launch_source = _source_stamp(store)[1] if launch_binding else None
    while True:
        if launch_binding and selection(store) != store._read_selection:
            raise StoreIdentityError('store removed or replaced since selection; explicitly reopen')
        if launch_source is not None:
            current_source = _source_stamp(store)[1]
            # Rename ABA changes ctime without a database write.
            _metadata_fence(launch_source, current_source)
            launch_source = current_source
        if refresh_stamp is not None and _source_stamp(store) != refresh_stamp:
            raise ReadUnavailable('unable to open database: source changed')
        try:
            connection = ReadConnection(store, max(0, deadline - time.monotonic()) if timeout > 0 else 0.1,
                                        reuse=reuse, attach_binding=attach_binding, launch_binding=launch_binding)
            if refresh_stamp is not None and _source_stamp(store) != refresh_stamp:
                connection._abort()
                raise ReadUnavailable('unable to open database: source changed')
            return connection
        except sqlite3.OperationalError as exc:
            stamp = getattr(exc, 'stable_helper_stamp', None)
            # A reused helper can fail after its short retry window expires.
            # Refresh once only if the full source (including directory/sidecar
            # history) remained unchanged through helper teardown.
            if refresh_stamp is None and stamp is not None and time.monotonic() < deadline:
                refresh_stamp = stamp
                continue
            # ReadConnection aborts and reaps its helper before reaching here.
            # Codes are authoritative; old Python/our advisory lock protocol
            # can omit them, so accept only SQLite's exact lock messages then.
            code = getattr(exc, 'sqlite_errorcode', None)
            locked = _locked(exc)
            unavailable = isinstance(exc, ReadUnavailable) and any(
                reason in str(exc) for reason in ('live sidecars', 'active reader'))
            launch_transient = launch_binding and (code is None or locked) and isinstance(exc, ReadUnavailable) and (
                str(exc) in ('unable to open database: active reader; sidecars may be missing',
                             'unable to open database: live sidecars; retry after checkpoint') or
                (str(exc) == 'unable to open database: source changed' and
                 getattr(exc, 'launch_topology_change', False)))
            if launch_binding:
                unavailable = launch_transient
            retry_deadline = (deadline if launch_transient or locked and (launch_binding or attach_binding)
                              else unavailable_deadline)
            if not (locked or unavailable) or time.monotonic() >= retry_deadline:
                raise
            time.sleep(min(.05, max(0, retry_deadline - time.monotonic())))
            if time.monotonic() >= retry_deadline:
                raise
