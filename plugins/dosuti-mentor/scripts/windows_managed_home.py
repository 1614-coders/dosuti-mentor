"""Windows x64/local NTFS managed-home bootstrap; no pathname write fallback.

The initial OS volume mapping is trusted. Metadata is observed, not frozen.
PowerShell bootstrap and subsequent storage operations are separate contracts.
"""
import ctypes as c
from contextlib import contextmanager
import json
import os
from pathlib import Path, PureWindowsPath
import platform
import re
import struct
import sys
import time
import uuid

U32, I32, U64, W16 = c.c_uint32, c.c_int32, c.c_uint64, c.c_uint16
HANDLE = c.c_void_p
MARKER = '.dosuti-home.json'
SYNC, ATTR = 0x100000, 0x80
DIR_ACCESS = SYNC | ATTR | 1 | 0x20
READ_ACCESS = SYNC | ATTR | 1
STAGE_ACCESS = 0x80000000 | 0x40000000 | 0x10000 | SYNC
NOT_FOUND, COLLISION, SHARING = 0xC0000034, 0xC0000035, 0xC0000043


class UNICODE_STRING(c.Structure):
    _fields_ = [('Length', c.c_uint16), ('MaximumLength', c.c_uint16), ('Buffer', HANDLE)]


class OBJECT_ATTRIBUTES(c.Structure):
    _fields_ = [('Length', U32), ('RootDirectory', HANDLE), ('ObjectName', c.POINTER(UNICODE_STRING)),
                ('Attributes', U32), ('SecurityDescriptor', HANDLE), ('SecurityQualityOfService', HANDLE)]


class STATUS_UNION(c.Union):
    _fields_ = [('Status', I32), ('Pointer', HANDLE)]


class IO_STATUS_BLOCK(c.Structure):
    _fields_ = [('Result', STATUS_UNION), ('Information', c.c_size_t)]


class FILE_ID_INFO(c.Structure):
    _fields_ = [('VolumeSerialNumber', U64), ('FileId', c.c_ubyte * 16)]


class FILE_STANDARD_INFO(c.Structure):
    _fields_ = [('AllocationSize', c.c_int64), ('EndOfFile', c.c_int64), ('NumberOfLinks', U32),
                ('DeletePending', c.c_ubyte), ('Directory', c.c_ubyte)]


class FILE_ATTRIBUTE_TAG_INFO(c.Structure):
    _fields_ = [('FileAttributes', U32), ('ReparseTag', U32)]


class FILE_RENAME_INFO(c.Structure):
    _fields_ = [('Flags', U32), ('RootDirectory', HANDLE), ('FileNameLength', U32), ('FileName', W16 * 1)]


def check_abi():
    layouts = [(UNICODE_STRING, 16, 8, {'Buffer': 8}),
               (OBJECT_ATTRIBUTES, 48, 8, {'RootDirectory': 8, 'ObjectName': 16, 'Attributes': 24}),
               (IO_STATUS_BLOCK, 16, 8, {'Information': 8}),
               (FILE_ID_INFO, 24, 8, {'FileId': 8}),
               (FILE_STANDARD_INFO, 24, 8, {'NumberOfLinks': 16, 'Directory': 21}),
               (FILE_ATTRIBUTE_TAG_INFO, 8, 4, {'ReparseTag': 4}),
               (FILE_RENAME_INFO, 24, 8, {'RootDirectory': 8, 'FileNameLength': 16, 'FileName': 20})]
    for cls, size, alignment, offsets in layouts:
        if c.sizeof(cls) != size or c.alignment(cls) != alignment or any(
                getattr(cls, field).offset != offset for field, offset in offsets.items()):
            raise ValueError('Unsupported Windows managed-home ABI: ' + cls.__name__)


def components(path):
    raw = os.fspath(path)
    if not isinstance(raw, str) or not re.match(r'^[A-Za-z]:[\\/]', raw):
        raise ValueError('Managed home requires an absolute local drive path')
    tail = raw[3:].replace('/', '\\')
    parts = tail.split('\\') if tail else []
    if parts and not parts[-1]: parts.pop()
    for part in parts:
        if (not part or part in ('.', '..') or part[-1:] in (' ', '.') or
                any(ord(ch) < 32 or ch in '<>:"|?*' for ch in part) or
                re.fullmatch(r'(?i)(CON|PRN|AUX|NUL|COM[1-9¹²³]|LPT[1-9¹²³])(?:\..*)?', part)):
            raise ValueError('Ambiguous or unsupported managed-home path component')
    return raw[:2].upper(), parts


