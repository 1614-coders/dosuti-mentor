import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import pilot
import pilot_activity as activity
import pilot_assessment as assessment
import pilot_cleanup as cleanup
from storage_lock import file_lock

class Evidence(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.base=Path(self.tmp.name).resolve();self.root=self.base/'тест с пробелами'
        pilot.start(self.root)
        self.material=self.root/'course'/'notes.txt';self.material.write_text('Java Map: put replaces the current mapping',encoding='utf-8')
    def plan(self):
        return dict(lesson='java-map',sources=['notes.txt'],questions=[dict(id=f'{kind}{i}',kind=kind,prompt=f'Question {i}',origin='assistant',source='notes.txt',criteria='Explain independently',homework=False,provenance='Exercise written for this lesson') for kind in ('theory','practice') for i in range(5)])
    def answer(self,**kw):
        value=dict(lesson='java-map',plan_revision=1,question='theory0',session='chat-a',answer='3, put replaced 2',hints=0,verdict='correct',evaluation='Assistant: correct replacement semantics')
        value.update(kw);return value
    def test_timing_nested_and_no_double_count_claim(self):
        with activity.span('preparation',self.root):
            with activity.span('asr',self.root): pass
        data=activity.read(self.root)['activities']
        self.assertEqual(data[1]['parent'],data[0]['id'])
        self.assertTrue(all(a['status']=='passed' and a['seconds']>=0 for a in data))
        self.assertIn('нельзя складывать',Path(pilot.export(self.root)['report']).read_text())
    def test_failed_and_open_timers(self):
        with self.assertRaises(RuntimeError):
            with activity.span('asr',self.root): raise RuntimeError('failure')
        opened=activity.start(self.root,'learner_wait','chat-b')
        self.assertEqual(activity.read(self.root)['activities'][0]['status'],'failed')
        self.assertIsNone(opened['seconds'])
        activity.finish(self.root,opened['id'],'interrupted')
        with self.assertRaises(ValueError): activity.finish(self.root,opened['id'])
    def test_course_scope_real_pilot_only(self):
        with activity.course_scope(self.root/'course','prepare'): pass
        ordinary=self.base/'ordinary';ordinary.mkdir()
        with patch.dict(os.environ,{activity.ENV:str(self.root)}):
            with activity.course_scope(ordinary,'prepare'):
                self.assertNotIn(activity.ENV,os.environ)
        self.assertFalse((ordinary/'activities.json').exists())
        self.assertEqual(len(activity.read(self.root)['activities']),1)
    def test_scope_outside_pilot_not_instrumented(self):
        with patch.dict(os.environ,{activity.ENV:str(self.root)}):
            with activity.span('asr',scope=self.base/'outside'): pass
        self.assertEqual(activity.read(self.root)['activities'],[])
    def test_plan_answer_resume_history_and_revision(self):
        assessment.record(self.root,0,'plan',self.plan())
        assessment.record(self.root,1,'answer',self.answer())
        assessment.record(self.root,2,'answer',self.answer(session='chat-b',answer='Second attempt',hints=1,verdict='partial',evaluation='Needed a hint'))
        self.assertEqual(assessment.read(self.root)['revision'],3)
        with self.assertRaises(ValueError): assessment.record(self.root,1,'answer',self.answer())
        report=Path(pilot.export(self.root)['report']).read_text()
        self.assertIn('Second attempt',report);self.assertIn('3, put replaced 2',report)
        self.assertIn('chat-b',report);self.assertIn('theory: заданий 5, отвечено 1, текущих оценок correct 0',report)
    def test_invalid_plan_counts_and_provenance(self):
        for mutation in ('count','duplicate','external','homework'):
            plan=self.plan()
            if mutation=='count': plan['questions'].pop()
            elif mutation=='duplicate': plan['questions'][1]['id']=plan['questions'][0]['id']
            elif mutation=='external': plan['questions'][0]['origin']='external'
            else: plan['questions'][0]['homework']=True
            with self.assertRaises(ValueError): assessment.record(self.root,0,'plan',plan)
        self.assertEqual(assessment.read(self.root)['revision'],0)
    def test_changed_source_invalidates_answer_and_report(self):
        assessment.record(self.root,0,'plan',self.plan());assessment.record(self.root,1,'answer',self.answer())
        self.material.write_text('Changed lesson')
        with self.assertRaisesRegex(ValueError,'Source changed'): assessment.record(self.root,2,'answer',self.answer())
        report=Path(pilot.export(self.root)['report']).read_text()
        self.assertIn('ИЗМЕНЕНЫ',report);self.assertIn('текущих оценок correct 0',report)
        assessment.record(self.root,2,'plan',self.plan())
        with self.assertRaises(ValueError): assessment.record(self.root,3,'answer',self.answer())
    def test_source_traversal_and_link_refused(self):
        plan=self.plan();plan['sources']=['../pilot.json']
        with self.assertRaises(ValueError): assessment.record(self.root,0,'plan',plan)
        if os.name!='nt':
            self.material.unlink();self.material.symlink_to(self.root/'pilot.json')
            with self.assertRaises(ValueError): assessment.record(self.root,0,'plan',self.plan())
    def test_answers_require_actual_response_and_evaluation(self):
        assessment.record(self.root,0,'plan',self.plan())
        for value in (self.answer(answer=''),self.answer(evaluation=''),self.answer(hints=True),self.answer(question='missing')):
            with self.assertRaises(ValueError): assessment.record(self.root,1,'answer',value)
    def test_report_includes_setup_and_partial_failure(self):
        root=self.base/'setup';root.mkdir();run=root/'run';pilot.start(run)
        (root/'setup.json').write_text(json.dumps(dict(schema='pilot_setup_v1',test_only=True,attempts=[dict(number=1,status='failed',environment={'system':'Windows'},events=[dict(step='model',status='failed',observation='Network timeout',at='now',seconds=1.5,timing_method='monotonic')])])),encoding='utf-8')
        report=Path(pilot.export(run)['report']).read_text()
        self.assertIn('Network timeout',report);self.assertIn('1.5',report)
        (run/'activities.json').write_text('{bad')
        self.assertIn('Журнал времени недоступен',Path(pilot.export(run)['report']).read_text())
    def test_external_report_and_link_protection(self):
        out=self.base/'report.md';pilot.export(self.root,out);self.assertTrue(out.exists())
        before=(self.root/'pilot.json').read_bytes()
        with self.assertRaises(ValueError): pilot.export(self.root,self.root/'pilot.json')
        self.assertEqual(before,(self.root/'pilot.json').read_bytes())
        if os.name!='nt':
            link=self.base/'link.md';link.symlink_to(out)
            with self.assertRaises(ValueError): pilot.export(self.root,link)
    def test_cleanup_requires_report_then_only_owned_tree(self):
        outside=self.base/'keep';outside.write_text('keep')
        result=cleanup.plan(self.root,self.base/'cleanup.json')
        self.assertTrue(Path(result['report']).is_file());self.assertTrue(self.root.exists())
        done=cleanup.apply(self.root,self.base/'cleanup.json')
        self.assertFalse(self.root.exists());self.assertEqual(outside.read_text(),'keep')
        self.assertFalse(done['plugin_removed']);self.assertFalse(done['chat_history_removed'])
        self.assertEqual(cleanup.apply(self.root,self.base/'cleanup.json')['status'],'already_removed')
    def test_cleanup_changed_files_nothing_removed(self):
        cleanup.plan(self.root,self.base/'cleanup.json');self.material.write_text('changed')
        with self.assertRaisesRegex(ValueError,'changed'): cleanup.apply(self.root,self.base/'cleanup.json')
        self.assertTrue((self.root/'pilot.json').exists());self.assertFalse((self.root/cleanup.MARKER).exists())
    def test_cleanup_open_activity_and_legacy(self):
        item=activity.start(self.root,'teaching')
        with self.assertRaises(ValueError): cleanup.plan(self.root,self.base/'a.json')
        activity.finish(self.root,item['id'],'interrupted')
        p=self.root/'pilot.json';data=json.loads(p.read_text());data.pop('cleanup_owner');p.write_text(json.dumps(data))
        with self.assertRaises(ValueError): cleanup.plan(self.root,self.base/'b.json')
    def test_cleanup_external_report_missing_or_modified(self):
        result=cleanup.plan(self.root,self.base/'cleanup.json');Path(result['report']).write_text('changed')
        with self.assertRaises(ValueError): cleanup.apply(self.root,self.base/'cleanup.json')
        self.assertTrue(self.root.exists())
    @unittest.skipIf(os.name=='nt','POSIX venv link fixture')
    def test_cleanup_unlinks_external_symlink_without_following(self):
        outside=self.base/'outside';outside.mkdir();(outside/'secret').write_text('keep')
        (self.root/'external').symlink_to(outside,target_is_directory=True)
        cleanup.plan(self.root,self.base/'cleanup.json');cleanup.apply(self.root,self.base/'cleanup.json')
        self.assertEqual((outside/'secret').read_text(),'keep')
    def test_cleanup_lock_busy(self):
        lock=self.root/'.pilot-use.lock'
        with file_lock(lock):
            with self.assertRaises(TimeoutError): cleanup.plan(self.root,self.base/'cleanup.json')
    def test_cleanup_interruption_resumes_and_freezes_course(self):
        cleanup.plan(self.root,self.base/'cleanup.json')
        original=Path.unlink
        def fail(path,*a,**kw):
            if path==self.material: raise OSError('simulated interruption')
            return original(path,*a,**kw)
        with patch.object(Path,'unlink',fail):
            with self.assertRaises(OSError): cleanup.apply(self.root,self.base/'cleanup.json')
        with self.assertRaisesRegex(ValueError,'cleanup is pending'): pilot.load(self.root)
        cleanup.apply(self.root,self.base/'cleanup.json');self.assertFalse(self.root.exists())

    def test_teacher_homework_required_and_pinned(self):
        folder=self.root/'course/lessons/java-map';folder.mkdir(parents=True)
        hw=folder/'homework.txt';hw.write_text('Implement a map exercise')
        metadata=folder/'lesson.json';metadata.write_text(json.dumps(dict(homework=dict(status='provided',file=str(hw),sha256=assessment.digest(hw)))))
        plan=self.plan()
        with self.assertRaisesRegex(ValueError,'Include provided'): assessment.record(self.root,0,'plan',plan)
        plan['questions'][5].update(homework=True,origin='teacher',source='lessons/java-map/homework.txt',provenance='Teacher assignment')
        plan['sources'].append('lessons/java-map/homework.txt')
        event=assessment.record(self.root,0,'plan',plan)
        self.assertIn('lessons/java-map/lesson.json',event['value']['pins'])
        hw.write_text('New assignment')
        with self.assertRaisesRegex(ValueError,'Source changed'): assessment.record(self.root,1,'answer',self.answer())
    def test_owned_setup_tree_and_external_runtime_preserved(self):
        import pilot_setup
        outer=self.base/'new-setup';outer.mkdir();run=outer/'run';pilot.start(run)
        (outer/'setup.json').write_text(json.dumps(dict(schema=pilot_setup.SCHEMA,test_only=True,owner='owned',attempts=[])))
        external=self.base/'shared-model';external.mkdir();(external/'model.bin').write_bytes(b'keep')
        (outer/'runtime').mkdir();(outer/'runtime/local-test.bin').write_bytes(b'remove')
        result=cleanup.plan(run,self.base/'setup-cleanup.json')
        cleanup.apply(run,self.base/'setup-cleanup.json')
        self.assertFalse(outer.exists());self.assertEqual((external/'model.bin').read_bytes(),b'keep')

    def test_parallel_activity_writers_preserve_records(self):
        from concurrent.futures import ThreadPoolExecutor
        def task(i):
            item=activity.start(self.root,'other',f'chat-{i}')
            activity.finish(self.root,item['id'])
        with ThreadPoolExecutor(max_workers=4) as executor: list(executor.map(task,range(8)))
        data=activity.read(self.root)
        self.assertEqual(data['revision'],16);self.assertEqual(len(data['activities']),8)
        self.assertTrue(all(a['status']=='passed' for a in data['activities']))
    def test_network_download_timed_but_cache_not_downloaded_twice(self):
        from functools import partial
        from http.server import ThreadingHTTPServer,SimpleHTTPRequestHandler
        import shutil,threading
        import video_sources
        clip=Path(__file__).resolve().parents[5]/'work/limits-0.4.3/technical-terms.mp4'
        if not clip.is_file(): self.skipTest('Optional local real-media fixture unavailable')
        shutil.copyfile(clip,self.base/'sample.mp4')
        class Quiet(SimpleHTTPRequestHandler):
            def log_message(self,*args): pass
        server=ThreadingHTTPServer(('127.0.0.1',0),partial(Quiet,directory=str(self.base)))
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        try:
            directory=self.root/'course/lessons/net';directory.mkdir(parents=True)
            video=dict(source=f'http://127.0.0.1:{server.server_port}/sample.mp4')
            with activity.course_scope(self.root/'course','prepare'):
                first=video_sources.acquire(directory,video)
                second=video_sources.acquire(directory,video)
            self.assertEqual(first,second)
            self.assertEqual(sum(a['stage']=='download' for a in activity.read(self.root)['activities']),1)
        finally: server.shutdown();server.server_close();thread.join(timeout=5)
    def test_download_failure_has_failed_timing(self):
        import video_sources
        directory=self.root/'course/lessons/net';directory.mkdir(parents=True)
        with patch.object(video_sources,'fetch_direct',side_effect=RuntimeError('network failure')):
            with self.assertRaises(RuntimeError):
                with activity.course_scope(self.root/'course','prepare'):
                    video_sources.acquire(directory,dict(source='https://example.org/test.mp4'))
        data=activity.read(self.root)['activities']
        self.assertEqual([a['status'] for a in data],['failed','failed'])
    def test_malformed_activity_journal_has_partial_report(self):
        (self.root/'activities.json').write_text('[]')
        report=Path(pilot.export(self.root)['report']).read_text()
        self.assertIn('Журнал времени недоступен',report)
        self.assertIn('resume: not_checked',report)

if __name__=='__main__': unittest.main()
