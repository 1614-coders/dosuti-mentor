"""Pinned HTTP cache slots; external transport never receives mutable output names."""
import hashlib
import json
import os
from pathlib import Path
import stat
from safe_material_import import Attempt, stamp
from safe_zip_import import new_file, identity
from temporary_retention import preserve_temporary


class DownloadIO:
    def __init__(self, target):
        self.target = Path(target).absolute()
        self.owner = Attempt(self.target.parent, lesson=False, readonly=False)
        self.fd = self.owner.fd
        self.slots = {}
        self.stamps = {}
        self.part = self.target.name + '.part'
        self.receipt = self.target.name + '.resume.json'
        self.preserve = False

    def binding(self, name):
        self.owner.guard.check()
        before = os.fstat(self.slots[name])
        current = os.stat(name, dir_fd=self.fd, follow_symlinks=False)
        if not stat.S_ISREG(current.st_mode) or current.st_nlink != 1 or identity(current) != identity(before):
            raise ValueError('HTTP cache file was replaced; no uncertain entry removed')
        if name in self.stamps and stamp(before) != self.stamps[name]:
            raise ValueError('HTTP cache changed during transfer; preserve it and retry separately')

    def open_cache(self, name):
        fd = os.open(name, os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=self.fd)
        self.slots[name] = fd
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise ValueError('HTTP cache must be an ordinary private file')
        self.stamps[name] = stamp(info)
        self.binding(name)
        return fd

    def state(self, url):
        self.owner.guard.check()
        try:
            self.open_cache(self.part); self.open_cache(self.receipt)
        except FileNotFoundError:
            self.preserve = bool(self.slots)
            self.discard_cache_handles()
            return None
        self.preserve = True
        raw = os.pread(self.slots[self.receipt], 1024 * 1024 + 1, 0)
        try:
            value = json.loads(raw) if len(raw) <= 1024 * 1024 else None
            info = os.fstat(self.slots[self.part])
            os.lseek(self.slots[self.part], 0, os.SEEK_SET)
            with os.fdopen(os.dup(self.slots[self.part]), 'rb') as stream:
                checksum = hashlib.file_digest(stream, 'sha256').hexdigest()
            valid = (isinstance(value, dict) and value['url'] == url and value['size'] == info.st_size
                     and value['sha256'] == checksum and 0 < value['size'] < value['total'])
            import re
            valid = valid and isinstance(value['etag'], str) and re.fullmatch(r'"[^"\r\n]+"', value['etag'])
        except (ValueError, KeyError, TypeError): valid = False
        self.binding(self.part); self.binding(self.receipt)
        if valid: return value
        # Invalid legacy files are read-only evidence, never automatic cleanup targets.
        self.discard_cache_handles()
        return None

    def discard_cache_handles(self):
        for fd in self.slots.values(): os.close(fd)
        self.slots.clear(); self.stamps.clear()

    def absent_target(self):
        self.owner.guard.check()
        try: os.stat(self.target.name, dir_fd=self.fd, follow_symlinks=False)
        except FileNotFoundError: return
        raise ValueError('HTTP destination already exists; preserve it and use a fresh destination')

    def write(self, name, blocks, offset=0):
        self.owner.guard.check()
        if name not in self.slots:
            self.slots[name] = new_file(self.fd, name)
        self.binding(name)
        fd = self.slots[name]
        os.ftruncate(fd, offset); os.lseek(fd, offset, os.SEEK_SET)
        for block in blocks:
            view = memoryview(block)
            while view:
                count = os.write(fd, view)
                if not count: raise OSError('Incomplete HTTP cache write')
                view = view[count:]
        os.fsync(fd)
        self.stamps[name] = stamp(os.fstat(fd)); self.binding(name)

    def blocks(self, body, offset):
        if offset:
            self.binding(self.part); os.lseek(self.slots[self.part], 0, os.SEEK_SET)
            remaining = offset
            while remaining:
                block = os.read(self.slots[self.part], min(1024 * 1024, remaining))
                if not block: raise ValueError('Verified HTTP prefix became incomplete')
                remaining -= len(block)
                yield block
            self.binding(self.part)
        body.seek(0)
        while block := body.read(1024 * 1024): yield block

    def partial(self, body, offset, url, etag, total):
        self.preserve = True
        body.seek(0)
        self.write(self.part, iter(lambda: body.read(1024 * 1024), b''), offset)
        os.lseek(self.slots[self.part], 0, os.SEEK_SET)
        with os.fdopen(os.dup(self.slots[self.part]), 'rb') as stream:
            checksum = hashlib.file_digest(stream, 'sha256').hexdigest()
        self.binding(self.part)
        value = dict(url=url, etag=etag, total=total, size=os.fstat(self.slots[self.part]).st_size, sha256=checksum)
        self.write(self.receipt, [json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False).encode('utf-8')])

    def close(self):
        self.discard_cache_handles(); self.owner.close()
        if self.preserve: preserve_temporary(self.target.parent)
