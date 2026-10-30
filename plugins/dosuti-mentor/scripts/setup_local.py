"""Create a private runtime outside the plugin install cache."""
import argparse
import os
from pathlib import Path
import shutil
import subprocess
import sys
import uuid
import venv
from managed_storage import find_home, initialize, session, safe
from storage_lock import file_lock
from bootstrap import verify_python


def install(runtime, *, if_needed=False):
    target = safe(runtime / 'venv')
    if Path(sys.prefix).resolve() == target:
        raise ValueError('Run repair with the base Python, outside this venv')
    if target.exists():
        config = safe(target / 'pyvenv.cfg')
        if not config.is_file(): raise ValueError('Unknown venv folder; preserve it before repair')
        if if_needed:
            python = target / ('Scripts/python.exe' if sys.platform == 'win32' else 'bin/python')
            try:
                verify_python(python, runner=subprocess.run)
                return python
            except (ValueError, subprocess.CalledProcessError):
                pass
    staging = safe(runtime / ('venv-staging-' + uuid.uuid4().hex))
    backup = None
    prepared = False
    try:
        venv.EnvBuilder(with_pip=True).create(staging)
        python = staging / ('Scripts/python.exe' if sys.platform == 'win32' else 'bin/python')
        cache = os.environ.get('PIP_CACHE_DIR', str(runtime / 'cache/pip'))
        subprocess.run([str(python), '-m', 'pip', '--isolated', 'install', '--disable-pip-version-check',
                        '--cache-dir', cache,
                        '--only-binary=:all:', '-r', str(Path(__file__).with_name('requirements.txt'))],
                       env=dict(os.environ, PIP_CONFIG_FILE=os.devnull), check=True)
        verify_python(python, runner=subprocess.run)
        prepared = True
        if target.exists():
            backup = runtime / ('venv-previous-' + uuid.uuid4().hex)
            target.rename(backup)
        staging.rename(target)
        verify_python(target / python.relative_to(staging), runner=subprocess.run)
    except BaseException:
        # Renames can succeed immediately before an interrupt; inspect actual paths.
        # Never delete the old environment, even if rollback itself cannot complete.
        if backup is not None and backup.exists():
            if target.exists():
                target.rename(runtime / ('venv-failed-' + uuid.uuid4().hex))
            backup.rename(target)
        elif prepared and not staging.exists() and target.exists():
            target.rename(staging)  # Failed first install must not look ready.
        if staging.exists(): shutil.rmtree(staging)
        raise
    if backup is not None: print('Previous environment preserved at:', backup)
    return target / ('Scripts/python.exe' if sys.platform == 'win32' else 'bin/python')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--runtime', type=Path, required=True)
    parser.add_argument('--if-needed', action='store_true', help='Keep a compatible existing environment')
    args = parser.parse_args()
    if sys.version_info < (3, 11):
        parser.error('Python 3.11 or newer is required; Python 3.12 is the tested version')
    runtime = safe(args.runtime)
    home = find_home(runtime) or initialize(runtime, legacy_runtime=True)
    from heavy_work import slot
    with session(home), slot(home), file_lock(runtime / '.setup.lock'):
        python = install(runtime, if_needed=args.if_needed)
    print('Local runtime ready:', python)


if __name__ == '__main__': main()
