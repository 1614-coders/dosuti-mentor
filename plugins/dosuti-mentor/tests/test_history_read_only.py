"""History reads preserve files and legacy schemas; writers migrate explicitly."""
import contextlib
import io
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import unittest
import test_persistence
import course
import managed_storage


class HistoryReadOnly(unittest.TestCase):
    def setUp(self):
        self.f = test_persistence.PersistenceRegression(); self.f.setUp()
        self.addCleanup(self.f.tearDown)
        self.path = self.f.path / 'learners/default/progress.sqlite3'

    def files(self, root=None):
        root = root or self.f.path
        return {str(p.relative_to(root)): ((p.stat().st_mtime_ns, p.read_bytes()) if p.is_file() else 'directory')
                for p in root.rglob('*')}

    def read(self, limit=100):
        return self.f.capture(course.history, limit=limit)

    def legacy(self, count=1):
        self.path.parent.mkdir(parents=True)
        with contextlib.closing(sqlite3.connect(self.path)) as db:
            db.execute('CREATE TABLE events(id INTEGER PRIMARY KEY, lesson TEXT, topic TEXT, stage TEXT, details TEXT, created TEXT)')
            db.executemany('INSERT INTO events VALUES (?,?,?,?,?,?)',
                [(i, 'lesson-01', 'Topic', 'explained', str(i), 'old') for i in range(1,count+1)])
            db.execute("INSERT INTO events VALUES (?, 'other', 'Topic', 'explained', 'other', 'old')", (count+1,))
            db.commit()

    def test_missing_database_and_profile_are_not_created(self):
        before = self.files()
        for _ in range(2):
            self.assertEqual(self.read()['events'], [])
        self.assertEqual(self.files(), before)
        self.assertFalse(self.path.parent.exists())

    def test_legacy_history_limit_order_and_schema_are_preserved(self):
        self.legacy(120)
        before = self.files()
        for _ in range(2):
            events = self.read(limit=110)['events']
            self.assertEqual([e['id'] for e in events], list(range(120,10,-1)))
            self.assertTrue(all(e['homework_revision'] is None for e in events))
        self.assertEqual(self.files(), before)

    def test_empty_database_returns_empty_without_creating_tables(self):
        self.path.parent.mkdir(parents=True)
        with contextlib.closing(sqlite3.connect(self.path)): pass
        before = self.files()
        self.assertEqual(self.read()['events'], [])
        self.assertEqual(self.files(), before)

    def test_corrupt_database_and_schema_fail_without_writes(self):
        self.path.parent.mkdir(parents=True)
        for invalid in (b'not sqlite', None):
            with self.subTest(invalid=invalid):
                self.path.unlink(missing_ok=True)
                if invalid is not None: self.path.write_bytes(invalid)
                else:
                    with contextlib.closing(sqlite3.connect(self.path)) as db:
                        db.execute('CREATE TABLE events(unexpected TEXT)'); db.commit()
                before = self.files()
                for _ in range(2):
                    with self.assertRaisesRegex(ValueError, 'Cannot read lesson progress'):
                        self.read()
                self.assertEqual(self.files(), before)

    def test_wal_is_refused_without_checkpoint_or_sidecar_writes(self):
        self.legacy()
        with contextlib.closing(sqlite3.connect(self.path)) as db:
            db.execute('PRAGMA journal_mode=WAL')
            before = self.files()
            with self.assertRaisesRegex(ValueError, 'WAL'): self.read()
            self.assertEqual(self.files(), before)

    def test_writer_migrates_legacy_schema_preserving_history(self):
        self.legacy()
        before = self.files()
        self.read()
        self.assertEqual(self.files(), before)
        with contextlib.closing(course.db(self.f.args())) as db:
            self.assertIn('homework_revision', [r['name'] for r in db.execute('PRAGMA table_info(events)')])
        self.assertEqual(self.read()['events'][0]['details'], '1')

    def test_cli_read_under_managed_home_creates_no_runtime_files(self):
        home = managed_storage.initialize(self.f.directory.resolve() / 'managed')
        root = home / 'course'
        with contextlib.redirect_stdout(io.StringIO()):
            course.init(self.f.args(course=root)); course.add_lesson(self.f.args(course=root))
        before = self.files(home)
        command = [sys.executable, '-B', str(course.SCRIPT_DIR / 'course.py'), 'history',
                   '--course', str(root), '--lesson', 'lesson-01']
        for _ in range(2):
            result = subprocess.run(command, capture_output=True, text=True, timeout=15)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout)['events'], [])
        self.assertEqual(self.files(home), before)
        (home / '.cleanup-pending.json').write_text(json.dumps(dict(seal='test',owner='test')))
        before = self.files(home)
        result = subprocess.run(command, capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 1)
        self.assertIn('cleanup marker', result.stderr)
        self.assertEqual(self.files(home), before)

    def test_invalid_limit_does_not_open_or_create_database(self):
        before = self.files()
        for limit in (0,1001):
            with self.assertRaises(ValueError): self.read(limit)
        self.assertEqual(self.files(), before)

    def test_reader_preserves_uncommitted_writer_and_sees_commit_later(self):
        self.legacy()
        with contextlib.closing(sqlite3.connect(self.path)) as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute("INSERT INTO events VALUES (3,'lesson-01','Topic','explained','new','now')")
            before = self.files()
            self.assertEqual([r['id'] for r in self.read()['events']], [1])
            self.assertEqual(self.files(), before)
            db.commit()
        self.assertEqual([r['id'] for r in self.read()['events']], [3,1])

    def test_corrupt_cli_returns_controlled_error_without_writes(self):
        self.path.parent.mkdir(parents=True)
        self.path.write_bytes(b'corrupt')
        before = self.files()
        result = subprocess.run([sys.executable, '-B', str(course.SCRIPT_DIR / 'course.py'),
            'history', '--course', str(self.f.path), '--lesson', 'lesson-01'],
            capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 1)
        self.assertIn('Cannot read lesson progress', result.stderr)
        self.assertNotIn('Traceback', result.stderr)
        self.assertEqual(self.files(), before)
