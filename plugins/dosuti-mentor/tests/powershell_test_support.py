"""PowerShell transport shared by fixtures; never executes the code itself."""
import base64
import os
import shutil


def powershell_executable():
    # An explicit CI engine wins; an invalid explicit path must fail at launch,
    # not silently fall back to another version or turn into a skipped test.
    return os.environ.get('DOSUTI_TEST_PWSH') or shutil.which('pwsh') or shutil.which('powershell')


def literal(value):
    return "'" + str(value).replace("'", "''") + "'"


def powershell_command(executable, code):
    encoded = base64.b64encode(code.encode('utf-16-le')).decode('ascii')
    return [str(executable), '-NoLogo', '-NoProfile', '-NonInteractive', '-EncodedCommand', encoded]
