"""Windows x64/NTFS registry adapter. No pathname writes or managed-home rename changes."""
from contextlib import contextmanager
import contextvars
import ctypes as c
from dataclasses import dataclass
import errno
import os
from pathlib import Path
import signal
import threading
import time

import windows_managed_home as native

# Exceptional close outcomes cannot be repaired by retrying a numeric HANDLE.
# Retain a lock protecting an unconfirmed write HANDLE until process teardown.
_QUARANTINED_REGISTRIES = []


def owned_io(function):
    """Keep the owning transaction alive through Ctrl+C, including thread startup."""
    go, cancel, done = threading.Event(), threading.Event(), threading.Event()
    outcome = {}
    context = contextvars.copy_context()
    def execute():
        try:
            while not go.wait(.05): pass
            if not cancel.is_set(): outcome['result'] = context.run(function)
        except BaseException as error:
            outcome['error'] = error
        finally:
            done.set()
    worker = threading.Thread(target=execute, name='dosuti-registry-update', daemon=False)
    previous, interrupted, deferred = None, [], None
    try:
        if threading.current_thread() is threading.main_thread():
            previous = signal.getsignal(signal.SIGINT)
            if previous is None: raise ValueError('Cannot defer unknown SIGINT handler')
            def remember(signum, frame): interrupted[:] = [(signum, frame)]
            signal.signal(signal.SIGINT, remember)
        try:
            worker.start()
            go.set()
        finally:
            while True:
                try:
                    if not go.is_set():
                        cancel.set(); go.set()
                    if not cancel.is_set() and not done.wait(.05): continue
                    break
                except BaseException as error:
                    if deferred is None: deferred = error
    finally:
        if previous is not None: signal.signal(signal.SIGINT, previous)
    if interrupted:
        try:
            if callable(previous): previous(*interrupted[-1])
            elif previous == signal.SIG_DFL: signal.raise_signal(signal.SIGINT)
        except KeyboardInterrupt as error:
            from registry_update import Receipt, CommitInterrupted
            failure = outcome.get('error')
            if isinstance(failure, CommitInterrupted):
                failure.add_note('SIGINT also arrived while the transaction was unwinding')
                raise failure
            result = outcome.get('result')
            if isinstance(result, Receipt):
                raise CommitInterrupted(result, 'committed') from error
            raise
    if deferred is not None:
        from registry_update import Receipt, CommitInterrupted
        failure = outcome.get('error')
        if isinstance(failure, CommitInterrupted):
            failure.add_note('Caller wait was also interrupted during transaction cleanup')
            raise failure
        result = outcome.get('result')
        if isinstance(result, Receipt): raise CommitInterrupted(result, 'committed') from deferred
        raise deferred
    if 'error' in outcome: raise outcome['error']
    return outcome['result']


def replace_relative(api, handle, parent):
    """One same-volume native rename replacing the cooperative registry binding."""
    name = 'lesson.json'.encode('utf-16-le')
    buffer = c.create_string_buffer(c.sizeof(native.FILE_RENAME_INFO) + len(name))
    header = native.FILE_RENAME_INFO.from_buffer(buffer)
    header.Flags, header.RootDirectory, header.FileNameLength = 3, parent, len(name)  # REPLACE_IF_EXISTS | POSIX_SEMANTICS
    c.memmove(c.addressof(buffer) + native.FILE_RENAME_INFO.FileName.offset, name, len(name))
    ios = native.IO_STATUS_BLOCK()
    ios.Result.Status = 0x103
    result = api.rename_info(handle, c.byref(ios), buffer, len(buffer), 65)
    if result & 0xffffffff == 0x103:
        deferred = None
        while True:
            try:
                waited = api.wait_io(handle, False, None)
                final = ios.Result.Status & 0xffffffff
                if waited == 0 and final != 0x103:
                    result = native.I32(final).value
                    break
                if deferred is None:
                    deferred = OSError(f'Registry completion not established: 0x{waited & 0xffffffff:08X}')
                time.sleep(.01)
            except BaseException as error:
                if deferred is None: deferred = error
        if deferred is not None: raise deferred
    api.status(result, 'Registry FileRenameInformationEx')


