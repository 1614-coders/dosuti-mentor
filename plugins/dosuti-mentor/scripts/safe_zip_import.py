"""ZIP-only, descriptor-relative writes. Never recursively discard a failed attempt."""
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import stat
import sys
import uuid
import zipfile

from material_import import preserve_attempt
from storage_lock import file_lock


def supported():
    functions = (os.open, os.mkdir, os.stat, os.rename)
    return (os.name == 'posix' and hasattr(os, 'O_NOFOLLOW') and hasattr(os, 'O_DIRECTORY')
            and all(f in os.supports_dir_fd for f in functions)
            and os.stat in os.supports_follow_symlinks)


def identity(info):
    return info.st_dev, info.st_ino


class Directories:
    """Pin ancestors; every write uses a descriptor, never a re-resolved pathname."""
    def __init__(self):
        self.fds = []
        self.links = []

    def open(self, parent, name, *, create=False, exclusive=False):
        if create:
            try: os.mkdir(name, 0o700, dir_fd=parent)
            except FileExistsError:
                if exclusive: raise
        before = os.stat(name, dir_fd=parent, follow_symlinks=False)
        fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
        self.fds.append(fd)
        if identity(before) != identity(os.fstat(fd)):
            raise ValueError('ZIP destination changed while opening a directory')
        self.links.append((parent, name, fd))
        return fd

    def pin(self, path):
        path = Path(path).absolute()
        # macOS owns this single root alias. Rewrite to its fixed physical
        # prefix; never resolve user components or follow the alias as an FD.
        if sys.platform == 'darwin' and path.parts[1:2] == ('var',):
            info = os.lstat('/var')
            if (not stat.S_ISLNK(info.st_mode) or info.st_uid != 0
                    or os.readlink('/var') != 'private/var'):
                raise ValueError('Untrusted macOS system alias /var')
            path = Path('/private/var').joinpath(*path.parts[2:])
        fd = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        self.fds.append(fd)
        for name in path.parts[1:]: fd = self.open(fd, name)
        return fd

    def check(self):
        for parent, name, fd in self.links:
            entry = os.stat(name, dir_fd=parent, follow_symlinks=False)
            if not stat.S_ISDIR(entry.st_mode) or identity(entry) != identity(os.fstat(fd)):
                raise ValueError('ZIP destination or its parent changed; nothing registered')

    def close(self):
        for fd in reversed(self.fds): os.close(fd)


def new_file(parent, name):
    return os.open(name, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parent)


