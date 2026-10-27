import json,subprocess,sys,tempfile
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import patch
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import video_sources as v

class DownloadOptions(unittest.TestCase):
    def test_bounds_and_types(self):
        for f,t in [(0,1800),(9,1800),(True,1800),(4,119),(4,7201),(4,True)]:
            with self.subTest(f=f,t=t),self.assertRaises(ValueError):v.site_options(f,t)
        self.assertEqual(v.site_options(8,7200),(8,7200))
    def test_command_limits_progress_and_no_fragment_skipping(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder=Path(tmp)
            def fake(cmd,**kw):
                if '--dump-single-json' in cmd:return NS(stdout='{}')
                self.assertEqual(cmd[cmd.index('--concurrent-fragments')+1],'8');self.assertEqual(kw['timeout'],240)
                self.assertIn('--progress',cmd);self.assertIn('--abort-on-unavailable-fragments',cmd)
                p=folder/'lecture.mp4';p.write_bytes(b'fake');(folder/'completed.json').write_text(json.dumps(str(p)))
                return NS(returncode=0)
            with patch.object(v,'preparation_run',side_effect=fake):self.assertEqual(v.fetch_site('https://example.org/watch',folder,8,240)[0],(folder/'lecture.mp4').resolve())
    def test_classification(self):
        for raw,category in [('Sign in to confirm you are not a bot','access_required'),('HTTP Error 429','access_required'),('Unable to resolve host','network'),('Unsupported URL','unsupported')]:
            self.assertEqual(v.download_failure(subprocess.CalledProcessError(1,['yt-dlp']),raw)[0],category)
        self.assertEqual(v.download_failure(subprocess.TimeoutExpired(['x'],1),'')[0],'timeout')
    def test_download_stderr_is_classified_without_leaking_raw_data(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder=Path(tmp)/'staging';folder.mkdir()
            def fake(cmd,**kw):
                if '--dump-single-json' in cmd:return NS(stdout='{}')
                kw['stderr'].write('ERROR: Sign in; private-token=secret');kw['stderr'].flush()
                raise subprocess.CalledProcessError(1,cmd)
            with patch.object(v,'preparation_run',side_effect=fake),self.assertRaisesRegex(ValueError,'login'):v.fetch_site('https://example.org/watch',folder)
            d=json.loads((folder.parent/'download-status.json').read_text());self.assertEqual(d['category'],'access_required');self.assertNotIn('secret',json.dumps(d))
    def test_timeout_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            f=Path(tmp)/'staging';f.mkdir()
            with patch.object(v,'preparation_run',side_effect=subprocess.TimeoutExpired(['x'],120)),self.assertRaisesRegex(ValueError,'timed out'):v.fetch_site('https://example.org/watch',f)
            self.assertEqual(json.loads((f.parent/'download-status.json').read_text())['category'],'timeout')
