"""Private bounded SQL reader process; stdin/stdout are a local JSON protocol."""
import ctypes
import json
import os
import sqlite3
import sys
import time
from pathlib import Path


def stamp(st):
    return (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns)


def source_stamp(path=None, *, fd=None):
    """Use ChangeTime on Windows, where stat's ctime can mean CreationTime.

    Only Windows opens a temporary metadata descriptor here: closing an fd on
    POSIX could release another connection's process-wide SQLite locks.
    """
    if os.name != 'nt':
        return stamp(os.stat(path) if fd is None else os.fstat(fd))
    import msvcrt
    from ctypes import wintypes
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    class BasicInfo(ctypes.Structure):
        _fields_ = [('creation', ctypes.c_longlong), ('access', ctypes.c_longlong),
                    ('write', ctypes.c_longlong), ('change', ctypes.c_longlong),
                    ('attributes', wintypes.DWORD)]
    # FILE_BASIC_INFO.ChangeTime includes renames, unlike CreationTime.
    # https://learn.microsoft.com/windows/win32/api/winbase/ns-winbase-file_basic_info
    query = kernel.GetFileInformationByHandleEx
    query.argtypes = (wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD)
    query.restype = wintypes.BOOL
    owned = fd is None
    if owned:
        create = kernel.CreateFileW
        create.argtypes = (wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                           ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE)
        create.restype = wintypes.HANDLE
        # Attributes only, shared read/write/delete, including directories.
        handle = create(str(path), 0x80, 7, None, 3, 0x02000000, None)
        if handle == wintypes.HANDLE(-1).value:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            fd = msvcrt.open_osfhandle(handle, os.O_RDONLY)
        except BaseException:
            close = kernel.CloseHandle
            close.argtypes = (wintypes.HANDLE,)
            close.restype = wintypes.BOOL
            close(handle)
            raise
    try:
        st = os.fstat(fd)
        info = BasicInfo()
        if not query(msvcrt.get_osfhandle(fd), 0, ctypes.byref(info), ctypes.sizeof(info)):
            raise ctypes.WinError(ctypes.get_last_error())
        epoch = 116444736000000000
        return (st.st_dev, st.st_ino, st.st_size,
                (info.write - epoch) * 100, (info.change - epoch) * 100)
    finally:
        if owned:
            os.close(fd)


def same_store_write(before, after):
    # A changed size/mtime on the same file proves a pre-snapshot write race.
    # Windows can publish size before LastWriteTime on a live writer handle.
    # ctime-only drift (rename ABA/permissions) never earns a retry.
    return (before is not None and after is not None and
            before[:2] == after[:2] and before[2:4] != after[2:4])


def emit(value):
    print(json.dumps(value), flush=True)


def stamps(path):
    result = []
    for suffix in ('', '-wal', '-shm', '-journal'):
        try:
            result.append(source_stamp(str(path) + suffix))
        except FileNotFoundError:
            result.append(None)
    return result


def checkpointed_sidecars(path, before, journal):
    """Constant-size WAL-index proof, under an exclusive main-file lock.

    Some SQLite builds retain a zero-length WAL and its checkpointed index on
    last close. Presence alone does not mean live or uncheckpointed WAL.
    """
    if before[3] is not None:
        return False
    if before[1] is None and before[2] is None:
        return True
    # Apple SQLite can leave only an old SHM after a successful switch to
    # DELETE mode. The main header and empty/absent WAL prove it is not live.
    if journal == b'\x01\x01' and (before[1] is None or before[1][2] == 0):
        return True
    if before[2] is None:
        return False
    with open(str(path) + '-shm', 'rb') as index:
        header = index.read(100)
    if len(header) != 100 or header[:48] != header[48:96] or header[12] != 1:
        return False
    number = lambda start, stop: int.from_bytes(header[start:stop], sys.byteorder)
    if number(0, 4) != 3007000:
        return False
    first = second = 0
    for offset in range(0, 40, 8):
        first = (first + number(offset, offset + 4) + second) & 0xffffffff
        second = (second + number(offset + 4, offset + 8) + first) & 0xffffffff
    if (first, second) != (number(40, 44), number(44, 48)):
        return False
    return number(16, 20) == number(96, 100)


