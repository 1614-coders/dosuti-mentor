"""History data can be consumed without invoking CLI serialization or writes."""
import contextlib
import io
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import unittest
from unittest.mock import patch
import test_persistence
import course


class HistoryCLIBoundary(unittest.TestCase):
    def setUp(self):
        self.f=test_persistence.PersistenceRegression();self.f.setUp();self.addCleanup(self.f.tearDown)
        self.path=self.f.path/'learners/default/progress.sqlite3'

    def files(self):
        return {str(p.relative_to(self.f.path)):(p.stat().st_mtime_ns,p.read_bytes())
                for p in self.f.path.rglob('*') if p.is_file()}

    def seed(self,modern=False):
        self.path.parent.mkdir(parents=True)
        extra=', homework_revision TEXT' if modern else ''
        with contextlib.closing(sqlite3.connect(self.path)) as db:
            db.execute('CREATE TABLE events(id INTEGER PRIMARY KEY, lesson TEXT, topic TEXT, stage TEXT, details TEXT, created TEXT'+extra+')')
            db.execute('CREATE TABLE progress(lesson TEXT, topic TEXT, stage TEXT, details TEXT, updated TEXT'+extra+')')
            for n in range(1,4):
                values=(n,'lesson-01','Ключи','explained','Пример '+str(n),'old')+((None if n==1 else 'revision-'+str(n),) if modern else ())
                db.execute('INSERT INTO events VALUES ('+','.join('?'*len(values))+')',values)
            db.execute("INSERT INTO events VALUES (9,'other','Other','explained','not this lesson','old'"+(",NULL" if modern else '')+")")
            db.execute("INSERT INTO progress VALUES ('lesson-01','Ключи','explained','Пример','old'"+(",'hw-current'" if modern else '')+")")
            db.commit()

    def cli(self,*extra):
        return subprocess.run([sys.executable,'-B',str(course.SCRIPT_DIR/'course.py'),'history',
            '--course',str(self.f.path),'--lesson','lesson-01',*extra],capture_output=True,text=True,encoding='utf-8',timeout=15)

    def test_data_api_is_silent_and_missing_database_is_not_created(self):
        before=self.files();output=io.StringIO()
        with contextlib.redirect_stdout(output),contextlib.redirect_stderr(output):
            result=course.history_data(self.f.args(limit=100))
        self.assertEqual(result,dict(lesson='lesson-01',profile='default',events=[]))
        self.assertEqual(output.getvalue(),'')
        self.assertEqual(self.files(),before)
        self.assertFalse(self.path.parent.exists())

    def test_legacy_cli_exact_json_order_unicode_and_export_return_are_preserved(self):
        self.seed();before=self.files();output=io.StringIO()
        expected=dict(lesson='lesson-01',profile='default',events=[
            dict(homework_revision=None,id=n,lesson='lesson-01',topic='Ключи',stage='explained',details='Пример '+str(n),created='old')
            for n in (3,2)])
        with contextlib.redirect_stdout(output):result=course.history(self.f.args(limit=2))
        self.assertIsNone(result)
        self.assertEqual(output.getvalue(),json.dumps(expected,ensure_ascii=False,indent=2)+'\n')
        process=self.cli('--limit','2')
        self.assertEqual((process.returncode,process.stderr),(0,''))
        self.assertEqual(process.stdout,output.getvalue())
        self.assertEqual(self.files(),before)

    def test_modern_rows_preserve_homework_revision_and_column_order(self):
        self.seed(modern=True);before=self.files()
        result=self.cli('--limit','3');self.assertEqual(result.returncode,0,result.stderr)
        rows=json.loads(result.stdout)['events']
        self.assertEqual([r['homework_revision'] for r in rows],['revision-3','revision-2',None])
        self.assertEqual(list(rows[0]),['id','lesson','topic','stage','details','created','homework_revision'])
        self.assertEqual(course.read_progress(self.f.args())['progress'][0]['homework_revision'],'hw-current')
        self.assertEqual(self.files(),before)

    def test_legacy_progress_uses_the_same_compatibility_rule_without_migration(self):
        self.seed();before=self.files()
        value=course.read_progress(self.f.args())['progress'][0]
        self.assertEqual(value,dict(homework_revision=None,lesson='lesson-01',topic='Ключи',stage='explained',details='Пример',updated='old'))
        self.assertEqual(list(value)[0],'homework_revision')
        self.assertEqual(self.files(),before)

    def test_main_keeps_history_outside_managed_session_and_writer_initialization(self):
        argv=['course.py','history','--course',str(self.f.path),'--lesson','lesson-01']
        output=io.StringIO()
        with patch.object(sys,'argv',argv),patch.object(course,'find_home') as home,\
                patch.object(course,'session',side_effect=AssertionError('history entered a managed session')),\
                patch.object(course,'db',side_effect=AssertionError('history initialized the writer')),\
                contextlib.redirect_stdout(output):
            self.assertIsNone(course.main())
        home.assert_called_once_with(self.f.path)
        self.assertEqual(json.loads(output.getvalue())['events'],[])

    def test_history_wrapper_uses_data_api_without_reimplementing_read_logic(self):
        value=dict(lesson='lesson-01',profile='default',events=[dict(details='Факт')])
        args=self.f.args(limit=100);output=io.StringIO()
        with patch.object(course,'history_data',return_value=value) as data,contextlib.redirect_stdout(output):
            self.assertIsNone(course.history(args))
        data.assert_called_once_with(args)
        self.assertEqual(json.loads(output.getvalue()),value)

    def test_cli_invalid_limit_and_parse_errors_keep_distinct_exit_codes(self):
        before=self.files()
        result=self.cli('--limit','0')
        self.assertEqual(result.returncode,1)
        self.assertEqual(result.stdout,'')
        self.assertEqual(result.stderr,'Error: History limit must be between 1 and 1000\n')
        for extra in (['--limit','not-an-int'],['--unknown'],['--compact']):
            with self.subTest(extra=extra):
                result=self.cli(*extra)
                self.assertEqual(result.returncode,2)
                self.assertEqual(result.stdout,'')
                self.assertIn('error:',result.stderr)
                self.assertNotIn('Traceback',result.stderr)
        self.assertEqual(self.files(),before)

    def test_main_controlled_errors_and_interrupt_exit_codes_are_preserved(self):
        argv=['course.py','history','--course',str(self.f.path),'--lesson','lesson-01']
        cases=[(ValueError('invalid'),1,'Error: invalid\n'),(OSError('unreadable'),1,'Error: unreadable\n'),
               (KeyboardInterrupt('cancelled'),130,'cancelled\n'),
               (KeyboardInterrupt(),130,'Подготовка прервана; готовые этапы сохранены.\n')]
        for error,code,message in cases:
            with self.subTest(error=repr(error)):
                output=io.StringIO();errors=io.StringIO()
                with patch.object(sys,'argv',argv),patch.object(course,'find_home'),\
                        patch.object(course,'history',side_effect=error),\
                        contextlib.redirect_stdout(output),contextlib.redirect_stderr(errors),self.assertRaises(SystemExit) as raised:
                    course.main()
                self.assertEqual(raised.exception.code,code)
                self.assertEqual(output.getvalue(),'')
                self.assertEqual(errors.getvalue(),message)
