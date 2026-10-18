"""Interrupted stages, coherent publication and resumable model byte transfers."""
from concurrent.futures import ThreadPoolExecutor
import contextlib
import hashlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace as NS
import unittest
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'
sys.path.insert(0, str(SCRIPTS))
import course
import download_model
import processing_job
import publication
import test_integrity as fixtures
import tutor_packet
from atomic_files import atomic_json
from model_readiness import inspect_model, ModelNotReady
from source_integrity import file_hash

SIDECARS = {'config.json': b'{"lang_ids":[50259]}',
            'tokenizer.json': b'{"model":{"vocab":{"test":0}}}', 'vocabulary.txt': b'test\n'}


def fixture_model(path, payload=b'fixture-model'):
    path.mkdir(parents=True, exist_ok=True)
    (path / 'model.bin').write_bytes(payload)
    for name, data in SIDECARS.items():
        (path / name).write_bytes(data)
    return path


class JobRecovery(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fixtures.SourceIntegrityRegression.setUpClass.__func__(cls)
        import imageio_ffmpeg
        cls.ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()

    @classmethod
    def tearDownClass(cls):
        cls.media_temp.cleanup()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.video = self.root / 'lecture.mp4'
        self.video.write_bytes(self.a)
        self.model = fixture_model(self.root / 'model')
        self.out = self.root / 'job'
        self.args = NS(video=self.video, output=self.out, start=0, duration=2,
                       frame_every=.5, model=str(self.model), language='ru', prompt='',
                       resume=True, prepare_only=False)
        self.loads, self.recognitions = [], []
        self.vad_patch = patch('bounded_vad.install')
        self.vad_patch.start()
        owner = self
        class Model:
            def __init__(self, *a, **kw):
                owner.loads.append(True)
                self.feature_extractor = NS()
            def transcribe(self, *a, **kw):
                owner.recognitions.append(True)
                time.sleep(.05)
                return iter([NS(start=.1, end=1.5, text='Teacher example', avg_logprob=-.1,
                                no_speech_prob=.01, compression_ratio=1)]), NS(language='ru')
        self.model_class = Model
        self.saved_model_module = sys.modules.get('faster_whisper')
        sys.modules['faster_whisper'] = NS(WhisperModel=Model)

    def tearDown(self):
        self.vad_patch.stop()
        if self.saved_model_module is None:
            sys.modules.pop('faster_whisper', None)
        else:
            sys.modules['faster_whisper'] = self.saved_model_module
        self.temp.cleanup()

    def run_job(self):
        return processing_job.run_job(self.args, self.ffmpeg)

    def interrupt(self, stage):
        original = processing_job.subprocess.run
        calls = {'audio': 0, 'frames': 0}
        failed = []
        def injected(command, **kw):
            kind = 'audio' if '-ar' in command else 'frames'
            calls[kind] += 1
            result = original(command, **kw)
            if kind == stage and not failed:
                failed.append(True)
                raise subprocess.CalledProcessError(9, command)
            return result
        with patch.object(processing_job.subprocess, 'run', injected):
            with self.assertRaises(subprocess.CalledProcessError):
                self.run_job()
            state = json.loads((self.out / 'checkpoint.json').read_text())
            self.assertNotEqual(state.get('status'), 'complete')
            self.assertFalse((self.out / 'tutor_packet.json').exists())
            self.run_job()
        return calls

    def test_failure_during_audio_rebuilds_uncommitted_audio(self):
        self.assertEqual(self.interrupt('audio'), {'audio': 2, 'frames': 1})
        self.assertEqual(json.loads((self.out / 'checkpoint.json').read_text())['status'], 'complete')

    def test_failure_during_frames_preserves_verified_audio(self):
        self.assertEqual(self.interrupt('frames'), {'audio': 1, 'frames': 2})

    def test_asr_exception_reuses_audio_and_frames(self):
        original = self.model_class.transcribe
        def broken(*a, **kw):
            def segments():
                yield NS(start=.1, end=.5, text='Partial', avg_logprob=-.1, no_speech_prob=.01, compression_ratio=1)
                raise RuntimeError('Simulated ASR interruption')
            return segments(), NS(language='ru')
        with patch.object(self.model_class, 'transcribe', broken):
            with self.assertRaisesRegex(RuntimeError, 'interruption'):
                self.run_job()
        self.assertFalse((self.out / 'transcript.json').exists())
        before = file_hash(self.out / 'audio.wav')
        with patch.object(processing_job.subprocess, 'run', side_effect=AssertionError('FFmpeg should be cached')):
            self.run_job()
        self.assertEqual(file_hash(self.out / 'audio.wav'), before)

    def test_packet_failure_reuses_completed_asr(self):
        with patch.object(tutor_packet, 'build_packet', side_effect=OSError('Disk interruption')):
            with self.assertRaises(OSError):
                self.run_job()
        self.assertEqual(len(self.recognitions), 1)
        self.run_job()
        self.assertEqual(len(self.loads), 1)
        self.assertEqual(len(self.recognitions), 1)

    def test_economy_limits_inference_and_extraction(self):
        self.args.cpu_mode = 'economy'
        with patch.object(self.model_class, '__init__', return_value=None) as load, \
                patch('bounded_features.BoundedFeatureExtractor'), \
                patch.object(self.model_class, 'feature_extractor', NS(), create=True), \
                patch.object(processing_job, 'extract_audio', wraps=processing_job.extract_audio) as audio, \
                patch.object(processing_job, 'extract_frames', wraps=processing_job.extract_frames) as frames:
            self.run_job()
        self.assertEqual(load.call_args.kwargs['cpu_threads'], 2)
        self.assertEqual(audio.call_args.kwargs['threads'], 1)
        self.assertEqual(frames.call_args.kwargs['threads'], 1)
        info = json.loads((self.out / 'manifest.json').read_text())['recognition']
        self.assertEqual(info['cpu_mode'], 'economy')

    def test_switching_cpu_mode_reuses_completed_result_and_progress(self):
        self.run_job()
        files = ['audio.wav', 'transcript.json', 'tutor_packet.json', 'progress.json']
        before = {name: file_hash(self.out / name) for name in files}
        self.args.cpu_mode = 'economy'
        with patch.object(processing_job.subprocess, 'run', side_effect=AssertionError('No extraction')):
            self.run_job()
        self.assertEqual(len(self.recognitions), 1)
        self.assertEqual(before, {name: file_hash(self.out / name) for name in files})
        self.assertEqual(json.loads((self.out / 'manifest.json').read_text())['recognition']['cpu_mode'], 'standard')

    def test_hashes_are_reused_only_during_one_processing_call(self):
        import hashlib
        hashed = []
        original = hashlib.file_digest
        def count(stream, *args, **kwargs):
            hashed.append(str(stream.name))
            return original(stream, *args, **kwargs)
        with patch('hashlib.file_digest', count): self.run_job()
        if processing_job.CACHE_SOURCE_HASHES: self.assertEqual(hashed.count(str(self.video)), 1)
        else: self.assertGreater(hashed.count(str(self.video)), 1)
        self.assertGreater(hashed.count(str((self.out / 'audio.wav').resolve())), 1)
        hashed.clear()
        with patch('hashlib.file_digest', count): self.run_job()
        if processing_job.CACHE_SOURCE_HASHES: self.assertEqual(hashed.count(str(self.video)), 1)
        else: self.assertGreater(hashed.count(str(self.video)), 1)

    def test_windows_hash_policy_preserves_full_source_checks(self):
        import hashlib
        original = hashlib.file_digest; sources = []
        def count(stream, *args, **kwargs):
            if str(stream.name) == str(self.video): sources.append(True)
            return original(stream, *args, **kwargs)
        with patch.object(processing_job, 'CACHE_SOURCE_HASHES', False), patch('hashlib.file_digest', count):
            self.run_job()
        self.assertGreater(len(sources), 1)

    def test_source_mutation_during_asr_still_rejected_with_hash_reuse(self):
        original = self.model_class.transcribe
        def mutate(model, *args, **kwargs):
            result = original(model, *args, **kwargs)
            self.video.write_bytes(self.b)
            return result
        with patch.object(self.model_class, 'transcribe', mutate):
            with self.assertRaisesRegex(ValueError, 'changed'):
                self.run_job()
        self.assertFalse((self.out / 'transcript.json').exists())

    def test_model_and_pcm_released_before_packet_assembly(self):
        import weakref
        references = []
        original = self.model_class.transcribe
        def capture(model, audio, **kwargs):
            references.extend([weakref.ref(model), weakref.ref(audio)])
            return original(model, audio, **kwargs)
        build = tutor_packet.build_packet
        def check(*args, **kwargs):
            self.assertTrue(references)
            self.assertTrue(all(ref() is None for ref in references))
            return build(*args, **kwargs)
        with patch.object(self.model_class, 'transcribe', capture), patch.object(tutor_packet, 'build_packet', check):
            self.run_job()

    def test_completed_repeat_does_no_extraction_or_asr(self):
        self.run_job()
        checksum = file_hash(self.out / 'tutor_packet.json')
        with patch.object(processing_job.subprocess, 'run', side_effect=AssertionError('No extraction')):
            self.run_job()
        self.assertEqual(len(self.loads), 1)
        self.assertEqual(len(self.recognitions), 1)
        self.assertEqual(file_hash(self.out / 'tutor_packet.json'), checksum)

    def test_parallel_same_job_has_one_recognition(self):
        with ThreadPoolExecutor(2) as pool:
            futures = [pool.submit(self.run_job) for _ in range(2)]
            for future in futures:
                self.assertEqual(future.result(timeout=20)['status'], 'complete')
        self.assertEqual(len(self.recognitions), 1)

    def test_orphaned_files_without_manifest_are_preserved_and_rebuilt(self):
        (self.out / 'frames').mkdir(parents=True)
        (self.out / 'audio.wav').write_bytes(b'unverified partial audio')
        (self.out / 'frames/old.jpg').write_bytes(b'old')
        self.run_job()
        old = list((self.out / '.unverified').glob('*/audio.wav'))
        self.assertEqual(old[0].read_bytes(), b'unverified partial audio')
        self.assertNotEqual((self.out / 'audio.wav').read_bytes(), old[0].read_bytes())

    def test_changed_source_cannot_resume_unfinished_checkpoint(self):
        original = processing_job.subprocess.run
        def broken(command, **kw):
            if '-vf' in command:
                raise subprocess.CalledProcessError(9, command)
            return original(command, **kw)
        with patch.object(processing_job.subprocess, 'run', broken):
            with self.assertRaises(subprocess.CalledProcessError):
                self.run_job()
        self.video.write_bytes(self.b)
        with self.assertRaisesRegex(ValueError, 'does not match'):
            self.run_job()

    def test_publication_rebases_paths_and_preserves_notes_on_repeat(self):
        self.run_job()
        prepared = self.root / 'prepared'
        target = publication.publish_job(self.out, prepared, 'key')
        packet = json.loads((target / 'tutor_packet.json').read_text())
        self.assertTrue(all(Path(f['absolute_path']).is_file() for f in packet['frames']))
        atomic_json(target / 'visual_notes.json', {'observations': [{'topic': 'Teacher correction'}], 'uncertainties': []})
        again = publication.publish_job(self.out, prepared, 'key', preferred=target)
        self.assertEqual(again, target)
        self.assertEqual(json.loads((again / 'visual_notes.json').read_text())['observations'][0]['topic'], 'Teacher correction')

    def test_failed_publication_does_not_expose_partial_fragment(self):
        self.run_job()
        prepared = self.root / 'prepared'
        original = publication.shutil.copyfile
        def corrupt(src, dst, **kw):
            result = original(src, dst, **kw)
            if Path(dst).name == 'transcript.json':
                Path(dst).write_text('[]')
            return result
        with patch.object(publication.shutil, 'copyfile', corrupt):
            with self.assertRaises(ValueError):
                publication.publish_job(self.out, prepared, 'key')
        self.assertEqual(list(prepared.glob('v4-*')), [])
        self.assertEqual(list(prepared.glob('.pending-*')), [])

    def test_damaged_publication_creates_new_copy_without_overwriting_old(self):
        self.run_job()
        prepared = self.root / 'prepared'
        old = publication.publish_job(self.out, prepared, 'key')
        (old / 'transcript.json').write_text('[]')
        new = publication.publish_job(self.out, prepared, 'key', preferred=old)
        self.assertNotEqual(new, old)
        self.assertEqual((old / 'transcript.json').read_text(), '[]')
        self.assertEqual(json.loads((new / 'transcript.json').read_text())[0]['text'], 'Teacher example')

    def test_course_repairs_missing_sidecar_before_running_asr(self):
        self.path = self.root / 'course'
        args = NS(course=self.path, lesson='one', title='Recovery', video=str(self.video), order=1,
                  profile='default', import_prepared=None, start=0, duration=2, frame_every=.5,
                  model='tiny', model_path=self.model, language='ru', prompt='', rebuild=False)
        course.init(args)
        course.add_lesson(args)
        (self.model / 'vocabulary.txt').unlink()
        downloads = []
        original = subprocess.run
        def dispatch(command, **kw):
            if str(command[1]).endswith('download_model.py'):
                downloads.append(True)
                fixture_model(self.model)
                return NS(returncode=0)
            if str(command[1]).endswith('lecture.py'):
                self.args.output = Path(command[command.index('--output') + 1])
                self.run_job()
                return NS(returncode=0)
            return original(command, **kw)
        with patch.object(course.subprocess, 'run', dispatch):
            course.prepare(args)
        self.assertEqual(downloads, [True])
        data = course.read(self.path / 'lessons/one/lesson.json')
        self.assertEqual(len(data['prepared_fragments']), 1)
        self.assertTrue((Path(data['prepared_fragments'][0]['folder']) / 'publication.json').is_file())

    def test_explicit_selection_keeps_rebuild_as_candidate_and_reuses_it(self):
        import fragment_selection
        path = self.root / 'guarded-course'
        args = NS(course=path, lesson='one', title='Guarded', video=str(self.video), order=1,
                  profile='default', import_prepared=None, start=0, duration=2, frame_every=.5,
                  model='tiny', model_path=self.model, language='ru', prompt='', rebuild=False)
        course.init(args); course.add_lesson(args)
        meta = path / 'lessons/one/lesson.json'
        original = subprocess.run
        def dispatch(command, **kw):
            if str(command[1]).endswith('lecture.py'):
                self.args.output = Path(command[command.index('--output') + 1])
                self.run_job()
                return NS(returncode=0)
            return original(command, **kw)
        with patch.object(course.subprocess, 'run', dispatch):
            course.prepare(args)
            data = course.read(meta); old = data['prepared_fragments'][0]['folder']
            fragment_selection.fragments(NS(**vars(args), keep=[Path(old).name], expected_revision=data['revision']))
            args.rebuild = True; course.prepare(args)
            data = course.read(meta)
            self.assertEqual(data['prepared_fragments'][0]['folder'], old)
            candidate = data['retired_fragments'][0]
            self.assertEqual(candidate['activation_reason'], 'explicit_selection_overlap')
            job = next(iter(data['processing_jobs'].values()))
            self.assertEqual((job['status'],job['activation_status']),('complete','candidate'))
            args.rebuild = False; course.prepare(args)
            data = course.read(meta); self.assertEqual(len(data['retired_fragments']),1)
            fragment_selection.fragments(NS(**vars(args), keep=[Path(candidate['folder']).name], expected_revision=data['revision']))
            course.prepare(args)
            data = course.read(meta)
            self.assertEqual(data['prepared_fragments'][0]['folder'],candidate['folder'])
            self.assertEqual(next(iter(data['processing_jobs'].values()))['activation_status'],'active')

    def test_killed_cli_releases_lock_and_preserves_audio_checkpoint(self):
        script = '''import sys,time
from pathlib import Path
sys.path.insert(0,sys.argv[1])
import lecture,subprocess
original=subprocess.run
folder=Path(sys.argv[4])
def paused(command,**kw):
 if '-vf' in command:
  (folder/'paused').write_text('ready')
  time.sleep(30)
 return original(command,**kw)
subprocess.run=paused
sys.argv=['lecture.py',sys.argv[2],'--output',sys.argv[4],'--duration','2','--frame-every','0.5','--prepare-only','--resume']
lecture.main()
'''
        process = subprocess.Popen([sys.executable, '-c', script, str(SCRIPTS), str(self.video), 'unused', str(self.out)],
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            deadline = time.monotonic() + 5
            while not (self.out / 'paused').exists() and time.monotonic() < deadline and process.poll() is None:
                time.sleep(.02)
            self.assertTrue((self.out / 'paused').exists())
            checksum = file_hash(self.out / 'audio.wav')
        finally:
            process.kill()
            process.communicate(timeout=5)
        result = subprocess.run([sys.executable, str(SCRIPTS / 'lecture.py'), str(self.video), '--output', str(self.out),
                    '--duration', '2', '--frame-every', '.5', '--prepare-only', '--resume'], capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(file_hash(self.out / 'audio.wav'), checksum)

    def test_parallel_course_preparation_registers_one_coherent_fragment(self):
        path = self.root / 'parallel-course'
        args = NS(course=path, lesson='one', title='Parallel', video=str(self.video), order=1,
                  profile='default', import_prepared=None, start=0, duration=2, frame_every=.5,
                  model='tiny', model_path=self.model, language='ru', prompt='', rebuild=False)
        course.init(args)
        course.add_lesson(args)
        original = subprocess.run
        def dispatch(command, **kw):
            if str(command[1]).endswith('lecture.py'):
                job_args = NS(**vars(self.args))
                job_args.output = Path(command[command.index('--output') + 1])
                processing_job.run_job(job_args, self.ffmpeg)
                return NS(returncode=0)
            return original(command, **kw)
        with patch.object(course.subprocess, 'run', dispatch), ThreadPoolExecutor(2) as pool:
            futures = [pool.submit(course.prepare, args) for _ in range(2)]
            for future in futures:
                future.result(timeout=20)
        data = course.read(path / 'lessons/one/lesson.json')
        self.assertEqual(len(data['prepared_fragments']), 1)
        self.assertEqual(len(self.recognitions), 1)
        self.assertEqual(len(data['processing_jobs']), 1)

    def test_failed_rebuild_resumes_new_job_and_retires_old_fragment(self):
        path = self.root / 'rebuild-course'
        args = NS(course=path, lesson='one', title='Rebuild', video=str(self.video), order=1,
                  profile='default', import_prepared=None, start=0, duration=2, frame_every=.5,
                  model='tiny', model_path=self.model, language='ru', prompt='', rebuild=False)
        course.init(args)
        course.add_lesson(args)
        original = subprocess.run
        def dispatch(command, **kw):
            if str(command[1]).endswith('lecture.py'):
                self.args.output = Path(command[command.index('--output') + 1])
                self.run_job()
                return NS(returncode=0)
            return original(command, **kw)
        with patch.object(course.subprocess, 'run', dispatch):
            course.prepare(args)
            old = Path(course.read(path / 'lessons/one/lesson.json')['prepared_fragments'][0]['folder'])
            (old / 'transcript.json').write_text('[]')
            args.rebuild = True
            with patch.object(self.model_class, 'transcribe', side_effect=RuntimeError('Interrupted rebuild')):
                with self.assertRaises(RuntimeError):
                    course.prepare(args)
            pending = course.read(path / 'lessons/one/lesson.json')['processing_jobs']
            selected = next(iter(pending.values()))['job_key']
            args.rebuild = False
            course.prepare(args)
        data = course.read(path / 'lessons/one/lesson.json')
        self.assertEqual(len(data['prepared_fragments']), 1)
        self.assertEqual(data['prepared_fragments'][0]['job_key'], selected)
        self.assertEqual(data['retired_fragments'][0]['folder'], str(old))
        self.assertEqual((old / 'transcript.json').read_text(), '[]')

    def test_late_prepare_cannot_replace_new_rebuild(self):
        import threading
        args = NS(course=self.root / 'race', lesson='one', title='Race', video=str(self.video), order=1,
                  profile='default', import_prepared=None, start=0, duration=2, frame_every=.5,
                  model='tiny', model_path=self.model, language='ru', prompt='', rebuild=False)
        course.init(args); course.add_lesson(args)
        real_run = subprocess.run
        def dispatch(command, **kw):
            if str(command[1]).endswith('lecture.py'):
                job_args = NS(**vars(self.args))
                job_args.output = Path(command[command.index('--output') + 1])
                processing_job.run_job(job_args, self.ffmpeg)
                return NS(returncode=0)
            return real_run(command, **kw)
        ready, release = threading.Event(), threading.Event()
        real_publish = publication.publish_job
        def gated(*aa, **kw):
            folder = real_publish(*aa, **kw)
            if threading.current_thread().name.startswith('old-job'):
                ready.set()
                if not release.wait(15): raise TimeoutError('Test interleaving timed out')
            return folder
        with patch.object(course.subprocess, 'run', dispatch):
            course.prepare(args)
            with patch.object(publication, 'publish_job', gated), ThreadPoolExecutor(1, thread_name_prefix='old-job') as pool:
                old = pool.submit(course.prepare, args)
                self.assertTrue(ready.wait(15))
                try:
                    course.prepare(NS(**{**vars(args), 'rebuild': True}))
                finally:
                    release.set()
                old.result(timeout=15)
        data = course.read(args.course / 'lessons/one/lesson.json')
        selected = next(iter(data['processing_jobs'].values()))['job_key']
        self.assertEqual([f['job_key'] for f in data['prepared_fragments']], [selected])
        self.assertNotIn(selected, [f['job_key'] for f in data['retired_fragments']])

    def test_moved_course_reuses_completed_job_without_new_recognition(self):
        path = self.root / 'portable-course'
        args = NS(course=path, lesson='one', title='Portable', video=str(self.video), order=1,
                  profile='default', import_prepared=None, start=0, duration=2, frame_every=.5,
                  model='tiny', model_path=self.model, language='ru', prompt='', rebuild=False)
        course.init(args);course.add_lesson(args)
        original=subprocess.run
        def dispatch(command, **kw):
            if str(command[1]).endswith('lecture.py'):
                self.args.output=Path(command[command.index('--output')+1])
                self.run_job()
                return NS(returncode=0)
            return original(command,**kw)
        with patch.object(course.subprocess,'run',dispatch):
            course.prepare(args)
            before=course.read(path/'lessons/one/lesson.json')['prepared_fragments'][0]
            moved=self.root/'moved-course';path.rename(moved);args.course=moved
            course.prepare(args)
        after=course.read(moved/'lessons/one/lesson.json')['prepared_fragments']
        self.assertEqual(len(self.recognitions),1)
        self.assertEqual(len(after),1)
        self.assertEqual(after[0]['packet_sha256'],before['packet_sha256'])
        self.assertTrue(Path(after[0]['folder']).is_relative_to(moved.resolve()))


class ModelRecovery(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.dest = self.root / 'model'
        self.payload = b'verified weights' * 40000
        self.checksum = hashlib.sha256(self.payload).hexdigest()
        self.calls = []
        self.metadata_calls = []
        self.fail = False
        self.bad_sidecar = False
        self.bad_weights = False
        self.metadata_patch = patch.object(download_model, 'metadata', self.metadata)
        self.curl_patch = patch.object(download_model, 'curl', self.transport)
        self.sleep_patch = patch.object(download_model.time, 'sleep', lambda seconds: None)
        for patcher in [self.metadata_patch, self.curl_patch, self.sleep_patch]:
            patcher.start()

    def tearDown(self):
        for patcher in [self.metadata_patch, self.curl_patch, self.sleep_patch]:
            patcher.stop()
        self.temp.cleanup()

    def metadata(self, model, folder):
        self.metadata_calls.append(True)
        return dict(url='https://weights.invalid/model.bin', sha256=self.checksum,
                    size=len(self.payload), revision='1'*40, base='https://weights.invalid/' + '1'*40 + '/')

    def transport(self, url, dest, headers=None, byte_range=None):
        dest = Path(dest)
        if byte_range:
            first, last = map(int, byte_range.split('-'))
            self.calls.append((first, last))
            if self.fail and first > 0:
                return NS(returncode=28)
            Path(headers).write_text(f'content-range: bytes {first}-{last}/{len(self.payload)}\n')
            dest.write_bytes(b'x'*(last-first+1) if self.bad_weights else self.payload[first:last+1])
        else:
            name = url.rsplit('/', 1)[-1]
            dest.write_bytes(b'{}' if self.bad_sidecar and name == 'config.json' else SIDECARS[name])
        return NS(returncode=0)

    def run_download(self):
        return download_model.download('tiny', self.dest)

    def test_only_weights_is_not_ready_and_sidecars_are_repaired_without_redownloading_weights(self):
        self.dest.mkdir()
        (self.dest / 'model.bin').write_bytes(self.payload)
        with self.assertRaises(ModelNotReady):
            inspect_model(self.dest)
        self.run_download()
        self.assertEqual(self.calls, [])
        self.assertTrue((self.dest / 'model-ready.json').is_file())

    def test_interrupted_range_resumes_verified_prefix(self):
        self.fail = True
        with self.assertRaisesRegex(RuntimeError, 'interrupted'):
            self.run_download()
        self.assertFalse((self.dest / 'model-ready.json').exists())
        self.fail = False
        self.calls.clear()
        self.run_download()
        self.assertEqual(self.calls[0][0], 256*1024)
        self.assertEqual(file_hash(self.dest / 'model.bin'), self.checksum)

    def test_corrupt_same_size_part_without_marker_is_replaced(self):
        parts = self.dest / '.parts' / self.checksum
        parts.mkdir(parents=True)
        (parts / '0000.part').write_bytes(b'x' * len(self.payload))
        self.run_download()
        self.assertTrue(self.calls)
        self.assertEqual(file_hash(self.dest / 'model.bin'), self.checksum)

    def test_self_consistent_corrupt_part_is_detected_by_full_checksum_and_retried(self):
        parts = self.dest / '.parts' / self.checksum
        parts.mkdir(parents=True)
        bad = parts / '0000.part'
        bad.write_bytes(b'x' * len(self.payload))
        atomic_json(parts / '0000.json', dict(size=bad.stat().st_size, sha256=file_hash(bad)))
        self.run_download()
        self.assertTrue(self.calls)
        self.assertEqual(file_hash(self.dest / 'model.bin'), self.checksum)

    def test_persistently_bad_weights_stop_after_bounded_retry_without_activation(self):
        self.bad_weights = True
        with self.assertRaisesRegex(RuntimeError, 'clean retry'):
            self.run_download()
        self.assertFalse((self.dest / 'model-ready.json').exists())
        self.assertFalse((self.dest / 'model.bin').exists())
        self.assertLessEqual(len(self.calls), 6)

    def test_invalid_sidecar_is_not_published_and_retry_reuses_weight_cache(self):
        self.bad_sidecar = True
        with self.assertRaises(ModelNotReady):
            self.run_download()
        self.assertFalse((self.dest / 'model.bin').exists())
        self.assertFalse((self.dest / 'model-ready.json').exists())
        before = len(self.calls)
        self.bad_sidecar = False
        self.run_download()
        self.assertEqual(len(self.calls), before)

    def test_complete_model_works_offline_and_cleans_generated_cache(self):
        self.run_download()
        (self.dest / '.parts/junk').mkdir(parents=True)
        (self.dest / '.parts/junk/old.part').write_bytes(b'old')
        with patch.object(download_model, 'metadata', side_effect=AssertionError('No network')):
            self.run_download()
        self.assertFalse((self.dest / '.parts').exists())
        self.assertTrue((self.dest / 'model-ready.json').exists())

    def test_parallel_downloaders_publish_once(self):
        with ThreadPoolExecutor(2) as pool:
            futures = [pool.submit(self.run_download) for _ in range(2)]
            for future in futures:
                self.assertEqual(future.result(timeout=20)['files']['model.bin']['sha256'], self.checksum)
        self.assertEqual(len(self.metadata_calls), 1)
        self.assertFalse((self.dest / '.parts').exists())
        self.assertEqual(list(self.dest.glob('.bundle-*')), [])

    def test_parallel_distinct_parts_are_assembled_in_byte_order(self):
        self.payload = b'A'*(2*1024*1024) + b'B'*(2*1024*1024) + b'C'*123
        self.checksum = hashlib.sha256(self.payload).hexdigest()
        self.run_download()
        self.assertEqual((self.dest / 'model.bin').read_bytes(), self.payload)
        self.assertTrue({0, 2*1024*1024, 4*1024*1024}.issubset({first for first, last in self.calls}))

    def test_model_inventory_detects_changed_tokenizer(self):
        self.run_download()
        (self.dest / 'tokenizer.json').write_text('{"model":{"vocab":{"changed":1}}}')
        with self.assertRaisesRegex(ModelNotReady, 'inventory changed'):
            inspect_model(self.dest)
        self.run_download()
        inspect_model(self.dest)

    def test_interrupted_installation_is_not_adopted_as_legacy_ready_model(self):
        fixture_model(self.dest, self.payload)
        pending = inspect_model(self.dest)
        atomic_json(self.dest / '.model-installing.json', pending)
        with self.assertRaisesRegex(ModelNotReady, 'installation is incomplete'):
            inspect_model(self.dest)
        self.run_download()
        self.assertFalse((self.dest / '.model-installing.json').exists())

    def test_completed_ready_marker_finishes_interrupted_final_cleanup_offline(self):
        self.run_download()
        atomic_json(self.dest / '.model-installing.json', inspect_model(self.dest))
        with patch.object(download_model, 'metadata', side_effect=AssertionError('No network')):
            self.run_download()
        self.assertFalse((self.dest / '.model-installing.json').exists())

    def test_stale_owned_staging_is_cleaned_but_unrelated_directory_is_preserved(self):
        self.run_download()
        stale = self.dest / ('.bundle-' + '1'*32)
        stale.mkdir()
        (stale / 'partial').write_text('old')
        unrelated = self.dest / '.bundle-my-notes'
        unrelated.mkdir()
        self.run_download()
        self.assertFalse(stale.exists())
        self.assertTrue(unrelated.exists())


if __name__ == '__main__':
    unittest.main()
