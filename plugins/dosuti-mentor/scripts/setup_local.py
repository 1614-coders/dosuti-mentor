"""Create a private runtime outside the plugin install cache."""
import argparse
from pathlib import Path
import subprocess
import sys
import venv

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--runtime', type=Path, required=True)
args = parser.parse_args()
if sys.version_info < (3, 11):
    parser.error('Python 3.11 or newer is required; Python 3.12 is the tested version')
runtime = args.runtime.expanduser().resolve()
venv.EnvBuilder(with_pip=True).create(runtime / 'venv')
python = runtime / 'venv' / ('Scripts/python.exe' if sys.platform == 'win32' else 'bin/python')
subprocess.run([str(python), '-m', 'pip', 'install', '--disable-pip-version-check',
                '-r', str(Path(__file__).with_name('requirements.txt'))], check=True)
print('Local runtime ready:', python)
