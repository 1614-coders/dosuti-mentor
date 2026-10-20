"""Component acceptance, not a real Codex dialogue; learner answers are scripted QA."""
import contextlib
import io
import json
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch
import test_learning_journey
import adaptive_learning
import compact_lesson
import course
import diagnostic_learning as diagnostic
import learning_intent as intent
import learning_tools as learning
import material_status
import study_units
import unit_learning


MAP='Trace HashMap replacement'
COUNT='Implement frequency counting'
LOOP='Trace loop boundaries'
NULL='Explain null unboxing'
STRINGS='Compare strings'
OOP='Trace method overriding'
STREAM='Trace a stream pipeline'
FLATMAP='Flatten nested streams'
# Expected next difficulty follows the observed answer, including decreases.
QUESTIONS=[
    (MAP,'basic','put("a",2);put("a",3): get("a")?', '3: the existing value is replaced','confirmed','applied'),
    (MAP,'applied','put("a",2);put("b",7);put("a",3): size()?', '3 because there were three puts','unknown','basic'),
    (LOOP,'basic','int s=0;for(int i=1;i<=2;i++)s+=i; what is s?', '3: 1+2','confirmed','applied'),
    (LOOP,'applied','for i=0..3: if(i==1)continue; how many visits reach the body after continue?', '3: i=0,2,3','confirmed','transfer'),
    (LOOP,'transfer','for(int i=0;i<=a.length;i++) print(a[i]); is this safe?', 'Yes, length is a valid final index','unknown','basic'),
    (NULL,'basic','Map<String,Integer> m=new HashMap<>(); int n=m.get("x"); what happens?', 'n becomes zero','unknown','basic'),
    (STRINGS,'basic','new String("a")==new String("a"): true or false?', 'true because the characters match','needs_practice','basic'),
    (OOP,'basic','A a=new B(); B overrides f(); which f runs for a.f()?', 'B.f because runtime dispatch uses the actual object','confirmed','applied'),
    (STREAM,'applied','Stream.of(1,2,3).filter(x->x>1).count(): result?', '2 because 2 and 3 pass','confirmed','transfer'),
    (FLATMAP,'transfer','How would you flatten List<List<Integer>> into a stream of integers?', None,None,'basic'),
    (COUNT,'basic','Count repeated words in a HashMap without replacing a count by 1 every time.', 'I do not know how to keep the previous count','unknown','basic'),
]


