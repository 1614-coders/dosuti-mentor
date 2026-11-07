"""One local file on Windows x64/local NTFS; cooperative application storage.

DACLs prevent ordinary accidental writes, not a hostile process with the owner's
security token. No recursive import, downloads, pathname writes or rollback.
"""
from contextlib import contextmanager
import ctypes as c
import hashlib
import json
from pathlib import Path
import re
import sys
import uuid

import registry_update as registry
from registry_windows import WindowsRegistry, owned_io
import windows_managed_home as n

MAX_BYTES = 32 * 1024 * 1024
READ_CONTROL, WRITE_DAC, DELETE = 0x20000, 0x40000, 0x10000
READ = n.READ_ACCESS | READ_CONTROL
# Keep permission repair and deletion available to the owner, like POSIX chmod.
# Neither mask grants data writes, append, add-child or delete-child.
SEALED_FILE, SEALED_DIRECTORY = 0x170189, 0x1701A9
DIRECTORY_WRITE = n.DIR_ACCESS | READ_CONTROL | WRITE_DAC | DELETE | 0x106
FILE_WRITE = n.STAGE_ACCESS | READ_CONTROL | WRITE_DAC
_QUARANTINED = []


class BASIC_INFO(c.Structure):
    _fields_ = [('CreationTime', c.c_int64), ('LastAccessTime', c.c_int64),
                ('LastWriteTime', c.c_int64), ('ChangeTime', c.c_int64),
                ('FileAttributes', n.U32)]


def component(name):
    if (not name or name in ('.', '..') or name[-1:] in (' ', '.')
            or any(ch in name for ch in '/\\:\0<>\"|?*')
            or any(ord(ch) < 32 for ch in name)
            or re.fullmatch(r'(CON|PRN|AUX|NUL|COM[1-9¹²³]|LPT[1-9¹²³])', name.split('.')[0], re.I)):
        raise ValueError('Unsafe Windows material component')
    return name


def sddl(sid, mask):
    return f'D:P(A;;0x{mask:08x};;;{sid})'


