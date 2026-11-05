"""Request-local pinned worker writes; external decoders only receive open streams."""
from contextlib import contextmanager
from contextvars import ContextVar
import json
import hashlib
import re
import os
from pathlib import Path
import stat
import uuid
from safe_material_import import Attempt

MAX_SNAPSHOTS = 10000
MAX_SNAPSHOT_BYTES = 32 * 1024 * 1024
MAX_JOURNAL_BYTES = 256 * 1024 * 1024

_CURRENT = ContextVar('worker_io', default=None)


def current(folder):
    value = _CURRENT.get()
    if value is None or Path(folder).absolute() != value.out:
        raise ValueError('Worker writes require a pinned job directory; no unsafe fallback')
    return value


class WorkerIO:
    def __init__(self, out):
        self.out = Path(out).absolute()
        self.owner = Attempt(self.out, lesson=False, readonly=False, create_parents=True)
        self.owner.paths.append(self.out)
        self.fd = self.owner.fd
        self.force_fresh = False
        self.slots = {}
        self.folders = {self.out: self.fd}

    def check(self):
        self.owner.check()

    def enroll(self):
        # Called under the job lock, including after another process completed.
        for name in ('checkpoint.json','processing-status.json','manifest.json','transcript.json',
                     'transcript.md','tutor_packet.json','tutor_context.md','TUTOR_RULES.md','audio.wav'):
            try: fd = os.open(name, os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=self.fd)
            except FileNotFoundError: continue
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                os.close(fd); raise ValueError('Worker metadata must be an ordinary file')
            self.slots[name] = fd
            self.binding(name, fd)

    def binding(self, name, fd):
        self.owner.guard.check()
        info = os.stat(name, dir_fd=self.fd, follow_symlinks=False)
        original = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or (info.st_dev,info.st_ino) != (original.st_dev,original.st_ino):
            raise ValueError('Worker file was replaced; preserve uncertain entries')

    def write_slot(self, name, blocks):
        from safe_zip_import import new_file
        self.owner.guard.check()
        if name not in self.slots: self.slots[name] = new_file(self.fd, name)
        fd = self.slots[name]; self.binding(name, fd)
        os.ftruncate(fd,0); os.lseek(fd,0,os.SEEK_SET)
        for block in blocks:
            view=memoryview(block)
            while view:
                count=os.write(fd,view)
                if not count: raise OSError('Incomplete worker write')
                view=view[count:]
        os.fsync(fd); self.binding(name,fd)

    def journal(self):
        try: return self.owner.guard.open(self.fd,'.worker-state')
        except FileNotFoundError: return self.owner.fresh_at(self.fd,'.worker-state')

    def text(self, name, text):
        # A complete immutable snapshot precedes the update of our pinned inode.
        # This preserves crash recovery without unlinking a substituted leaf.
        from safe_zip_import import new_file
        self.owner.guard.check(); journal=self.journal()
        encoded=text.encode('utf-8'); checksum=hashlib.sha256(encoded).hexdigest()
        pattern=re.compile(re.escape(name)+r'\.(\d{20})-[0-9a-f]{64}\.snapshot')
        entries=os.listdir(journal)
        if len(entries)>=MAX_SNAPSHOTS:raise ValueError('Worker snapshot count limit reached; preserve this job and rebuild separately')
        total=sum(os.stat(entry,dir_fd=journal,follow_symlinks=False).st_size for entry in entries)
        if len(encoded)>MAX_SNAPSHOT_BYTES or total+len(encoded)>MAX_JOURNAL_BYTES:
            raise ValueError('Worker snapshots exceed storage limit; no history removed')
        sequence=1+max((int(match[1]) for entry in entries if (match:=pattern.fullmatch(entry))),default=0)
        filename=f'{name}.{sequence:020d}-{checksum}.snapshot'
        with os.fdopen(new_file(journal,filename),'wb') as stream:
            stream.write(encoded);stream.flush();os.fsync(stream.fileno())
        self.write_slot(name,[encoded])

    def json(self, name, value):
        self.text(name, json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False))

    def read_json(self, name):
        if name not in self.slots: raise ValueError('Worker state appeared after lock acquisition')
        fd=self.slots[name];self.binding(name,fd);os.lseek(fd,0,os.SEEK_SET)
        with os.fdopen(os.dup(fd),'rb') as stream: raw=stream.read(32*1024*1024+1)
        if len(raw)>32*1024*1024:raise ValueError('Worker state exceeds 32 MiB')
        try: return json.loads(raw)
        except (ValueError,UnicodeError):
            # Only a torn prefix of a complete, checksummed worker write may heal.
            journal=self.journal();pattern=re.compile(re.escape(name)+r'\.(\d{20})-([0-9a-f]{64})\.snapshot')
            entries=sorted(((int(m[1]),entry,m[2]) for entry in os.listdir(journal) if (m:=pattern.fullmatch(entry))),reverse=True)
            for _,entry,checksum in entries:
                source=os.open(entry,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=journal)
                with os.fdopen(source,'rb') as stream:
                    if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):continue
                    complete=stream.read(32*1024*1024+1)
                if len(complete)>32*1024*1024 or hashlib.sha256(complete).hexdigest()!=checksum:continue
                if not complete.startswith(raw):break
                value=json.loads(complete);self.write_slot(name,[complete])
                print('Recovered interrupted worker state:',name,flush=True)
                return value
            raise ValueError('Worker JSON is damaged; preserve it and its snapshots')

    def frames_folder(self):
        parent = self.owner.guard.open(self.fd, 'frames', create=True)
        name = 'attempt-' + uuid.uuid4().hex
        folder = self.out / 'frames' / name
        self.folders[folder] = self.owner.fresh_at(parent, name)
        self.owner.paths.append(folder)
        return folder

    @contextmanager
    def output(self, folder, name):
        self.owner.guard.check()
        folder = Path(folder).absolute()
        if folder not in self.folders: raise ValueError('Unowned worker output directory')
        with self.owner.output(self.folders[folder], name, folder / name) as stream:
            yield stream
        self.owner.guard.check()

    def blob(self, folder, name, value):
        with self.output(folder, name) as stream: stream.write(value)

    def audio_name(self):
        return 'audio-' + uuid.uuid4().hex + '.staging.wav'

    def publish_audio(self, temporary):
        temporary = Path(temporary).absolute()
        self.check()
        if temporary.parent != self.out or temporary not in self.owner.files:
            raise ValueError('Unowned audio output')
        with self.owner.input(temporary) as stream:
            self.write_slot('audio.wav',iter(lambda: stream.read(1024*1024),b''))
        self.check()

    def initialize_user_files(self):
        for name, value in [('visual_notes.json', dict(observations=[], uncertainties=[])),
                            ('progress.json', dict(status='prepared', taught_topics=[], last_taught_source_time=None, learner_answers=[]))]:
            self.owner.guard.check()
            try: info = os.stat(name, dir_fd=self.fd, follow_symlinks=False)
            except FileNotFoundError:
                self.owner.text(self.out, self.fd, name, json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False))
            else:
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                    raise ValueError('User progress/notes must be ordinary files; preserve them')

    def quarantine(self, preserved):
        self.force_fresh = True
        if 'checkpoint.json' in self.slots:
            state=self.read_json('checkpoint.json')
            state['stages']={};state.pop('media',None);state['status']='unverified'
            self.json('checkpoint.json',state)
        folder, fd = self.owner.fresh('.unverified')
        # Copy instead of moving/deleting entries whose names may be replaced.
        from processing_job import MANAGED
        for name in dict.fromkeys(('checkpoint.json', *MANAGED)):
            if name in preserved: continue
            self.owner.guard.check()
            try: info = os.stat(name, dir_fd=self.fd, follow_symlinks=False)
            except FileNotFoundError: continue
            if stat.S_ISDIR(info.st_mode):
                child = self.owner.fresh_at(fd, name)
                self.owner.tree(self.out / name, folder / name, child)
            else: self.owner.copy(self.out / name, fd, name, destination=folder / name)
        print('Unverified files preserved at:', folder, flush=True)

    def close(self):
        try:
            for fd in self.slots.values():os.close(fd)
        finally:self.owner.close()


@contextmanager
def session(out):
    value = WorkerIO(out)
    token = _CURRENT.set(value)
    try: yield value
    finally:
        _CURRENT.reset(token)
        value.close()
