"""All optimizations across scripted CLI processes; no live semantic grading."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import course
import test_personal_lesson_acceptance as lessons
import test_diagnostic_assessment as diagnostics
import test_video_hash_reuse as videos


class OptimizationAcceptance(unittest.TestCase):
    def setUp(self):
        self.f=lessons.PersonalLessonAcceptance();self.f.setUp();self.addCleanup(self.f.doCleanups)
        self.root=self.f.args().course;self.script=Path(course.__file__)

    def cli(self,command,*options,compact=True,ok=True):
        result=subprocess.run([sys.executable,'-B',str(self.script),command,'--course',str(self.root),
            *map(str,options),*(['--compact'] if compact else [])],capture_output=True,text=True,timeout=30,
            env=dict(os.environ,PYTHONDONTWRITEBYTECODE='1'))
        if not ok:
            self.assertNotEqual(result.returncode,0);self.assertNotIn('Traceback',result.stderr);return result
        self.assertEqual(result.returncode,0,result.stderr)
        value=json.loads(result.stdout)
        if compact:self.assertEqual(value['output_view'],'compact_v1')
        return value

    def payload(self,value):
        path=self.f.folder/'combined-input.json';path.write_text(json.dumps(value));return path

    def files(self):
        return {str(p.relative_to(self.root)):hashlib.sha256(p.read_bytes()).hexdigest()
                for p in self.root.rglob('*') if p.is_file()}

    def test_onboarding_diagnostic_lesson_and_new_process_preserve_evidence(self):
        answers=dict(outcome=['topics'],subject=dict(value='Java Backend',area='Programming',direction='Backend',focus='Java',text='Java Backend'),
            self_assessment='basic_experience',learning_format='step_by_step',assistant_role='mentor',
            check_frequency='adaptive',explanation_detail='balanced',constraints=dict(selected=['free_pace'],details=''))
        state=self.cli('settings')
        for key,value in answers.items():
            self.assertEqual(state['onboarding_next'],key)
            state=self.cli('settings','--onboarding-file',self.payload(dict(answers={key:value})))
        self.assertEqual(state['onboarding'],'complete')
        diagnostic_revision=0
        def diagnose(value):
            nonlocal diagnostic_revision
            state=self.cli('diagnostic','--update-file',self.payload(value),'--expected-revision',diagnostic_revision,'--reason','Scripted combined QA')
            diagnostic_revision=state['revision'];self.assertTrue(state['valid']);return state
        state=diagnose(dict(action='accept',competences=['Maps'],question_policy='fixed_15_v1',selection_policy='adaptive_v2',
            competence_prerequisites={},next_difficulty='applied'))
        for i in range(15):
            state=diagnose(dict(action='prompt',competence='Maps',difficulty=state['current']['next_difficulty'],
                prompt=f'put("a",{i}); put("a",{i+1}): get("a") and size()? Explain.',format='code_analysis',
                rubric=diagnostics.DiagnosticAssessment.rubric(None)))
            if i==7:
                restored=self.cli('diagnostic','--session','chat-b')
                self.assertEqual(restored['current']['pending_prompt'],state['current']['pending_prompt'])
            state=diagnose(dict(action='answer',prompt_revision=state['current']['pending_prompt']['revision'],
                answer=f'{i+1} and size 1: the duplicate key replaces its value',evaluation='Scripted criterion assessment',
                evidence='confirmed',assessment=diagnostics.DiagnosticAssessment.assessment(None),next_competence='Maps',
                next_difficulty='transfer',selection_reason='Verify the next distinct example'))
        self.assertEqual(state['status'],'completed');self.assertEqual(state['current']['answer_count'],15)
        before=self.files();full=self.cli('diagnostic',compact=False)
        self.assertEqual(state['summary'],full['summary']);self.assertEqual(before,self.files())
        area=lessons.AREAS['backend'];state=self.cli('learn-unit','--unit-id','backend','--session','chat-a')
        def write(option,value,ok=True,revision=None):
            nonlocal state
            result=self.cli('learn-unit','--unit-id','backend','--session','chat-a',
                *(['--lesson-id',identity] if identity else []),option,self.payload(value),
                '--expected-revision',state['learning']['revision'] if revision is None else revision,
                '--expected-unit-sha256',state['unit']['content_sha256'],ok=ok)
            if ok:state=result
            return result
        identity=None
        write('--lesson-context-file',dict(kind='new_topic',topic=area['title'],goal=area['goal'],plan=['Explain replacement','Trace a map','Independent practice']))
        identity=state['learning']['lesson_context']['event']['value']['id']
        self.assertIn(area['source'],state['unit']['source_contexts'][0]['text'])
        write('--event-file',dict(stage='explained',details='Explained; learner said понятно',question=None,answer=None,hints=0,criteria=[]))
        self.assertNotIn('backend',state['learning_progress']['demonstrated_units'])
        write('--practice-file',dict(action='task',id='fresh-map',question=area['fresh'],criteria=[1],rationale=area['goal']))
        answer=dict(stage='solved_independently',details='Correct independent execution trace',question=area['fresh'],answer=area['fresh_answer'],hints=0,criteria=[1],task_id='fresh-map')
        write('--event-file',answer);self.assertIn('backend',state['learning_progress']['demonstrated_units'])
        self.assertEqual(state['evidence'][0]['value']['answer'],area['fresh_answer'])
        before=self.files();write('--event-file',answer,ok=False,revision=state['learning']['revision']-1);self.assertEqual(before,self.files())
        checkpoint=dict(recap='Map replacement practiced',next_step='Review another case',pending_question=None)
        write('--lesson-state-file',dict(action='pause',checkpoint=checkpoint))
        before=self.files();restored=self.cli('learn-unit','--unit-id','backend','--session','chat-b','--lesson-id',identity)
        full=self.cli('learn-unit','--unit-id','backend','--session','chat-b','--lesson-id',identity,compact=False)
        self.assertEqual(restored['learning']['lesson_state']['status'],'paused')
        self.assertEqual(restored['learning']['lesson_checkpoint']['event']['value']['recap'],checkpoint['recap'])
        self.assertEqual(restored['learning']['criteria'],full['learning']['criteria'])
        self.assertEqual(restored['learning']['practice'],full['learning']['practice'])
        self.assertEqual(self.cli('settings')['onboarding'],'complete');self.assertEqual(before,self.files())
        source=self.root/state['unit']['sources'][0]['snapshot'];source.chmod(0o600);source.write_text('changed material')
        before=self.files();stale=self.cli('learn-unit','--unit-id','backend','--session','chat-b','--lesson-id',identity)
        self.assertTrue(stale['unit']['stale']);self.assertFalse(stale['learning']['all_criteria_demonstrated'])
        self.assertEqual(before,self.files())
        video=videos.VideoHashReuse();video.setUp();self.addCleanup(video.doCleanups)
        self.assertEqual(video.read()[1],video.sha)
        video.video.write_bytes(b'b'*4096)
        with self.assertRaisesRegex(ValueError,'integrity changed'):video.read()

    def test_compact_other_process_write_invalidates_operation_reuse(self):
        import unit_learning
        from read_snapshot import operation
        video=videos.VideoHashReuse();video.setUp();self.addCleanup(video.doCleanups)
        with operation():
            initial=unit_learning.learn_unit(self.f.args(session='chat-a',compact=True))
            self.assertEqual(initial['learning']['revision'],0)
            self.assertEqual(video.read()[1],video.sha)
            area=lessons.AREAS['backend']
            answer=dict(stage='solved_independently',details='Scripted independent execution trace',
                question=area['fresh'],answer=area['fresh_answer'],hints=0,criteria=[1])
            written=self.cli('learn-unit','--unit-id','backend','--session','chat-b','--event-file',self.payload(answer),
                '--expected-revision','0','--expected-unit-sha256',initial['unit']['content_sha256'])
            restored=unit_learning.learn_unit(self.f.args(session='chat-a',compact=True))
            self.assertEqual(restored['learning']['revision'],1)
            self.assertEqual(restored['learning']['criteria'],written['learning']['criteria'])
            self.assertIn('backend',restored['learning_progress']['demonstrated_units'])
            video.video.write_bytes(b'b'*4096)
            with self.assertRaisesRegex(ValueError,'integrity changed'):video.read()

    def check_source_change(self,kind):
        from unittest.mock import patch
        import adaptive_learning
        import learning_intent
        import study_units
        import test_learning_journey
        journey=test_learning_journey.LearningJourney();journey.setUp();self.addCleanup(journey.doCleanups)
        card=dict(id='example',title='Assignment',summary='Trace an assignment',objectives=['Read a value'],
            criteria=['Explain a new assignment independently'],prerequisites=[],uncertainties=[],
            sources=[dict(kind='video',role='teacher',notes='Synthetic prepared QA source',lesson='lesson-01',start=0,end=60)])
        study_units.unit(journey.args(unit_file=journey.write('unit.json',card),expected_revision=0))
        target=(journey.video if kind=='video' else journey.media.f.pub/('frames/0.jpg' if kind=='frame' else 'transcript.json'))
        original=learning_intent.view;changed=False
        def interleave(*args,**kwargs):
            nonlocal changed
            if not changed:
                changed=True;target.write_bytes(b'Changed source')
            return original(*args,**kwargs)
        with patch.object(learning_intent,'view',interleave):
            result=adaptive_learning.study_next(journey.args())
        self.assertEqual(result['recommendations'],[])
        self.assertTrue(result['blocked'] or not result['valid'])

    def test_video_change_between_recommendation_and_progress_is_not_mixed(self):
        self.check_source_change('video')

    def test_frame_change_between_recommendation_and_progress_is_not_mixed(self):
        self.check_source_change('frame')

    def test_transcript_change_between_recommendation_and_progress_is_not_mixed(self):
        self.check_source_change('transcript')
