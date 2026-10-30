"""Checkpointed processing in a private job folder, serialized by an OS lock."""
from contextlib import nullcontext
import json
import os
from pathlib import Path
import shutil
import subprocess
import uuid
import wave
from atomic_files import atomic_json, atomic_text
from model_readiness import inspect_model, model_lock
from source_integrity import file_hash, source_matches, evidence_hashes, verify_evidence, verification_session
from storage_lock import file_lock
from screen_evidence import extract_frames
from media_input import PIPELINE_VERSION, probe_media, extract_audio, timeline_end

MANAGED = ('audio.wav', 'frames', 'manifest.json', 'transcript.json', 'transcript.md',
           'tutor_packet.json', 'tutor_context.md', 'TUTOR_RULES.md', 'visual_notes.json', 'progress.json')
# Windows st_ctime is creation time, so retain full source rehashes there.
CACHE_SOURCE_HASHES = os.name != 'nt'


def pcm_duration(path):
    with wave.open(str(path), 'rb') as audio:
        if (audio.getnchannels(), audio.getsampwidth(), audio.getframerate()) != (1, 2, 16000) or audio.getnframes() == 0:
            raise ValueError('Expected nonempty mono 16-bit PCM at 16 kHz')
        return audio.getnframes() / 16000


def persist(folder, state):
    atomic_json(folder / 'checkpoint.json', state)


def run_job(args, ffmpeg):
    out = args.output.resolve()
    out.mkdir(parents=True, exist_ok=True)
    with file_lock(out / '.processing.lock'):
        state = None
        from processing_status import Reporter
        report = Reporter(out)
        report.set('verify')
        try:
            state = process(args, ffmpeg, out, report.set)
            terminal = ('complete' if state.get('status') == 'complete' else
                        'recognized' if 'recognition' in state['stages'] else 'prepared')
            report.set(terminal, 'complete')
            return state
        except BaseException as error:
            report.set(report.data['stage'], 'interrupted' if isinstance(error, (KeyboardInterrupt, SystemExit)) else 'failed', error)
            checkpoint = out / 'checkpoint.json'
            if checkpoint.is_file():
                state = json.loads(checkpoint.read_text())
                state['last_error'] = type(error).__name__ + ': ' + str(error)[:300]
                persist(out, state)
            raise


