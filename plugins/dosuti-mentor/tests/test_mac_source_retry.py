"""Endpoint proofs only authorize a fresh attempt, never a failed attempt's output."""
import hashlib
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
from types import SimpleNamespace as NS
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import mac_source_retry as guard
import processing_job as jobs
import source_integrity as integrity
import test_recovery as recovery
import tutor_packet
from atomic_files import atomic_bytes,atomic_json
from storage_lock import file_lock


class PacketPublication(unittest.TestCase):
    @classmethod
    def setUpClass(cls): recovery.JobRecovery.setUpClass.__func__(cls)

    @classmethod
    def tearDownClass(cls): recovery.JobRecovery.tearDownClass.__func__(cls)

    def setUp(self): recovery.JobRecovery.setUp(self)
    def tearDown(self): recovery.JobRecovery.tearDown(self)

    def test_late_source_failure_cannot_initialize_learning_progress(self):
        original = integrity.identity; bump = [0]
        def identity(path):
            value = original(path)
            return (*value[:4],value[4]+bump[0]) if Path(path)==self.video else value
        build = tutor_packet.build_packet
        def late_change(*a,**kw):
            result = build(*a,**kw); bump[0] += 1
            return result
        with patch.object(integrity,'identity',identity), patch.object(guard,'capture',return_value=None), \
                patch.object(tutor_packet,'build_packet',late_change):
            with self.assertRaises(integrity.SourceDrift):jobs.run_job(self.args,self.ffmpeg)
        for name in ('progress.json','visual_notes.json'):
            self.assertFalse((self.out/name).exists(),name)
        checkpoint=json.loads((self.out/'checkpoint.json').read_text())
        self.assertEqual(checkpoint['stages'],{});self.assertEqual(checkpoint['status'],'unverified')
        from publication import publish_job
        with self.assertRaises(ValueError):publish_job(self.out,self.root/'published','unverified-job')

    def test_successful_packet_still_initializes_legacy_user_files(self):
        jobs.run_job(self.args,self.ffmpeg)
        self.assertEqual(json.loads((self.out/'progress.json').read_text())['status'],'prepared')
        self.assertEqual(json.loads((self.out/'visual_notes.json').read_text())['observations'],[])

    def test_real_fresh_retry_preserves_progress_created_during_failed_packet(self):
        self.args.resume=False; build=tutor_packet.build_packet; calls=[]
        raw=b'{"learner_answers": ["another chat"]}'
        def first_failure(*a,**kw):
            result=build(*a,**kw);calls.append(True)
            if len(calls)==1:
                (self.out/'progress.json').write_bytes(raw)
                error=integrity.SourceDrift('fixture: proof tested separately')
                error.checksum=integrity.file_hash(self.video)
                raise error
            return result
        with patch.object(tutor_packet,'build_packet',first_failure), \
                patch.object(guard,'retry_proof',return_value={}), patch.object(guard,'require_same'):
            jobs.run_job(self.args,self.ffmpeg)
        self.assertEqual(len(calls),2)
        self.assertEqual((self.out/'progress.json').read_bytes(),raw)


