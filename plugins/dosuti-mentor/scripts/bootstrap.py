"""Prepare the private DoSuti runtime in one managed data directory."""
import argparse
import json
import os
from pathlib import Path
import platform
import subprocess
import sys

from managed_storage import initialize, safe, validate


def default_home():
    if sys.platform == 'win32':
        root = os.environ.get('LOCALAPPDATA')
        if not root:
            raise ValueError('LOCALAPPDATA is unavailable; pass --home explicitly')
        return Path(root) / 'DoSutiMentor'
    if sys.platform == 'darwin':
        return Path.home() / 'Library/Application Support/DoSutiMentor'
    return Path(os.environ.get('XDG_DATA_HOME', Path.home() / '.local/share')) / 'DoSutiMentor'


def compatibility():
    system = sys.platform
    machine = platform.machine().lower()
    if system not in ('win32', 'darwin', 'linux'):
        raise ValueError(f'Unsupported operating system: {system}')
    if sys.implementation.name != 'cpython':
        raise ValueError('CPython 3.11 or 3.12 is required')
    if not (3, 11) <= sys.version_info[:2] <= (3, 12):
        raise ValueError('CPython 3.11 or 3.12 is required')
    if system == 'win32' and machine not in ('amd64', 'x86_64'):
        raise ValueError('Windows setup currently supports x64 only')
    if system == 'darwin' and machine not in ('arm64', 'x86_64'):
        raise ValueError(f'Unsupported macOS architecture: {machine}')
    if system == 'linux' and machine not in ('aarch64', 'arm64', 'amd64', 'x86_64'):
        raise ValueError(f'Unsupported Linux architecture: {machine}')
    return dict(platform=system, machine=machine, python=platform.python_version())


def runtime_python(home):
    home = safe(home)
    if home.is_relative_to(Path(__file__).resolve().parents[1]):
        raise ValueError('Runtime must stay outside the installed plugin')
    return safe(home / 'runtime/venv' / ('Scripts/python.exe' if sys.platform == 'win32' else 'bin/python'))


def verify(home, runner=subprocess.run):
    compatibility()
    home = validate(home)
    python = runtime_python(home)
    if not python.is_file():
        raise ValueError('Private Python is unavailable; complete technical setup before learning')
    code = '''import sys, subprocess
assert (3, 11) <= sys.version_info[:2] <= (3, 12)
import av, certifi, faster_whisper, PIL, yt_dlp, imageio_ffmpeg
subprocess.run([imageio_ffmpeg.get_ffmpeg_exe(), '-version'], check=True,
               stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=15)
'''
    runner([str(python), '-I', '-B', '-c', code], check=True, capture_output=True, timeout=60)
    return dict(home=str(home), python=str(python), verified=True)


def prepare(home, runner=subprocess.run):
    compatibility()
    python = runtime_python(home)
    home = initialize(safe(home))
    if python.is_file():
        return verify(home, runner=runner)
    if python.parent.parent.exists():
        raise ValueError('Existing runtime is incomplete; preserve it before explicit repair')
    env = dict(os.environ, DOSUTI_HOME=str(home))
    runner([sys.executable, '-B', str(Path(__file__).with_name('setup_local.py')),
            '--runtime', str(home / 'runtime')], env=env, check=True)
    return verify(home, runner=runner)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--home', type=Path, default=default_home())
    parser.add_argument('--check', action='store_true', help='Validate this platform without changing files')
    parser.add_argument('--verify', action='store_true', help='Run the installed private Python and check dependencies without installing')
    args = parser.parse_args()
    try:
        result = compatibility() if args.check else verify(args.home) if args.verify else prepare(args.home)
    except (ValueError, OSError, subprocess.SubprocessError) as error:
        parser.error(str(error))
    print(json.dumps(result, ensure_ascii=False))


if __name__ == '__main__':
    main()
