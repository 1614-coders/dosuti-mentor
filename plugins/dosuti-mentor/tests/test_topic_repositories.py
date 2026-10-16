"""Named code references must invalidate only their dependants, without losing progress."""
import contextlib
import io
import shutil
import sys
import unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import course
import learning_tools as lt
import test_learning_tools


class TopicRepositories(unittest.TestCase):
    def setUp(self):
        self.f = test_learning_tools.LearningTools(); self.f.setUp()
        self.attach(None, 'class A {}'); self.attach('streams', 'class B {}')

    def tearDown(self): self.f.tearDown()

    def data(self): return course.read(self.f.fixture.lesson / 'lesson.json')

    def attach(self, name, content):
        path = self.f.base / 'A.java'; path.write_text(content)
        with contextlib.redirect_stdout(io.StringIO()):
            course.attach(self.f.args(kind='code', source=str(path), repository=name, none=False, ref=None))

    def add(self, refs=None, **kw):
        return self.f.add_topic(self.f.card() | {'code_references': refs if refs is not None else [dict(repository='streams', files=['A.java'])]} | kw)

    def save(self):
        return self.f.save_cursor(value=self.f.state() | {'dependency_topics': ['maps']})

    def valid(self): return lt.cursor(self.f.a)['valid']

    def test_named_reference_pins_snapshot_and_search_returns_named_path(self):
        row = self.add()['topics'][0]
        self.assertFalse(row['stale'])
        self.assertEqual(row['code_references'][0]['code_sha256'], self.data()['code_repositories']['streams']['content_sha256'])
        hit = lt.search(self.f.args(query='словарь', limit=10))['results'][0]
        self.assertEqual(hit['code_snapshots']['streams'], self.data()['code_repositories']['streams']['snapshot'])

    def test_primary_update_preserves_named_only_topic_and_cursor(self):
        self.add(); self.save(); self.attach(None, 'class Changed {}')
        self.assertTrue(self.valid())

    def test_named_update_invalidates_and_rejects_stale_cursor_save(self):
        self.add(); self.save(); self.attach('streams', 'class Changed {}')
        self.assertFalse(self.valid())
        self.assertTrue(lt.topic(self.f.args(topic_file=None))['topics'][0]['stale'])
        self.assertFalse(lt.search(self.f.args(query='словарь', limit=10))['results'])
        with self.assertRaisesRegex(ValueError, 'snapshot changed'):
            self.f.save_cursor(revision=1, value=self.f.state() | {'dependency_topics': ['maps']})
        with course.db(self.f.a) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM progress').fetchone()[0], 0)
        self.attach('streams', 'class B {}'); self.assertTrue(self.valid())

    def test_multiple_repositories_all_bound(self):
        self.add([dict(repository=n, files=['A.java']) for n in ['default', 'streams']]); self.save()
        self.attach(None, 'class Changed {}'); self.assertFalse(self.valid())

    def test_legacy_topic_and_cursor_ignore_secondary_update(self):
        self.f.add_topic(self.f.card() | {'code_files': ['A.java']}); self.save()
        self.attach('streams', 'class Changed {}'); self.assertTrue(self.valid())
        self.attach(None, 'class Changed {}'); self.assertFalse(self.valid())

    def test_invalid_references_do_not_modify_lesson(self):
        before = (self.f.fixture.lesson / 'lesson.json').read_bytes()
        refs = [dict(repository='streams', files=['A.java'])]
        for value in [[], None, 'streams', [None], [dict(repository='missing', files=['A.java'])],
                      [dict(repository='streams', files=['missing'])], refs * 2,
                      [dict(repository='../escape', files=['A.java'])],
                      [dict(repository='streams', files=[])],
                      [dict(repository='streams', files=['A.java'], code_sha256='forged')]]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.f.add_topic(self.f.card() | {'code_references': value})
            self.assertEqual((self.f.fixture.lesson / 'lesson.json').read_bytes(), before)

    def test_corrupt_named_snapshot_invalidates(self):
        self.add(); self.save()
        path = Path(self.data()['code_repositories']['streams']['snapshot']) / 'A.java'
        path.chmod(0o600); path.write_text('tampered')
        self.assertFalse(self.valid())

    def test_removed_named_snapshot_invalidates(self):
        self.add(); self.save()
        with contextlib.redirect_stdout(io.StringIO()):
            course.attach(self.f.args(kind='code', source=None, repository='streams', none=True, ref=None))
        self.assertFalse(self.valid())

    def test_relocation_keeps_named_topic_and_cursor_valid(self):
        self.add(); self.save(); moved = self.f.base / 'moved'
        shutil.move(self.f.a.course, moved); self.f.a.course = moved
        self.assertTrue(self.valid())

    def note(self, repository='streams'):
        code = self.data()['code_repositories']['streams']
        value = dict(range=[5, 15], source_kind='attached_code', source_sha256=code['content_sha256'], verification='confirmed')
        if repository is not None: value['source_repository'] = repository
        course.save(self.f.fragment / 'visual_notes.json', dict(observations=[value], uncertainties=[]))

    def read_note(self):
        return self.f.fixture.context(5, 10)['visual_notes']['observations'][0]

    def test_named_note_current_then_historical(self):
        self.note(); self.assertTrue(self.read_note()['usable_as_current_evidence'])
        self.attach(None, 'class Changed {}'); self.assertFalse(self.read_note()['stale'])
        self.attach('streams', 'class Changed {}')
        row = self.read_note(); self.assertEqual(row['source_status'], 'historical')
        self.assertEqual(row['verification'], 'requires_review')

    def test_note_never_infers_repository_from_hash(self):
        for repository in [None, 'missing', '../escape', []]:
            self.note(repository); self.assertEqual(self.read_note()['source_status'], 'unverifiable')

    def test_repository_identity_change_invalidates_cursor_even_if_bytes_equal(self):
        self.attach('other', 'class B {}'); self.add(); self.save()
        data = self.data(); data['topics']['maps']['code_references'][0]['repository'] = 'other'
        course.save(self.f.fixture.lesson / 'lesson.json', data)
        self.assertFalse(self.valid())
