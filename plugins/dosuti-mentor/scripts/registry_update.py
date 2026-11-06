"""Cooperative lesson-registry transactions; descriptors stay inside storage adapters."""
from dataclasses import dataclass, field
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import stat
import sys
import uuid


class RevisionConflict(ValueError):
    pass


@dataclass(frozen=True)
class Receipt:
    directory: str
    revision: int
    sha256: str
    identity: str
    previous_sha256: str
    previous_identity: str
    temporary: str


class CommitInterrupted(OSError):
    """Publication did not return normally; inspect/recover receipt, never retry blindly."""
    def __init__(self, receipt, outcome, *, phase='replace', cleanup_errors=()):
        self.receipt, self.outcome = receipt, outcome
        self.phase, self.cleanup_errors = phase, tuple(cleanup_errors)
        super().__init__(f'Registry commit interrupted; observed outcome={outcome}; '
                         f'candidate={receipt.temporary}; no automatic retry or deletion')


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def temporary_name():
    return 'lesson.json.' + uuid.uuid4().hex + '.tmp'


def revision(data):
    value = data.get('revision', 0)
    if type(value) is not int or value < 0:
        raise ValueError('Invalid lesson registry revision')
    return value


@dataclass
class _CommitState:
    receipt: Receipt | None = None
    outcome: str = 'unknown'
    phase: str = 'prepare'
    failures: list = field(default_factory=list)

    def failed(self, error, phase=None):
        if not any(error is previous for _, previous in self.failures):
            self.failures.append((phase or self.phase, error))


@contextmanager
def _scope(resource, state, exit_phase):
    """Unwind storage scopes without losing a primary BaseException to cleanup."""
    value = resource.__enter__()
    try:
        yield value
    except BaseException as primary:
        state.failed(primary)
        state.phase = exit_phase
        try: resource.__exit__(type(primary), primary, primary.__traceback__)
        except BaseException as cleanup: state.failed(cleanup, exit_phase)
        raise
    else:
        state.phase = exit_phase
        try: resource.__exit__(None, None, None)
        except BaseException as cleanup:
            state.failed(cleanup, exit_phase)
            raise


def _observe(store, receipt):
    raw, identity = store.read()
    if identity == receipt.identity and digest(raw) == receipt.sha256:
        return 'committed'
    if identity == receipt.previous_identity and digest(raw) == receipt.previous_sha256:
        return 'unchanged'
    return 'unknown'


def _update(store, change, expected_revision, state):
    import course
    if expected_revision is not None and (type(expected_revision) is not int or expected_revision < 0):
        raise ValueError('Expected revision must be a nonnegative integer')
    store.commit_state = state
    with _scope(store.lock(), state, 'unlock'):
        raw, previous_identity = store.read()
        data = json.loads(raw.decode('utf-8-sig'))
        if not isinstance(data, dict):
            raise ValueError('Invalid lesson registry object')
        current = revision(data)
        if expected_revision is not None and current != expected_revision:
            raise RevisionConflict(f'Lesson revision conflict: expected {expected_revision}, found {current}')
        data = course.resolved(data, store.directory / 'lesson.json')
        change(data)
        data['revision'], data['updated'] = current + 1, course.now()
        payload = json.dumps(course.anchored(data, store.directory / 'lesson.json'),
                             ensure_ascii=False, indent=2).encode('utf-8')
        name = temporary_name()
        try:
            stage = store.stage(name, payload)
        except BaseException as error:
            error.add_note(f'Registry publication not started; candidate {name} preserved if present; no cleanup')
            raise
        receipt = Receipt(str(store.directory), current + 1, digest(payload), stage.identity,
                          digest(raw), previous_identity, stage.name)
        state.receipt, state.phase = receipt, 'replace'
        try:
            store.replace(stage)
            state.phase = 'verification'
            state.outcome = _observe(store, receipt)
            if state.outcome != 'committed':
                raise OSError('Registry post-publication verification did not confirm commit')
            return receipt
        except BaseException as primary:
            state.failed(primary)
            if state.outcome != 'committed':
                try: state.outcome = _observe(store, receipt)
                except BaseException as observation:
                    state.failed(observation, 'verification')
            raise


def backend_type():
    if sys.platform == 'win32':
        from registry_windows import WindowsRegistry
        return WindowsRegistry
    return PosixRegistry