class CleanSourceRetry(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name).resolve();self.video=self.root/'source.mp4';self.video.write_bytes(b'source-01')
        self.out=self.root/'job';self.args=NS(video=self.video,output=self.out,resume=True,prepare_only=False)
        self.flags=0;self.bump=0;self.security=((),None);self.owner_offset=0;self.fixed_mtime=None
        self.calls=[];self.actions={1:self.tracking};self.before_read={};self.original_identity=integrity.identity
        self.addCleanup(patch.stopall)
        patch.object(integrity,'identity',self.identity).start()
        patch.object(guard,'capture',self.capture).start()
        patch.object(jobs,'process',integrity.verification_session(paths=lambda *a:[self.video])(self.worker)).start()

    def identity(self,path):
        value=self.original_identity(path)
        if Path(path)==self.video:
            value=(*value[:3],value[3] if self.fixed_mtime is None else self.fixed_mtime,value[4]+self.bump)
        return value

    def capture(self,path):
        path=Path(path)
        if path.is_symlink() or not path.is_file() or path.stat().st_nlink!=1:return None
        s=path.stat()
        return dict(identity=self.identity(path),flags=self.flags,security=self.security,
            rights=(s.st_mode,s.st_uid+self.owner_offset,s.st_gid,s.st_nlink,getattr(s,'st_birthtime',None)))

    def tracking(self):self.flags|=64;self.bump+=1

    def worker(self,args,ffmpeg,out,report):
        count=len(self.calls)+1
        self.calls.append(dict(lock=(out/'.processing.lock').stat().st_ino,checkpoint=(out/'checkpoint.json').exists()))
        if count==2:
            from worker_io import current
            self.assertTrue(current(out).force_fresh, 'Retry must explicitly ignore the retained failed cache')
            current(out).force_fresh=False
            self.assertEqual((out/'audio.wav').read_bytes(),b'attempt-1')
            with self.assertRaises(TimeoutError):
                with file_lock(out/'.processing.lock',timeout=0):pass
        if count in self.before_read:self.before_read[count]()
        checksum=integrity.file_hash(args.video)
        from worker_io import current
        io=current(out)
        io.write_slot('audio.wav',[('attempt-'+str(count)).encode()])
        jobs.atomic_json(out/'tutor_packet.json',{})
        jobs.atomic_json(out/'manifest.json',{})
        state=dict(source_sha256=checksum,stages={'packet':{'sha256':integrity.file_hash(out/'tutor_packet.json')}})
        jobs.atomic_json(out/'checkpoint.json',state)
        if count in self.actions:self.actions[count]()
        integrity.file_hash(args.video)
        return state

    def run_job(self):return jobs.run_job(self.args,'unused-fixture-ffmpeg')

    def rejected(self,expected_calls=1):
        with self.assertRaises((ValueError,OSError)):self.run_job()
        self.assertEqual(len(self.calls),expected_calls)
        checkpoint=json.loads((self.out/'checkpoint.json').read_text())
        self.assertEqual(checkpoint['stages'],{})
        self.assertEqual(checkpoint['status'],'unverified')
        self.assertFalse((self.out/'publication.json').exists())
        from publication import publish_job
        with self.assertRaises(ValueError):publish_job(self.out,self.root/'published','retry-rejected')
        self.assertEqual(json.loads((self.out/'processing-status.json').read_text())['status'],'failed')

    def test_tracking_restarts_once_with_empty_state_and_same_lock(self):
        result=self.run_job()
        self.assertEqual(result['status'],'complete');self.assertEqual(len(self.calls),2)
        self.assertEqual(self.calls[0]['lock'],self.calls[1]['lock'])
        self.assertEqual((self.out/'audio.wav').read_bytes(),b'attempt-2')
        self.assertEqual([p.read_bytes() for p in (self.out/'.unverified').glob('*/audio.wav')],[b'attempt-1'])
        self.assertEqual(self.video.read_bytes(),b'source-01')

    def test_existing_progress_and_notes_remain_byte_identical(self):
        self.out.mkdir();values={'progress.json':b'{"learned": ["old"]}','visual_notes.json':b'{"notes": "user"}'}
        for name,raw in values.items():(self.out/name).write_bytes(raw)
        self.run_job()
        for name,raw in values.items():self.assertEqual((self.out/name).read_bytes(),raw)

    def test_progress_created_by_another_chat_during_attempt_is_preserved(self):
        def added():
            (self.out/'progress.json').write_bytes(b'{"learner": "new chat"}')
            self.tracking()
        self.actions[1]=added;self.run_job()
        self.assertEqual((self.out/'progress.json').read_bytes(),b'{"learner": "new chat"}')

    def test_unknown_ctime_change_cannot_retry(self):
        self.actions={1:lambda:setattr(self,'bump',1)};self.rejected()

    def test_already_tracked_source_cannot_retry_another_ctime_change(self):
        self.flags=64;self.rejected()

    def test_other_flag_change_cannot_retry(self):
        self.actions={1:lambda:(self.tracking(),setattr(self,'flags',self.flags|2))};self.rejected()

    def test_same_size_changed_content_is_rejected_even_with_reported_same_mtime(self):
        self.fixed_mtime=self.video.stat().st_mtime_ns
        self.actions={1:lambda:(self.video.write_bytes(b'source-02'),self.tracking())};self.rejected()

    def test_permissions_change_is_rejected(self):
        self.actions={1:lambda:(self.video.chmod(0o400),self.tracking())};self.rejected()

    def test_owner_change_is_rejected_without_requiring_chown_privilege(self):
        self.actions={1:lambda:(setattr(self,'owner_offset',1),self.tracking())};self.rejected()

    def test_acl_or_xattr_change_is_rejected(self):
        self.actions={1:lambda:(setattr(self,'security',((b'changed',),b'changed-acl')),self.tracking())};self.rejected()

    def test_identical_atomic_replacement_is_rejected(self):
        self.actions={1:lambda:(atomic_bytes(self.video,b'source-01'),self.tracking())};self.rejected()

    def test_symlink_or_hardlink_cannot_authorize_retry(self):
        other=self.root/'other';other.write_bytes(b'source-01')
        self.actions={1:lambda:(self.video.unlink(),self.video.symlink_to(other),self.tracking())}
        self.rejected()

    def test_hardlink_added_during_attempt_is_rejected(self):
        self.actions={1:lambda:(os.link(self.video,self.root/'alias'),self.tracking())};self.rejected()

    def test_replacement_between_attempts_is_not_a_new_trusted_baseline(self):
        self.before_read[2]=lambda:atomic_bytes(self.video,b'source-01');self.rejected(2)

    def test_rights_change_between_attempts_is_rejected(self):
        self.before_read[2]=lambda:self.video.chmod(0o400);self.rejected(2)

    def test_owner_change_between_attempts_is_rejected(self):
        self.before_read[2]=lambda:setattr(self,'owner_offset',1);self.rejected(2)

    def test_second_drift_exhausts_limit_without_third_attempt(self):
        self.actions[2]=lambda:setattr(self,'bump',self.bump+1);self.rejected(2)

    def test_missing_security_proof_keeps_original_refusal(self):
        with patch.object(guard,'capture',return_value=None):self.rejected()

    def test_normal_job_never_retries(self):
        self.actions={};self.run_job();self.assertEqual(len(self.calls),1)


if __name__=='__main__':unittest.main()
