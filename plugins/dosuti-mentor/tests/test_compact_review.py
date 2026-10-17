"""Regression coverage for full-lecture QA findings R1/R2/R3."""
import contextlib
import copy
import io
import json
from pathlib import Path
import shutil
import sys
from types import SimpleNamespace as NS
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import compact_lesson as compact
import course
import spoken_homework as spoken
from atomic_files import atomic_json
import test_compact_lesson as fixtures


class Review(unittest.TestCase):
    def setUp(self):
        self.f = fixtures.Compact();self.f.setUp();self.addCleanup(self.f.doCleanups)
        self.d = self.f.directory

    def task(self):
        value = spoken.draft(self.d, self.f.data(), 0, 60)
        value.update(instructions='Implement the test described in this QA speech.', uncertainties=['QA interpretation.'])
        value['review'] = dict(status='reviewed', notes='QA reviewer checked the source; not an audio verification.')
        return value

    def grade(self, homework):
        args = NS(**(vars(self.f.f.f.args) | dict(stage='homework_passed', details='Synthetic QA answer',
                   topic='qa-task', homework_revision=course.homework_revision(homework))))
        with contextlib.redirect_stdout(io.StringIO()): course.progress(args)

    def test_narrow_window_hides_unscoped_future_prose_and_code(self):
        memory = self.f.memory()
        memory['topics'][0].update(teacher_explanation='Later at 55 all tests passed.',
                                  additions='The future fix is complete.', uncertainties=['A future outcome.'])
        plan = compact.plan(self.d, self.f.data(), memory);compact.apply(self.d, plan)
        before = (self.d/'compact.json').read_bytes()
        result = compact.memory_context(self.d, 10, 14, set())
        topic = result['topics'][0]
        self.assertTrue(topic['selection']['overview_withheld'])
        for key in ('teacher_explanation', 'additions'): self.assertEqual(topic[key], '')
        for key in ('teacher_examples', 'code_examples', 'frame_ids', 'uncertainties'): self.assertEqual(topic[key], [])
        self.assertEqual(compact.memory_context(self.d, 0, 60)['topics'], memory['topics'])
        self.assertEqual((self.d/'compact.json').read_bytes(), before)

    def test_timed_code_visible_only_with_selected_frame(self):
        plan=self.f.plan();compact.apply(self.d,plan)
        frame=plan['memory']['topics'][0]['frame_ids'][0]
        self.assertEqual(len(compact.memory_context(self.d,14,16,{frame})['topics'][0]['code_examples']),1)
        self.assertEqual(compact.memory_context(self.d,14,16,set())['topics'][0]['code_examples'],[])

    def test_legacy_memory_reads_without_rewrite_or_invented_scope(self):
        memory=self.f.memory();memory['schema']=compact.LEGACY_MEMORY;memory.pop('review_scope')
        plan=compact.plan(self.d,self.f.data(),memory);compact.apply(self.d,plan)
        raw=(self.d/'compact.json').read_bytes()
        result=compact.memory_context(self.d,0,float('inf'))
        self.assertEqual(result['review_scope']['status'],'legacy_unknown')
        self.assertEqual(result['requested_window'],[0,60])
        self.assertEqual((self.d/'compact.json').read_bytes(),raw)
        json.dumps(result,allow_nan=False)

    def test_scope_does_not_claim_audio_or_full_screen_from_reviewed(self):
        plan=self.f.plan();compact.apply(self.d,plan)
        scope=compact.memory_context(self.d,0,60)['review_scope']
        self.assertEqual(scope['audio'],'not_checked');self.assertEqual(scope['speakers'],'not_checked')
        self.assertEqual(scope['frames'],'sampled')
        for change in (dict(checked_frame_ids=['unknown']), dict(checked_frame_ids=[]),
                       dict(transcript='not_checked'), dict(audio='verified'),dict(unresolved=[1])):
            m=self.f.memory();m['review_scope'].update(change)
            with self.assertRaises(ValueError):compact.validate_memory(m,dict(source_sha256=m['source_sha256'],
                source_revision=m['source_revision'],duration=60),self.f.context())

    def test_draft_is_read_only_and_unreviewed_or_changed_source_rejected(self):
        before=self.f.fp();draft=spoken.draft(self.d,self.f.data(),0,60)
        self.assertEqual(self.f.fp(),before)
        with self.assertRaisesRegex(ValueError,'review'):spoken.accept(self.d,draft,0)
        for mutate in (lambda t:t['source']['speech'][0].update(text='Invented'),
                       lambda t:t['source'].update(video_sha256='0'*64),
                       lambda t:t.update(instructions='')):
            value=self.task();mutate(value)
            with self.assertRaises(ValueError):spoken.accept(self.d,value,0)
        self.assertIsNone(spoken.record(self.d))

    def test_assignment_works_after_compaction_without_mutating_sealed_lesson(self):
        self.f.managed();plan=self.f.plan();compact.apply(self.d,plan)
        raw=(self.d/'compact.json').read_bytes();meta=(self.d/'lesson.json').read_bytes()
        task=self.task();result=spoken.accept(self.d,task,0);homework=result['homework']
        self.assertEqual(homework['source_kind'],'lecture_speech')
        self.assertEqual(course.homework_integrity(homework),'verified')
        self.assertTrue(homework['revision'].startswith('verbal:'))
        self.assertEqual(self.f.context()['homework']['revision'],homework['revision'])
        self.assertEqual((self.d/'compact.json').read_bytes(),raw)
        self.assertEqual((self.d/'lesson.json').read_bytes(),meta)
        self.assertEqual(self.f.context()['learner_progress'],[])
        self.grade(homework)
        self.assertEqual(self.f.context()['learner_progress'][0]['stage'],'homework_passed')

    def test_new_task_invalidates_grade_even_with_identical_instructions(self):
        task=self.task();homework=spoken.accept(self.d,task,0)['homework'];self.grade(homework)
        task['review']['notes']='Another explicit QA review.'
        fresh=spoken.accept(self.d,task,1)['homework']
        self.assertEqual(fresh['sha256'],homework['sha256']);self.assertNotEqual(fresh['revision'],homework['revision'])
        self.assertEqual(self.f.context()['learner_progress'][0]['stage'],'needs_review')
        with self.assertRaisesRegex(ValueError,'changed'):self.grade(homework)

    def test_cas_and_corrupt_text_block_updates_or_grading(self):
        task=self.task();homework=spoken.accept(self.d,task,0)['homework']
        with self.assertRaisesRegex(ValueError,'conflict'):spoken.accept(self.d,task,0)
        self.grade(homework)
        Path(homework['file']).write_bytes(b'corrupted')
        self.assertEqual(spoken.resolve(self.d,self.f.data())['status'],'needs_review')
        self.assertEqual(self.f.context()['learner_progress'][0]['stage'],'needs_review')
        with self.assertRaises(ValueError):self.grade(homework)

    def test_teacher_file_has_priority_and_original_status_is_unchanged(self):
        spoken.accept(self.d,self.task(),0)
        data=self.f.data();self.assertEqual(data['homework']['status'],'not_asked')
        task=self.d/'teacher.txt';task.write_bytes(b'Teacher task')
        data['homework']=dict(status='provided',file=str(task),sha256=course.file_hash(task))
        self.assertEqual(spoken.resolve(self.d,data),data['homework'])

    def test_registry_links_and_traversal_rejected(self):
        folder,_=spoken.location(self.d);folder.symlink_to(self.d,target_is_directory=True)
        with self.assertRaises(ValueError):spoken.accept(self.d,self.task(),0)
        folder.unlink()
        value=self.task();value['source']['extra_path']='../../outside'
        with self.assertRaises(ValueError):spoken.accept(self.d,value,0)

    def test_registry_and_assignment_survive_course_relocation(self):
        homework=spoken.accept(self.d,self.task(),0)['homework']
        root=self.d.parent.parent;moved=root.with_name('moved');shutil.move(root,moved)
        directory=moved/'lessons'/self.d.name
        result=spoken.resolve(directory,course.read(directory/'lesson.json'))
        self.assertEqual(result['revision'],homework['revision']);self.assertEqual(course.homework_integrity(result),'verified')

    def test_homework_unit_pins_provenance_and_exposes_uncertainties(self):
        import unit_sources
        task=self.task();spoken.accept(self.d,task,0)
        source=dict(kind='homework',role='teacher',notes='QA source',lesson=self.d.name,start_line=1,end_line=1)
        value,context=unit_sources.evidence(self.d.parent.parent,source,writing=True)
        unit_sources.validate(value,stored=True)
        self.assertEqual(context['source_kind'],'lecture_speech')
        self.assertEqual(context['uncertainties'],task['uncertainties'])
        task['review']['notes']='Revised source interpretation.';spoken.accept(self.d,task,1)
        with self.assertRaisesRegex(ValueError,'revision'):unit_sources.evidence(self.d.parent.parent,value)

    def test_homework_dependencies_do_not_recurse_into_code_stages(self):
        import material_dependencies
        spoken.accept(self.d,self.task(),0)
        # resolve validates only raw speech, not derived annotations depending on homework.
        with patch('code_stages.checked',side_effect=AssertionError('recursive stage resolution')):
            self.assertEqual(spoken.resolve(self.d,self.f.data())['status'],'provided')
        first=material_dependencies.collect(self.d,self.f.data(),dict(homework=True))
        task=self.task();task['review']['notes']='Updated review';spoken.accept(self.d,task,1)
        self.assertNotEqual(material_dependencies.collect(self.d,self.f.data(),dict(homework=True)),first)

    def test_oversized_record_is_rejected_before_publication(self):
        task=self.task()
        with patch.object(spoken,'MAX_JSON',1):
            with self.assertRaisesRegex(ValueError,'JSON limit'):spoken.accept(self.d,task,0)
        folder,path=spoken.location(self.d)
        self.assertFalse(path.exists());self.assertFalse((folder/'text').exists())

    def test_changed_homework_review_blocks_old_compact_plan(self):
        task=self.task();spoken.accept(self.d,task,0)
        memory=self.f.memory();memory['review']['homework']='reviewed'
        plan=compact.plan(self.d,self.f.data(),memory)
        task['review']['notes']='New QA source interpretation';spoken.accept(self.d,task,1)
        with self.assertRaisesRegex(ValueError,'homework revision changed'):compact.apply(self.d,plan)
        self.assertTrue((self.f.f.pub/'audio.wav').exists())