def read_lesson(fd, path):
    import course
    raw = os.open('lesson.json', os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
    with os.fdopen(raw, 'r', encoding='utf-8') as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise ValueError('Lesson registry must be an ordinary file')
        data = json.load(stream)
    if (not isinstance(data, dict) or not isinstance(data.get('id'), str)
            or data['id'].casefold() != path.parent.name.casefold()):
        raise ValueError('Lesson registry identity changed')
    return course.resolved(data, path)


def publish(args, directory, fd, guard, material, *, item=None, change=None):
    """Use the same lesson lock, merge and serialization contract, with anchored I/O."""
    import course
    path = directory / 'lesson.json'
    guard.check()
    with file_lock(directory / '.lesson.lock', directory_fd=fd):
        data = read_lesson(fd, path)
        if change is None: course.publish_attachment(data, args, material, item)
        else: change(data)
        data['revision'] = data.get('revision', 0) + 1
        data['updated'] = course.now()
        text = json.dumps(course.anchored(data, path), ensure_ascii=False, indent=2)
        temporary = 'lesson.json.' + uuid.uuid4().hex + '.tmp'
        with os.fdopen(new_file(fd, temporary), 'w', encoding='utf-8') as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        guard.check()
        os.rename(temporary, 'lesson.json', src_dir_fd=fd, dst_dir_fd=fd)


def attach_zip(args, directory, source):
    import course
    from material_sources import MAX_BYTES, validate_archive
    retained = preserve_attempt()
    guard = Directories()
    target = None
    try:
        if not supported():
            raise ValueError('Safe ZIP import is unavailable on this platform: descriptor-relative '
                             'no-follow operations are required; no unsafe fallback was used')
        # Validate all members before creating a destination; read the same open ZIP.
        with source.open('rb') as raw:
            if os.fstat(raw.fileno()).st_size > MAX_BYTES:
                raise ValueError('Code ZIP exceeds 32 MiB')
            with zipfile.ZipFile(raw) as archive:
                validate_archive(archive)
                entries = []
                for entry in archive.infolist():
                    parts = PurePosixPath(entry.filename).parts
                    if not parts or len(parts) > 64:
                        raise ValueError('ZIP member path is empty or too deep')
                    if any(part.casefold() == '.git' for part in parts): continue
                    entries.append((entry, parts))
                lesson_fd = guard.pin(directory)
                read_lesson(lesson_fd, directory / 'lesson.json')
                code_fd = guard.open(lesson_fd, 'code', create=True)
                name = uuid.uuid4().hex[:12]
                target = directory / 'code' / name
                root_fd = guard.open(code_fd, name, create=True, exclusive=True)
                dirs = {(): root_fd}
                inventory = {}
                total = 0
                for entry, parts in entries:
                    parents = parts if entry.is_dir() else parts[:-1]
                    for depth in range(1, len(parents) + 1):
                        key = parents[:depth]
                        if key not in dirs:
                            dirs[key] = guard.open(dirs[key[:-1]], key[-1], create=True, exclusive=True)
                    if entry.is_dir(): continue
                    relative = '/'.join(parts)
                    parent = dirs[parts[:-1]]
                    with archive.open(entry) as incoming:
                        with os.fdopen(new_file(parent, parts[-1]), 'w+b') as output:
                            expected = hashlib.sha256()
                            size = 0
                            while block := incoming.read(1024 * 1024):
                                size += len(block); total += len(block)
                                if size > entry.file_size or total > 256 * 1024 * 1024:
                                    raise ValueError('ZIP expanded data exceeds extraction limits')
                                output.write(block); expected.update(block)
                            output.flush(); os.fsync(output.fileno()); output.seek(0)
                            checksum = hashlib.file_digest(output, 'sha256').hexdigest()
                            if size != entry.file_size or checksum != expected.hexdigest():
                                raise ValueError('ZIP output changed during extraction')
                            before = os.fstat(output.fileno())
                            if identity(os.stat(parts[-1], dir_fd=parent, follow_symlinks=False)) != identity(before):
                                raise ValueError('ZIP output was replaced during extraction')
                            os.fchmod(output.fileno(), 0o400)
                    inventory[relative] = dict(sha256=checksum, size=size)
                if not inventory: raise ValueError('ZIP contains no code files')
                guard.check()
                for fd in reversed(list(dirs.values())): os.fchmod(fd, 0o500)
                inventory = dict(sorted(inventory.items()))
                checksum = hashlib.sha256(json.dumps(inventory, sort_keys=True).encode()).hexdigest()
                material = dict(status='provided', snapshot=str(target),
                    origin=getattr(args, 'material_origin', None) or dict(path=str(source)),
                    lecture_match='unverified', revision=uuid.uuid4().hex,
                    inventory=inventory, content_sha256=checksum,
                    integrity='inventory_recorded', sealed_at=course.now())
                publish(args, directory, lesson_fd, guard, material)
        print('Attached:', args.kind)
    except BaseException as error:
        paths = retained + ([str(target)] if target is not None else [])
        if paths:
            print('ZIP import files preserved for inspection; no recursive cleanup: ' + ', '.join(paths), file=sys.stderr)
        if isinstance(error, (zipfile.BadZipFile, RuntimeError, NotImplementedError)):
            raise ValueError('Safe ZIP import failed: ' + str(error)) from error
        raise
    finally:
        guard.close()
