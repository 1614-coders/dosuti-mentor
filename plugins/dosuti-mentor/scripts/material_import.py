"""Request-local import ownership; lesson registry remains the source of truth."""
from contextvars import ContextVar
from functools import wraps
from pathlib import Path
import stat
import sys
from profile_storage import checked
from storage_lock import file_lock

_CURRENT = ContextVar('material_import_attempt', default=None)


def reserve(path, directory=False):
    """Claim a fresh path exclusively before copying; never adopt an existing file."""
    path = Path(path).absolute()
    state = _CURRENT.get()
    if state is not None and not path.is_relative_to(state['directory']):
        raise ValueError('Import target escapes lesson')
    for parent in reversed(path.parents): checked(parent, True)
    path.parent.mkdir(parents=True, exist_ok=True)
    if directory: path.mkdir()
    else:
        with path.open('xb'): pass
    if state is not None:
        info = path.lstat()
        state['owned'].append((path, (info.st_dev, info.st_ino)))
    return path


def remove_owned(path):
    """Remove sealed attempt files and links themselves, never their targets."""
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode): path.unlink(); return
    if getattr(info, 'st_file_attributes', 0) & 0x400:
        raise ValueError('Import cleanup refuses reparse points')
    if stat.S_ISDIR(info.st_mode):
        path.chmod(stat.S_IMODE(info.st_mode) | stat.S_IWUSR | stat.S_IXUSR)
        for child in path.iterdir(): remove_owned(child)
        path.rmdir()
    else:
        if info.st_nlink != 1: raise ValueError('Import cleanup refuses hard links')
        path.chmod(stat.S_IMODE(info.st_mode) | stat.S_IWUSR)
        path.unlink()


def references(value, path):
    if isinstance(value, dict): return any(references(v, path) for v in value.values())
    if isinstance(value, list): return any(references(v, path) for v in value)
    return isinstance(value, str) and Path(value).is_absolute() and Path(value).is_relative_to(path)


def cleanup(state):
    if not state['owned']: return
    import course
    with file_lock(state['directory'] / '.lesson.lock'):
        # save() may have raised after replace. Include histories and candidates.
        data = course.read(state['directory'] / 'lesson.json')
        if (not isinstance(data, dict) or not isinstance(data.get('id'), str)
                or data['id'].casefold() != state['directory'].name.casefold()):
            raise ValueError('Cannot verify lesson registry; preserve import files')
        for path, identity in reversed(state['owned']):
            if references(data, path): continue
            for parent in path.parents: checked(parent, True)
            try: info = path.lstat()
            except FileNotFoundError: continue
            if (info.st_dev, info.st_ino) != identity:
                raise ValueError('Import cleanup target changed; preserve it')
            remove_owned(path)


def importing(function):
    @wraps(function)
    def run(args, *a, **kw):
        import course
        directory = course.lesson_path(args).absolute()
        current = _CURRENT.get()
        if current is not None and current['directory'] == directory:
            return function(args, *a, **kw)
        state = dict(directory=directory, owned=[])
        token = _CURRENT.set(state)
        try:
            try: result = function(args, *a, **kw)
            except BaseException as error:
                try: cleanup(state)
                except Exception as failure:
                    error.add_note('Import cleanup incomplete: ' + str(failure))
                    print('Import files preserved; cleanup could not be verified:', failure, file=sys.stderr)
                raise
            cleanup(state)
            return result
        finally: _CURRENT.reset(token)
    return run
