"""Owned, descriptor-relative material writes; no path-based rollback or deletion."""
from contextlib import contextmanager
import hashlib
import json
import ctypes
import tempfile
import os
from pathlib import Path
import stat
import sys
import uuid
import safe_zip_import as secure

MAX_BYTES = 32 * 1024 * 1024
IGNORE = {'.git', '.venv', 'venv', 'node_modules', 'target', '__pycache__'}


def stamp(info):
    return (info.st_dev, info.st_ino, info.st_mode, info.st_size,
            info.st_mtime_ns, info.st_ctime_ns, info.st_uid, info.st_gid)


def move_exclusive(source_parent, source, destination_parent, destination):
    """Atomic no-replace publication, with pinned source and destination parents."""
    libc = ctypes.CDLL(None, use_errno=True)
    if sys.platform == 'darwin':
        function = libc.renameatx_np; flag = 4  # RENAME_EXCL
    elif sys.platform.startswith('linux') and hasattr(libc, 'renameat2'):
        function = libc.renameat2; flag = 1  # RENAME_NOREPLACE
    else:
        raise ValueError('Exclusive directory publication is unavailable; no unsafe fallback')
    function.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    function.restype = ctypes.c_int
    if function(source_parent, os.fsencode(source), destination_parent, os.fsencode(destination), flag):
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error), destination)