def marker_value(raw):
    if len(raw) > 4096:
        raise ValueError('Invalid managed home marker: exceeds 4096 bytes')
    try:
        value = json.loads(raw.decode('utf-8-sig'))
    except (UnicodeError, ValueError) as error:
        raise ValueError('Invalid managed home marker JSON/UTF-8') from error
    if (not isinstance(value, dict) or value.get('schema') != 'dosuti_home_v1' or
            not isinstance(value.get('id'), str) or not re.fullmatch('[a-f0-9]{32}', value['id'])):
        raise ValueError('Invalid managed home marker')
    return value


class NativeError(OSError):
    def __init__(self, operation, status, dos_error):
        self.status = status & 0xffffffff
        self.winerror = dos_error
        super().__init__(dos_error, f'{operation}: NTSTATUS 0x{self.status:08X}, Windows error {dos_error}')


class Native:
    def __init__(self):
        if os.name != 'nt' or platform.machine().upper() not in ('AMD64', 'X86_64') or c.sizeof(HANDLE) != 8:
            raise ValueError('Windows managed-home requires Windows x64/NTFS; no unsafe fallback')
        check_abi()
        self.k = c.WinDLL('kernel32', use_last_error=True)
        self.n = c.WinDLL('ntdll', use_last_error=True)
        def bind(dll, name, args, result):
            try: fn = getattr(dll, name)
            except AttributeError as error:
                raise ValueError('Required Windows managed-home API unavailable: ' + name) from error
            fn.argtypes, fn.restype = args, result
            return fn
        P = c.c_void_p
        self.create = bind(self.n, 'NtCreateFile', [c.POINTER(HANDLE), U32, c.POINTER(OBJECT_ATTRIBUTES),
            c.POINTER(IO_STATUS_BLOCK), P, U32, U32, U32, U32, P, U32], I32)
        self.ntclose = bind(self.n, 'NtClose', [HANDLE], I32)
        self.to_dos = bind(self.n, 'RtlNtStatusToDosError', [I32], U32)
        self.query_volume = bind(self.n, 'NtQueryVolumeInformationFile', [HANDLE, c.POINTER(IO_STATUS_BLOCK), P, U32, U32], I32)
        self.info = bind(self.k, 'GetFileInformationByHandleEx', [HANDLE, I32, P, U32], I32)
        self.set_info = bind(self.k, 'SetFileInformationByHandle', [HANDLE, I32, P, U32], I32)
        self.create_root = bind(self.k, 'CreateFileW', [c.c_wchar_p, U32, U32, P, U32, U32, HANDLE], HANDLE)
        self.query_device = bind(self.k, 'QueryDosDeviceW', [c.c_wchar_p, c.c_wchar_p, U32], U32)
        self.volume_name = bind(self.k, 'GetVolumeNameForVolumeMountPointW', [c.c_wchar_p, c.c_wchar_p, U32], I32)
        self.volume_info = bind(self.k, 'GetVolumeInformationByHandleW', [HANDLE, c.c_wchar_p, U32,
            P, P, P, c.c_wchar_p, U32], I32)
        self.drive_type = bind(self.k, 'GetDriveTypeW', [c.c_wchar_p], U32)
        self.file_type = bind(self.k, 'GetFileType', [HANDLE], U32)
        self.write_file = bind(self.k, 'WriteFile', [HANDLE, P, U32, c.POINTER(U32), P], I32)
        self.read_file = bind(self.k, 'ReadFile', [HANDLE, P, U32, c.POINTER(U32), P], I32)
        self.seek = bind(self.k, 'SetFilePointerEx', [HANDLE, c.c_int64, P, U32], I32)
        self.flush_file = bind(self.k, 'FlushFileBuffers', [HANDLE], I32)

    def status(self, result, operation):
        if result != 0:  # Includes STATUS_PENDING; no asynchronous buffers here.
            raise NativeError(operation, result, self.to_dos(result))

    def boolean(self, result):
        if not result: raise c.WinError(c.get_last_error())

    def close(self, handle):
        self.status(self.ntclose(handle), 'NtClose')

    def mapping(self, drive):
        device, volume = c.create_unicode_buffer(32768), c.create_unicode_buffer(64)
        self.boolean(self.query_device(drive, device, len(device)))
        if not re.fullmatch(r'\\Device\\HarddiskVolume[0-9]+', device.value) or self.drive_type(drive + '\\') != 3:
            raise ValueError('Only fixed local volumes are supported; SUBST/remote paths refused')
        self.boolean(self.volume_name(drive + '\\', volume, len(volume)))
        if not re.fullmatch(r'\\\\\?\\Volume\{[a-fA-F0-9-]+\}\\', volume.value):
            raise ValueError('Unsupported managed-home volume namespace')
        return device.value, volume.value

    def root(self, volume):
        h = self.create_root(volume, DIR_ACCESS, 3, None, 3, 0x02200000, None)
        if h == c.c_void_p(-1).value: raise c.WinError(c.get_last_error())
        return h

    def local_ntfs(self, handle):
        name = c.create_unicode_buffer(32)
        self.boolean(self.volume_info(handle, None, 0, None, None, None, name, len(name)))
        device, ios = (U32 * 2)(), IO_STATUS_BLOCK()
        self.status(self.query_volume(handle, c.byref(ios), device, c.sizeof(device), 4), 'FileFsDeviceInformation')
        self.status(ios.Result.Status, 'FileFsDeviceInformation IO_STATUS_BLOCK')
        if ios.Information != c.sizeof(device): raise ValueError('Incomplete volume device information')
        if name.value != 'NTFS' or device[0] != 7 or device[1] & 0x10:
            raise ValueError('Managed home requires local NTFS')

    def open(self, parent, name, *, directory=None, create=False, stage=False, metadata=False):
        if not name or name in ('.', '..') or any(ch in name for ch in '/\\:\0'):
            raise ValueError('Native open requires one component')
        raw = name.encode('utf-16-le')
        if len(raw) > 65532: raise ValueError('Native filename too long')
        buffer = c.create_string_buffer(raw + b'\0\0')
        us = UNICODE_STRING(len(raw), len(raw) + 2, c.cast(buffer, HANDLE))
        oa = OBJECT_ATTRIBUTES(c.sizeof(OBJECT_ATTRIBUTES), parent, c.pointer(us), 0x1040, None, None)
        ios, handle = IO_STATUS_BLOCK(), HANDLE()
        access = STAGE_ACCESS if stage else DIR_ACCESS if directory else SYNC | ATTR if metadata else READ_ACCESS
        share = 7 if metadata else 3 if directory else 1
        options = 0x200000 | 0x20 | (1 if directory else 0x40 if directory is False else 0)
        status = self.create(c.byref(handle), access, c.byref(oa), c.byref(ios), None, 0x80,
                             share, 2 if stage else 3 if create else 1, options, None, 0)
        self.status(status, 'NtCreateFile ' + name)
        if ios.Result.Status != 0:
            self.close(handle)
            self.status(ios.Result.Status, 'NtCreateFile IO_STATUS_BLOCK ' + name)
        if ios.Information not in (1, 2) or (stage and ios.Information != 2):
            self.close(handle)
            raise ValueError('Unexpected native create disposition')
        return handle.value

    def details(self, handle, directory):
        identity, standard, attributes = FILE_ID_INFO(), FILE_STANDARD_INFO(), FILE_ATTRIBUTE_TAG_INFO()
        for kind, value in ((18, identity), (1, standard), (9, attributes)):
            self.boolean(self.info(handle, kind, c.byref(value), c.sizeof(value)))
        if self.file_type(handle) != 1 or standard.DeletePending or attributes.FileAttributes & 0x400:
            raise ValueError('Managed-home object is reparse, pending deletion or not a disk file')
        if directory is not None and bool(standard.Directory) != directory:
            raise ValueError('Managed-home object has the wrong type')
        if not standard.Directory and standard.NumberOfLinks != 1:
            raise ValueError('Managed-home file must have one link')
        if standard.Directory:
            flags = U32()
            self.boolean(self.info(handle, 23, c.byref(flags), c.sizeof(flags)))
            if flags.value: raise ValueError('Case-sensitive managed-home directories are unsupported')
        return (identity.VolumeSerialNumber, bytes(identity.FileId)), standard.EndOfFile

    def entries(self, handle):
        result, restart = set(), True
        while True:
            buffer = c.create_string_buffer(65536)
            if not self.info(handle, 11 if restart else 10, buffer, len(buffer)):
                error = c.get_last_error()
                if error == 18: return result  # ERROR_NO_MORE_FILES only.
                raise c.WinError(error)
            restart = False
            offset = 0
            while True:
                if offset + 104 > len(buffer): raise ValueError('Invalid directory enumeration header')
                next_offset = struct.unpack_from('<I', buffer.raw, offset)[0]
                length = struct.unpack_from('<I', buffer.raw, offset + 60)[0]
                end = offset + 104 + length
                if not length or length % 2 or end > len(buffer):
                    raise ValueError('Invalid directory enumeration filename')
                name = buffer.raw[offset + 104:end].decode('utf-16-le')
                if name not in ('.', '..'): result.add(name)
                if len(result) > 200000: raise ValueError('Managed-home directory inventory limit exceeded')
                if next_offset == 0: break  # End of buffer, NOT end of enumeration.
                if next_offset % 8 or next_offset < 104 + length or offset + next_offset >= len(buffer):
                    raise ValueError('Invalid directory enumeration offset')
                offset += next_offset

    def read(self, handle):
        self.boolean(self.seek(handle, 0, None, 0))
        output = bytearray()
        while len(output) <= 4096:
            buffer, count = c.create_string_buffer(4097 - len(output)), U32()
            self.boolean(self.read_file(handle, buffer, len(buffer), c.byref(count), None))
            if count.value > len(buffer): raise ValueError('Invalid native read count')
            if not count.value: return bytes(output)
            output.extend(buffer.raw[:count.value])
        raise ValueError('Invalid managed home marker: exceeds 4096 bytes')

    def write(self, handle, payload):
        position = 0
        while position < len(payload):
            block, count = c.create_string_buffer(payload[position:]), U32()
            self.boolean(self.write_file(handle, block, len(payload) - position, c.byref(count), None))
            if not 0 < count.value <= len(payload) - position: raise OSError('Incomplete managed-home marker write')
            position += count.value

    def flush(self, handle):
        self.boolean(self.flush_file(handle))

    def rename(self, handle, home):
        name = MARKER.encode('utf-16-le')
        buffer = c.create_string_buffer(c.sizeof(FILE_RENAME_INFO) + len(name))
        header = FILE_RENAME_INFO.from_buffer(buffer)
        header.Flags, header.RootDirectory, header.FileNameLength = 0, home, len(name)
        c.memmove(c.addressof(buffer) + FILE_RENAME_INFO.FileName.offset, name, len(name))
        self.boolean(self.set_info(handle, 3, buffer, len(buffer)))


