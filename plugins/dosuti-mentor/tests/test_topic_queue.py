"""Persistent collection is separate from existing evidence-based learning."""
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import course
import test_unit_learning

WORKER = '''
import json, sys
from pathlib import Path
from types import SimpleNamespace as NS
sys.path.insert(0, sys.argv[1])
import topic_queue, atomic_files
mode, root, source, revision = sys.argv[2:]
def gate():
    print('ready', flush=True)
    if sys.stdin.readline().strip() != 'go': raise RuntimeError('Barrier cancelled')
if mode == 'staged':
    original = atomic_files.os.fsync
    def synced(fd):
        original(fd); gate()
    atomic_files.os.fsync = synced
else: gate()
try:
    print(json.dumps(topic_queue.topic_queue(NS(course=Path(root),profile='default',
        event_file=Path(source),expected_revision=int(revision)))), flush=True)
except (ValueError,OSError) as error:
    print(str(error),file=sys.stderr);sys.exit(1)
'''


class TopicQueue(unittest.TestCase):
    def setUp(self):
        self.fixture = test_unit_learning.UnitLearning(); self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.root = self.fixture.f.f.path
        self.input = self.root.parent / 'queue-event.json'
        self.counter = 0

    def args(self, **kw): return self.fixture.args(**kw)
    def read(self, **kw):
        from topic_queue import topic_queue
        return topic_queue(self.args(**kw))
    def save(self, action, **kw):
        self.counter += 1
        event = dict(request_id='r' + str(self.counter), action=action, **kw)
        self.input.write_text(json.dumps(event), encoding='utf-8')
        return self.read(event_file=self.input, expected_revision=self.read()['revision'])
    def add(self, title='Maps', source='https://example.org/maps'):
        value = self.save('add'); identity = value['draft']['id']
        for step, answer in [('title', title), ('source', source), ('homework', None), ('additional', None)]:
            self.assertEqual(value['next_question']['step'], step)
            value = self.save('answer', topic_id=identity, step=step, answer=answer)
        return identity, value

    def test_old_course_read_does_not_write_or_import_materials(self):
        before = {str(p.relative_to(self.root)): p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        value = self.read(); self.assertEqual(value['topics'], [])
        self.assertEqual(value['mode'], 'idle')
        self.assertEqual({str(p.relative_to(self.root)): p.read_bytes() for p in self.root.rglob('*') if p.is_file()}, before)

    def test_one_topic_waits_for_choice_and_explicit_start(self):
        identity, value = self.add()
        self.assertEqual(value['mode'], 'choice')
        self.assertEqual(value['message'], 'Тема сохранена. Начинаем изучение или добавим ещё одну тему?')
        self.assertEqual(value['topics'][0]['status'], 'waiting')
        self.assertIsNone(value['active_topic_id'])
        value = self.save('start', topic_id=identity)
        self.assertEqual(value['topics'][0]['status'], 'studying')
        self.assertEqual(value['active_topic_id'], identity)
        self.assertEqual(value['next_action'], 'prepare_selected_sources')

    def test_three_topics_repeat_all_questions_and_do_not_mix_sources(self):
        identities = []
        for index in range(3):
            identity, value = self.add('Same title', 'https://example.org/' + str(index))
            identities.append(identity)
            selected = self.read(topic_id=identity)['selected_topic']
            self.assertEqual(selected['answers']['source'], 'https://example.org/' + str(index))
            self.assertIsNone(selected['answers']['homework'])
            self.assertIsNone(selected['answers']['additional'])
        self.assertEqual(len(set(identities)), 3)
        self.assertEqual([t['id'] for t in value['topics']], identities)
        self.assertEqual(value['next_waiting_topic_id'], identities[0])

    def test_questions_cannot_be_skipped_or_answered_for_another_topic(self):
        identity = self.save('add')['draft']['id']
        with self.assertRaises(ValueError): self.save('answer', topic_id=identity, step='homework', answer=None)
        with self.assertRaises(ValueError): self.save('answer', topic_id='other', step='title', answer='Wrong')
        with self.assertRaises(ValueError): self.save('start', topic_id=identity)
        self.assertEqual(self.read()['next_question']['step'], 'title')

    def test_every_answer_and_next_question_survive_an_independent_process(self):
        value = self.save('add'); identity = value['draft']['id']
        for step, answer in [('title', 'Java'), ('source', 'https://example.org/java'), ('homework', None), ('additional', None)]:
            child = self.child()
            self.assertEqual(child['next_question']['step'], step)
            value = self.save('answer', topic_id=identity, step=step, answer=answer)
            reloaded = self.child()
            self.assertEqual(reloaded['revision'], value['revision'])
            self.assertEqual(reloaded['selected_topic']['answers'][step], answer)
        self.assertEqual(self.child()['mode'], 'choice')

    def child(self):
        code = ('import sys,json; from pathlib import Path; from types import SimpleNamespace as N; '
                'from topic_queue import topic_queue; print(json.dumps(topic_queue(N('
                'course=Path(sys.argv[1]),profile="default",session="new-chat"))))')
        done = subprocess.run([sys.executable, '-B', '-c', code, str(self.root)], capture_output=True,
                              text=True, timeout=15, env=os.environ | {'PYTHONPATH': str(Path(course.__file__).parent)})
        self.assertEqual(done.returncode, 0, done.stderr)
        return json.loads(done.stdout)

    def test_binding_reuses_real_learning_and_checkpoint_without_new_progress(self):
        identity, _ = self.add(source=str(self.fixture.f.text))
        unit = self.fixture.f.get()
        self.save('bind', topic_id=identity, unit_id='maps', unit_sha256=unit['content_sha256'],
                  source_review='QA checked the same source text and empty homework/additional answers')
        self.save('start', topic_id=identity)
        self.fixture.save(self.fixture.event(stage='practice_with_help', hints=1))
        self.assertEqual(self.read()['topics'][0]['status'], 'studying')
        self.fixture.save()
        checkpoint = dict(recap='Actual maps exercise', next_step='Practice iteration', pending_question='What about null?')
        self.fixture.save(checkpoint, checkpoint=True)
        view = self.child()
        self.assertEqual(view['topics'][0]['status'], 'completed')
        self.assertEqual(view['continuations'][0]['checkpoint'], checkpoint)
        before = self.fixture.path.read_bytes()
        self.read(); self.assertEqual(self.fixture.path.read_bytes(), before)

    def test_add_later_preserves_learning_and_requires_another_full_collection(self):
        identity, _ = self.add(); self.save('start', topic_id=identity)
        self.fixture.save()
        before = self.fixture.path.read_bytes()
        value = self.save('add')
        self.assertEqual(value['next_question']['step'], 'title')
        self.assertEqual(value['active_topic_id'], identity)
        self.assertEqual(self.fixture.path.read_bytes(), before)
        new_id = value['draft']['id']
        for step, answer in [('title', 'Added later'), ('source', 'https://example.org/later'), ('homework', None), ('additional', None)]:
            value = self.save('answer', topic_id=new_id, step=step, answer=answer)
        self.assertEqual(value['selected_topic']['id'], new_id)
        self.assertEqual(value['active_topic_id'], identity)
        self.assertEqual(self.fixture.path.read_bytes(), before)

    def test_tenth_topic_has_the_same_complete_sequence_and_list_paginates(self):
        for number in range(10): self.add('Topic ' + str(number))
        value = self.read(limit=3, queue_offset=3)
        self.assertEqual(value['total_topics'], 10); self.assertTrue(value['truncated'])
        self.assertEqual([t['title'] for t in value['topics']], ['Topic 3', 'Topic 4', 'Topic 5'])
        self.assertEqual(self.save('add')['next_question']['step'], 'title')

    def test_course_and_learner_isolation(self):
        self.add()
        self.assertEqual(self.read(profile='bob')['topics'], [])
        other = self.root.parent / 'other-course'; other.mkdir()
        (other / 'course.json').write_bytes((self.root / 'course.json').read_bytes())
        self.assertEqual(self.read(course=other)['topics'], [])
        self.assertFalse((other / 'learners').exists())

    def test_retry_is_idempotent_even_with_old_revision_but_id_cannot_be_reused(self):
        self.save('add'); path = self.root / 'learners/default/topic-queue.json'; before = path.read_bytes()
        value = self.read(event_file=self.input, expected_revision=0)
        self.assertTrue(value['replayed']); self.assertEqual(path.read_bytes(), before)
        self.assertEqual(self.child()['revision'], 1)
        event = json.loads(self.input.read_text()); event['extra'] = 'different payload'
        self.input.write_text(json.dumps(event))
        with self.assertRaisesRegex(ValueError, 'different data'):
            self.read(event_file=self.input, expected_revision=0)
        self.assertEqual(path.read_bytes(), before)

    def test_disk_failure_preserves_previous_answers_and_retry_succeeds(self):
        import topic_queue
        value = self.save('add'); identity = value['draft']['id']
        path = self.root / 'learners/default/topic-queue.json'; before = path.read_bytes()
        with patch.object(topic_queue, 'atomic_json', side_effect=OSError('QA disk failure')):
            with self.assertRaises(OSError): self.save('answer', topic_id=identity, step='title', answer='Kept answer')
        self.assertEqual(path.read_bytes(), before)
        value = self.read(event_file=self.input, expected_revision=1)
        self.assertEqual(value['draft']['answers']['title'], 'Kept answer')

    def test_replayed_old_add_identifies_its_own_topic_after_another_chat_adds_one(self):
        value = self.save('add'); first = value['draft']['id']; original_event = self.input.read_bytes()
        for step, answer in [('title', 'First'), ('source', 'https://example.org/first'), ('homework', None), ('additional', None)]:
            self.save('answer', topic_id=first, step=step, answer=answer)
        second = self.save('add')['draft']['id']
        self.input.write_bytes(original_event)
        value = self.read(event_file=self.input, expected_revision=0)
        self.assertTrue(value['replayed']); self.assertEqual(value['event_topic_id'], first)
        self.assertEqual(value['draft']['id'], second)
        self.assertEqual(self.read(topic_id=first)['selected_topic']['answers']['title'], 'First')

    def test_error_after_atomic_publication_can_be_retried_without_duplication(self):
        import topic_queue
        original = topic_queue.atomic_json
        def published(path, state):
            original(path, state); raise OSError('QA lost acknowledgement')
        with patch.object(topic_queue, 'atomic_json', published):
            with self.assertRaises(OSError): self.save('add')
        result = self.read(event_file=self.input, expected_revision=0)
        self.assertTrue(result['replayed']); self.assertEqual(len(result['topics']), 1)

    def test_homework_tabs_and_multiple_material_references_are_kept_exactly(self):
        identity = self.save('add')['draft']['id']
        homework = '\tdef task():\n\t\treturn 1\r\n'
        additions = 'https://example.org/one\nhttps://example.org/two\nLocal file: /tmp/notes.pdf'
        for step, value in [('title', 'Python'), ('source', 'https://example.org/main'), ('homework', homework), ('additional', additions)]:
            self.save('answer', topic_id=identity, step=step, answer=value)
        answers = self.child()['selected_topic']['answers']
        self.assertEqual(answers['homework'], homework); self.assertEqual(answers['additional'], additions)

    def test_corrupt_state_has_controlled_cli_error_without_overwrite(self):
        self.save('add'); path = self.root / 'learners/default/topic-queue.json'
        valid = json.loads(path.read_text()); valid['active_topic_id'] = []
        for raw in (b'[]', b'null', b'{', json.dumps(valid).encode()):
            with self.subTest(raw=raw[:30]):
                path.write_bytes(raw)
                done = subprocess.run([sys.executable, '-B', course.__file__, 'topic-queue', '--course', str(self.root)],
                                      capture_output=True, text=True, timeout=15)
                self.assertEqual(done.returncode, 1, done.stderr)
                self.assertIn('Error:', done.stderr); self.assertNotIn('Traceback', done.stderr)
                self.assertEqual(path.read_bytes(), raw)

    def test_existing_progress_and_new_queues_are_read_only_in_study_next_compact(self):
        import adaptive_learning, cli_output
        self.fixture.save(); before = self.fixture.path.read_bytes()
        self.assertNotIn('topic_queue', adaptive_learning.study_next(self.args()))
        value = self.save('add')
        result = adaptive_learning.study_next(self.args())
        compact = cli_output.compact('study-next', result)
        self.assertEqual(compact['topic_queue']['next_question'], value['next_question'])
        self.assertTrue(compact['topic_queue']['collection_blocks_teaching'])
        self.assertEqual(self.fixture.path.read_bytes(), before)

    def study_with_preserved_files(self, **kw):
        import adaptive_learning
        before = {str(p.relative_to(self.root.parent)): p.read_bytes()
                  for p in self.root.parent.rglob('*') if p.is_file()}
        result = adaptive_learning.study_next(self.args(**kw))
        self.assertEqual({str(p.relative_to(self.root.parent)): p.read_bytes()
                          for p in self.root.parent.rglob('*') if p.is_file()}, before)
        return result

    def test_study_next_corrupt_journal_keeps_ordered_queue_and_diagnostics(self):
        import cli_output
        identities = [self.add('Topic ' + str(i))[0] for i in range(3)]
        for raw in (b'{', b'[]', b'null', b'{}', b'{"schema":"wrong"}'):
            with self.subTest(raw=raw):
                self.fixture.path.write_bytes(raw)
                result = self.study_with_preserved_files()
                self.assertFalse(result['valid'])
                self.assertTrue(result['reason']); self.assertTrue(result['recovery'])
                self.assertEqual(result['recommendations'], [])
                queue = result['topic_queue']
                self.assertEqual([t['id'] for t in queue['topics']], identities)
                self.assertEqual([t['status'] for t in queue['topics']], ['waiting'] * 3)
                self.assertIsNone(queue['active_topic_id'])
                self.assertEqual(queue['mode'], 'choice')
                self.assertEqual(cli_output.compact('study-next', result)['topic_queue'], queue)

    def test_study_next_corrupt_journal_keeps_each_unfinished_question(self):
        identity = self.save('add')['draft']['id']
        self.fixture.path.write_bytes(b'[]')
        for step, answer in [('title', 'Maps'), ('source', 'https://example.org/maps'),
                             ('homework', None), ('additional', None)]:
            result = self.study_with_preserved_files()
            self.assertFalse(result['valid'])
            self.assertEqual(result['topic_queue']['next_question']['step'], step)
            self.assertTrue(result['topic_queue']['collection_blocks_teaching'])
            self.assertIsNone(result['topic_queue']['active_topic_id'])
            with self.assertRaises(ValueError):
                self.save('start', topic_id=identity)
            self.save('answer', topic_id=identity, step=step, answer=answer)
        self.assertEqual(self.study_with_preserved_files()['topic_queue']['mode'], 'choice')

    def test_study_next_corrupt_journal_does_not_leak_queue_to_other_scope(self):
        self.add('Private topic'); self.fixture.path.write_bytes(b'[]')
        other = self.root.parent / 'other-course'; other.mkdir()
        (other / 'course.json').write_bytes((self.root / 'course.json').read_bytes())
        for scope in (dict(profile='bob'), dict(course=other)):
            with self.subTest(scope=scope):
                self.assertNotIn('topic_queue', self.study_with_preserved_files(**scope))
        self.assertEqual(self.study_with_preserved_files()['topic_queue']['topics'][0]['title'], 'Private topic')

    def test_study_next_bad_registry_does_not_hide_independent_queue(self):
        self.add('Queued topic')
        (self.root / 'study-units.json').write_bytes(b'[]')
        result = self.study_with_preserved_files(knowledge_map=True)
        self.assertFalse(result['valid']); self.assertTrue(result['reason'])
        self.assertEqual(result['topic_queue']['topics'][0]['title'], 'Queued topic')
        self.assertIn('knowledge_map', result)

    def test_study_next_queue_error_does_not_replace_journal_diagnostic(self):
        self.add(); self.fixture.path.write_bytes(b'[]')
        reason = self.study_with_preserved_files()['reason']
        (self.root / 'learners/default/topic-queue.json').write_bytes(b'{')
        result = self.study_with_preserved_files()
        self.assertFalse(result['valid']); self.assertEqual(result['reason'], reason)
        self.assertTrue(result['topic_queue_error'])
        self.assertNotIn('topic_queue', result)

    def test_study_next_bound_queue_does_not_claim_completion_from_bad_journal(self):
        identity, _ = self.add(); unit = self.fixture.f.get()
        self.save('bind', topic_id=identity, unit_id='maps', unit_sha256=unit['content_sha256'],
                  source_review='QA reviewed source')
        self.save('start', topic_id=identity)
        self.fixture.path.write_bytes(b'[]')
        result = self.study_with_preserved_files()
        self.assertFalse(result['valid'])
        queue = result['topic_queue']
        self.assertTrue(queue['learning_error'])
        self.assertEqual(queue['topics'][0]['status'], 'studying')
        self.assertTrue(queue['topics'][0]['needs_review'])

    def test_real_learning_completion_offers_next_without_starting_it(self):
        first, _ = self.add(); second, _ = self.add('Next', 'https://example.org/next')
        unit = self.fixture.f.get()
        self.save('bind', topic_id=first, unit_id='maps', unit_sha256=unit['content_sha256'], source_review='QA full source review')
        with self.assertRaisesRegex(ValueError, 'share one unit'):
            self.save('bind', topic_id=second, unit_id='maps', unit_sha256=unit['content_sha256'], source_review='QA duplicate')
        self.save('start', topic_id=first); self.fixture.save()
        value = self.child()
        self.assertEqual(value['next_action'], 'offer_next_topic')
        self.assertEqual(value['next_waiting_topic_id'], second)
        self.assertEqual(value['active_topic_id'], first)
        self.assertEqual(self.read(topic_id=second)['selected_topic']['answers']['source'], 'https://example.org/next')

    def test_new_unit_version_or_future_evidence_cannot_complete_topic(self):
        first, _ = self.add(); unit = self.fixture.f.get()
        self.save('bind', topic_id=first, unit_id='maps', unit_sha256=unit['content_sha256'], source_review='QA checked')
        self.save('start', topic_id=first); self.fixture.save()
        with patch('course.now', return_value='2001-01-01T00:00:00Z'):
            self.assertEqual(self.read()['topics'][0]['status'], 'studying')
        self.fixture.f.save(self.fixture.f.card(title='New source version'))
        value = self.read()
        self.assertEqual(value['topics'][0]['status'], 'studying')
        self.assertTrue(value['topics'][0]['needs_review'])

    def test_no_false_completion_from_explanation_or_corrupt_progress(self):
        first, _ = self.add(); unit = self.fixture.f.get()
        self.save('bind', topic_id=first, unit_id='maps', unit_sha256=unit['content_sha256'], source_review='QA checked')
        self.save('start', topic_id=first)
        self.fixture.save(self.fixture.event(stage='explained', answer=None, question=None, hints=0, criteria=[]))
        self.assertEqual(self.read()['topics'][0]['status'], 'studying')
        self.fixture.path.write_bytes(b'[]')
        value = self.read()
        self.assertEqual(value['topics'][0]['status'], 'studying'); self.assertTrue(value['learning_error'])

    def test_local_source_link_and_plain_answers_are_not_executed_or_silently_truncated(self):
        identity, _ = self.add(source=str(self.fixture.f.text))
        before = self.fixture.f.text.read_bytes()
        self.assertEqual(self.read(topic_id=identity)['selected_topic']['answers']['source'], str(self.fixture.f.text))
        self.assertEqual(self.fixture.f.text.read_bytes(), before)
        identity = self.save('add')['draft']['id']
        with self.assertRaises(ValueError): self.save('answer', topic_id=identity, step='title', answer='x' * 4001)
        self.assertEqual(self.read()['next_question']['step'], 'title')

    def worker(self, event, revision=0, mode='race', name='worker'):
        source = self.root.parent / (name + '.json'); source.write_text(json.dumps(event), encoding='utf-8')
        process = subprocess.Popen([sys.executable, '-B', '-c', WORKER, str(Path(course.__file__).parent),
            mode, str(self.root), str(source), str(revision)], stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        def cleanup():
            if process.poll() is None: process.kill(); process.wait(timeout=10)
            for stream in (process.stdin, process.stdout, process.stderr): stream.close()
        self.addCleanup(cleanup)
        return process, source

    def test_two_independent_processes_cannot_overwrite_the_same_answer(self):
        identity = self.save('add')['draft']['id']
        children = [self.worker(dict(request_id='answer-' + str(i), action='answer', topic_id=identity,
                                    step='title', answer='Answer ' + str(i)), revision=1, name=str(i))[0] for i in range(2)]
        for child in children: self.assertEqual(child.stdout.readline().strip(), 'ready')
        for child in children: child.stdin.write('go\n'); child.stdin.flush()
        results = [child.communicate(timeout=15) for child in children]
        self.assertCountEqual([p.returncode for p in children], [0, 1])
        rejected = next(error for p, (_, error) in zip(children, results) if p.returncode)
        self.assertIn('changed', rejected)
        state = self.read(); self.assertEqual(state['revision'], 2)
        self.assertIn(state['draft']['answers']['title'], ['Answer 0', 'Answer 1'])
        self.assertEqual(state['next_question']['step'], 'source')

    def test_concurrent_retries_of_one_request_create_one_topic(self):
        event = dict(request_id='same-request', action='add')
        children = [self.worker(event, name='same-' + str(i))[0] for i in range(2)]
        for child in children: self.assertEqual(child.stdout.readline().strip(), 'ready')
        for child in children: child.stdin.write('go\n'); child.stdin.flush()
        outputs = []
        for child in children:
            output, error = child.communicate(timeout=15)
            self.assertEqual(child.returncode, 0, error); outputs.append(json.loads(output))
        self.assertEqual(outputs[0]['draft']['id'], outputs[1]['draft']['id'])
        self.assertEqual(self.read()['revision'], 1)

    def test_interrupted_writer_releases_locks_and_retry_preserves_old_answers(self):
        identity = self.save('add')['draft']['id']
        event = dict(request_id='interrupted-answer', action='answer', topic_id=identity, step='title', answer='Survives retry')
        path = self.root / 'learners/default/topic-queue.json'; before = path.read_bytes()
        child, source = self.worker(event, revision=1, mode='staged')
        self.assertEqual(child.stdout.readline().strip(), 'ready')
        self.assertEqual(path.read_bytes(), before)
        child.kill(); child.wait(timeout=10)
        value = self.read(event_file=source, expected_revision=1)
        self.assertEqual(value['draft']['answers']['title'], 'Survives retry')
        self.assertEqual(value['revision'], 2)

    def test_continued_learning_writes_the_existing_checkpoint_in_another_process(self):
        identity, _ = self.add(); unit = self.fixture.f.get()
        self.save('bind', topic_id=identity, unit_id='maps', unit_sha256=unit['content_sha256'], source_review='QA same material')
        self.save('start', topic_id=identity)
        self.fixture.save(dict(recap='Previous actual work', next_step='New exercise', pending_question=None), checkpoint=True)
        current = self.fixture.read()['learning']['revision']
        point = dict(recap='New process continued the exercise', next_step='Review result', pending_question='Actual pending question')
        source = self.root.parent / 'next-checkpoint.json'; source.write_text(json.dumps(point))
        done = subprocess.run([sys.executable, '-B', course.__file__, 'learn-unit', '--course', str(self.root),
            '--unit-id', 'maps', '--session', 'new-chat', '--checkpoint-file', str(source),
            '--expected-revision', str(current), '--expected-unit-sha256', unit['content_sha256']],
            capture_output=True, text=True, timeout=15)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(json.loads(done.stdout)['learning']['checkpoint']['event']['value'], point)
        reloaded = self.child()
        self.assertTrue(any(row['checkpoint'] == point for row in reloaded['continuations']))
        self.assertEqual(reloaded['active_topic_id'], identity)

    def test_queue_changes_do_not_change_career_consent(self):
        import career_route
        source = self.root.parent / 'career-decision.json'
        source.write_text(json.dumps(dict(action='context', professional=True, role='Java backend', region=None, level=None)))
        career_route.route(self.args(market_file=source, expected_market_revision=0))
        source.write_text(json.dumps(dict(action='decision', decision='declined', statement='Synthetic QA decline')))
        career_route.route(self.args(market_file=source, expected_market_revision=1))
        path = self.root / 'learners/default/career-market.json'; before = path.read_bytes()
        identity, _ = self.add(); self.save('start', topic_id=identity)
        self.assertEqual(path.read_bytes(), before)