class EducationalAcceptance(unittest.TestCase):
    def setUp(self):
        self.j=test_learning_journey.LearningJourney();self.j.setUp();self.addCleanup(self.j.doCleanups)

    def cli(self,command,*extra):
        result=subprocess.run([sys.executable,'-B',str(Path(course.__file__)),command,'--course',str(self.j.fixture.path),
            '--profile','journey','--session','chat-b',*extra],capture_output=True,text=True,timeout=20)
        self.assertEqual(result.returncode,0,result.stderr)
        return json.loads(result.stdout)

    def diagnose(self,value,session='chat-a'):
        return diagnostic.diagnostic(self.j.args(session=session,update_file=self.j.write('acceptance-diagnostic.json',value),
            expected_revision=diagnostic.view(self.j.args())['revision'],reason='Scripted QA learner action'))

    def material(self):
        text=self.j.base/'java-map.txt'
        text.write_text('Java HashMap\nput with an existing key replaces its value without increasing size.\n'
            'For each word: counts.put(word, counts.getOrDefault(word, 0) + 1).\n',encoding='utf-8')
        card=dict(id='example',title='HashMap replacement and frequency counting',summary='Two independent map criteria',
            objectives=[MAP,COUNT],criteria=['Trace duplicate-key replacement and map size','Implement counting with the previous value'],
            prerequisites=[],uncertainties=[],sources=[dict(kind='text',role='assistant',notes='QA explanatory text, not teacher video',
                path=str(text),start_line=1,end_line=3)])
        study_units.unit(self.j.args(unit_file=self.j.write('acceptance-unit.json',card),expected_revision=0))
        return study_units.unit(self.j.args(unit_id='example'))['unit']

    def proposal(self,unit,revisions):
        bases=[]
        for number,competence,quote in ((1,MAP,'put with an existing key replaces its value without increasing size.'),
                                      (2,COUNT,'counts.put(word, counts.getOrDefault(word, 0) + 1)')):
            refs=[revisions[competence]] if competence in revisions else []
            reason=('The duplicate-key size answer counts calls instead of unique keys.' if number==1 else
                    'The learner cannot retain and increment the previous count.')
            target=dict(competence=competence,rationale=reason,unit_id='example',unit_sha256=unit['content_sha256'],
                criteria=[number],diagnostic_event_revisions=refs,alignment_review=dict(verdict='supported',
                    competence=competence,diagnostic_event_revisions=refs,diagnostic_reason=reason if refs else 'Goal-based map practice; no diagnostic evidence.',
                    criterion_links=[dict(criterion=number,criterion_text=unit['criteria'][number-1],source=1,quote=quote,
                        explanation=('Tracing value and size checks replacement versus insertion.' if number==1 else
                                     'An independent counting implementation checks accumulation, not merely duplicate-key knowledge.'))]))
            basis=dict(unit_ids=['example'],targets=[target])
            if refs:basis['diagnostic_event_revisions']=refs
            if number==2:basis['prerequisites']=[1]
            bases.append(basis)
        return dict(summary='Repair map tracing, then implement frequency counting',steps=[MAP,COUNT],step_bases=bases)

    def attempt(self,stage,question,answer,criteria,hints=0,session='chat-a'):
        return self.j.learn(dict(stage=stage,details='Scripted Java QA evaluation of the specified criterion',
            question=question,answer=answer,criteria=criteria,hints=hints),session=session)

    def test_full_current_cycle_eleven_java_questions_and_cross_process_continuation(self):
        j=self.j
        self.assertEqual(learning.preferences(j.args())['onboarding'],'not_asked')
        self.assertFalse(intent.path_for(j.args()).exists())
        answers=dict(outcome=['profession'],subject='Java backend',self_assessment='basics',learning_format='step_by_step',
            assistant_role='mentor',check_frequency='adaptive',explanation_detail='detailed',
            constraints=dict(selected=['study_time'],details='Three to five hours per week'))
        j.settings(onboarding_file=j.write('acceptance-onboarding.json',dict(answers=answers)))
        learning.settings(j.args(scope='profile',set=['format=steps','detail=detailed'],reset=False,onboarding=None))
        self.assertEqual(learning.preferences(j.args(session='chat-b'))['onboarding'],'complete')
        j.decide(dict(goal=dict(description='Become a Java backend developer',purpose='First internship')))
        self.assertFalse(diagnostic.path_for(j.args()).exists())
        self.diagnose(dict(action='accept',competences=list(dict.fromkeys(q[0] for q in QUESTIONS)),next_difficulty='basic'))
        revisions={}
        for index,(competence,difficulty,question,answer,evidence,next_difficulty) in enumerate(QUESTIONS):
            prompt=self.diagnose(dict(action='prompt',competence=competence,prompt=question,difficulty=difficulty,format='code_analysis'))
            revision=prompt['current']['pending_prompt']['revision']
            if index==3:
                self.diagnose(dict(action='pause'))
                restored=self.cli('diagnostic')
                self.assertEqual(restored['status'],'paused')
                self.assertEqual(restored['current']['pending_prompt']['revision'],revision)
                self.diagnose(dict(action='resume'),session='chat-b')
            next_competence=QUESTIONS[index+1][0] if index+1<len(QUESTIONS) else None
            decision=dict(action='skip' if answer is None else 'answer',prompt_revision=revision,
                next_difficulty=next_difficulty,next_competence=next_competence,
                selection_reason='Follow the recorded answer: probe transfer after success, return to basics after a gap.')
            if answer is None:decision['skip_reason']='Have not studied flatMap yet'
            else:decision.update(answer=answer,evidence=evidence,evaluation='Scripted QA: '+evidence+' for this Java example')
            saved=self.diagnose(decision)
            if answer is not None:revisions[competence]=saved['revision']
        finished=self.diagnose(dict(action='complete'));summary=finished['summary']
        self.assertEqual(len(finished['current']['answers']),10)
        self.assertEqual(len(finished['current']['skips']),1)
        self.assertIn(MAP,summary['identified_gaps']);self.assertIn(COUNT,summary['identified_gaps'])
        self.assertIn(STRINGS,summary['partially_confirmed_knowledge'])
        self.assertEqual(summary['unverified_areas'],[FLATMAP])
        self.assertEqual(self.cli('diagnostic')['summary'],summary)

        unit=self.material();proposal=self.proposal(unit,revisions)
        j.decide(dict(proposal=proposal));self.assertIsNone(intent.view(j.args())['plan'])
        accepted=j.decide(dict(accept_proposal=True))
        self.assertEqual(accepted['learning_progress']['blocked_steps'],[2])
        self.assertEqual(adaptive_learning.study_next(j.args(limit=10))['roadmap']['uncovered_diagnostic_gaps'],[LOOP,NULL])
        self.attempt('explained',None,None,[])
        self.assertEqual(intent.view(j.args())['learning_progress']['completed_steps'],[])
        self.attempt('needs_review','put(a,2);put(a,3): size?','2',[1])
        self.assertEqual(adaptive_learning.study_next(j.args(limit=10))['roadmap']['recommendations'][0]['action'],'review_error')
        self.attempt('practice_with_help','put(a,2);put(a,3): size?','1 after a hint about unique keys',[1],hints=1)
        self.assertEqual(adaptive_learning.study_next(j.args(limit=10))['roadmap']['recommendations'][0]['action'],'fresh_independent_task')
        self.attempt('solved_independently','put(a,4);put(b,8);put(a,9): value(a), size?','9 and 2: a is replaced',[1])
        progress=intent.view(j.args())['learning_progress']
        self.assertEqual(progress['completed_steps'],[1]);self.assertEqual(progress['next_step']['index'],2)
        self.assertEqual(progress['demonstrated_units'],[])
        self.attempt('solved_independently','Implement counts for each word without hints.',
            'for(String w:words) counts.put(w,counts.getOrDefault(w,0)+1);',[2])
        self.assertEqual(intent.view(j.args())['learning_progress']['completed_steps'],[1,2])
        remaining=adaptive_learning.study_next(j.args(limit=10))['roadmap']
        self.assertEqual(remaining['uncovered_diagnostic_gaps'],[LOOP,NULL])
        self.assertTrue(any(row['action']=='review_route' for row in remaining['recommendations']))

        checkpoint=dict(recap='Both map criteria demonstrated',next_step='Review map transfer later',pending_question=None)
        j.learn(checkpoint,checkpoint=True)
        roadmap=adaptive_learning.study_next(j.args(limit=10))['roadmap']
        revised=dict(proposal,summary='Proposed review of the completed map criteria',adaptation_basis=roadmap['adaptation_basis'])
        old=intent.view(j.args())['plan'];j.decide(dict(proposal=revised))
        self.assertEqual(intent.view(j.args())['plan'],old)
        j.learn(dict(checkpoint,next_step='Independent chat-b review'),checkpoint=True,session='chat-b')
        with self.assertRaisesRegex(ValueError,'adaptation'):j.decide(dict(accept_proposal=True))
        j.decide(dict(proposal=None));self.assertEqual(intent.view(j.args())['plan'],old)
        revised['adaptation_basis']=adaptive_learning.study_next(j.args(limit=10))['roadmap']['adaptation_basis']
        j.decide(dict(proposal=revised));j.decide(dict(accept_proposal=True))
        self.assertEqual(self.cli('intent')['plan']['summary'],revised['summary'])

        with contextlib.redirect_stdout(io.StringIO()):
            course.progress(j.args(topic='Synthetic video lesson explanation',stage='explained',details='No mastery asserted'))
        cursor=dict(lesson='lesson-01',time=10,topic='Synthetic video example',recap='Read prepared example',
            next_step='Review the example',pending_question='What value is assigned in the prepared example?',misconceptions=[])
        learning.resume(j.args(state_file=j.write('acceptance-cursor.json',cursor),expected_revision=0))
        resumed=learning.resume(j.args(session='chat-b'))['learning_progress']
        self.assertTrue(resumed['continuation_choice_required']);self.assertIsNone(resumed['stopped_at'])
        self.assertEqual(len(resumed['continuations']),3)
        returned=unit_learning.learn_unit(j.args(unit_id='example',session='chat-b',history_limit=20))
        self.assertEqual(returned['learning_progress'],resumed)
        self.assertEqual(returned['learning']['checkpoint']['event']['value']['next_step'],'Independent chat-b review')
        self.assertEqual(self.cli('intent')['learning_progress']['completed_steps'],[1,2])

        protected=[learning.pref_file(j.args(),'profile'),diagnostic.path_for(j.args()),intent.path_for(j.args()),
            j.fixture.path/'learners/journey/unit-learning.json',j.fixture.path/'study-units.json']
        before={path:path.read_bytes() for path in protected}
        speech=j.fixture.context(0,60)['speech']
        compact_lesson.apply(j.media.directory,j.media.plan())
        self.assertFalse(j.video.exists());self.assertTrue(j.fixture.video.exists())
        with patch.object(course,'prepare',side_effect=AssertionError('Repeated ASR forbidden')), \
             patch('screen_ocr.view',side_effect=AssertionError('Repeated OCR forbidden')):
            revisited=unit_learning.learn_unit(j.args(unit_id='example',session='chat-c'))
            self.assertTrue(revisited['learning']['all_criteria_demonstrated'])
            self.assertTrue(material_status.view(j.args(unit_id='example',lesson=None))['material']['reuse_preparation'])
            self.assertEqual(j.fixture.context(0,60)['speech'],speech)
        self.assertEqual({path:path.read_bytes() for path in protected},before)
        changed=j.decide(dict(goal=dict(description='Build a desktop Java application')))
        self.assertTrue(changed['plan']['needs_review']);self.assertIsNone(changed['learning_progress']['next_step'])
        self.assertEqual(changed['learning_progress']['demonstrated_units'],['example'])
        self.assertEqual(diagnostic.view(j.args())['summary'],summary)

    def test_decline_and_early_completion_do_not_block_learning_or_invent_answers(self):
        j=self.j;j.decide(dict(goal=dict(description='Explore Java maps')))
        self.diagnose(dict(action='decline'))
        unit=self.material();j.decide(dict(proposal=self.proposal(unit,{})));j.decide(dict(accept_proposal=True))
        self.assertEqual(diagnostic.view(j.args())['current']['answers'],[])
        self.assertEqual(adaptive_learning.study_next(j.args())['roadmap']['status'],'ready')
        self.diagnose(dict(action='accept',competences=[MAP,COUNT],next_difficulty='basic'))
        self.diagnose(dict(action='prompt',competence=MAP,prompt='Trace duplicate keys?',difficulty='basic',format='open'))
        stopped=self.diagnose(dict(action='complete'))
        self.assertEqual(stopped['current']['answers'],[])
        self.assertEqual(stopped['summary']['unverified_areas'],[MAP,COUNT])
        self.assertEqual(self.cli('diagnostic')['summary'],stopped['summary'])


if __name__=='__main__':unittest.main()