class Security:
    """HANDLE-based descriptor creation/set/readback; all pointers stay owned."""
    def __init__(self, api):
        self.api = api
        dll = c.WinDLL('advapi32', use_last_error=True)
        def bind(owner, name, args, result):
            fn = getattr(owner, name)
            fn.argtypes, fn.restype = args, result
            return fn
        p, pp = n.HANDLE, c.POINTER(n.HANDLE)
        self.convert = bind(dll, 'ConvertStringSecurityDescriptorToSecurityDescriptorW',
                            [c.c_wchar_p, n.U32, pp, c.POINTER(n.U32)], n.I32)
        self.dacl = bind(dll, 'GetSecurityDescriptorDacl',
                         [p, c.POINTER(n.I32), pp, c.POINTER(n.I32)], n.I32)
        self.control = bind(dll, 'GetSecurityDescriptorControl',
                            [p, c.POINTER(c.c_uint16), c.POINTER(n.U32)], n.I32)
        self.get = bind(dll, 'GetSecurityInfo', [p, n.I32, n.U32, pp, pp, pp, pp, pp], n.U32)
        self.set = bind(dll, 'SetSecurityInfo', [p, n.I32, n.U32, p, p, p, p], n.U32)
        self.free = bind(api.k, 'LocalFree', [p], p)
        current = bind(api.k, 'GetCurrentProcess', [], p)
        open_token = bind(dll, 'OpenProcessToken', [p, n.U32, pp], n.I32)
        token_info = bind(dll, 'GetTokenInformation', [p, n.I32, p, n.U32, c.POINTER(n.U32)], n.I32)
        sid_string = bind(dll, 'ConvertSidToStringSidW', [p, pp], n.I32)
        token = n.HANDLE()
        api.boolean(open_token(current(), 8, c.byref(token)))
        try:
            count = n.U32()
            if token_info(token, 1, None, 0, c.byref(count)) or c.get_last_error() != 122:
                raise OSError('TokenUser size query did not report insufficient buffer')
            buffer = c.create_string_buffer(count.value)
            api.boolean(token_info(token, 1, buffer, len(buffer), c.byref(count)))
            value = n.HANDLE()
            api.boolean(sid_string(c.cast(buffer, c.POINTER(n.HANDLE))[0], c.byref(value)))
            try: self.sid = c.wstring_at(value)
            finally: self.free(value)
        except BaseException as primary:
            try: api.close(token)
            except BaseException as cleanup:
                _QUARANTINED.append((api, token.value, cleanup))
                primary.add_note('Token HANDLE close unconfirmed: ' + repr(cleanup))
            raise
        else:
            try: api.close(token)
            except BaseException as error:
                _QUARANTINED.append((api, token.value, error))
                raise

    @contextmanager
    def descriptor(self, text):
        value = n.HANDLE()
        self.api.boolean(self.convert(text, 1, c.byref(value), None))
        try: yield value
        finally: self.free(value)

    def acl(self, descriptor):
        present, defaulted, value = n.I32(), n.I32(), n.HANDLE()
        self.api.boolean(self.dacl(descriptor, c.byref(present), c.byref(value), c.byref(defaulted)))
        if not present.value or not value.value or defaulted.value:
            raise ValueError('Missing, null or defaulted material DACL')
        size = c.c_uint16.from_address(value.value + 2).value
        if size < 8: raise ValueError('Invalid material ACL size')
        return value, c.string_at(value, size)

    def verify(self, handle, text):
        with self.descriptor(text) as expected:
            desired = self.acl(expected)[1]
            actual = n.HANDLE()
            error = self.get(handle, 1, 4, None, None, None, None, c.byref(actual))
            if error: raise c.WinError(error)
            try:
                control, revision = c.c_uint16(), n.U32()
                self.api.boolean(self.control(actual, c.byref(control), c.byref(revision)))
                if not control.value & 0x1000 or self.acl(actual)[1] != desired:
                    raise ValueError('Material protected DACL readback mismatch')
            finally: self.free(actual)

    def seal(self, handle, directory):
        text = sddl(self.sid, SEALED_DIRECTORY if directory else SEALED_FILE)
        with self.descriptor(text) as descriptor:
            acl = self.acl(descriptor)[0]
            error = self.set(handle, 1, 4 | 0x80000000, None, None, acl, None)
            if error: raise c.WinError(error)
        if not directory:
            before, value = BASIC_INFO(), BASIC_INFO()
            self.api.boolean(self.api.info(handle, 0, c.byref(before), c.sizeof(before)))
            value.FileAttributes = (before.FileAttributes & ~0x80) | 1  # NORMAL must not be combined with READONLY.
            self.api.boolean(self.api.set_info(handle, 0, c.byref(value), c.sizeof(value)))
        self.verify_seal(handle, directory)

    def verify_seal(self, handle, directory):
        self.verify(handle, sddl(self.sid, SEALED_DIRECTORY if directory else SEALED_FILE))
        if not directory:
            value = BASIC_INFO()
            self.api.boolean(self.api.info(handle, 0, c.byref(value), c.sizeof(value)))
            if not value.FileAttributes & 1:
                raise ValueError('Material readonly attribute readback mismatch')


class BoundRegistry(WindowsRegistry):
    def __init__(self, directory, identity):
        super().__init__(directory)
        self.expected_identity = identity

    def __enter__(self):
        super().__enter__()
        try:
            if self.session.inspect(self.parent, True)[0] != self.expected_identity:
                raise ValueError('Import lesson identity changed before registry commit')
        except BaseException as primary:
            try: self.__exit__(type(primary), primary, primary.__traceback__)
            except BaseException as cleanup: primary.add_note('Registry close failed: ' + repr(cleanup))
            raise
        return self


