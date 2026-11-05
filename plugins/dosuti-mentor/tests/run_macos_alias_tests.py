"""Run alias tests in both TMPDIR modes; argument: directory for run logs."""
import os
from pathlib import Path
import subprocess
import sys


def main():
    if sys.platform != 'darwin':
        raise SystemExit('This two-mode fixture runner requires macOS')
    project = Path(__file__).resolve().parents[3]
    logs = Path(sys.argv[1]).absolute()
    logs.mkdir(parents=True, exist_ok=True)
    # Query once with the original environment. Do not clear temp variables:
    # confstr may require them under the current execution sandbox.
    location = subprocess.run(['/usr/bin/getconf', 'DARWIN_USER_TEMP_DIR'],
                              check=True, capture_output=True, text=True, timeout=10).stdout.strip()
    if not location or not Path(location).is_absolute():
        raise RuntimeError('System temp query did not return an absolute path')
    parent = Path(location).resolve(strict=True)
    parent.relative_to('/private/var')
    command = [sys.executable, '-B', '-m', 'unittest', 'discover', '-v',
               '-s', 'plugins/dosuti-mentor/tests', '-p', 'test_macos_system_alias.py']
    accepted = True
    for mode in ('original', 'override'):
        environment = dict(os.environ, DOSUTI_TEST_MACOS_ALIAS_ROOT=str(parent))
        if mode == 'override':
            environment['TMPDIR'] = '/private/tmp'
        result = subprocess.run(command, cwd=project, env=environment,
                                capture_output=True, text=True, timeout=60)
        output = result.stdout + result.stderr
        (logs / (mode + '.log')).write_text(output)
        print(mode + ':\n' + output, flush=True)
        if result.returncode != 0 or output.count(' ... ok\n') != 5 or 'skipped' in output:
            accepted = False
    if not accepted:
        raise SystemExit('Acceptance failed: require 5 passed, 0 skipped in each mode')


if __name__ == '__main__':
    main()