class HomeSession:
    def __init__(self, path, *, create=False):
        self.api = Native()
        self.handles, self.bindings = [], []
        self.drive, self.parts = components(path)
        self.path = Path(str(PureWindowsPath(self.drive + '\\', *self.parts)))
        try:
            self.mapping = self.api.mapping(self.drive)
            self.root = self.own(self.api.root(self.mapping[1]))
            self.api.local_ntfs(self.root)
            self.volume = self.api.details(self.root, True)[0][0]
            self.home = self.root
            self.parent = None
            for name in self.parts:
                self.parent = self.home
                child = self.own(self.api.open(self.parent, name, directory=True, create=create))
                identity = self.inspect(child, True)[0]
                self.bindings.append((self.parent, name, child, identity))
                self.home = child
        except BaseException:
            self.close()
            raise

    def own(self, handle):
        self.handles.append(handle)
        return handle

    def close(self):
        errors = []
        while self.handles:
            try: self.api.close(self.handles.pop())
            except OSError as error: errors.append(error)
        if errors:
            # Diagnostic only: never hide an exception already in flight.
            if sys.exc_info()[0] is None: raise errors[0]
            print('Managed-home handle close failed: ' + str(errors[0]), file=sys.stderr)

    def __enter__(self): return self
    def __exit__(self, *args): self.close()

    @contextmanager
    def opened(self, parent, name, **options):
        h = self.api.open(parent, name, **options)
        failed = False
        try:
            yield h
        except BaseException:
            failed = True
            raise
        finally:
            try: self.api.close(h)
            except OSError as error:
                if not failed: raise
                print('Managed-home handle close failed: ' + str(error), file=sys.stderr)

    def inspect(self, handle, directory):
        result = self.api.details(handle, directory)
        if result[0][0] != self.volume: raise ValueError('Managed-home volume changed')
        return result

    def check(self):
        if self.api.mapping(self.drive) != self.mapping: raise ValueError('Managed-home drive mapping changed')
        self.inspect(self.root, True)
        for parent, name, child, identity in self.bindings:
            if self.inspect(child, True)[0] != identity: raise ValueError('Managed-home identity changed')
            with self.opened(parent, name, directory=True) as current:
                if self.inspect(current, True)[0] != identity: raise ValueError('Managed-home binding changed')

    def sentinel(self):
        for parent in (self.home, self.parent) if self.parts and self.parts[-1].lower() == 'run' else (self.home,):
            try:
                with self.opened(parent, '.cleanup-pending.json', metadata=True):
                    raise ValueError('Legacy cleanup marker found; preserve this folder')
            except NativeError as error:
                if error.status != NOT_FOUND: raise

    def winner(self, wait=True):
        deadline = time.monotonic() + (30 if wait else 0)
        while True:
            try:
                with self.opened(self.home, MARKER, directory=False) as handle:
                    identity, size = self.inspect(handle, False)
                    if size > 4096: raise ValueError('Invalid managed home marker: exceeds 4096 bytes')
                    raw = self.api.read(handle)
                    marker_value(raw)
                    if len(raw) != size: raise ValueError('Managed home marker length changed')
                    if self.inspect(handle, False) != (identity, size) or self.api.read(handle) != raw:
                        raise ValueError('Managed home marker changed while reading')
                    self.check(); self.sentinel()
                    return raw
            except NativeError as error:
                if error.status == NOT_FOUND: return None
                if error.status != SHARING or time.monotonic() >= deadline: raise
                self.check()
                time.sleep(0.02)

    def classification(self, legacy):
        self.check(); self.sentinel()
        winner = self.winner()
        if winner is not None: return winner
        if self.api.entries(self.home):
            # A publisher may have won between absence and enumeration.
            winner = self.winner()
            if winner is not None: return winner
            if not legacy: raise ValueError('Existing folder is not managed; choose an empty folder')
            with self.opened(self.home, 'venv', directory=True) as venv:
                self.inspect(venv, True)
                with self.opened(venv, 'pyvenv.cfg', directory=False) as cfg:
                    self.inspect(cfg, False)
        return None

    def publish(self, legacy):
        winner = self.classification(legacy)
        if winner is not None: return
        stage = None
        for _ in range(8):
            try:
                stage = self.own(self.api.open(self.parent, '.dosuti-home-' + uuid.uuid4().hex,
                                               directory=False, stage=True))
                break
            except NativeError as error:
                if error.status != COLLISION: raise
        if stage is None: raise ValueError('Cannot reserve managed-home staging file')
        identity = self.inspect(stage, False)[0]
        payload = json.dumps(dict(schema='dosuti_home_v1', id=uuid.uuid4().hex), indent=2).encode('utf-8')
        self.api.write(stage, payload)
        self.api.flush(stage)
        self.verify_stage(stage, identity, payload)
        if self.classification(legacy) is not None: return
        self.check(); self.sentinel()
        try:
            self.api.rename(stage, self.home)
        except OSError as error:
            # Rename may have committed despite an ambiguous I/O result. Never
            # delete/retry the name: establish the actual committed state.
            self.check(); self.sentinel()
            try:
                with self.opened(self.home, MARKER, metadata=True) as current:
                    own_commit = self.inspect(current, False)[0] == identity
            except NativeError as missing:
                if missing.status != NOT_FOUND: raise
                own_commit = False
            if own_commit:
                self.verify_stage(stage, identity, payload)
            elif self.winner() is None:
                raise error
            self.check(); self.sentinel()
            return
        self.verify_stage(stage, identity, payload)
        with self.opened(self.home, MARKER, metadata=True) as current:
            if self.inspect(current, False)[0] != identity: raise ValueError('Published marker binding changed')
        self.check(); self.sentinel()

    def verify_stage(self, stage, identity, payload):
        if self.inspect(stage, False) != (identity, len(payload)) or self.api.read(stage) != payload:
            raise ValueError('Managed-home staging readback mismatch')
        marker_value(payload)
        if self.inspect(stage, False) != (identity, len(payload)):
            raise ValueError('Managed-home staging metadata changed during readback')


def _run(home, *, create, legacy=False):
    # Lexical validation precedes all filesystem calls and any directory creation.
    drive, parts = components(home)
    if not parts: raise ValueError('Choose a dedicated data folder')
    profile = os.fspath(Path.home())
    if PureWindowsPath(home) == PureWindowsPath(profile): raise ValueError('Choose a dedicated data folder')
    with HomeSession(home, create=create) as session:
        # File identity also rejects a differently spelled profile directory.
        with HomeSession(profile) as user:
            if session.inspect(session.home, True)[0] == user.inspect(user.home, True)[0]:
                raise ValueError('Choose a dedicated data folder')
        if create: session.publish(legacy)
        elif session.classification(False) is None: raise ValueError('Managed home marker is missing')
        session.check(); session.sentinel()
        return session.path


def initialize(home, legacy_runtime=False):
    return _run(home, create=True, legacy=legacy_runtime)


def validate(home):
    return _run(home, create=False)
