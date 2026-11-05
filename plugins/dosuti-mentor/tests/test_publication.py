"""Malformed publication markers are rejected without editing published data."""
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import publication
import test_context
from atomic_files import atomic_json
from source_integrity import file_hash


class PublicationValidation(unittest.TestCase):
    def setUp(self):
        self.fixture = test_context.ContextRegression()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.working = self.fixture.add('working', 0, 60, [(1, 2, 'Example')])
        (self.working / 'audio.wav').write_bytes(b'fixture audio')
        manifest = json.loads((self.working / 'manifest.json').read_text())
        manifest['audio_sha256'] = file_hash(self.working / 'audio.wav')
        atomic_json(self.working / 'manifest.json', manifest)
        atomic_json(self.working / 'checkpoint.json', dict(status='complete',
            stages=dict(packet=dict(sha256=file_hash(self.working / 'tutor_packet.json')))))
        self.key = 'test-job'
        self.checksum = file_hash(self.fixture.video)
        self.prepared = self.fixture.directory / 'publications'
        self.published = publication.publish_job(self.working, self.prepared, self.key)
        self.marker = self.published / 'publication.json'

    def valid(self):
        return publication.valid_publication(self.published, self.key, self.checksum)

    def snapshot(self):
        return {str(p.relative_to(self.published)): p.read_bytes()
                for p in self.published.rglob('*') if p.is_file()}

    def assert_bad_checkpoint_preserved(self, raw):
        checkpoint = self.working / 'checkpoint.json'
        checkpoint.write_bytes(raw)
        def inventory():
            return {str(p.relative_to(self.fixture.directory)): p.read_bytes()
                    for p in self.fixture.directory.rglob('*') if p.is_file()}
        before = inventory()
        with patch.object(publication, 'build_packet') as build:
            with self.assertRaisesRegex(ValueError, 'checkpoint.json'):
                publication.publish_job(self.working, self.prepared, self.key,
                                        preferred=self.published)
            build.assert_not_called()
        self.assertEqual(inventory(), before)

    def test_invalid_checkpoint_types_and_nested_structure_are_preserved(self):
        checksum = file_hash(self.working / 'tutor_packet.json')
        cases = [[], None, 'text', 42, True, {},
                 {'status': [], 'stages': {}}, {'status': 'complete'}]
        cases += [dict(status='complete', stages=value) for value in (None, [], 'text', 1, True, {})]
        cases += [dict(status='complete', stages=dict(packet=value))
                  for value in (None, [], 'text', 1, True, {}, {'sha256': []},
                                {'sha256': None}, {'sha256': 'bad'})]
        cases.append(dict(status=False, stages=dict(packet=dict(sha256=checksum))))
        for value in cases:
            with self.subTest(value=value):
                self.assert_bad_checkpoint_preserved(json.dumps(value).encode())

    def test_malformed_checkpoint_json_is_diagnosed_and_preserved(self):
        for raw in (b'{', b'', b'\xff'):
            with self.subTest(raw=raw):
                self.assert_bad_checkpoint_preserved(raw)

    def test_array_marker_is_rejected_without_modifying_files(self):
        self.marker.write_bytes(b'[]')
        before = self.snapshot()
        self.assertFalse(self.valid())
        self.assertEqual(self.snapshot(), before)

    def test_invalid_marker_types_fields_and_json_are_rejected(self):
        good = json.loads(self.marker.read_text())
        cases = [None, 'text', 42, True, {}, {'job_key': self.key},
                 {'packet_sha256': good['packet_sha256']},
                 dict(good, job_key=[]), dict(good, packet_sha256=[]),
                 dict(good, packet_sha256='not a hash')]
        for value in cases:
            with self.subTest(value=value):
                raw = json.dumps(value).encode()
                self.marker.write_bytes(raw)
                self.assertFalse(self.valid())
                self.assertEqual(self.marker.read_bytes(), raw)
        for raw in (b'{', b'', b'\xff'):
            with self.subTest(raw=raw):
                self.marker.write_bytes(raw)
                self.assertFalse(self.valid())
                self.assertEqual(self.marker.read_bytes(), raw)

    def test_missing_marker_is_not_created(self):
        self.marker.unlink()
        self.assertFalse(self.valid())
        self.assertFalse(self.marker.exists())

    def test_actual_replacement_bytes_are_checked_on_each_call(self):
        good = self.marker.read_bytes()
        original = Path.read_text
        replaced = []
        def read(path, *args, **kwargs):
            if path == self.marker and not replaced:
                temporary = path.with_suffix('.replacement')
                temporary.write_bytes(b'[]'); temporary.replace(path)
                replaced.append(True)
            return original(path, *args, **kwargs)
        with patch.object(Path, 'read_text', read):
            self.assertFalse(self.valid())
        self.assertEqual(replaced, [True])
        self.assertEqual(self.marker.read_bytes(), b'[]')
        self.marker.write_bytes(good)
        self.assertTrue(self.valid())
        self.marker.write_bytes(b'null')
        self.assertFalse(self.valid())

    def test_invalid_manifest_is_rejected_before_using_fields(self):
        path = self.published / 'manifest.json'
        for value in ([], None, 'text', {}, {'frames': [None]}):
            with self.subTest(value=value):
                raw = json.dumps(value).encode(); path.write_bytes(raw)
                self.assertFalse(self.valid())
                self.assertEqual(path.read_bytes(), raw)

    def test_valid_publication_is_reused_without_editing_files(self):
        before = self.snapshot()
        self.assertTrue(self.valid())
        self.assertEqual(publication.publish_job(self.working, self.prepared,
                         self.key, preferred=self.published), self.published)
        self.assertEqual(self.snapshot(), before)

    def test_corrupt_marker_is_preserved_when_fresh_publication_is_built(self):
        self.marker.write_bytes(b'[]'); before = self.snapshot()
        fresh = publication.publish_job(self.working, self.prepared,
                                        self.key, preferred=self.published)
        self.assertNotEqual(fresh, self.published)
        self.assertEqual(self.snapshot(), before)
        self.assertTrue(publication.valid_publication(fresh, self.key, self.checksum))
        self.assertFalse(list(self.prepared.glob('.pending-*')))


if __name__ == '__main__': unittest.main()