def _transaction(directory, operation, state=None):
    state = state or _CommitState()
    if sys.platform == 'win32':
        from registry_windows import owned_io
        def execute():
            with _scope(backend_type()(directory), state, 'close') as store:
                return operation(store)
        return owned_io(execute)
    with _scope(PosixRegistry(directory), state, 'close') as store:
        return operation(store)


def update(directory, change, *, expected_revision=None, registry=None):
    """Read fresh state under the common lock; optional registry is an opaque adapter."""
    state = _CommitState()
    try:
        if registry is not None:
            if Path(directory).absolute() != registry.directory:
                raise ValueError('Pinned registry belongs to a different lesson')
            with _scope(registry, state, 'close') as store:
                return _update(store, change, expected_revision, state)
        return _transaction(directory, lambda store: _update(store, change, expected_revision, state), state)
    except BaseException as error:
        if state.receipt is None: raise
        state.failed(error)
        phase, primary = state.failures[0]
        failure = CommitInterrupted(state.receipt, state.outcome, phase=phase,
                                    cleanup_errors=[item for _, item in state.failures[1:]])
        for cleanup_phase, cleanup in state.failures[1:]:
            failure.add_note(f'Additional {cleanup_phase} failure: {type(cleanup).__name__}: {cleanup}')
        raise failure from primary


def recover(directory, receipt):
    """Read-only classification under the same lock; never replay or clean a candidate."""
    if str(Path(directory).absolute()) != receipt.directory:
        raise ValueError('Receipt belongs to a different lesson')
    def observe(store):
        with store.lock():
            try: return _observe(store, receipt)
            except (OSError, ValueError): return 'unknown'
    return _transaction(directory, observe)


@dataclass
class _PosixStage:
    name: str
    identity: str


def _identity(info):
    return f'{info.st_dev}:{info.st_ino}'


class PosixRegistry:
    """Own or borrow a pinned parent; never close a borrowed guard."""
    def __init__(self, directory, *, parent=None, guard=None):
        self.directory = Path(directory).absolute()
        self.parent, self.guard, self.owned = parent, guard, guard is None

    def __enter__(self):
        import safe_zip_import as secure
        if not secure.supported():
            raise ValueError('Pinned POSIX registry operations unavailable; no fallback')
        if self.owned:
            self.guard = secure.Directories()
            try:
                self.parent = self.guard.pin(self.directory)
                self.guard.check()
            except BaseException:
                self.guard.close()
                raise
        else: self.guard.check()
        return self

    def __exit__(self, *args):
        if self.owned: self.guard.close()

    def lock(self):
        from storage_lock import file_lock
        self.guard.check()
        return file_lock(self.directory / '.lesson.lock', directory_fd=self.parent)

    def read(self):
        self.guard.check()
        fd = os.open('lesson.json', os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=self.parent)
        with os.fdopen(fd, 'rb') as stream:
            before = os.fstat(fd)
            if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
                raise ValueError('Lesson registry must be an ordinary single-link file')
            raw = stream.read()
            after = os.fstat(fd)
            if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
                    after.st_size, after.st_mtime_ns, after.st_ctime_ns):
                raise ValueError('Lesson registry changed while reading')
        self.guard.check()
        return raw, _identity(before)

    def stage(self, name, payload):
        import safe_zip_import as secure
        self.guard.check()
        with os.fdopen(secure.new_file(self.parent, name), 'w+b') as stream:
            info = os.fstat(stream.fileno())
            if stream.write(payload) != len(payload): raise OSError('Short registry write')
            stream.flush(); os.fsync(stream.fileno()); stream.seek(0)
            if stream.read() != payload: raise OSError('Registry readback mismatch')
            identity = _identity(info)
            entry = os.stat(name, dir_fd=self.parent, follow_symlinks=False)
            if _identity(entry) != identity or entry.st_nlink != 1 or not stat.S_ISREG(entry.st_mode):
                raise ValueError('Registry stage replaced')
        return _PosixStage(name, identity)

    def replace(self, stage):
        self.guard.check()
        entry = os.stat(stage.name, dir_fd=self.parent, follow_symlinks=False)
        if _identity(entry) != stage.identity or entry.st_nlink != 1 or not stat.S_ISREG(entry.st_mode):
            raise ValueError('Registry stage identity changed')
        os.rename(stage.name, 'lesson.json', src_dir_fd=self.parent, dst_dir_fd=self.parent)


def pinned_posix(directory, parent, guard):
    """Compatibility boundary for existing POSIX storage code, not a subject-level FD API."""
    return PosixRegistry(directory, parent=parent, guard=guard)