class Attempt:
    def __init__(self, directory, *, lesson=True, readonly=True, create_parents=False):
        if not secure.supported():
            raise ValueError('Safe material import is unavailable on this platform: '
                             'descriptor-relative no-follow operations are required; no unsafe fallback was used')
        self.directory = directory
        self.readonly = readonly
        self.guard = secure.Directories()
        self.files = {}
        self.paths = []
        self.scratch = []
        self.retained = []
        self.published = False
        try:
            if lesson:
                self.fd = self.guard.pin(directory)
                secure.read_lesson(self.fd, directory / 'lesson.json')
            else:
                if create_parents:
                    parent = self.guard.pin(Path(directory.anchor))
                    for part in directory.parent.parts[1:]: parent = self.guard.open(parent, part, create=True)
                else: parent = self.guard.pin(directory.parent)
                try: self.fd = self.guard.open(parent, directory.name)
                except FileNotFoundError:
                    try: self.fd = self.fresh_at(parent, directory.name)
                    except FileExistsError: self.fd = self.guard.open(parent, directory.name)
        except BaseException:
            self.guard.close()
            raise

    def fresh_at(self, parent, name):
        # Create outside the replaceable destination, pin first, then move the
        # very same directory exclusively. mkdir-at followed by open-at alone
        # cannot prove ownership when the name is replaced between those calls.
        temporary = Path(tempfile.mkdtemp(prefix='dosuti-import-', dir=Path(tempfile.gettempdir()).resolve())).absolute()
        self.scratch.append(temporary)
        fd = self.guard.pin(temporary)
        self.guard.links.pop()  # This name is about to move; the FD stays pinned.
        source_parent = self.guard.pin(temporary.parent)
        move_exclusive(source_parent, temporary.name, parent, name)
        self.scratch.remove(temporary)
        self.guard.links.append((parent, name, fd))
        self.guard.check()
        return fd

    def fresh(self, category, name=None):
        parent = self.guard.open(self.fd, category, create=True)
        name = name or uuid.uuid4().hex[:12]
        path = self.directory / category / name
        self.paths.append(path)
        return path, self.fresh_at(parent, name)

    def check(self):
        self.guard.check()
        for path, (parent, name, identity, checksum) in self.files.items():
            fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
            try:
                info = os.fstat(fd)
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or secure.identity(info) != identity:
                    raise ValueError('Imported file was replaced; preserve the attempt')
                with os.fdopen(os.dup(fd), 'rb') as stream:
                    if hashlib.file_digest(stream, 'sha256').hexdigest() != checksum:
                        raise ValueError('Imported bytes changed; preserve the attempt')
            finally:
                os.close(fd)

    @contextmanager
    def input(self, source):
        path = Path(source).expanduser().absolute()
        self.guard.check()
        known = self.files.get(path)
        parent = known[0] if known else self.guard.pin(path.parent)
        name = known[1] if known else path.name
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        try:
            before = os.fstat(fd)
            if not stat.S_ISREG(before.st_mode): raise ValueError('Material must be an ordinary local file')
            if known and secure.identity(before) != known[2]: raise ValueError('Import source was replaced')
            with os.fdopen(os.dup(fd), 'rb') as stream:
                yield stream
            if stamp(os.fstat(fd)) != stamp(before): raise ValueError('Import source changed during reading')
            self.guard.check()
        finally:
            os.close(fd)

    @contextmanager
    def output(self, parent, name, path):
        """Expose only an exclusively created FD, never reopen a reserved path."""
        fd = secure.new_file(parent, name)
        try:
            before = os.fstat(fd)
            with os.fdopen(os.dup(fd), 'w+b') as stream:
                yield stream
                stream.flush(); os.fsync(stream.fileno()); stream.seek(0)
                checksum = hashlib.file_digest(stream, 'sha256').hexdigest()
            after = os.stat(name, dir_fd=parent, follow_symlinks=False)
            if secure.identity(after) != secure.identity(before) or not stat.S_ISREG(after.st_mode) or after.st_nlink != 1:
                raise ValueError('Import output was replaced; nothing registered')
            os.fchmod(fd, 0o400 if self.readonly else 0o600)
            self.files[Path(path).absolute()] = (parent, name, secure.identity(before), checksum)
        finally:
            os.close(fd)

    def copy(self, source, parent, name, maximum=MAX_BYTES, *, destination):
        with self.input(source) as incoming:
            before = os.fstat(incoming.fileno())
            if before.st_size > maximum: raise ValueError('Material exceeds import size limit')
            with self.output(parent, name, destination) as output:
                size = 0; expected = hashlib.sha256()
                while block := os.read(incoming.fileno(), 1024 * 1024):
                    size += len(block)
                    if size > maximum: raise ValueError('Material exceeds import size limit')
                    output.write(block); expected.update(block)
                output.flush(); output.seek(0)
                if size != before.st_size or hashlib.file_digest(output, 'sha256').hexdigest() != expected.hexdigest():
                    raise ValueError('Material changed during copying')
            return dict(size=size, sha256=expected.hexdigest())

    def download(self, source, folder, fd, name):
        from preparation_control import run
        from urllib.parse import urlsplit
        import subprocess
        parsed = urlsplit(source)
        if parsed.username or parsed.password: raise ValueError('Material URL must not contain credentials')
        target = folder / name
        with self.output(fd, name, target) as stream:
            result = run(['curl', '--fail', '--location', '--silent', '--show-error',
                '--connect-timeout', '15', '--max-time', '120', '--max-filesize', str(MAX_BYTES),
                '--max-redirs', '5', '--proto', '=http,https', '--proto-redir', '=http,https',
                '--output', '-', '--write-out', '%{stderr}%{json}', '--', source],
                stdout=stream, stderr=subprocess.PIPE, encoding='utf-8', timeout=130)
            if result.returncode: raise ValueError('Material download failed (HTTP, timeout or size limit); provide a public file or local copy')
            if os.fstat(stream.fileno()).st_size > MAX_BYTES: raise ValueError('Material exceeds 32 MiB')
            try: info = json.loads(result.stderr)
            except ValueError as error: raise ValueError('Invalid material download receipt') from error
        return target, info

    def text(self, folder, fd, name, value):
        path = folder / name
        with self.output(fd, name, path) as stream: stream.write(value.encode('utf-8'))
        return path, self.files[path.absolute()][3]

    def tree(self, source, target, fd):
        source = Path(source).expanduser().absolute()
        root = self.guard.pin(source)
        inventory = {}; total = 0; entries = 0
        def walk(src, dst, relative=()):
            nonlocal total, entries
            before = stamp(os.fstat(src))
            for name in sorted(os.listdir(src)):
                if name in IGNORE: continue
                entries += 1
                if entries > 5000 or len(relative) >= 64: raise ValueError('Code tree exceeds import limits')
                info = os.stat(name, dir_fd=src, follow_symlinks=False)
                parts = (*relative, name)
                if stat.S_ISDIR(info.st_mode):
                    child = self.guard.open(src, name)
                    created = self.fresh_at(dst, name)
                    walk(child, created, parts)
                    os.fchmod(created, 0o500 if self.readonly else 0o700)
                elif stat.S_ISREG(info.st_mode):
                    # Source traversal is anchored too; input() reuses this descriptor.
                    source_fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=src)
                    try:
                        if secure.identity(os.fstat(source_fd)) != secure.identity(info): raise ValueError('Code source changed')
                        with self.output(dst, name, target.joinpath(*parts)) as stream:
                            checksum = hashlib.sha256(); size = 0
                            while block := os.read(source_fd, 1024 * 1024):
                                size += len(block); total += len(block)
                                if total > 256 * 1024 * 1024: raise ValueError('Code tree exceeds 256 MiB')
                                stream.write(block); checksum.update(block)
                            stream.flush(); stream.seek(0)
                            if hashlib.file_digest(stream, 'sha256').hexdigest() != checksum.hexdigest(): raise ValueError('Code output changed')
                            if size != info.st_size or stamp(os.fstat(source_fd)) != stamp(info): raise ValueError('Code source changed')
                        inventory['/'.join(parts)] = dict(size=size, sha256=checksum.hexdigest())
                    finally: os.close(source_fd)
                else: raise ValueError('Code sources must not contain symbolic links or special files')
            if stamp(os.fstat(src)) != before: raise ValueError('Code source directory changed')
        walk(root, fd)
        os.fchmod(fd, 0o500 if self.readonly else 0o700)
        return dict(sorted(inventory.items()))

    def publish(self, args, material=None, item=None, change=None):
        self.check()
        secure.publish(args, self.directory, self.fd, self.guard, material, item=item, change=change)
        self.published = True

    def close(self):
        self.guard.close()
        remaining = self.retained + self.scratch + ([] if self.published else self.paths)
        if remaining:
            print('Import files preserved for inspection; no path-based cleanup: ' + ', '.join(map(str, remaining)), file=sys.stderr)


