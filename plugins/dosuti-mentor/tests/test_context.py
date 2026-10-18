"""Context must preserve evidence across fragments and expose missing coverage."""
import contextlib
import io
import json
import math
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace as NS
import unittest

SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'
sys.path.insert(0, str(SCRIPTS))
import course
import tutor_packet


class ContextRegression(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.directory = Path(self.temp.name).resolve()
        self.path = self.directory / 'course'
        self.video = self.directory / 'lecture.mp4'
        self.video.write_bytes(b'Fixture video identity')
        self.args = NS(course=self.path, lesson='lesson-01', profile='default',
                       title='Context', video=str(self.video), order=1)
        with contextlib.redirect_stdout(io.StringIO()):
            course.init(self.args)
            course.add_lesson(self.args)
        self.lesson = self.path / 'lessons/lesson-01'

    def tearDown(self):
        self.temp.cleanup()

    def add(self, name, start, end, speech=(), frames=(), notes=None, source='lecture.mp4'):
        folder = self.lesson / 'prepared' / name
        (folder / 'frames').mkdir(parents=True)
        frame_list = []
        for i, item in enumerate(frames):
            seconds, content = item
            relative = f'frames/{i}.jpg'
            (folder / relative).write_bytes(content)
            frame_list.append(dict(approx_seconds=seconds, file=relative))
        course.save(folder / 'manifest.json', dict(source=source, start=start,
                    source_sha256=course.file_hash(self.video),
                    actual_duration=end-start, requested_duration=end-start, frames=frame_list))
        course.save(folder / 'transcript.json',
                    [dict(start=s, end=e, text=t) for s, e, t in speech])
        from source_integrity import evidence_hashes
        manifest = course.read(folder / 'manifest.json')
        manifest['evidence_sha256'] = evidence_hashes(folder, manifest, transcript=True)
        course.save(folder / 'manifest.json', manifest)
        tutor_packet.build_packet(folder)
        if notes is not None:
            course.save(folder / 'visual_notes.json', notes)
        data = course.read(self.lesson / 'lesson.json')
        data['prepared_fragments'].append(dict(folder=str(folder),
            source_sha256=course.file_hash(self.video),
            packet_sha256=course.file_hash(folder / 'tutor_packet.json'),
            coverage=dict(start=start, end=end), status='prepared_needs_review'))
        course.save(self.lesson / 'lesson.json', data)
        return folder

    def save_packet(self, folder, packet):
        course.save(folder / 'tutor_packet.json', packet)
        data = course.read(self.lesson / 'lesson.json')
        for fragment in data['prepared_fragments']:
            if fragment['folder'] == str(folder):
                fragment['packet_sha256'] = course.file_hash(folder / 'tutor_packet.json')
        course.save(self.lesson / 'lesson.json', data)

    def context(self, time, window):
        args = NS(**vars(self.args), time=time, window=window)
        stream = io.StringIO()
        with contextlib.redirect_stdout(stream):
            course.context(args)
        return json.loads(stream.getvalue())

    def test_asr_quality_reaches_teaching_context_and_markdown(self):
        folder = self.add('loop', 0, 60, [(i * 3, i * 3 + 3, 'Ничего.') for i in range(15)])
        result = self.context(0, 60)
        self.assertIn('asr_mass_repetition_requires_review', result['warnings'])
        summary=result['quality_summary']
        self.assertEqual(summary['audio']['status'],'requires_review')
        self.assertIn('asr_mass_repetition_requires_review',summary['audio']['fragments'][0]['warnings'])
        self.assertFalse(summary['meaning_verified'])
        self.assertIn('asr_mass_repetition_requires_review', (folder / 'tutor_context.md').read_text())

    def test_legacy_packet_is_diagnosed_without_rewriting(self):
        folder = self.add('loop', 0, 60, [(i * 3, i * 3 + 3, 'Ничего.') for i in range(15)])
        packet = course.read(folder / 'tutor_packet.json')
        packet.pop('asr_quality')
        self.save_packet(folder, packet)
        before = (folder / 'tutor_packet.json').read_bytes()
        self.assertIn('asr_mass_repetition_requires_review', self.context(0, 60)['warnings'])
        self.assertEqual((folder / 'tutor_packet.json').read_bytes(), before)

    def test_adjacent_fragments_include_continuation(self):
        self.add('a', 0, 60, [(50, 60, 'First')])
        self.add('b', 60, 120, [(61, 80, 'Continuation')])
        result = self.context(50, 40)
        self.assertEqual([r['text'] for r in result['speech']], ['First', 'Continuation'])
        self.assertEqual(result['gaps'], [])
        self.assertTrue(result['coverage']['complete'])

    def test_new_short_overlay_does_not_hide_long_remainder(self):
        self.add('long', 0, 120, [(80, 100, 'Remainder')])
        self.add('short', 0, 30, [(12, 20, 'New short')])
        result = self.context(10, 100)
        self.assertEqual([r['text'] for r in result['speech']], ['New short', 'Remainder'])
        self.assertEqual(result['coverage']['end'], 110)
        self.assertTrue(result['coverage']['complete'])

    def test_identical_events_deduplicate_without_losing_provenance(self):
        for name in ['a', 'b']:
            self.add(name, 0, 60, [(10, 20, 'Same')], [(15, b'image')])
        result = self.context(0, 30)
        self.assertEqual(len(result['speech']), 1)
        self.assertEqual(len(result['frames']), 1)
        for event in result['speech'] + result['frames']:
            self.assertEqual(len(event['provenance']), 2)
        self.assertEqual(result['speech_overlaps'], [])

    def test_conflicting_recognitions_are_retained_and_flagged(self):
        self.add('a', 0, 60, [(10, 20, 'Has duplicates')])
        self.add('b', 0, 60, [(11, 21, 'Has no duplicates')])
        result = self.context(0, 30)
        self.assertEqual(len(result['speech']), 2)
        self.assertEqual(len(result['speech_overlaps']), 1)
        self.assertIn('overlapping_transcripts_need_review', result['warnings'])

    def test_repeated_words_at_different_times_remain(self):
        self.add('a', 0, 60, [(1, 5, 'Repeat'), (20, 25, 'Repeat')])
        self.assertEqual(len(self.context(0, 30)['speech']), 2)

    def test_gaps_are_explicit_and_silence_is_not_a_gap(self):
        self.add('a', 0, 30)
        self.add('b', 40, 70)
        result = self.context(10, 50)
        self.assertEqual(result['gaps'], [dict(start=30, end=40)])
        self.assertEqual(result['coverage']['ranges'],
                         [dict(start=10, end=30), dict(start=40, end=60)])
        self.assertFalse(result['coverage']['complete'])
        self.assertEqual(result['speech'], [])
        self.assertIn('unprepared_intervals', result['warnings'])

    def test_window_start_in_gap_still_returns_prepared_continuation(self):
        self.add('a', 40, 80, [(42, 48, 'Later')])
        result = self.context(35, 20)
        self.assertEqual(result['gaps'], [dict(start=35, end=40)])
        self.assertEqual(result['speech'][0]['text'], 'Later')

    def test_fully_unprepared_window_returns_only_gap(self):
        result = self.context(15, 20)
        self.assertEqual(result['speech'], [])
        self.assertEqual(result['frames'], [])
        self.assertEqual(result['gaps'], [dict(start=15, end=35)])
        self.assertFalse(result['coverage']['complete'])

    def test_preceding_frame_is_included_but_not_claimed_current(self):
        self.add('a', 0, 60, frames=[(5, b'image')])
        result = self.context(6, 2)
        frame = result['frames'][0]
        self.assertEqual(frame['approx_seconds'], 5)
        self.assertEqual(frame['selection'], 'preceding_sample')
        self.assertFalse(frame['screen_state_confirmed'])
        self.assertEqual(frame['age_seconds'], 1)

    def test_preceding_frame_at_fragment_boundary(self):
        self.add('a', 0, 60, frames=[(55, b'image')])
        self.add('b', 60, 120, frames=[(65, b'next')])
        result = self.context(60, 10)
        self.assertEqual([f['approx_seconds'] for f in result['frames']], [55, 65])

    def test_no_preceding_frame_is_carried_across_unprepared_gap(self):
        self.add('a', 0, 30, frames=[(29, b'old')])
        self.add('b', 40, 80, frames=[(45, b'new')])
        result = self.context(35, 20)
        self.assertEqual([f['approx_seconds'] for f in result['frames']], [45])

    def test_frames_at_same_time_with_different_content_are_both_retained(self):
        self.add('a', 0, 60, frames=[(15, b'a')])
        self.add('b', 0, 60, frames=[(15, b'b')])
        result = self.context(0, 30)
        self.assertEqual(len(result['frames']), 2)
        self.assertEqual(len({f['id'] for f in result['frames']}), 2)
        self.assertIn('different_frames_at_same_timestamp', result['warnings'])

    def test_notes_from_both_fragments_have_unambiguous_frame_ids(self):
        for name, start, end, seconds in [('a', 0, 60, 55), ('b', 60, 120, 65)]:
            self.add(name, start, end, frames=[(seconds, name.encode())], notes={
                'observations': [dict(topic=name, range=[start, end], evidence_frames=['f00001'],
                                      code=dict(frame_id='f00001', text=name))],
                'uncertainties': []})
        result = self.context(50, 30)
        frames = {f['id'] for f in result['frames']}
        observations = result['visual_notes']['observations']
        self.assertEqual(len(observations), 2)
        for item in observations:
            self.assertIn(item['code']['frame_id'], frames)
            self.assertEqual(item['evidence_frames'], [item['code']['frame_id']])
            self.assertTrue(Path(item['provenance']['notes_file']).is_file())

    def test_source_and_local_ids_are_exposed(self):
        self.add('a', 0, 60, [(1, 5, 'First')], source='first-name.mp4')
        self.add('b', 60, 120, [(61, 65, 'Second')], source='second-name.mp4')
        result = self.context(0, 120)
        self.assertEqual(result['source'], str(self.video))
        self.assertEqual({s['source'] for s in result['sources']},
                         {'first-name.mp4', 'second-name.mp4'})
        self.assertEqual(len({r['id'] for r in result['speech']}), 2)
        for row in result['speech']:
            evidence = row['provenance'][0]
            self.assertEqual(evidence['local_id'], 's00001')
            self.assertTrue(Path(evidence['packet_file']).is_file())
        self.assertEqual({s['identity_verification'] for s in result['sources']},
                         {'pinned_source_hash_matches'})

    def test_exclusive_end_and_unrelated_fragment_not_loaded(self):
        self.add('a', 0, 60, [(10, 20, 'Selected'), (20, 25, 'Outside')])
        outside = self.add('b', 60, 120, [(61, 65, 'Other')])
        (outside / 'tutor_packet.json').write_text('bad json')
        result = self.context(10, 10)
        self.assertEqual([r['text'] for r in result['speech']], ['Selected'])

    def test_invalid_requests_are_rejected(self):
        self.add('a', 0, 60)
        for time, window in [(-1, 10), (0, 0), (0, -1), (math.nan, 10),
                             (0, math.inf), (math.inf, 1), (1e308, 1e308)]:
            with self.subTest(time=time, window=window), self.assertRaises(ValueError):
                self.context(time, window)

    def test_packet_and_registry_coverage_must_match(self):
        folder = self.add('a', 0, 60)
        packet = course.read(folder / 'tutor_packet.json')
        packet['coverage']['end'] = 30
        self.save_packet(folder, packet)
        with self.assertRaisesRegex(ValueError, 'coverage'):
            self.context(0, 60)

    def test_invalid_speech_timestamps_fail_clearly(self):
        folder = self.add('a', 0, 60, [(1, 5, 'Okay')])
        for start, end in [(999, -5), (0, math.inf), (-1, 5), (10, 1)]:
            packet = course.read(folder / 'tutor_packet.json')
            packet['speech'][0].update(start=start, end=end)
            self.save_packet(folder, packet)
            with self.subTest(start=start, end=end), self.assertRaises(ValueError):
                self.context(0, 60)

    def test_missing_selected_frame_is_reported(self):
        folder = self.add('a', 0, 60, frames=[(5, b'image')])
        (folder / 'frames/0.jpg').unlink()
        with self.assertRaisesRegex(ValueError, 'frame'):
            self.context(0, 10)

    def test_missing_older_unselected_frame_does_not_hide_nearest_evidence(self):
        folder = self.add('a', 0, 60, frames=[(5, b'old'), (15, b'nearest')])
        (folder / 'frames/0.jpg').unlink()
        result = self.context(16, 2)
        self.assertEqual([f['approx_seconds'] for f in result['frames']], [15])

    def test_speech_crossing_window_boundary_keeps_original_and_selected_times(self):
        self.add('a', 0, 60, [(5, 30, 'Complete original phrase')])
        result = self.context(10, 10)
        row = result['speech'][0]
        self.assertEqual((row['start'], row['end']), (5, 30))
        self.assertEqual(row['selected_interval'], dict(start=10, end=20))
        self.assertEqual(row['text'], 'Complete original phrase')

    def test_duplicate_local_ids_are_rejected(self):
        folder = self.add('a', 0, 60, [(1, 5, 'One'), (10, 15, 'Two')])
        packet = course.read(folder / 'tutor_packet.json')
        packet['speech'][1]['id'] = packet['speech'][0]['id']
        self.save_packet(folder, packet)
        with self.assertRaisesRegex(ValueError, 'unique'):
            self.context(0, 30)

    def test_coverage_merges_out_of_order_and_transitive_overlaps(self):
        for name, start, end in [('a', 0, 30), ('c', 50, 70), ('b', 25, 50), ('nested', 28, 29)]:
            self.add(name, start, end)
        result = self.context(10, 80)
        self.assertEqual(result['coverage']['ranges'], [dict(start=10, end=70)])
        self.assertEqual(result['gaps'], [dict(start=70, end=90)])

    def test_overlap_report_limit_does_not_truncate_original_speech(self):
        for i in range(16):
            self.add(str(i), 0, 30, [(10, 20, f'Variant {i}')])
        result = self.context(0, 30)
        self.assertEqual(len(result['speech']), 16)
        self.assertEqual(len(result['speech_overlaps']), 100)
        self.assertTrue(result['speech_overlap_details_truncated'])

    def test_deduplicated_frame_notes_reference_the_returned_id(self):
        for name in ['a', 'b']:
            self.add(name, 0, 60, frames=[(5, b'same')], notes=dict(
                observations=[dict(range=[0, 10], evidence_frames=['f00001'])], uncertainties=[]))
        result = self.context(0, 10)
        self.assertEqual(len(result['frames']), 1)
        for note in result['visual_notes']['observations']:
            self.assertEqual(note['evidence_frames'], [result['frames'][0]['id']])

    def test_frame_path_cannot_escape_evidence_folder(self):
        folder = self.add('a', 0, 60, frames=[(5, b'image')])
        outside = self.directory / 'other.jpg'
        outside.write_bytes(b'other')
        packet = course.read(folder / 'tutor_packet.json')
        packet['frames'][0]['file'] = str(outside)
        self.save_packet(folder, packet)
        with self.assertRaisesRegex(ValueError, 'outside'):
            self.context(0, 10)


if __name__ == '__main__':
    unittest.main()