class SingleFile:
    """Own everything through completion; never remove an uncertain pathname."""
    def __init__(self, directory):
        self.directory = Path(directory).absolute()
        self.sessions, self.attempts, self.writers = [], {}, set()
        self.residue, self.receipt, self.closed = None, None, False

    def pin(self, path):
        session = n.HomeSession(path)
        self.sessions.append(session)
        return session

    def retire(self, session, handle):
        if handle not in session.handles: return
        if handle in self.attempts: raise self.attempts[handle]
        try: self.api.close(handle)
        except BaseException as error:
            self.attempts[handle] = error
            _QUARANTINED.append(self)
            raise
        session.handles.remove(handle)
        self.writers.discard(handle)

    def close(self):
        if self.closed: return
        self.closed = True
        errors = []
        for session in reversed(self.sessions):
            for handle in list(reversed(session.handles)):
                try: self.retire(session, handle)
                except BaseException as error: errors.append(error)
        if self.residue is not None and self.receipt is None:
            print('Material residue preserved; no automatic cleanup: ' + str(self.residue), file=sys.stderr)
        if errors:
            for error in errors[1:]: errors[0].add_note('Additional material close failure: ' + repr(error))
            raise errors[0]

    def open(self, session, parent, name, directory, access, *, create=False):
        component(name)
        raw = name.encode('utf-16-le')
        if len(raw) > 65532: raise ValueError('Material filename too long')
        buffer = c.create_string_buffer(raw + b'\0\0')
        text = n.UNICODE_STRING(len(raw), len(raw) + 2, c.cast(buffer, n.HANDLE))
        # Explicit protected descriptor is passed in the creation operation itself.
        with self.security.descriptor(sddl(self.security.sid, 0x1F01FF)) as descriptor:
            oa = n.OBJECT_ATTRIBUTES(c.sizeof(n.OBJECT_ATTRIBUTES), parent,
                                    c.pointer(text), 0x1040, descriptor if create else None, None)
            ios, handle = n.IO_STATUS_BLOCK(), n.HANDLE()
            ios.Result.Status = 0x103
            result = self.api.create(c.byref(handle), access | n.SYNC, c.byref(oa), c.byref(ios),
                                     None, 0x80, 3 if directory else 1, 2 if create else 1,
                                     0x200000 | 0x20 | (1 if directory else 0x40), None, 0)
            # Synchronous native handles; still retain all buffers on real pending.
            deferred = None
            if result & 0xffffffff == 0x103:
                while True:
                    try:
                        waited = self.api.wait_io(handle, False, None)
                        if waited == 0 and ios.Result.Status & 0xffffffff != 0x103:
                            result = ios.Result.Status
                            break
                        if deferred is None:
                            deferred = OSError('Material create completion not established')
                    except BaseException as error:
                        if deferred is None: deferred = error
            self.api.status(result, 'Material NtCreateFile')
            session.own(handle.value)
            if create: self.writers.add(handle.value)
            if deferred is not None: raise deferred
            self.api.status(ios.Result.Status, 'Material NtCreateFile IOSB')
            if ios.Information != (2 if create else 1):
                raise ValueError('Material open/create disposition mismatch')
            session.inspect(handle.value, directory)
            if create: self.security.verify(handle.value, sddl(self.security.sid, 0x1F01FF))
            return handle.value

    def read(self, handle):
        self.api.boolean(self.api.seek(handle, 0, None, 0))
        data = bytearray()
        while True:
            buffer, count = c.create_string_buffer(65536), n.U32()
            self.api.boolean(self.api.read_file(handle, buffer, len(buffer), c.byref(count), None))
            if count.value > len(buffer): raise OSError('Invalid material read count')
            if not count.value: return bytes(data)
            data.extend(buffer.raw[:count.value])
            if len(data) > MAX_BYTES: raise ValueError('Material exceeds 32 MiB')

    def stamp(self, handle):
        value = BASIC_INFO()
        self.api.boolean(self.api.info(handle, 0, c.byref(value), c.sizeof(value)))
        return self.api.details(handle, False), value.CreationTime, value.LastWriteTime, value.ChangeTime, value.FileAttributes

    def denied(self, parent, name, directory):
        """Require ACL ACCESS_DENIED, never count a sharing failure as sealing."""
        try:
            handle = self.open(self.lesson, parent, name, directory, 2)
        except n.NativeError as error:
            if error.status == 0xC0000022: return
            raise
        self.retire(self.lesson, handle)
        raise ValueError('Native material sealing did not deny write/add-child access')

    def prepare(self, source, category):
        self.lesson = self.pin(self.directory)
        self.api = self.lesson.api
        self.security = Security(self.api)
        self.lesson_id = self.lesson.inspect(self.lesson.home, True)[0]
        registered = self.open(self.lesson, self.lesson.home, 'lesson.json', False, n.READ_ACCESS)
        try:
            value = json.loads(self.read(registered).decode('utf-8-sig'))
            if (not isinstance(value, dict) or not isinstance(value.get('id'), str)
                    or value['id'].casefold() != self.directory.name.casefold()):
                raise ValueError('Import lesson registry identity changed')
        finally: self.retire(self.lesson, registered)
        incoming = self.pin(source.parent)
        handle = self.open(incoming, incoming.home, source.name, False, n.READ_ACCESS)
        before = self.stamp(handle)
        if before[0][1] > MAX_BYTES: raise ValueError('Material exceeds 32 MiB')
        payload = self.read(handle)
        if len(payload) != before[0][1] or self.stamp(handle) != before:
            raise ValueError('Material source changed during read')
        incoming.check()
        if payload.startswith((b'%PDF', b'PK\x03\x04')) or b'\0' in payload:
            raise ValueError('Single-file import requires local text/code, not PDF/ZIP/binary')
        try: payload.decode('utf-8-sig')
        except UnicodeDecodeError as error: raise ValueError('Single-file import requires UTF-8') from error
        self.retire(incoming, handle)
        self.category_name = category
        parent = self.lesson.own(self.api.open(self.lesson.home, category, directory=True, create=True))
        self.category_id = self.lesson.inspect(parent, True)[0]
        self.category = parent
        self.snapshot_name = uuid.uuid4().hex[:12]
        self.folder = self.directory / category / self.snapshot_name
        snapshot = self.open(self.lesson, parent, self.snapshot_name, True, DIRECTORY_WRITE, create=True)
        self.residue = self.folder
        self.snapshot_id = self.lesson.inspect(snapshot, True)[0]
        self.name = source.name
        output = self.open(self.lesson, snapshot, self.name, False, FILE_WRITE, create=True)
        self.file_id = self.lesson.inspect(output, False)[0]
        self.api.write(output, payload)
        self.api.flush(output)
        self.checksum = hashlib.sha256(payload).hexdigest()
        self.size = len(payload)
        if self.read(output) != payload: raise OSError('Material readback mismatch')
        self.security.seal(output, False)
        self.retire(self.lesson, output)
        self.denied(snapshot, self.name, False)
        self.file = self.open(self.lesson, snapshot, self.name, False, READ)
        self.security.seal(snapshot, True)
        self.retire(self.lesson, snapshot)
        self.denied(parent, self.snapshot_name, True)
        self.snapshot = self.open(self.lesson, parent, self.snapshot_name, True, n.DIR_ACCESS | READ_CONTROL)
        self.verify()

    def verify(self):
        self.lesson.check()
        for parent, name, held, expected, directory in (
                (self.lesson.home, self.category_name, self.category, self.category_id, True),
                (self.category, self.snapshot_name, self.snapshot, self.snapshot_id, True),
                (self.snapshot, self.name, self.file, self.file_id, False)):
            current = self.open(self.lesson, parent, name, directory, n.DIR_ACCESS if directory else n.READ_ACCESS)
            try:
                if self.lesson.inspect(current, directory)[0] != expected or self.lesson.inspect(held, directory)[0] != expected:
                    raise ValueError('Material binding/identity changed')
            finally: self.retire(self.lesson, current)
        if self.api.entries(self.snapshot) != {self.name}:
            raise ValueError('Single-file snapshot inventory changed')
        self.security.verify_seal(self.file, False)
        self.security.verify_seal(self.snapshot, True)
        data = self.read(self.file)
        if len(data) != self.size or hashlib.sha256(data).hexdigest() != self.checksum:
            raise ValueError('Sealed material bytes changed')
        return data

    def publish(self, args, source):
        if self.writers: raise ValueError('Writable material HANDLE still owned before registry commit')
        import course
        target = self.folder / self.name
        if args.kind == 'code':
            inventory = {self.name: dict(size=self.size, sha256=self.checksum)}
            material = dict(status='provided', snapshot=str(self.folder), origin=dict(path=str(source)),
                lecture_match='unverified', revision=uuid.uuid4().hex, inventory=inventory,
                content_sha256=hashlib.sha256(json.dumps(inventory, sort_keys=True).encode()).hexdigest(),
                integrity='inventory_recorded', sealed_at=course.now())
            item = None
        elif args.kind == 'homework':
            material = dict(status='provided', file=str(target), sha256=self.checksum, origin=dict(source=str(source)))
            material['revision'] = course.homework_revision(material)
            item = None
        else:
            material = None
            item = dict(id=uuid.uuid4().hex, source=str(target), kind=args.resource_type, status='downloaded',
                origin=dict(source=str(source), raw_file=str(target), sha256=self.checksum, size=self.size, media_type=''))
        def change(data):
            self.verify()
            if not isinstance(data.get('id'), str) or data['id'].casefold() != self.directory.name.casefold():
                raise ValueError('Import lesson registry identity changed')
            course.publish_attachment(data, args, material, item)
        self.receipt = registry.update(self.directory, change,
            registry=BoundRegistry(self.directory, self.lesson_id))
        try: self.verify()  # Re-read sealed bytes after confirmed registry commit.
        except BaseException as error:
            raise registry.CommitInterrupted(self.receipt, 'committed', phase='material-verification') from error
        return self.receipt