def current(directory):
    from material_import import _CURRENT, preserve_attempt
    state = _CURRENT.get()
    if state is None or state['directory'] != Path(directory).absolute(): raise ValueError('Material import context required')
    if state.get('safe') is None:
        preserve_attempt()
        state['safe'] = Attempt(Path(directory).absolute())
    return state['safe']


def attach(args, directory):
    import course
    attempt = current(directory)
    source = Path(args.source).expanduser().absolute()
    if args.kind == 'code' and source.is_dir():
        if directory.resolve().is_relative_to(source.resolve()): raise ValueError('Code source cannot contain course storage')
        target, fd = attempt.fresh('code')
        inventory = attempt.tree(source, target, fd)
        checksum = hashlib.sha256(json.dumps(inventory, sort_keys=True).encode()).hexdigest()
        material = dict(status='provided', snapshot=str(target), origin=getattr(args, 'material_origin', None) or dict(path=str(source)),
            lecture_match='unverified', revision=uuid.uuid4().hex, inventory=inventory, content_sha256=checksum,
            integrity='inventory_recorded', sealed_at=course.now())
        attempt.publish(args, material)
    else:
        category = 'code' if args.kind == 'code' else 'homework' if args.kind == 'homework' else 'resources'
        folder, fd = attempt.fresh(category)
        name = source.name
        if name in ('', '.', '..') or args.kind == 'code' and name == '.git': raise ValueError('Invalid material filename')
        target = folder / name
        if getattr(args, 'material_text', None) is not None:
            name = 'extracted-' + uuid.uuid4().hex + '.txt'; target, checksum = attempt.text(folder, fd, name, args.material_text)
            copied = dict(sha256=checksum, size=len(args.material_text.encode('utf-8')))
        else: copied = attempt.copy(source, fd, name, destination=target)
        origin = getattr(args, 'material_origin', None)
        if args.kind == 'code':
            os.fchmod(fd, 0o500)
            inventory = {name: dict(sha256=copied['sha256'], size=copied['size'])}
            material = dict(status='provided', snapshot=str(folder), origin=origin or dict(path=str(source)),
                lecture_match='unverified', revision=uuid.uuid4().hex, inventory=inventory,
                content_sha256=hashlib.sha256(json.dumps(inventory, sort_keys=True).encode()).hexdigest(),
                integrity='inventory_recorded', sealed_at=course.now())
            attempt.publish(args, material)
        elif args.kind == 'homework':
            material = dict(status='provided', file=str(target), sha256=copied['sha256'], origin=origin or dict(source=str(source)))
            material['revision'] = course.homework_revision(material); attempt.publish(args, material)
        else:
            item = dict(id=uuid.uuid4().hex, source=str(target), kind=args.resource_type, status='downloaded',
                origin=dict(source=str(source), raw_file=str(target), sha256=copied['sha256'], size=copied['size'], media_type=''))
            attempt.publish(args, item=item)
    print('Attached:', args.kind)


def attach_github(args, directory):
    """Clone in private scratch, then use the same anchored ordinary-file importer."""
    import copy
    import course
    current(directory)  # Refuse unsupported platforms before starting a checkout.
    scratch = Path(tempfile.mkdtemp(prefix='dosuti-checkout-', dir=Path(tempfile.gettempdir()).resolve())).absolute()
    checkout = scratch / 'checkout'
    try:
        command = ['git', 'clone', '--depth', '1']
        if args.ref: command += ['--branch', args.ref]
        course.preparation_run(command + ['--', args.source, str(checkout)], check=True, timeout=600)
        commit = course.preparation_run(['git', '-C', str(checkout), 'rev-parse', 'HEAD'],
                                       check=True, capture_output=True, text=True, timeout=30).stdout.strip()
        clone = copy.copy(args); clone.source = str(checkout)
        clone.material_origin = dict(url=args.source, commit=commit, ref=args.ref)
        return attach(clone, directory)
    finally:
        # git is an external writer; its leftovers cannot be recursively removed
        # by pathname after an error. Never traverse a replacement during cleanup.
        print('Private checkout preserved for inspection: ' + str(scratch), file=sys.stderr)
