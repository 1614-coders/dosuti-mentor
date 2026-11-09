"""Only Apple's fixed /var prefix is trusted; user symlinks remain forbidden."""
import os
from pathlib import Path
import sys
import subprocess
import tempfile
import unittest
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from safe_zip_import import Directories, identity
from safe_material_import import Attempt


@unittest.skipUnless(sys.platform == 'darwin', 'Native macOS root alias')
class MacOSSystemAlias(unittest.TestCase):
    def setUp(self):
        # The two-mode launcher prepares this physical root BEFORE changing
        # TMPDIR. Standalone discovery can query the unchanged environment.
        root = Path('/private/var')
        location = os.environ.get('DOSUTI_TEST_MACOS_ALIAS_ROOT')
        if location is None:
            location = subprocess.run(
                ['/usr/bin/getconf', 'DARWIN_USER_TEMP_DIR'], check=True,
                capture_output=True, text=True, timeout=10).stdout.strip()
        if not location or not Path(location).is_absolute():
            raise RuntimeError('macOS alias fixture requires an absolute system temp root')
        parent = Path(location).resolve(strict=True)
        try:
            parent.relative_to(root)
        except ValueError as error:
            raise RuntimeError('macOS alias fixture root must be inside /private/var; '
                               'prepare DOSUTI_TEST_MACOS_ALIAS_ROOT before changing TMPDIR') from error
        # On macOS any preparation failure is a setup error, never a skip.
        self.tmp = tempfile.TemporaryDirectory(prefix='dosuti-alias-', dir=parent)
        self.addCleanup(self.tmp.cleanup)
        self.physical = Path(self.tmp.name).resolve(strict=True)
        relative = self.physical.relative_to(root)
        self.alias = Path('/var') / relative
        self.guard = Directories()
        self.addCleanup(self.guard.close)

    def test_system_alias_pins_same_physical_directory(self):
        a = self.guard.pin(self.alias)
        b = self.guard.pin(self.physical)
        self.assertEqual(identity(os.fstat(a)), identity(os.fstat(b)))
        self.guard.check()

    def test_write_uses_pinned_physical_directory(self):
        owner = Attempt(self.alias, lesson=False)
        try:
            owner.text(self.alias, owner.fd, 'owned.txt', 'own bytes')
            owner.check()
        finally:
            owner.close()
        self.assertEqual((self.physical / 'owned.txt').read_text(), 'own bytes')

    def test_user_symlink_under_alias_is_rejected(self):
        (self.physical / 'link').symlink_to(self.physical, target_is_directory=True)
        with self.assertRaises((ValueError, OSError)):
            self.guard.pin(self.alias / 'link')

    def test_untrusted_system_alias_target_is_refused(self):
        with patch('safe_zip_import.os.readlink', return_value='elsewhere'):
            with self.assertRaisesRegex(ValueError, 'Untrusted'):
                self.guard.pin(self.alias)

    def test_replaced_parent_binding_is_rejected(self):
        folder = self.physical / 'parent'
        folder.mkdir()
        self.guard.pin(self.alias / 'parent')
        folder.rename(self.physical / 'owned-parent')
        folder.symlink_to(self.physical / 'owned-parent', target_is_directory=True)
        with self.assertRaises((ValueError, OSError)):
            self.guard.check()
