"""URL bootstrap shares a pinned lesson asset with subsequent teaching."""
import contextlib
import io
import json
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import pilot_setup as setup
import course
import video_sources
import test_pilot_setup as fixture


class URLStart(unittest.TestCase):
    def setUp(self):
        self.f=fixture.Setup();self.f.setUp();self.addCleanup(self.f.tmp.cleanup)
        self.f.args.video='https://example.invalid/lecture.mp4?part=1&token=fixture'
        original=self.f.fake
        def worker(command,timeout,**kw):
            result=original(command,timeout,**kw)
            if '--worker' in command and command[command.index('--worker')+1]=='video':
                setup.video_worker(Path(command[command.index('--root')+1]),command[command.index('--video')+1],
                    Path(command[command.index('--output')+1]),'--offline' in command)
            return result
        self.f.fake=worker
        self.valid=patch.object(video_sources,'validate_media');self.valid.start();self.addCleanup(self.valid.stop)
        def fetched(source,folder,*args):
            p=folder/'lecture.mp4';p.write_bytes(b'fixture-video');return p,'fixture'
        self.fetch=patch.object(video_sources,'fetch_direct',side_effect=fetched).start()
        self.addCleanup(patch.stopall)
    def read_source(self):
        number=len(setup.read(self.f.root)['attempts'])
        return json.loads((self.f.root/f'attempt-{number:02d}-source.json').read_text())
    def test_url_bootstrap_pins_asset_and_repeat_has_no_second_download(self):
        first=self.f.prepare();source=self.read_source();path=Path(source['path'])
        data=course.read(Path(source['course'])/'lessons'/source['lesson']/'lesson.json')
        self.assertEqual(data['video']['source'],self.f.args.video)
        self.assertEqual(data['video']['sha256'],source['sha256'])
        from preparation_status import describe
        status=describe(Path(source['course'])/'lessons'/source['lesson'])
        self.assertEqual(status['stage'],'video_ready')
        self.assertFalse(status['teaching_ready'])
        self.assertTrue(path.is_relative_to(self.f.root/'run/course'))
        self.f.prepare();again=self.read_source()
        self.assertEqual(again,source);self.assertEqual(self.fetch.call_count,1)
        self.assertEqual(len(list((self.f.root/'run/course/lessons').iterdir())),1)
        smoke=next(c for c in self.f.commands if any(str(a).endswith('lecture.py') for a in c))
        self.assertIn(str(path),smoke)
        self.assertTrue(Path(first['report']).is_file())
    def test_offline_reuses_verified_cache_and_never_fetches(self):
        self.f.prepare();source=self.read_source()
        exe=self.f.root/'runtime/venv'/('Scripts/python.exe' if os.name=='nt' else 'bin/python')
        exe.parent.mkdir(parents=True);exe.write_text('fixture')
        self.f.args.offline=True
        self.fetch.side_effect=AssertionError('offline network')
        self.f.prepare();self.assertEqual(self.read_source(),source)
    def test_offline_missing_cache_is_reported_without_network(self):
        self.f.prepare();Path(self.read_source()['path']).unlink()
        exe=self.f.root/'runtime/venv'/('Scripts/python.exe' if os.name=='nt' else 'bin/python')
        exe.parent.mkdir(parents=True);exe.write_text('fixture')
        self.f.args.offline=True;self.fetch.side_effect=AssertionError('offline network')
        with self.assertRaisesRegex(RuntimeError,'Offline video cache'):self.f.prepare()
        self.assertEqual(setup.read(self.f.root)['attempts'][-1]['status'],'failed')
    def test_download_failure_is_early_report_and_retry_keeps_history(self):
        self.fetch.side_effect=ValueError('download unavailable')
        with self.assertRaisesRegex(RuntimeError,'partial report'):self.f.prepare()
        self.assertIn('download unavailable',(self.f.root/'SETUP_REPORT.md').read_text())
        self.assertEqual(setup.read(self.f.root)['attempts'][0]['events'][-1]['step'],'video')
        def recovered(source,folder,*args):
            p=folder/'lecture.mp4';p.write_bytes(b'fixture-video');return p,'fixture'
        self.fetch.side_effect=recovered;self.f.prepare()
        self.assertEqual([a['status'] for a in setup.read(self.f.root)['attempts']],['failed','passed'])
    def test_changed_cached_asset_refused_without_resetting_pin(self):
        self.f.prepare();source=self.read_source();path=Path(source['path']);path.write_bytes(b'changed')
        with self.assertRaisesRegex(RuntimeError,'integrity changed'):self.f.prepare()
        lesson=course.read(Path(source['course'])/'lessons'/source['lesson']/'lesson.json')
        self.assertEqual(lesson['video']['sha256'],source['sha256'])
        self.assertEqual(self.fetch.call_count,1)
    def test_yandex_and_site_routes_share_same_registration_contract(self):
        for url,adapter in [('https://disk.yandex.ru/i/fixture','fetch_yandex'),('https://www.youtube.com/watch?v=fixture','fetch_site')]:
            self.f.args.video=url
            def fetched(source,folder,*args):
                p=folder/'lecture.mp4';p.write_bytes(b'fixture-video');return p,'fixture'
            with patch.object(video_sources,adapter,side_effect=fetched) as fetch:
                self.f.prepare();self.assertEqual(fetch.call_count,1)
                self.assertTrue(Path(self.read_source()['path']).exists())
