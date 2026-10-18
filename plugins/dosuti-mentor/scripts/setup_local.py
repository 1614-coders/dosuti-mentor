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


def install(runtime):
    target = safe(runtime / 'venv')
    backup = None
    if target.exists():
        config = safe(target / 'pyvenv.cfg')
        if not config.is_file(): raise ValueError('Unknown venv folder; preserve it before repair')
        fields = dict((k.strip(), v.strip()) for line in config.read_text().splitlines()
                      if '=' in line for k, v in [line.split('=', 1)])
        base = Path(getattr(sys, '_base_executable', sys.executable)).resolve().parent
        if (Path(fields.get('home', '')).resolve() != base or
                fields.get('version', '').split('.')[:2] != [str(v) for v in sys.version_info[:2]]):
            if Path(sys.prefix).resolve() == target: raise ValueError('Run repair with the base Python, outside this venv')
            backup = runtime / ('venv-previous-' + uuid.uuid4().hex)
            target.rename(backup)
    try:
        venv.EnvBuilder(with_pip=True).create(target)
        python = target / ('Scripts/python.exe' if sys.platform == 'win32' else 'bin/python')
        cache = os.environ.get('PIP_CACHE_DIR', str(runtime / 'cache/pip'))
        subprocess.run([str(python), '-m', 'pip', '--isolated', 'install', '--disable-pip-version-check',
                        '--cache-dir', cache,
                        '--only-binary=:all:', '-r', str(Path(__file__).with_name('requirements.txt'))],
                       env=dict(os.environ, PIP_CONFIG_FILE=os.devnull), check=True)
    except BaseException:
        if backup is not None:
            if target.exists(): shutil.rmtree(target)
            backup.rename(target)
        raise
    if backup is not None: shutil.rmtree(backup)
    return python


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--runtime', type=Path, required=True)
    args = parser.parse_args()
    if sys.version_info < (3, 11):
        parser.error('Python 3.11 or newer is required; Python 3.12 is the tested version')
    runtime = safe(args.runtime)
    home = find_home(runtime) or initialize(runtime, legacy_runtime=True)
    from heavy_work import slot
    with session(home), slot(home), file_lock(runtime / '.setup.lock'):
        python = install(runtime)
    print('Local runtime ready:', python)


if __name__ == '__main__': main()
