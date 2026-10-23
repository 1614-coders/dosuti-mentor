"""Model-provided self-report interpretation is not proof of knowledge."""
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
import test_learning_tools
import diagnostic_learning as d
import learning_tools as lt

PLUGIN = Path(__file__).resolve().parents[1]

class StartingGuidance(unittest.TestCase):
    def setUp(self):
        self.f=test_learning_tools.LearningTools(); self.f.setUp(); self.addCleanup(self.f.tearDown)

    def save(self,value):
        return d.diagnostic(self.f.args(update_file=self.f.write('diagnostic-input.json',value),
            expected_revision=d.view(self.f.a)['revision'],reason='Explicit consent; subject calibration'))

    def test_free_experience_requires_calibration_and_preserves_subject_and_text(self):
        original='Я три года разрабатываю Java-приложения, хорошо знаю Spring Boot, но хочу разобраться в архитектуре микросервисов'
        self.f.onboarding(dict(subject=dict(value='Java Backend',text='Java Backend',direction='Архитектура микросервисов'),self_assessment=dict(text=original)))
        path=lt.pref_file(self.f.a,'profile');before=path.read_bytes()
        guide=d.diagnostic(self.f.a)['starting_guidance']
        self.assertIsNone(guide['difficulty']);self.assertTrue(guide['calibration'])
        self.assertEqual(guide['next_action'],'interpret_text_or_select_subject_calibration')
        self.assertEqual(guide['self_assessment']['text'],original)
        self.assertEqual(guide['subject']['direction'],'Архитектура микросервисов')
        self.assertEqual(path.read_bytes(),before);self.assertFalse(d.path_for(self.f.a).exists())

    def test_ambiguous_accept_needs_explicit_task_difficulty_and_writes_no_evidence(self):
        self.f.onboarding(dict(self_assessment=dict(text='Не знаю свой уровень, где-то хорошо, где-то плохо')))
        with self.assertRaisesRegex(ValueError,'calibration'):
            self.save(dict(action='accept',competences=['Проанализировать транзакцию']))
        self.assertFalse(d.path_for(self.f.a).exists())
        state=self.save(dict(action='accept',competences=['Проанализировать транзакцию'],next_difficulty='applied'))
        self.assertEqual(state['current']['next_difficulty'],'applied')
        self.assertEqual(state['current']['answers'],[]);self.assertEqual(state['summary']['confirmed_knowledge'],[])

    def test_clear_assistant_interpretations_preserve_original_across_subjects(self):
        # Supplied interpretations exercise storage/routing, not live model semantics.
        cases=[('Java','Я только начинаю изучать Java','zero','basic'),
            ('Java','Пишу на Java, знаю коллекции и ООП, но сложные задачи пока даются тяжело','basic_experience','applied'),
            ('Java Backend','Работаю Java Backend-разработчиком, хочу углубить знания многопоточности и архитектуры','confident','applied'),
            ('Дизайн интерфейсов','Уверенно проектирую интерфейсы, хочу глубже изучить типографику','confident','applied'),
            ('SQL','Могу решать сложные задачи анализа и переносить решения на новые данные','significant_experience','transfer')]
        for subject,original,level,difficulty in cases:
            with self.subTest(subject=subject,level=level):
                self.f.onboarding(dict(subject=subject,self_assessment=dict(value=level,text=original)))
                guide=d.diagnostic(self.f.a)['starting_guidance']
                self.assertEqual(guide['difficulty'],difficulty);self.assertFalse(guide['calibration'])
                self.assertEqual(guide['self_assessment']['text'],original)
                self.assertEqual(guide['subject']['value'],subject)
                self.assertEqual(guide['usage'],'new_attempt_only');self.assertFalse(d.path_for(self.f.a).exists())

    def test_legacy_free_answer_can_be_interpreted_without_losing_original(self):
        original='Знаю основы Java и ООП, решал небольшие задачи'
        self.f.onboarding(dict(self_assessment=dict(text=original)))
        saved=lt.preferences(self.f.a)['onboarding_answers']['self_assessment']
        self.f.onboarding(dict(self_assessment=dict(value='basic_experience',text=saved['text'])))
        state=self.save(dict(action='accept',competences=['Применить полиморфизм']))
        self.assertEqual(state['current']['next_difficulty'],'applied')
        self.assertEqual(lt.preferences(self.f.a)['onboarding_answers']['self_assessment']['text'],original)
        journal=json.loads(d.path_for(self.f.a).read_text())
        self.assertEqual(journal['schema'],'diagnostic_learning_v1');self.assertNotIn('evidence',journal['events'][0]['value'])

    def test_unknown_skipped_pending_and_years_alone_are_not_beginner_assignments(self):
        for record in [dict(value='unknown',text='Не знаю'),dict(text='10 лет опыта'),dict(value='confident',text='Уточняю ответ',pending=True)]:
            with self.subTest(record=record):
                self.f.onboarding(dict(self_assessment=record))
                guide=d.diagnostic(self.f.a)['starting_guidance']
                self.assertIsNone(guide['difficulty']);self.assertTrue(guide['calibration'])
        self.f.onboarding(skip=['self_assessment'])
        self.assertIsNone(d.diagnostic(self.f.a)['starting_guidance']['difficulty'])

    def test_error_and_new_cli_chat_use_actual_results_after_preferences_change(self):
        self.f.onboarding(dict(subject='Java concurrency',self_assessment='significant_experience'))
        self.save(dict(action='accept',competences=['Объяснить happens-before']))
        state=self.save(dict(action='prompt',competence='Объяснить happens-before',
            prompt='Когда другой поток увидит поле, записанное внутри synchronized?',difficulty='transfer',format='code_analysis'))
        self.save(dict(action='answer',prompt_revision=state['current']['pending_prompt']['revision'],
            answer='Всегда, даже без синхронизации',evaluation='Не названа связь освобождения и захвата одного монитора',evidence='unknown',
            next_difficulty='applied',next_competence='Объяснить happens-before',selection_reason='Проверить конкретный пробел на двух блоках'))
        self.save(dict(action='pause'));self.f.onboarding(dict(self_assessment=dict(text='Теперь не уверен в уровне')))
        path=d.path_for(self.f.a);before=path.read_bytes()
        result=subprocess.run([sys.executable,'-B',str(PLUGIN/'scripts/course.py'),'diagnostic','--course',str(self.f.a.course),
            '--profile',self.f.a.profile,'--session','new-chat'],capture_output=True,encoding='utf-8',timeout=20,env=dict(os.environ,PYTHONUTF8='1'))
        self.assertEqual(result.returncode,0,result.stderr);state=json.loads(result.stdout)
        self.assertIsNone(state['starting_guidance']['difficulty'])
        self.assertEqual(state['current']['next_difficulty'],'applied')
        self.assertEqual(state['summary']['identified_gaps'],[])
        self.assertEqual(state['summary']['unverified_areas'],['Объяснить happens-before'])
        self.assertEqual(state['summary']['confirmed_knowledge'],[]);self.assertEqual(path.read_bytes(),before)
        self.assertEqual(self.save(dict(action='resume'))['current']['next_difficulty'],'applied')

    def test_calibration_accept_cli_requires_explicit_choice_and_uses_subject_task(self):
        self.f.onboarding(dict(subject='Дизайн интерфейсов',self_assessment=dict(text='В разных темах по-разному')))
        source=self.f.write('calibration-input.json',dict(action='accept',competences=['Выбрать читаемую типографику']))
        command=[sys.executable,'-B',str(PLUGIN/'scripts/course.py'),'diagnostic','--course',str(self.f.a.course),
            '--profile',self.f.a.profile,'--session','design-chat','--update-file',str(source),
            '--expected-revision','0','--reason','Learner agreed; calibrate applied skills using two text layouts']
        prefs=lt.pref_file(self.f.a,'profile');before=prefs.read_bytes()
        failed=subprocess.run(command,capture_output=True,encoding='utf-8',timeout=20,env=dict(os.environ,PYTHONUTF8='1'))
        self.assertNotEqual(failed.returncode,0)
        self.assertIn('calibration',failed.stdout+failed.stderr)
        self.assertFalse(d.path_for(self.f.a).exists());self.assertEqual(prefs.read_bytes(),before)
        source.write_text(json.dumps(dict(action='accept',competences=['Выбрать читаемую типографику'],next_difficulty='applied')))
        accepted=subprocess.run(command,capture_output=True,encoding='utf-8',timeout=20,env=dict(os.environ,PYTHONUTF8='1'))
        self.assertEqual(accepted.returncode,0,accepted.stderr)
        state=json.loads(accepted.stdout)
        self.assertEqual(state['current']['next_difficulty'],'applied')
        self.assertEqual(state['starting_guidance']['subject']['value'],'Дизайн интерфейсов')
        state=self.save(dict(action='prompt',competence='Выбрать читаемую типографику',difficulty='applied',
            format='practical',prompt='Сравни два макета текста: 10 px без интерлиньяжа и 16 px с интерлиньяжем 24 px; объясни выбор для статьи.'))
        self.assertEqual(state['current']['pending_prompt']['difficulty'],'applied')
        self.assertEqual(state['current']['answers'],[])
        self.assertEqual(prefs.read_bytes(),before)

    def test_skill_consumes_text_and_calibration_before_accept(self):
        skill=(PLUGIN/'skills/start-learning/SKILL.md').read_text()
        ref=(PLUGIN/'references/start-learning.md').read_text()
        self.assertIn('interpret_text_or_select_subject_calibration',skill)
        self.assertIn('исходный text без изменений',skill)
        self.assertIn('difficulty=null',ref);self.assertNotIn('basic, calibration=true',ref)

if __name__=='__main__':unittest.main()
