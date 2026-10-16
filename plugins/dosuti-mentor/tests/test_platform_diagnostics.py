import sys
from pathlib import Path
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import maintenance
import screen_ocr


class PlatformDiagnostics(unittest.TestCase):
    def test_windows_and_linux_do_not_probe_swift(self):
        for system in ('Windows', 'Linux'):
            with self.subTest(system=system), patch('maintenance.platform.system', return_value=system), patch.object(Path, 'is_file', side_effect=AssertionError('No Swift probe')):
                result = maintenance.ocr_capability()
            self.assertEqual(result['status'], 'unsupported_platform')
            self.assertFalse(result['blocks_teaching'])
            self.assertFalse(result['execution_verified'])

    def test_mac_prerequisites_do_not_claim_inference(self):
        for present, status in ((True, 'prerequisites_present'), (False, 'missing_swift')):
            with patch('maintenance.platform.system', return_value='Darwin'), patch.object(Path, 'is_file', return_value=present):
                result = maintenance.ocr_capability()
            self.assertEqual(result['status'], status)
            self.assertFalse(result['execution_verified'])

    def test_windows_ocr_refuses_before_process_or_image_work(self):
        with patch('screen_ocr.platform.system', return_value='Windows'), patch('screen_ocr.subprocess.run', side_effect=AssertionError('No process')):
            with self.assertRaisesRegex(RuntimeError, 'macOS'):
                screen_ocr.recognize(b'not-an-image', None)
