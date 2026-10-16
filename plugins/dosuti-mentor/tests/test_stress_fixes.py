"""Regression probes for stress-audit findings. All learner evidence is synthetic."""
import copy,json,hashlib,shutil,sys
from pathlib import Path
from unittest.mock import patch
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import test_unit_learning
import unit_learning as ul
import learning_segments as seg
import source_integrity as si
import adaptive_learning as al

class StressFixes(unittest.TestCase):
    def setUp(self):self.f=test_unit_learning.UnitLearning();self.f.setUp()
    def tearDown(self):self.f.tearDown()
    def filled(self):
        self.f.save(self.f.event(answer='я'*4000,details='д'*1000));d=json.loads(self.f.path.read_text());base=d['events'][0]
        d['events']=[]
        for i in range(85):
            e=copy.deepcopy(base);e['revision']=i+1;e['value'].update(answer='я'*4000,details='д'*1000)
            d['events'].append(e)
        d['revision']=85;self.f.path.write_text(json.dumps(d,ensure_ascii=False,indent=2));return d
    def test_dates_do_not_crash_or_claim_time_verification(self):
        self.f.save(self.f.event(answer='я'*4000,details='д'*1000));original=json.loads(self.f.path.read_text())
        for value in ['9999-12-31T23:59:59Z','0001-01-01T00:00:00+23:00','not-a-date','2099-01-01T00:00:00Z']:
            with self.subTest(value=value):
                d=copy.deepcopy(original);d['events'][0]['at']=value;self.f.path.write_text(json.dumps(d));before=self.f.path.read_bytes()
                x=self.f.read()['learning'];self.assertTrue(x['valid']);self.assertTrue(x['all_criteria_demonstrated'])
                self.assertFalse(x['evidence_time_valid']);self.assertFalse(x['assessment_and_time_verified'])
                self.assertEqual(x['adaptive']['next_actions'][0]['action'],'review_evidence_time');self.assertEqual(before,self.f.path.read_bytes())
    def test_identical_save_is_noop_but_real_change_is_versioned(self):
        before=self.f.f.path.read_bytes();revision=self.f.f.save()['revision']
        self.assertEqual(before,self.f.f.path.read_bytes());self.assertEqual(revision,1)
        self.assertEqual(self.f.f.save(self.f.f.card(title='Changed'))['revision'],2)
    def test_rollover_preserves_every_event_and_global_revision(self):
        d=self.filled();x=self.f.save(self.f.event(answer='я'*4000,details='д'*1000));stored=json.loads(self.f.path.read_text());self.assertEqual(stored['schema'],seg.SCHEMA)
        journal=ul.load(self.f.path);self.assertEqual(journal['events'][:85],d['events']);self.assertEqual(journal['revision'],86)
        self.assertTrue(x['learning']['valid']);self.assertLess(self.f.path.stat().st_size,seg.LIMIT)
        self.f.save(self.f.event(answer='я'*4000,details='д'*1000));self.assertEqual(ul.load(self.f.path)['revision'],87)
    def test_archived_baseline_supports_later_reference(self):
        self.filled();self.f.save(self.f.event(answer='я'*4000,details='д'*1000));d=ul.load(self.f.path);e=copy.deepcopy(d['events'][0])
        # Verify archived references resolve against flattened history, with explicit synthetic times.
        e['revision']=len(d['events'])+1;e['at']='2099-01-03T00:00:00Z';e['value'].update(stage='checked_later',question='Fresh QA question',recheck_of={'1':86})
        al.validate_recheck(e,d['events'])
    def test_segment_damage_blocks_read_and_write_without_overwrite(self):
        self.filled();self.f.save(self.f.event(answer='я'*4000,details='д'*1000));manifest=json.loads(self.f.path.read_text());entry=manifest['segments'][0]
        p=self.f.path.parent/'unit-learning-segments'/(entry['sha256']+'.json');p.write_text('[]');before=self.f.path.read_bytes()
        self.assertFalse(self.f.read()['learning']['valid'])
        with self.assertRaises(ValueError):self.f.save(revision=86)
        self.assertEqual(before,self.f.path.read_bytes())
    def test_manifest_commit_failure_leaves_original_readable(self):
        self.filled();before=self.f.path.read_bytes();original=seg.atomic_json
        def write(path,value):
            if path==self.f.path:raise OSError('QA manifest commit failed')
            return original(path,value)
        with patch.object(seg,'atomic_json',side_effect=write),self.assertRaises(OSError):self.f.save(self.f.event(answer='я'*4000,details='д'*1000))
        self.assertEqual(before,self.f.path.read_bytes());self.assertEqual(ul.load(self.f.path)['revision'],85)
        self.f.save(self.f.event(answer='я'*4000,details='д'*1000));self.assertEqual(ul.load(self.f.path)['revision'],86)
    def test_segments_survive_course_move(self):
        self.filled();self.f.save(self.f.event(answer='я'*4000,details='д'*1000));old=self.f.f.f.path;new=old.with_name('moved');shutil.move(old,new)
        self.assertEqual(ul.load(new/'learners/default/unit-learning.json')['revision'],86)
    def test_archived_event_corruption_not_hidden_by_recomputed_hash(self):
        self.filled();self.f.save(self.f.event(answer='я'*4000,details='д'*1000));manifest=json.loads(self.f.path.read_text());entry=manifest['segments'][0];folder=self.f.path.parent/'unit-learning-segments'
        p=folder/(entry['sha256']+'.json');events=json.loads(p.read_text());events[0]['revision']=999
        raw=seg.encoded(events);entry['sha256']=hashlib.sha256(raw).hexdigest();(folder/(entry['sha256']+'.json')).write_bytes(raw);self.f.path.write_text(json.dumps(manifest))
        self.assertFalse(self.f.read()['learning']['valid'])
    def test_hash_reuse_scoped_to_read_and_detects_changes(self):
        p=self.f.f.text;original=hashlib.file_digest
        @si.verification_session
        def read():return [si.file_hash(p) for _ in range(30)]
        with patch.object(hashlib,'file_digest',wraps=original) as digest:
            self.assertEqual(len(set(read())),1);self.assertEqual(digest.call_count,1)
            read();self.assertEqual(digest.call_count,2)
        @si.verification_session
        def changed():
            si.file_hash(p);p.write_text('Changed during operation')
        with self.assertRaisesRegex(ValueError,'changed'):changed()
        self.assertEqual(read()[0],hashlib.sha256(p.read_bytes()).hexdigest())
    def test_segment_symlink_rejected(self):
        self.filled();self.f.save(self.f.event(answer='я'*4000,details='д'*1000));m=json.loads(self.f.path.read_text());p=self.f.path.parent/'unit-learning-segments'/(m['segments'][0]['sha256']+'.json');target=p.with_suffix('.saved');p.rename(target);p.symlink_to(target)
        self.assertFalse(self.f.read()['learning']['valid'])
    def test_checked_later_can_reference_archived_baseline_after_more_segments(self):
        d=self.filled()
        for event in d['events'][1:]:event['unit']='other'
        # Cross the boundary with an unrelated synthetic event; baseline 1 is archived.
        event=copy.deepcopy(d['events'][-1]);event['revision']=86;d['events'].append(event);d['revision']=86
        seg.write(self.f.path,d)
        manifest=json.loads(self.f.path.read_text());self.assertGreater(manifest['segments'][0]['count'],1)
        self.assertTrue(all(e['revision']!=1 for e in manifest['events']))
        with patch('course.now',return_value='2099-01-03T00:00:00Z'):
            result=self.f.save(self.f.event(stage='checked_later',question='Fresh archived-baseline transfer?',recheck_of={'1':1}))
        self.assertEqual(result['learning']['criteria']['1'],'checked_later')
        d=ul.load(self.f.path)
        for i in range(88,220):
            event=copy.deepcopy(d['events'][1]);event['revision']=i;d['events'].append(event);d['revision']=i;seg.write(self.f.path,d)
        self.assertGreater(len(json.loads(self.f.path.read_text())['segments']),1)
        reread=ul.load(self.f.path);self.assertEqual(reread['revision'],219);self.assertEqual(reread['events'][86]['value']['recheck_of'],{'1':1})
    def test_missing_segment_rejected_and_manifest_preserved(self):
        self.filled();self.f.save(self.f.event(answer='я'*4000,details='д'*1000));m=json.loads(self.f.path.read_text());before=self.f.path.read_bytes()
        (self.f.path.parent/'unit-learning-segments'/(m['segments'][0]['sha256']+'.json')).unlink()
        self.assertFalse(self.f.read()['learning']['valid']);self.assertEqual(before,self.f.path.read_bytes())
