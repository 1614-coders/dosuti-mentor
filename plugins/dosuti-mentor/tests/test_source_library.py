"""Bounded offline navigation, not a semantic or legal validation service."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from zipfile import ZipFile
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import source_library as lib


def metadata(value):
    return '```yaml\n'+'\n'.join(k+': '+json.dumps(v,ensure_ascii=False) for k,v in value.items())+'\n```'

class SourceLibrary(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.path=Path(self.tmp.name)/'библиотека r10.zip'
        self.files={'INDEX.md':'# Образовательная библиотека r10\n[Programming](search/programming/INDEX.md)',
            'search/programming/INDEX.md':'| SQL →[S10-01](../topics/S10-01.md) | SQL, запросы |',
            'maintenance/QUEUE.md':'Snapshot only; no complete freshness guarantee.'}
        self.entries=[]
        for i in range(1,5):
            entry=f'E10-00{i}';source=f'SQL-0{i}'
            self.entries.append(dict(entry_id=entry,source=source,url=f'https://example.org/lesson/{i}',
                title='SQL lesson',language='EN',level='Н',prerequisites='Rows and columns',
                status='checked_text_excerpt',ai_use_mode='reference_link',license_limits='No AI ingestion permission',
                scope='One section only',checked_on='2026-10-30'))
            self.files[f'entries/{entry}.md']=metadata(self.entries[-1])
            self.files[f'domains/programming/{source}.md']=f'<a id="{source.lower()}"></a>\n## {source} — SQL\n'+metadata(dict(id=source,title='SQL',priority='B',ai_use_mode='reference_link',quality_reliability='R1',technology_versions='Unknown'))+'\nЛицензия: неизвестна. Важная оговорка вне YAML.'
        self.table()
        self.seal()
    def table(self):
        self.files['search/topics/S10-01.md']='# SQL\n'+ '\n'.join(f'| [{r["entry_id"]}](../../entries/{r["entry_id"]}.md) · [{r["source"]}](../../domains/programming/{r["source"]}.md#{r["source"].lower()}) | {r["level"]} / {r["language"]} | HTML/text | sample only |' for r in self.entries)
    def seal(self):
        with ZipFile(self.path,'w') as z:
            for p,t in self.files.items():z.writestr('dosuti-source-library/'+p,t)
        digest=hashlib.sha256(self.path.read_bytes()).hexdigest()
        if hasattr(self,'pin'):self.pin.stop()
        self.pin=patch.object(lib,'R10_SHA256',digest);self.pin.start();self.addCleanup(self.pin.stop)
    def find(self,**kw):return lib.search(self.path,direction='programming',topic='S10-01',level='basic',language='EN',**kw)
    def test_missing_configuration_preserves_optional_operation(self):
        with patch.dict(os.environ,{},clear=True):
            self.assertEqual(lib.search()['status'],'not_configured')
    def test_only_confirmed_archive_is_accepted_no_sibling_fallback(self):
        self.path.write_bytes(b'old or corrupt release')
        with self.assertRaisesRegex(ValueError,'r10'):self.find()
    def test_three_distinct_sources_preserve_rights_and_no_fact_verification(self):
        result=self.find();self.assertEqual(len(result['candidates']),3)
        self.assertEqual(len({r['entry']['source'] for r in result['candidates']}),3)
        self.assertFalse(result['fact_verified']);self.assertTrue(result['learner_materials_first'])
        self.assertIn('Важная оговорка',result['candidates'][0]['card_notes'])
        self.assertIn('No AI ingestion',result['candidates'][0]['entry']['license_limits'])
        self.assertLessEqual(result['io']['files'],10)
        self.assertNotIn('SQL-04',str(result['candidates']))
    def test_unknown_topic_cannot_route_outside_direction(self):
        for topic in ['../INDEX','S8-99']:
            with self.subTest(topic=topic),self.assertRaises(ValueError):lib.search(self.path,direction='programming',topic=topic,level='basic',language='EN')
    def test_unknown_level_requires_explicit_calibration_not_easy_default(self):
        with self.assertRaises(ValueError):lib.search(self.path,direction='programming',topic='S10-01',level='unknown',language='EN')
    def test_language_and_level_are_not_inferred_from_request_or_negative_notes(self):
        self.entries[0].update(language='JA+EN; RU отсутствует',level='С')
        self.table();self.seal()
        result=lib.search(self.path,direction='programming',topic='S10-01',level='basic',language='RU')
        self.assertEqual(result['candidates'],[]);self.assertTrue(result['gap'])
    def test_human_only_reserve_and_unconfirmed_are_not_automatic_candidates(self):
        for key,value in [('ai_use_mode','human_link_only'),('status','metadata_only'),('status','unconfirmed')]:
            self.files['entries/E10-001.md']=metadata(self.entries[0]|{key:value});self.seal()
            self.assertNotIn('E10-001',[r['entry']['entry_id'] for r in self.find()['candidates']])
        self.files['entries/E10-001.md']=metadata(self.entries[0]);self.files['domains/programming/SQL-01.md']=metadata(dict(id='SQL-01',priority='C',ai_use_mode='reference_link'));self.seal()
        self.assertNotIn('SQL-01',[r['entry']['source'] for r in self.find()['candidates']])
    def test_changed_file_is_revalidated_in_next_operation(self):
        self.find();self.path.write_bytes(self.path.read_bytes()+b'changed')
        with self.assertRaises(ValueError):self.find()
    def test_local_read_does_not_modify_archive_or_user_course(self):
        before={p:p.read_bytes() for p in Path(self.tmp.name).rglob('*') if p.is_file()}
        self.find();self.assertEqual({p:p.read_bytes() for p in before},before)
    def test_navigation_returns_only_one_direction_not_all_catalog(self):
        result=lib.search(self.path,direction='programming')
        self.assertEqual(result['topics'][0]['id'],'S10-01');self.assertEqual(result['io']['files'],2)
    def test_explicit_entries_must_belong_to_topic(self):
        with self.assertRaises(ValueError):self.find(entries=['E10-099'])
        self.assertEqual([r['entry']['entry_id'] for r in self.find(entries=['E10-003'])['candidates']],['E10-003'])
    def test_budget_stops_without_truncating_rights_or_claiming_completeness(self):
        with patch.object(lib,'MAX_READ_CHARS',500):result=self.find()
        self.assertTrue(result['limited']);self.assertEqual(result['candidates'],[])
    def test_unsafe_metadata_is_not_executed(self):
        self.files['entries/E10-001.md']='```yaml\nentry_id: !!python/object/apply:os.system ["touch BAD"]\n```';self.seal()
        with self.assertRaises(ValueError):self.find()
        self.assertFalse(Path('BAD').exists())
    def test_wrong_source_binding_is_rejected(self):
        self.files['entries/E10-001.md']=metadata(self.entries[0]|dict(source='SQL-99'));self.seal()
        with self.assertRaises(ValueError):self.find()
    def test_grouped_card_keeps_targeted_r10_footnote_outside_its_anchor(self):
        self.files['domains/programming/SQL-01.md']+='\n<a id="sql-99"></a>\n'+metadata(dict(id='SQL-99',priority='B'))+'\nSibling detail not needed.\n## r10 — SQL-01: correction\nIMPORTANT RIGHTS CORRECTION\n'
        self.seal();result=self.find();notes=result['candidates'][0]['card_notes']
        self.assertIn('IMPORTANT RIGHTS CORRECTION',notes)
        self.assertNotIn('Sibling detail',notes)
    def test_card_human_only_overrides_entry_reference_mode(self):
        self.files['domains/programming/SQL-01.md']=metadata(dict(id='SQL-01',priority='A',ai_use_mode='human_link_only'));self.seal()
        self.assertNotIn('SQL-01',[r['entry']['source'] for r in self.find()['candidates']])
    def test_selected_maintenance_rows_do_not_hide_global_caveats(self):
        self.files['maintenance/QUEUE.md']='Global caveat: snapshot not complete.\n| SQL-01 | rights review |\n| SQL-99 | unrelated |'
        self.seal();result=self.find();queue=result['maintenance']['queue_excerpt']
        self.assertIn('Global caveat',queue);self.assertIn('SQL-01',queue);self.assertNotIn('SQL-99',queue)
    def test_oversized_navigation_context_fails_without_truncation(self):
        with patch.object(lib,'MAX_CONTEXT_BYTES',100),self.assertRaisesRegex(ValueError,'context budget'):
            lib.search(self.path,direction='programming')
    def test_growth_after_stat_is_rejected_without_hashing_unbounded_input(self):
        original=Path.open;before=self.path.read_bytes();reads=[]
        class Counted:
            def __init__(self,file):self.file=file
            def __getattr__(self,key):return getattr(self.file,key)
            def __enter__(self):return self
            def __exit__(self,*args):return self.file.__exit__(*args)
            def read(self,n=-1):
                value=self.file.read(n);reads.append(len(value));return value
            def readinto(self,buffer):
                n=self.file.readinto(buffer);reads.append(n);return n
        def opened(path,*args,**kwargs):
            if path==self.path and args and args[0]=='rb':
                path.write_bytes(before+b'x'*(5*1024*1024))
                return Counted(original(path,*args,**kwargs))
            return original(path,*args,**kwargs)
        with patch.object(Path,'open',opened),self.assertRaises(ValueError):self.find()
        self.assertLessEqual(sum(reads),5*1024*1024+1)
    def test_topic_requires_direction_and_entries_require_topic(self):
        for kw in [dict(topic='S10-01',level='basic',language='EN'),dict(direction='programming',entries=['E10-001'])]:
            with self.subTest(kw=kw),self.assertRaises(ValueError):lib.search(self.path,**kw)
    def test_duplicate_zip_members_are_rejected(self):
        import warnings
        with warnings.catch_warnings():
            warnings.simplefilter('ignore',UserWarning)
            with ZipFile(self.path,'a') as z:z.writestr('dosuti-source-library/INDEX.md','duplicate')
        with patch.object(lib,'R10_SHA256',hashlib.sha256(self.path.read_bytes()).hexdigest()),self.assertRaisesRegex(ValueError,'archive bounds'):self.find()
    def test_archive_links_cannot_escape_or_access_host_paths(self):
        for path in ['../../../outside.md','/tmp/file','C:/file','\\server\\file']:
            with self.subTest(path=path),self.assertRaises(ValueError):lib.relative('search/topics/S10-01.md',path)
    def test_cli_bad_archive_is_controlled_without_traceback(self):
        result=subprocess.run([sys.executable,'-B',lib.__file__,'--library',str(self.path)],capture_output=True,text=True,timeout=10)
        self.assertEqual(result.returncode,2);self.assertNotIn('Traceback',result.stderr)
        self.assertIn('r10',result.stderr);self.assertEqual(result.stdout,'')
    def test_cli_without_library_needs_no_course_python_packages_or_network(self):
        env=dict(os.environ);env.pop('DOSUTI_SOURCE_LIBRARY',None)
        r=subprocess.run([sys.executable,'-B',lib.__file__],capture_output=True,text=True,env=env,timeout=10)
        self.assertEqual(r.returncode,0,r.stderr);self.assertEqual(json.loads(r.stdout)['status'],'not_configured')

if __name__=='__main__':unittest.main()