def main(path):
    path = Path(path)
    before = stamps(path)
    if before[0] is None:
        emit(dict(kind='unavailable', error='unable to open database: source missing'))
        return
    # A writable descriptor is used only for an exclusive advisory lock. No
    # source bytes are written. This excludes new WAL openers during the read,
    # so our lock cannot strand a writer's final checkpoint/sidecar cleanup.
    try:
        source = path.open('rb' if os.name == 'nt' else 'r+b')
        exclusive = True
    except PermissionError:
        source = path.open('rb')
        exclusive = False
    with source:
        descriptor_before = source_stamp(fd=source.fileno())
        if descriptor_before != before[0]:
            emit(dict(kind='unavailable', error='unable to open database: source changed',
                      same_store_write=same_store_write(before[0], descriptor_before)))
            return
        if os.name == 'nt':
            import msvcrt
            from ctypes import wintypes
            class Overlapped(ctypes.Structure):
                _fields_ = [('internal', ctypes.c_size_t), ('internal_high', ctypes.c_size_t),
                            ('offset', wintypes.DWORD), ('offset_high', wintypes.DWORD),
                            ('event', wintypes.HANDLE)]
            overlapped = Overlapped()
            overlapped.offset = 1073741826
            kernel = ctypes.WinDLL('kernel32', use_last_error=True)
            lock = kernel.LockFileEx
            lock.argtypes = (wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD,
                             wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(Overlapped))
            lock.restype = wintypes.BOOL
            # LockFileEx permits GENERIC_READ handles. Exclusively reserving
            # SQLite's shared range excludes live connections without writing.
            # https://learn.microsoft.com/windows/win32/api/fileapi/nf-fileapi-lockfileex
            if not lock(msvcrt.get_osfhandle(source.fileno()), 3, 0, 510, 0, ctypes.byref(overlapped)):
                emit(dict(kind='unavailable',
                          error='unable to open database: active reader; sidecars may be missing', code=5))
                return
        else:
            import fcntl
            if sys.platform == 'darwin':
                fields = [('start', ctypes.c_longlong), ('length', ctypes.c_longlong),
                          ('pid', ctypes.c_int), ('type', ctypes.c_short), ('whence', ctypes.c_short)]
            elif sys.platform.startswith('linux'):
                fields = [('type', ctypes.c_short), ('whence', ctypes.c_short),
                          ('start', ctypes.c_longlong), ('length', ctypes.c_longlong), ('pid', ctypes.c_int)]
            else:
                emit(dict(kind='unavailable', error='unable to open database: unsupported lock ABI'))
                return
            class Lock(ctypes.Structure):
                _fields_ = fields
            query = Lock()
            query.type, query.start, query.length = fcntl.F_WRLCK, 1073741826, 510
            answer = Lock.from_buffer_copy(fcntl.fcntl(source, fcntl.F_GETLK, bytes(query)))
            if answer.type != fcntl.F_UNLCK:
                source.seek(18)
                journal = source.read(2)
                # This classifies retry only; reading still requires the lock
                # and the checkpoint proof below. Missing WAL sidecars stay unavailable.
                retryable = (answer.type == fcntl.F_WRLCK or journal == b'\x01\x01' or
                             (before[1] is not None and before[2] is not None))
                emit(dict(kind='OperationalError' if answer.type == fcntl.F_WRLCK else 'unavailable',
                          error='database is locked' if answer.type == fcntl.F_WRLCK else 'unable to open database: active reader; sidecars may be missing',
                          code=5 if retryable else None))  # SQLITE_BUSY, also on Python before 3.11.
                return
            try:
                fcntl.lockf(source, (fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH) | fcntl.LOCK_NB, 510, 1073741826)
            except BlockingIOError:
                emit(dict(kind='OperationalError', error='database is locked'))
                return
        after = stamps(path)
        if after != before:
            emit(dict(kind='unavailable', error='unable to open database: source changed',
                      launch_topology_change=after[0] == before[0] and after[1:] != before[1:],
                      same_store_write=same_store_write(before[0], after[0])))
            return
        header = source.read(100)
        if not checkpointed_sidecars(path, before, header[18:20]):
            emit(dict(kind='unavailable', error='unable to open database: live sidecars; retry after checkpoint'))
            return
        after = stamps(path)
        if after != before:
            emit(dict(kind='unavailable', error='unable to open database: source changed',
                      launch_topology_change=after[0] == before[0] and after[1:] != before[1:]))
            return
        c = sqlite3.connect(path.resolve().as_uri() + '?mode=ro&immutable=1', uri=True)
        try:
            c.execute('PRAGMA foreign_keys=ON')
            c.execute('PRAGMA synchronous=NORMAL')
            c.execute('BEGIN')
            c.execute('SELECT rootpage FROM sqlite_master LIMIT 1').fetchone()
            if stamps(path) != before:
                emit(dict(kind='unavailable', error='unable to open database: source changed'))
                return
            def unchanged():
                return source_stamp(fd=source.fileno()) == descriptor_before
            emit(dict(journal='wal' if header[18:20] == b'\x02\x02' else 'delete'))
            def event(name, args):
                emit(dict(event=name, args=args))
                answer = sys.stdin.readline(1024)
                if not answer:
                    raise EOFError()
                return json.loads(answer)
            progress_interval = 0
            for line in sys.stdin:
                if len(line) > 1024 * 1024:
                    return
                if not unchanged():
                    emit(dict(kind='unavailable', error='unable to open database: source changed'))
                    return
                request = json.loads(line)
                if 'control' in request:
                    name, value = request['control'], request['value']
                    if name == 'release':
                        return True
                    if name == 'authorizer':
                        c.set_authorizer((lambda *args: event('authorize', args)) if value else None)
                    elif name == 'trace':
                        c.set_trace_callback((lambda sql: event('trace', [sql])) if value else None)
                    elif name == 'progress':
                        progress_interval = value
                    emit(dict(rows=[], names=[]))
                    continue
                sql, parameters = request['sql'], request['parameters']
                deadline = time.monotonic() + 5
                c.set_progress_handler(lambda: int(time.monotonic() > deadline) or
                                       (event('progress', []) if progress_interval else 0),
                                       progress_interval or 1000)
                try:
                    cursor = c.execute(sql, parameters)
                    rows = cursor.fetchmany(1002)
                    if len(rows) > 1001:
                        emit(dict(kind='unavailable', error='unable to open database: read row budget exceeded'))
                        continue
                    names = [d[0] for d in cursor.description] if cursor.description else []
                    response = dict(rows=rows, names=names)
                    if not unchanged() or len(json.dumps(response)) > 8 * 1024 * 1024:
                        emit(dict(kind='unavailable', error='unable to open database: changed source or read byte budget exceeded'))
                    else:
                        emit(response)
                except sqlite3.Error as exc:
                    emit(dict(kind=type(exc).__name__, error=str(exc), code=getattr(exc, 'sqlite_errorcode', None)))
        finally:
            c.close()


if __name__ == '__main__':
    try:
        path = sys.argv[1]
        while True:
            released = main(path)
            # main has closed SQLite and the locking fd before retry/release.
            # Failed opens already emitted their error; the parent either
            # terminates us or requests another bounded, fully fenced open.
            if released:
                emit(dict(rows=[], names=[]))
            request = sys.stdin.readline(1024 * 1024 + 1)
            if not request or len(request) > 1024 * 1024:
                break
            path = json.loads(request)['open']
    except OSError as exc:
        emit(dict(kind='unavailable', error='unable to open database: source changed or unavailable: ' + str(exc)))
    except sqlite3.Error as exc:
        emit(dict(kind=type(exc).__name__, error=str(exc), code=getattr(exc, 'sqlite_errorcode', None)))
