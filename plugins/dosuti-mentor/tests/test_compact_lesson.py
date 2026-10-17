"""Compact media pruning must preserve evidence, user originals and continuation."""
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
import course
import compact_lesson as compact
import test_job_cleanup
from atomic_files import atomic_json
from storage_lock import file_lock


class Compact(unittest.TestCase):
    def setUp(self):
        self.f = test_job_cleanup.JobCleanup();self.f.setUp()
        self.addCleanup(self.f.tearDown)
        self.directory = self.f.f.lesson
        self.probe = patch('media_input.probe_media', return_value=dict(video=dict(end=60)))
        self.probe.start();self.addCleanup(self.probe.stop)

    def data(self): return self.f.data()
    def memory(self):
        result = compact.draft(self.directory, self.data())
        result['review'] = dict(speech='reviewed', screen='reviewed', code='not_present', homework='not_present', notes='QA review only')
        topic = result['topics'][0]
        topic['teacher_explanation'] = 'QA teacher explained a test with a code example.'
        topic['teacher_examples'] = ['QA example, not an actual learner answer.']
        from lecture_context import assemble
        frame = assemble(self.directory, self.data(), 0, 60)['frames'][0]['id']
        result['review_scope'].update(transcript='read', frames='sampled', checked_frame_ids=[frame])
        topic['frame_ids'] = [frame]
        topic['code_examples'] = [dict(language='java', code='int x = 1;', at=15, frame_id=frame, stage='QA example')]
        return result
    def plan(self): return compact.plan(self.directory, self.data(), self.memory())
    def fp(self):
        import hashlib
        return {p.relative_to(self.directory).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                for p in self.directory.rglob('*') if p.is_file()}
    def context(self): return self.f.f.context(0, 60)
    def managed(self):
        video_dir = self.directory / 'video';video_dir.mkdir()
        video = video_dir / ('video-' + 'a' * 32 + '.mp4');video.write_bytes(self.f.f.video.read_bytes())
        data = self.data();data['video'].update(source='https://example.test/course.mp4', path=str(video))
        course.save(self.directory / 'lesson.json', data)
        atomic_json(video_dir / 'download.json', dict(source=data['video']['source'], file=video.name,
                    sha256=course.file_hash(video), size=video.stat().st_size))
        return video

    def test_plan_is_read_only_and_unreviewed_draft_is_rejected(self):
        before = self.fp();draft = compact.draft(self.directory, self.data())
        self.assertEqual(before, self.fp())
        with self.assertRaisesRegex(ValueError, 'Review all'): compact.plan(self.directory, self.data(), draft)
        plan = self.plan()
        self.assertEqual(before, self.fp())
        self.assertNotIn(str(self.f.f.video), [r['file'] for r in plan['remove']])

    def test_prune_keeps_user_original_frames_text_notes_and_locks(self):
        before = self.context();plan = self.plan()
        result = compact.apply(self.directory, plan)
        self.assertEqual(result['state'], 'compact')
        self.assertTrue(self.f.f.video.exists())
        self.assertFalse((self.f.pub / 'audio.wav').exists())
        self.assertFalse((self.f.work / 'audio.wav').exists())
        self.assertFalse((self.f.work / 'frames/0.jpg').exists())
        self.assertTrue((self.f.work / 'payload-cleaned.json').exists())
        self.assertTrue((self.f.work / '.processing.lock').exists())
        self.assertEqual((self.f.work / 'visual_notes.json').read_text(), 'keep this')
        after = self.context()
        for key in ('speech', 'frames', 'visual_notes', 'teacher_code', 'code_stages', 'term_notes'):
            self.assertEqual(before[key], after[key])
        self.assertEqual(after['lecture_memory']['topics'], plan['memory']['topics'])
        self.assertEqual(compact.apply(self.directory, plan)['removed_logical_bytes'], 0)

    def test_managed_download_removed_and_context_works_without_video_or_audio(self):
        video = self.managed();plan = self.plan();compact.apply(self.directory, plan)
        self.assertFalse(video.exists())
        self.assertTrue(self.f.f.video.exists())
        self.assertEqual(self.context()['video_integrity'], 'original_unavailable_using_pinned_hash')
        self.assertTrue(self.context()['lecture_memory']['memory_available'])
        with patch('course.download_video', side_effect=AssertionError('Must not download')):
            with self.assertRaisesRegex(ValueError, 'compact'): course.checked_video(self.directory, self.data())

    def test_stale_memory_or_changed_plan_prevents_deletion(self):
        memory = self.memory();data = self.data();data['title'] = 'Changed'
        course.save(self.directory / 'lesson.json', data)
        with self.assertRaisesRegex(ValueError, 'revision'): compact.plan(self.directory, self.data(), memory)
        plan = self.plan();(self.f.pub / 'visual_notes.json').write_text('{"observations":[],"uncertainties":[{"text":"new"}]}')
        with self.assertRaises(ValueError): compact.apply(self.directory, plan)
        self.assertTrue((self.f.pub / 'audio.wav').exists())

    def test_incomplete_coverage_or_changed_original_blocks_plan(self):
        with patch('media_input.probe_media', return_value=dict(video=dict(end=90))):
            with self.assertRaisesRegex(ValueError, 'Unprepared'): self.plan()
        self.f.f.video.write_bytes(b'changed')
        with self.assertRaisesRegex(ValueError, 'Video changed'): self.plan()

    def test_incomplete_jobs_or_busy_locks_protect_payload(self):
        for lock in (self.directory / '.lesson.lock', self.f.work / '.processing.lock'):
            plan = self.plan()
            with file_lock(lock):
                with self.assertRaises(TimeoutError): compact.apply(self.directory, plan)
        data = self.data();data['processing_jobs']['base']['status'] = 'pending'
        course.save(self.directory / 'lesson.json', data)
        with self.assertRaisesRegex(ValueError, 'pending'): self.plan()
        self.assertTrue((self.f.pub / 'audio.wav').exists())

    def test_damaged_published_evidence_or_audio_blocks_cleanup(self):
        p = self.f.pub / 'frames/0.jpg';raw = p.read_bytes();p.write_bytes(b'bad')
        with self.assertRaises(ValueError): self.plan()
        p.write_bytes(raw);(self.f.pub / 'audio.wav').write_bytes(b'bad')
        with self.assertRaises(ValueError): self.plan()

    def test_bad_topics_review_frame_or_code_are_rejected(self):
        for modify in (
                lambda m: m['topics'][0].update(teacher_explanation=''),
                lambda m: m['topics'][0].update(end=50),
                lambda m: m['topics'][0].update(frame_ids=['unknown']),
                lambda m: m['review'].update(screen='not_present'),
                lambda m: m['topics'][0]['code_examples'][0].update(at=61)):
            memory = self.memory();modify(memory)
            with self.assertRaises(ValueError): compact.plan(self.directory, self.data(), memory)

    def test_checksum_tampering_and_forged_deletion_are_rejected(self):
        plan = self.plan();bad = copy.deepcopy(plan);bad['remove'][0]['size'] += 1
        with self.assertRaises(ValueError): compact.apply(self.directory, bad)
        bad = copy.deepcopy(plan);bad['remove'].append(compact.row(self.directory / 'lesson.json', self.directory))
        bad['revision'] = compact.signature({k: v for k, v in bad.items() if k != 'revision'})
        with self.assertRaises(ValueError): compact.apply(self.directory, bad)
        self.assertTrue((self.directory / 'lesson.json').exists())

    def test_interrupted_pruning_resumes_and_does_not_reconstruct_media(self):
        self.managed();plan = self.plan();real = Path.unlink
        def interrupt(path, *a, **kw):
            if path == self.f.pub / 'audio.wav': raise OSError('QA interruption')
            return real(path, *a, **kw)
        with patch.object(Path, 'unlink', interrupt):
            with self.assertRaises(OSError): compact.apply(self.directory, plan)
        self.assertEqual(compact.receipt(self.directory)['state'], 'pruning')
        self.assertTrue(self.context()['lecture_memory']['memory_available'])
        self.assertEqual(compact.apply(self.directory, plan)['state'], 'compact')

    def test_resume_rechecks_kept_assets_before_further_removal(self):
        plan = self.plan();real = Path.unlink
        def interrupt(path, *a, **kw):
            if path == self.f.pub / 'audio.wav': raise OSError('QA interruption')
            return real(path, *a, **kw)
        with patch.object(Path, 'unlink', interrupt):
            with self.assertRaises(OSError): compact.apply(self.directory, plan)
        (self.f.pub / 'frames/0.jpg').write_bytes(b'changed')
        with self.assertRaises(ValueError): compact.apply(self.directory, plan)
        self.assertTrue((self.f.pub / 'audio.wav').exists())
        with self.assertRaises(ValueError): self.context()

    def test_hardlinks_and_symlinks_are_not_pruned(self):
        import os
        path = self.f.pub / 'audio.wav';raw = path.read_bytes();path.unlink()
        path.symlink_to(self.f.work / 'audio.wav')
        with self.assertRaises(ValueError): self.plan()
        path.unlink();path.write_bytes(raw)
        outside = self.directory / 'alias';os.link(path, outside)
        with self.assertRaises(ValueError): self.plan()

    def test_cross_lesson_and_current_material_references_protect_media(self):
        video = self.managed();other = self.directory.parent / 'other';other.mkdir()
        course.save(other / 'lesson.json', dict(video=dict(source=str(video))))
        with self.assertRaisesRegex(ValueError, 'Another lesson'): self.plan()
        (other / 'lesson.json').unlink()
        data = self.data();data['future_path'] = str(self.f.pub / 'audio.wav')
        course.save(self.directory / 'lesson.json', data)
        with self.assertRaisesRegex(ValueError, 'Lesson material'): self.plan()

    def test_compact_context_survives_course_relocation(self):
        self.managed();plan = self.plan();compact.apply(self.directory, plan)
        before = self.context();moved = self.f.f.path.with_name('moved');shutil.move(self.f.f.path, moved)
        self.f.f.path = moved;self.f.f.lesson = moved / 'lessons/lesson-01';self.f.f.args.course = moved
        after = self.context()
        self.assertEqual([(s['id'], s['text'], s['start'], s['end']) for s in before['speech']],
                         [(s['id'], s['text'], s['start'], s['end']) for s in after['speech']])
        self.assertEqual(before['lecture_memory'], after['lecture_memory'])
        self.assertTrue(Path(after['frames'][0]['absolute_path']).is_file())

    def test_reopen_is_explicit_and_does_not_download(self):
        self.managed();plan = self.plan();compact.apply(self.directory, plan)
        result = compact.dispatch(NS(**vars(self.f.f.args), action='reopen'))
        self.assertFalse(result['media_restored'])
        compact.require_media_open(self.directory)
        self.assertEqual(compact.memory_context(self.directory, 0, 60)['state'], 'reopened')

    def test_progress_and_learner_cursors_are_not_in_media_plan(self):
        args = NS(**vars(self.f.f.args), stage='explained', details='QA only',topic='qa-topic',homework_revision=None)
        # Dedicated profile SQLite belongs to course, outside lesson inventory.
        with contextlib.redirect_stdout(io.StringIO()): course.progress(args)
        before = self.context()['learner_progress']
        plan = self.plan();compact.apply(self.directory, plan)
        self.assertEqual(before, self.context()['learner_progress'])

    def test_retained_code_and_video_unit_attempt_checkpoint_remain_valid(self):
        from source_integrity import seal_snapshot
        import study_units as su
        import unit_learning as ul
        folder = self.directory / 'code/snapshot';folder.mkdir(parents=True)
        (folder / 'Demo.java').write_text('class Demo { int x = 1; }')
        inventory, checksum = seal_snapshot(folder)
        data = self.data();data['code'] = dict(status='provided', snapshot=str(folder), inventory=inventory, content_sha256=checksum)
        course.save(self.directory / 'lesson.json', data)
        unit_file = self.directory.parent.parent.parent / 'unit.json'
        atomic_json(unit_file, dict(id='demo', title='Demo', summary='QA unit', objectives=['Explain'], criteria=['Explain x'],
            prerequisites=[], uncertainties=[], sources=[dict(kind='video', role='teacher', notes='QA reviewed',
                lesson=self.f.f.args.lesson, start=0, end=60)]))
        args = vars(self.f.f.args) | dict(unit_file=unit_file, expected_revision=0)
        su.unit(NS(**args));unit = su.load(self.f.f.path)['units']['demo']
        event_file = unit_file.with_name('event.json')
        atomic_json(event_file, dict(stage='solved_independently', details='QA answer only', question='What is x?',
            answer='1', hints=0, criteria=[1]))
        args = vars(self.f.f.args) | dict(unit_id='demo', session='qa-chat', expected_revision=0,
            expected_unit_sha256=unit['content_sha256'], event_file=event_file)
        ul.learn_unit(NS(**args))
        checkpoint_file = unit_file.with_name('checkpoint.json')
        atomic_json(checkpoint_file, dict(recap='QA example', next_step='Next topic', pending_question='Why?'))
        args.update(expected_revision=1, event_file=None, checkpoint_file=checkpoint_file)
        before = ul.learn_unit(NS(**args))
        memory = self.memory();memory['review']['code'] = 'reviewed';memory['review_scope']['code'] = 'reviewed'
        plan = compact.plan(self.directory, self.data(), memory);compact.apply(self.directory, plan)
        args.update(checkpoint_file=None)
        after = ul.learn_unit(NS(**args))
        self.assertTrue(after['learning']['checkpoint']['valid'])
        self.assertTrue(after['learning']['all_criteria_demonstrated'])
        self.assertEqual(before['learning']['criteria'], after['learning']['criteria'])
        self.assertEqual(before['learning']['revision'], after['learning']['revision'])
        self.assertEqual((folder / 'Demo.java').read_text(), 'class Demo { int x = 1; }')

    def test_changed_saved_additional_material_prevents_pruning(self):
        material = self.directory / 'resources/note.txt';material.parent.mkdir()
        material.write_text('changed resource')
        data = self.data();data['resources']['items'] = [dict(id='qa', kind='article', source=str(material),
            status='downloaded', origin=dict(raw_file=str(material), sha256='0'*64))]
        course.save(self.directory / 'lesson.json', data)
        with self.assertRaisesRegex(ValueError, 'additional material'): self.plan()
        self.assertTrue((self.f.pub / 'audio.wav').exists())

    def test_parent_traversal_is_rejected_before_access(self):
        with self.assertRaises(ValueError):
            compact.ordinary(self.directory / 'prepared/../../outside', self.directory)

    def test_receipt_size_includes_pretty_printing_and_envelope(self):
        plan = self.plan();memory = plan['memory']
        compact_size = len(json.dumps(plan, ensure_ascii=False).encode())
        with patch.object(compact, 'MAX_JSON', compact_size + 1):
            with self.assertRaisesRegex(ValueError, 'too large'):
                compact.plan(self.directory, self.data(), memory)
        self.assertTrue((self.f.pub / 'audio.wav').exists())