def attach_single(args, directory):
    """Deliberately separate dispatch: other shared Attempt callers stay closed."""
    if args.kind not in ('code', 'homework', 'resources') or any(
            getattr(args, name, None) is not None for name in ('material_text', 'material_origin')):
        raise ValueError('Windows single-file import does not support extracted/remote/prepared inputs')
    source = Path(args.source).expanduser().absolute()
    if source.suffix.lower() in ('.zip', '.pdf', '.html', '.htm'):
        raise ValueError('Windows single-file import requires ordinary local text/code')
    component(source.name)
    attempt = SingleFile(directory)
    def execute():
        from material_import import _CURRENT
        state = _CURRENT.get()
        if state is not None:
            if state['directory'] != attempt.directory or state.get('safe') is not None:
                raise ValueError('Material import context already owns another attempt')
            state['safe'] = attempt
        try:
            attempt.prepare(source, args.kind)
            result = attempt.publish(args, source)
        except BaseException as primary:
            try: attempt.close()
            except BaseException as cleanup:
                primary.add_note('Material cleanup failure: ' + repr(cleanup))
                if isinstance(primary, registry.CommitInterrupted):
                    primary.cleanup_errors += (cleanup,)
            raise
        else:
            try: attempt.close()
            except BaseException as cleanup:
                raise registry.CommitInterrupted(result, 'committed', phase='material-close') from cleanup
            return result
    owned_io(execute)
    print('Attached:', args.kind)
