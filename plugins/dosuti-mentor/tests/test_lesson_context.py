"""A lesson is neither a material unit nor a chat; contexts are not assessments."""
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch
import test_unit_learning as fixtures
import unit_learning as ul
import learning_intent as intent

PLUGIN=Path(__file__).resolve().parents[1]

class LessonContext(unittest.TestCase):
    def setUp(self):
        self.f=fixtures.UnitLearning();self.f.setUp();self.addCleanup(self.f.tearDown)

    def value(self,kind='new_topic'):
        return dict(kind=kind,topic='Java Map replacement',goal='Explain replacement of an equal key',
                    plan=['Trace a duplicate key','Solve a fresh map task'])

    def create(self,value=None,revision=None,session='a'):
        state=self.f.read();p=self.f.f.f.directory/'lesson-context.json'
        p.write_text(json.dumps(value or self.value()))
        return ul.learn_unit(self.f.args(lesson_context_file=p,session=session,
            expected_revision=state['learning']['revision'] if revision is None else revision,
            expected_unit_sha256=state['unit']['content_sha256']))

    def context(self,state):return state['learning']['lesson_context']['event']

    def test_five_types_are_unique_and_do_not_assess_or_accept_plan(self):
        ids=[]
        for kind in ('new_topic','continuation','review','practice','gap_work'):
            state=self.create(self.value(kind));event=self.context(state);ids.append(event['value']['id'])
            self.assertEqual(event['value']['kind'],kind)
            self.assertEqual(event['unit'],'maps');self.assertEqual(event['session'],'a')
            self.assertNotIn(event['value']['id'],['maps','a'])
            self.assertEqual(event['value']['plan'],self.value()['plan'])
            self.assertEqual(state['learning']['attempt_count'],0)
            self.assertEqual(state['learning']['explanation_count'],0)
            self.assertFalse(state['learning']['all_criteria_demonstrated'])
            self.assertIsNone(state['learning_intent']['plan'])
        self.assertEqual(len(set(ids)),5)
        state=self.f.read(session='b')
        self.assertIsNone(state['learning']['lesson_context'])
        self.assertEqual(len(state['learning']['lesson_contexts']),5)
        self.assertEqual(state['learning_progress']['studied_units'],[])

    def test_cross_process_restore_and_checkpoint_binding(self):
        created=self.context(self.create());identity=created['value']['id']
        checkpoint=dict(recap='Discussed replacement',next_step='Trace another key',pending_question='What is size?')
        self.f.save(checkpoint,checkpoint=True,session='a',lesson_id=identity)
        before=self.f.path.read_bytes()
        process=subprocess.run([sys.executable,'-B',str(PLUGIN/'scripts/course.py'),'learn-unit',
            '--course',str(self.f.args().course),'--profile','default','--unit-id','maps',
            '--session','another-chat','--lesson-id',identity],capture_output=True,text=True,timeout=20,
            env=dict(os.environ,PYTHONDONTWRITEBYTECODE='1'))
        self.assertEqual(process.returncode,0,process.stderr)
        state=json.loads(process.stdout)['learning']
        self.assertEqual(state['lesson_context']['event'],created)
        self.assertTrue(state['lesson_context']['valid'])
        self.assertEqual(state['lesson_checkpoint']['event']['value']['lesson_id'],identity)
        self.assertEqual(state['lesson_checkpoint']['event']['value']['pending_question'],'What is size?')
        self.assertFalse(state['checkpoint']['valid'])  # Legacy chat-local projection stays chat-local.
        self.assertEqual(self.f.path.read_bytes(),before)

    def test_multiple_lessons_one_unit_keep_separate_checkpoints(self):
        first=self.context(self.create())['value']['id']
        self.f.save(dict(recap='First',next_step='First task',pending_question=None),checkpoint=True,lesson_id=first)
        second=self.context(self.create(self.value('practice')))['value']['id']
        self.f.save(dict(recap='Second',next_step='Second task',pending_question=None),checkpoint=True,lesson_id=second)
        for identity,recap in ((first,'First'),(second,'Second')):
            state=self.f.read(session='b',lesson_id=identity)['learning']
            self.assertEqual(state['lesson_checkpoint']['event']['value']['recap'],recap)
        self.assertNotEqual(first,second)

    def test_snapshot_uses_goal_roadmap_and_existing_criteria_without_mutating_them(self):
        args=self.f.args();intent.commit(self.f.args(expected_revision=0,reason='QA goal'),
            dict(goal=dict(description='Build a Java backend')))
        intent.commit(self.f.args(expected_revision=1,reason='QA proposal'),
            dict(proposal=dict(summary='Backend path',steps=['Maps','HTTP'])))
        intent.commit(self.f.args(expected_revision=2,reason='QA explicit acceptance'),dict(accept_proposal=True))
        self.f.save()
        before=intent.path_for(args).read_bytes();state=self.create(self.value('review'))
        basis=self.context(state)['value']['basis']
        self.assertEqual(basis['learning_goal']['description'],'Build a Java backend')
        self.assertEqual(basis['plan_revision'],3)
        self.assertEqual(basis['unit_learning_revision'],1)
        self.assertEqual(basis['diagnostic_revision'],0)
        self.assertEqual(basis['criteria'],{'1':'solved_independently'})
        self.assertEqual(intent.path_for(args).read_bytes(),before)
        self.assertEqual(state['learning']['attempt_count'],1)
        # A topic outside the route is still allowed; the accepted route is only an input.
        state=self.create(self.value('practice')|dict(topic='Maps outside the current route'))
        self.assertEqual(intent.path_for(args).read_bytes(),before)
        self.assertEqual(self.context(state)['value']['basis']['plan_revision'],3)

    def test_old_v1_v2_and_absent_journal_read_without_rewriting_or_invented_context(self):
        for schema in (None,'unit_learning_v1','unit_learning_v2'):
            with self.subTest(schema=schema):
                if schema is not None:
                    self.f.save();raw=json.loads(self.f.path.read_text());raw['schema']=schema
                    self.f.path.write_text(json.dumps(raw))
                before=self.f.path.read_bytes() if self.f.path.exists() else None
                state=self.f.read()['learning']
                self.assertTrue(state['valid']);self.assertIsNone(state['lesson_context'])
                self.assertEqual(state['lesson_contexts'],[])
                self.assertEqual(self.f.path.read_bytes() if self.f.path.exists() else None,before)
        state=self.create();self.assertTrue(state['learning']['valid'])
        self.assertEqual(state['learning']['attempt_count'],2)

    def test_invalid_input_and_stale_cas_do_not_write(self):
        self.create();before=self.f.path.read_bytes()
        for value in (self.value('unknown'),self.value()|dict(plan=[]),self.value()|dict(goal=''),
                      self.value()|dict(id='invented'),self.value()|dict(plan=['Same','Same'])):
            with self.subTest(value=value),self.assertRaises(ValueError):self.create(value)
            self.assertEqual(self.f.path.read_bytes(),before)
        with self.assertRaises(ValueError):self.create(revision=0)
        self.assertEqual(self.f.path.read_bytes(),before)
        with self.assertRaises(ValueError):
            self.f.save(dict(recap='Wrong',next_step='Wrong',pending_question=None),checkpoint=True,lesson_id='missing')
        self.assertEqual(self.f.path.read_bytes(),before)

    def test_concurrent_creations_one_cas_winner(self):
        state=self.f.read();root=self.f.f.f.directory
        paths=[]
        for i in range(2):
            p=root/('context-'+str(i)+'.json');p.write_text(json.dumps(self.value()));paths.append(p)
        def save(p):
            try:
                ul.learn_unit(self.f.args(lesson_context_file=p,session='a',expected_revision=0,
                    expected_unit_sha256=state['unit']['content_sha256']));return True
            except ValueError:return False
        with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(save,paths))
        self.assertEqual(sorted(results),[False,True])
        self.assertEqual(len(self.f.read()['learning']['lesson_contexts']),1)

    def test_cli_creation_and_cross_unit_profile_isolation(self):
        state=self.f.read();path=self.f.f.f.directory/'cli-context.json'
        path.write_text(json.dumps(self.value('gap_work')))
        process=subprocess.run([sys.executable,'-B',str(PLUGIN/'scripts/course.py'),'learn-unit',
            '--course',str(self.f.args().course),'--profile','default','--unit-id','maps','--session','cli-chat',
            '--lesson-context-file',str(path),'--expected-revision','0',
            '--expected-unit-sha256',state['unit']['content_sha256']],capture_output=True,text=True,timeout=20,
            env=dict(os.environ,PYTHONDONTWRITEBYTECODE='1'))
        self.assertEqual(process.returncode,0,process.stderr)
        identity=self.context(json.loads(process.stdout))['value']['id']
        self.f.f.save(self.f.f.card('outside',title='An unrelated topic'))
        from types import SimpleNamespace
        args=SimpleNamespace(**(vars(self.f.args())|dict(unit_id='outside',lesson_id=identity)))
        before=self.f.path.read_bytes()
        with self.assertRaisesRegex(ValueError,'Unknown lesson ID'):ul.learn_unit(args)
        # Selecting a maps lesson must not contaminate the progress read of another unit.
        self.assertTrue(self.f.read(lesson_id=identity)['learning_progress']['valid'])
        self.assertEqual(self.f.read(profile='other')['learning']['lesson_contexts'],[])
        self.assertEqual(self.f.path.read_bytes(),before)

    def test_duplicate_id_and_missing_chat_cannot_append(self):
        from types import SimpleNamespace
        with patch('uuid.uuid4',return_value=SimpleNamespace(hex='a'*32)):
            self.create();before=self.f.path.read_bytes()
            with self.assertRaises(ValueError):self.create()
        self.assertEqual(self.f.path.read_bytes(),before)
        with self.assertRaises(ValueError):self.create(session=None)
        self.assertEqual(self.f.path.read_bytes(),before)

    def test_off_route_unit_context_does_not_change_accepted_route(self):
        args=self.f.args()
        intent.commit(self.f.args(expected_revision=0,reason='QA goal'),dict(goal=dict(description='Learn Java maps')))
        intent.commit(self.f.args(expected_revision=1,reason='QA proposal'),
            dict(proposal=dict(summary='Maps only',steps=['Maps'],step_bases=[dict(unit_ids=['maps'])])))
        intent.commit(self.f.args(expected_revision=2,reason='QA consent'),dict(accept_proposal=True))
        self.f.f.save(self.f.f.card('outside',title='Extra topic'))
        from types import SimpleNamespace
        read_args=SimpleNamespace(**(vars(args)|dict(unit_id='outside')))
        state=ul.learn_unit(read_args);p=self.f.f.f.directory/'outside-context.json'
        p.write_text(json.dumps(self.value('new_topic')|dict(topic='Outside the accepted route')))
        before=intent.path_for(args).read_bytes()
        state=ul.learn_unit(SimpleNamespace(**(vars(read_args)|dict(session='outside-chat',lesson_context_file=p,
            expected_revision=0,expected_unit_sha256=state['unit']['content_sha256']))))
        self.assertEqual(self.context(state)['unit'],'outside')
        self.assertEqual(intent.path_for(args).read_bytes(),before)
        self.assertEqual(state['learning_progress']['completed_steps'],[])

    def test_contexts_survive_segmented_storage_and_source_changes(self):
        ids=[self.context(self.create())['value']['id'] for _ in range(5)]
        import learning_segments
        journal=json.loads(self.f.path.read_text())
        # Force the segmentation writer; read under the real segment-size limit.
        with patch('learning_segments.LIMIT',3000):learning_segments.write(self.f.path,journal)
        self.assertEqual(json.loads(self.f.path.read_text())['schema'],'unit_learning_v3')
        self.assertEqual(len(self.f.read()['learning']['lesson_contexts']),5)
        self.f.f.save(self.f.f.card(summary='Changed source description'))
        state=self.f.read(lesson_id=ids[0])['learning']
        self.assertFalse(state['lesson_context']['valid'])
        self.assertEqual(state['lesson_context']['event']['value']['id'],ids[0])
        self.assertFalse(state['all_criteria_demonstrated'])

if __name__=='__main__':unittest.main()