@verification_session(paths=lambda args, ffmpeg, out, report=None: [args.video] if CACHE_SOURCE_HASHES else [])
def process(args, ffmpeg, out, report=lambda stage, **details: None):
    from cpu_policy import policy
    limits = policy(getattr(args, 'cpu_mode', 'standard'))
    extraction_options = dict(threads=limits['ffmpeg_threads']) if limits['ffmpeg_threads'] else {}
    print(f"CPU mode: {limits['mode']} ({limits['asr_threads']} ASR threads); completed stages are reused", flush=True)
    checksum = file_hash(args.video)
    inputs = dict(start=args.start, duration=args.duration, frame_every=args.frame_every,
                  audio_track=getattr(args, 'audio_track', None), audio_channel=getattr(args, 'audio_channel', None))
    checkpoint = out / 'checkpoint.json'
    if checkpoint.exists():
        if not args.resume:
            raise ValueError('Use --resume for this existing job, or a fresh output directory')
        state = json.loads(checkpoint.read_text())
        source_matches(state, checksum)
        if state.get('pipeline_version') != PIPELINE_VERSION:
            raise ValueError('Legacy processing timeline; use a fresh output or course.py prepare --rebuild')
        if state.get('inputs') != inputs:
            raise ValueError('Resume arguments or frame interval differ; use a fresh output directory')
    else:
        state = dict(schema_version=2, pipeline_version=PIPELINE_VERSION, source_sha256=checksum, inputs=inputs, stages={})
        manifest = out / 'manifest.json'
        if manifest.exists():
            if not args.resume:
                raise ValueError('Use a fresh output directory or --resume')
            old = json.loads(manifest.read_text())
            source_matches(old, checksum)
            if old.get('pipeline_version') != PIPELINE_VERSION:
                raise ValueError('Legacy processing timeline; use a fresh output or course.py prepare --rebuild')
            verify_evidence(out, old, transcript='transcript.json' in old.get('evidence_sha256', {}), audio=True)
            if (old['start'], old['requested_duration'], old.get('extraction', {}).get('frame_every')) != (args.start, args.duration, args.frame_every):
                raise ValueError('Resume arguments or frame interval differ; use a fresh output directory')
            if old.get('recognition') and not args.prepare_only:
                raise ValueError('Legacy recognition has no checkpoint; use a fresh output directory')
            state['media'] = old['media']
            state['stages']['audio'] = dict(sha256=old['audio_sha256'], duration=pcm_duration(out / 'audio.wav'), quality=old['audio_quality'])
            state['stages']['frames'] = dict(items=old['frames'], hashes={f['file']: old['evidence_sha256'][f['file']] for f in old['frames']})
        elif any((out / name).exists() for name in MANAGED):
            if not args.resume:
                raise ValueError('Unfinished files without a checkpoint; use --resume to preserve and rebuild them')
            quarantine = out / '.unverified' / uuid.uuid4().hex
            quarantine.mkdir(parents=True)
            for name in MANAGED:
                if (out / name).exists():
                    (out / name).replace(quarantine / name)
            print('Preserved unverified old files; rebuilding from pinned source', flush=True)
        persist(out, state)
    stages = state['stages']
    if 'media' not in state:
        report('probe')
        state['media'] = probe_media(args.video)
        persist(out, state)
    media = state['media']
    duration = min(args.duration, timeline_end(media) - args.start)
    if duration <= 0:
        raise ValueError('No audio/video in requested interval')
    from resource_preflight import inspect as inspect_resources, enforce
    recognition_needed = not args.prepare_only and 'recognition' not in stages
    pending = any(name not in stages for name in ('audio', 'frames')) or (
        not args.prepare_only and 'packet' not in stages)
    resources = inspect_resources(out, duration, audio_needed='audio' not in stages,
                                  recognition_needed=recognition_needed, pending=pending)
    report('resources', resources=resources)
    enforce(resources)
    if 'audio' not in stages:
        report('audio')
        print('Extracting audio…', flush=True)
        with nullcontext():
            temporary, quality = extract_audio(args.video.resolve(), out, args.start, duration, media, ffmpeg,
                audio_track=inputs['audio_track'], audio_channel=inputs['audio_channel'], **extraction_options)
            pcm_duration(temporary)
            if file_hash(args.video) != checksum:
                raise ValueError('Video changed during extraction; use a fresh output directory')
            temporary.replace(out / 'audio.wav')
            stages['audio'] = dict(sha256=file_hash(out / 'audio.wav'), duration=duration, quality=quality,
                                   execution=limits)
            persist(out, state)
    elif not (out / 'audio.wav').is_file() or file_hash(out / 'audio.wav') != stages['audio']['sha256']:
        raise ValueError('Prepared audio integrity changed; use course.py prepare --rebuild')
    if 'frames' not in stages:
        report('frames')
        staging = out / '.frames-staging'
        if staging.exists():
            shutil.rmtree(staging)
        staging.mkdir()
        print('Extracting screen frames…', flush=True)
        try:
            frames=(extract_frames(args.video,staging,args.start,stages['audio']['duration'],args.frame_every,ffmpeg,
                scene_changes=True,**extraction_options) if media.get('video') else [])
            if file_hash(args.video) != checksum:
                raise ValueError('Video changed during extraction; use a fresh output directory')
            if (out / 'frames').exists():
                shutil.rmtree(out / 'frames')
            staging.replace(out / 'frames')
            for frame in frames:
                frame['file']='frames/'+frame['file']
            stages['frames'] = dict(items=frames, hashes=evidence_hashes(out, dict(frames=frames)), execution=limits,
                selection='scene_or_periodic_v1' if media.get('video') else 'audio_only')
            persist(out, state)
        finally:
            if staging.exists():shutil.rmtree(staging)
    metadata = dict(source=args.video.name, source_path=str(args.video.resolve()), source_size=args.video.stat().st_size,
        source_sha256=checksum, source_revision='sha256:' + checksum, start=args.start,
        requested_duration=args.duration, actual_duration=stages['audio']['duration'],
        pipeline_version=PIPELINE_VERSION, media=media, audio_quality=stages['audio']['quality'],
        extraction=dict(frame_every=args.frame_every,frame_selection=stages['frames'].get('selection','legacy_periodic')), audio_sha256=stages['audio']['sha256'],
        frames=stages['frames']['items'], evidence_sha256=dict(stages['frames']['hashes']),
        limitations=['Periodic frames may miss brief code changes.', 'Speech and code require verification.'])
    if stages['frames'].get('selection')=='scene_or_periodic_v1':
        metadata['limitations'].append('Scene candidates are pixel changes, not verified slide/code/table interpretations.')
    if not media.get('video'):
        metadata['limitations'].append('Audio-only source; no visual evidence.')
    verify_evidence(out, metadata, transcript=False, audio=True)
    if args.prepare_only:
        # A completed recognition must never be downgraded by a prepare-only call.
        if 'recognition' not in stages:
            atomic_json(out / 'manifest.json', metadata)
        state.pop('last_error', None)
        persist(out, state)
        print(f'Prepared audio/frames: {out}', flush=True)
        return state
    model_path = Path(args.model)
    if not model_path.is_dir():
        raise ValueError('A complete local --model folder is required; use course.py prepare to install it')
    with model_lock(model_path):
        identity = inspect_model(model_path)
        settings = dict(model_fingerprint=identity['fingerprint'], language=args.language, prompt=args.prompt)
        previous = stages.get('recognition')
        if previous and previous['settings'] != settings:
            raise ValueError('Recognition settings changed; use a fresh output directory')
        if previous:
            if not (out / 'transcript.json').is_file() or file_hash(out / 'transcript.json') != previous['sha256']:
                raise ValueError('Transcript integrity changed; use course.py prepare --rebuild')
        else:
            report('model_load')
            import onnxruntime
            onnxruntime.disable_telemetry_events()
            from faster_whisper import WhisperModel
            print(f'Loading local model {model_path}…', flush=True)
            with nullcontext():
                model = WhisperModel(str(model_path), device='cpu', compute_type='int8', cpu_threads=limits['asr_threads'])
                from bounded_features import BoundedFeatureExtractor
                model.feature_extractor = BoundedFeatureExtractor(model.feature_extractor)
    if not previous:
        report('asr')
        with nullcontext():
            from pcm_audio import read_pcm
            audio = read_pcm(out / 'audio.wav')
            if metadata['audio_quality']['status'] in ('silent','no_audio_track'):
                from types import SimpleNamespace
                segments, info = iter([]), SimpleNamespace(language=args.language)
            else:
                from bounded_vad import install as install_bounded_vad
                install_bounded_vad()
                segments, info = model.transcribe(audio, initial_prompt=args.prompt or None, language=args.language, vad_filter=True)
            records = [];discarded=0
            from lecture_context import number
            for segment_index,segment in enumerate(segments,1):
                limit = metadata['actual_duration']
                raw_start=number(segment.start,'ASR start');raw_end=number(segment.end,'ASR end')
                if raw_end<raw_start or not isinstance(segment.text,str):raise ValueError('Invalid ASR segment')
                start,end=max(0,min(raw_start,limit))+args.start,max(0,min(raw_end,limit))+args.start
                if end <= start:
                    discarded+=1
                    continue
                records.append(dict(start=start, end=end, text=segment.text.strip(), asr_segment_index=segment_index,
                    asr_start=segment.start + args.start, asr_end=segment.end + args.start,
                    avg_logprob=segment.avg_logprob, no_speech_prob=segment.no_speech_prob, compression_ratio=segment.compression_ratio))
                print(f'ASR source time: {end:.2f}', flush=True)
            # A finished generator can still hold the model and full PCM array.
            del segments, audio, model
            if file_hash(args.video) != checksum:
                raise ValueError('Video changed during recognition; use a fresh output directory')
            verify_evidence(out, metadata, transcript=False, audio=True)
            records.sort(key=lambda r:(r['start'],r['end']))
            atomic_json(out / 'transcript.json', records)
            atomic_text(out / 'transcript.md', '# Автоматическое распознавание; требует проверки\n\n' +
                        '\n\n'.join(f'[{r["start"]:.2f}–{r["end"]:.2f}] {r["text"]}' for r in records))
            stages['recognition'] = dict(settings=settings, sha256=file_hash(out / 'transcript.json'),
                info=dict(model=str(model_path), language=info.language, segments=len(records),
                          feature_extraction='bounded_logmel_v1', vad_batch_frames=512,
                          speech_collection='linear_if_32_chunks_v1',
                          discarded_zero_or_outside_segments=discarded,
                          cpu_mode=limits['mode'], cpu_threads=limits['asr_threads']))
            persist(out, state)
    metadata['recognition'] = stages['recognition']['info']
    if not metadata['recognition']['segments']:
        metadata['audio_quality'] = dict(metadata['audio_quality'],
            warnings=metadata['audio_quality']['warnings'] + ['no_speech_recognized_requires_review'])
    if metadata['recognition'].get('discarded_zero_or_outside_segments'):
        metadata['audio_quality']=dict(metadata['audio_quality'],
            warnings=metadata['audio_quality']['warnings']+['asr_segments_outside_coverage_require_review'])
    metadata['run_settings'] = stages['recognition']['settings']
    metadata['evidence_sha256']['transcript.json'] = stages['recognition']['sha256']
    verify_evidence(out, metadata, audio=True)
    if 'packet' in stages:
        if not (out / 'tutor_packet.json').is_file() or file_hash(out / 'tutor_packet.json') != stages['packet']['sha256']:
            raise ValueError('Packet integrity changed; use course.py prepare --rebuild')
        print(f'Reusing completed job: {out}', flush=True)
    else:
        report('packet')
        atomic_json(out / 'manifest.json', metadata)
        from tutor_packet import build_packet
        build_packet(out)
        stages['packet'] = dict(sha256=file_hash(out / 'tutor_packet.json'))
    state.pop('last_error', None)
    state['status'] = 'complete'
    persist(out, state)
    return state