def identity(value):
    return f'{value[0]}:{value[1].hex()}'


@dataclass
class _Stage:
    name: str
    identity: str
    handle: int


class WindowsRegistry:
    def __init__(self, directory):
        self.directory = Path(directory).absolute()
        self.stages = []
        self._session_closed = False

    def __enter__(self):
        # Only the path-pinning session is reused; no marker classification or
        # fixed-name Native.rename is called, overridden or monkeypatched.
        self.session = native.HomeSession(self.directory)
        self.api, self.parent = self.session.api, self.session.home
        return self

    def __exit__(self, *args):
        if not self._session_closed:
            self._close_registry_handles()

    def _finish_under_lock(self):
        """I/O has completed; retire write HANDLEs before another lock holder reads."""
        self._close_registry_handles()

    def _close_registry_handles(self):
        """Collect every failure without HomeSession's exception-time suppression.

        A failed/raised close is not ownership-transfer evidence. Keep that
        HANDLE recorded, but never retry its numeric value: an exceptional
        wrapper may already have closed it and Windows may have reused it.
        """
        state = getattr(self, 'commit_state', None)
        attempts = getattr(self, '_close_attempts', None)
        if attempts is None:
            attempts = self._close_attempts = {}
        stage_handles = {stage.handle for stage in self.stages}
        order = [stage.handle for stage in self.stages] + list(reversed(self.session.handles))
        errors, seen = [], set()
        for handle in order:
            if handle in seen: continue
            seen.add(handle)
            if handle in attempts:
                if attempts[handle] is not None: errors.append(attempts[handle])
                continue
            phase = 'stage_close' if handle in stage_handles else 'close'
            try:
                result = self.api.ntclose(handle)
                if result != 0:
                    self.api.status(result, 'Registry NtClose')
                    raise OSError(f'Unsuccessful registry NtClose: {result}')
            except BaseException as error:
                error.registry_handle = handle
                error.registry_close_outcome = 'unknown'
                error.add_note(f'Registry HANDLE {handle}: close unconfirmed; ownership retained; '
                               'no automatic retry or HANDLE reuse; a leak is not ruled out')
                attempts[handle] = error
                errors.append(error)
                if state is not None: state.failed(error, phase)
            else:
                attempts[handle] = None
                # Only an observed successful native return retires ownership.
                self.session.handles.remove(handle)
                self.stages[:] = [stage for stage in self.stages if stage.handle != handle]
        self._session_closed = not self.session.handles
        if errors: raise errors[0]

    def _read(self, handle):
        before = self.session.inspect(handle, False)
        self.api.boolean(self.api.seek(handle, 0, None, 0))
        output = bytearray()
        while True:
            buffer, count = c.create_string_buffer(65536), native.U32()
            self.api.boolean(self.api.read_file(handle, buffer, len(buffer), c.byref(count), None))
            if count.value > len(buffer): raise OSError('Invalid registry read count')
            if not count.value: break
            output.extend(buffer.raw[:count.value])
        if len(output) != before[1] or self.session.inspect(handle, False) != before:
            raise ValueError('Registry changed while reading')
        return bytes(output), identity(before[0])

    def read(self):
        self.session.check()
        # A renamed stage remains exclusively open for writing. Inspect its
        # binding using an attributes-only share-all handle, then read the owned
        # stage HANDLE; reopening it for data with share-read would self-conflict.
        with self.session.opened(self.parent, 'lesson.json', directory=False, metadata=True) as handle:
            current = identity(self.session.inspect(handle, False)[0])
        for stage in self.stages:
            if stage.identity == current:
                return self._read(stage.handle)
        with self.session.opened(self.parent, 'lesson.json', directory=False) as handle:
            result = self._read(handle)
        self.session.check()
        return result

    def _open_lock(self):
        name = '.lesson.lock'.encode('utf-16-le')
        buffer = c.create_string_buffer(name + b'\0\0')
        us = native.UNICODE_STRING(len(name), len(name) + 2, c.cast(buffer, native.HANDLE))
        oa = native.OBJECT_ATTRIBUTES(c.sizeof(native.OBJECT_ATTRIBUTES), self.parent,
                                     c.pointer(us), 0x1040, None, None)
        ios, handle = native.IO_STATUS_BLOCK(), native.HANDLE()
        # FILE_OPEN_IF, READ/WRITE sharing (never DELETE), synchronous nonalert,
        # non-directory, OPEN_REPARSE_POINT. Compatible with the old CRT lock.
        result = self.api.create(c.byref(handle), 0xC0000000 | native.SYNC | native.ATTR,
            c.byref(oa), c.byref(ios), None, 0x80, 3, 3, 0x200000 | 0x20 | 0x40, None, 0)
        self.api.status(result, 'Registry lock NtCreateFile')
        try:
            self.api.status(ios.Result.Status, 'Registry lock IOSB')
            if ios.Information not in (1, 2): raise ValueError('Invalid lock create disposition')
            self.session.inspect(handle.value, False)
            return handle.value
        except BaseException:
            self.api.close(handle)
            raise

    @contextmanager
    def lock(self):
        import msvcrt
        self.session.check()
        handle = self._open_lock()
        try:
            fd = msvcrt.open_osfhandle(handle, os.O_BINARY | os.O_NOINHERIT)
        except BaseException:
            self.api.close(handle)
            raise
        try: stream = os.fdopen(fd, 'r+b')
        except BaseException:
            os.close(fd)
            raise
        quarantined = False
        try:
            deadline = time.monotonic() + 30
            while True:
                try:
                    stream.seek(0)
                    msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                    break
                except OSError as error:
                    if error.errno not in (errno.EACCES, errno.EAGAIN, errno.EDEADLK): raise
                    if time.monotonic() >= deadline: raise TimeoutError('Lesson registry is busy') from error
                    time.sleep(.05)
            try:
                self.session.check()
                yield
            finally:
                try:
                    self._finish_under_lock()
                finally:
                    if self.stages:
                        # Do not hand off a registry whose write HANDLE may
                        # still be live. No retry, implicit stream.close(), or
                        # GC-driven unlock is safe evidence of its retirement.
                        quarantined = True
                        _QUARANTINED_REGISTRIES.append((self, stream))
                        for stage in self.stages:
                            error = self._close_attempts[stage.handle]
                            error.registry_lock_retained = True
                            error.add_note('Registry write HANDLE close unconfirmed; byte lock retained '
                                           'until process exit; no automatic cleanup or replay')
                    else:
                        try:
                            stream.seek(0)
                            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
                        except BaseException as error:
                            state = getattr(self, 'commit_state', None)
                            if state is not None: state.failed(error, 'unlock')
                            raise
        finally:
            if not quarantined: stream.close()
        # Closing the CRT stream owns the HANDLE close. Do not NtClose it again.

    def stage(self, name, payload):
        self.session.check()
        handle = self.session.own(self.api.open(self.parent, name, directory=False, stage=True))
        value = _Stage(name, identity(self.session.inspect(handle, False)[0]), handle)
        self.stages.append(value)
        self.api.write(handle, payload)
        self.api.flush(handle)
        if self._read(handle)[0] != payload: raise OSError('Registry readback mismatch')
        return value

    def replace(self, stage):
        self.session.check()
        if identity(self.session.inspect(stage.handle, False)[0]) != stage.identity:
            raise ValueError('Registry stage identity changed')
        with self.session.opened(self.parent, stage.name, directory=False, metadata=True) as handle:
            if identity(self.session.inspect(handle, False)[0]) != stage.identity:
                raise ValueError('Registry stage binding changed')
        replace_relative(self.api, stage.handle, self.parent)
