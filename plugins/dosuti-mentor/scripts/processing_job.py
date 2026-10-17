"""Checkpointed processing in a private job folder, serialized by an OS lock."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import uuid
import wave
from atomic_files import atomic_json, atomic_text
from pilot_activity import span
from model_readiness import inspect_model, model_lock
from source_integrity import file_hash, source_matches, evidence_hashes, verify_evidence
from storage_lock import file_lock
from screen_evidence import extract_frames
from media_input import PIPELINE_VERSION, probe_media, extract_audio

MANAGED = ('audio.wav', 'frames', 'manifest.json', 'transcript.json', 'transcript.md',
           'tutor_packet.json', 'tutor_context.md', 'TUTOR_RULES.md', 'visual_notes.json', 'progress.json')


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
        try:
            state = process(args, ffmpeg, out)
            return state
        except Exception as error:
            checkpoint = out / 'checkpoint.json'
            if checkpoint.is_file():
                state = json.loads(checkpoint.read_text())
                state['last_error'] = type(error).__name__ + ': ' + str(error)[:300]
                persist(out, state)
            raise


def process(args, ffmpeg, out):
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
        state['media'] = probe_media(args.video)
        persist(out, state)
    media = state['media']
    duration = min(args.duration, media['video']['end'] - args.start)
    if duration <= 0:
        raise ValueError('No video in requested interval')
    if 'audio' not in stages:
        print('Extracting audio…', flush=True)
        with span('audio', scope=out):
            temporary, quality = extract_audio(args.video.resolve(), out, args.start, duration, media, ffmpeg,
                audio_track=inputs['audio_track'], audio_channel=inputs['audio_channel'])
            pcm_duration(temporary)
            if file_hash(args.video) != checksum:
                raise ValueError('Video changed during extraction; use a fresh output directory')
            temporary.replace(out / 'audio.wav')
            stages['audio'] = dict(sha256=file_hash(out / 'audio.wav'), duration=duration, quality=quality)
            persist(out, state)
    elif not (out / 'audio.wav').is_file() or file_hash(out / 'audio.wav') != stages['audio']['sha256']:
        raise ValueError('Prepared audio integrity changed; use course.py prepare --rebuild')
    if 'frames' not in stages:
        staging = out / '.frames-staging'
        if staging.exists():
            shutil.rmtree(staging)
        staging.mkdir()
        print('Extracting screen frames…', flush=True)
        with span('frames', scope=out):
            frames=extract_frames(args.video,staging,args.start,stages['audio']['duration'],args.frame_every,ffmpeg)
            if file_hash(args.video) != checksum:
                raise ValueError('Video changed during extraction; use a fresh output directory')
            if (out / 'frames').exists():
                shutil.rmtree(out / 'frames')
            staging.replace(out / 'frames')
            for frame in frames:
                frame['file']='frames/'+frame['file']
            stages['frames'] = dict(items=frames, hashes=evidence_hashes(out, dict(frames=frames)))
            persist(out, state)
    metadata = dict(source=args.video.name, source_path=str(args.video.resolve()), source_size=args.video.stat().st_size,
        source_sha256=checksum, source_revision='sha256:' + checksum, start=args.start,
        requested_duration=args.duration, actual_duration=stages['audio']['duration'],
        pipeline_version=PIPELINE_VERSION, media=media, audio_quality=stages['audio']['quality'],
        extraction=dict(frame_every=args.frame_every), audio_sha256=stages['audio']['sha256'],
        frames=stages['frames']['items'], evidence_sha256=dict(stages['frames']['hashes']),
        limitations=['Periodic frames may miss brief code changes.', 'Speech and code require verification.'])
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
            import onnxruntime
            onnxruntime.disable_telemetry_events()
            from faster_whisper import WhisperModel
            print(f'Loading local model {model_path}…', flush=True)
            with span('model_load', scope=out):
                model = WhisperModel(str(model_path), device='cpu', compute_type='int8', cpu_threads=4)
                from bounded_features import BoundedFeatureExtractor
                model.feature_extractor = BoundedFeatureExtractor(model.feature_extractor)
    if not previous:
        with span('asr', scope=out):
            import numpy as np
            with wave.open(str(out / 'audio.wav'), 'rb') as wav:
                audio = np.frombuffer(wav.readframes(wav.getnframes()), dtype='<i2').astype(np.float32)
                audio /= 32768.0
            if metadata['audio_quality']['status'] in ('silent','no_audio_track'):
                from types import SimpleNamespace
                segments, info = iter([]), SimpleNamespace(language=args.language)
            else:
                segments, info = model.transcribe(audio, initial_prompt=args.prompt or None, language=args.language, vad_filter=True)
            records = []
            for segment in segments:
                limit = metadata['actual_duration']
                start, end = min(segment.start, limit) + args.start, min(segment.end, limit) + args.start
                if end <= start:
                    continue
                records.append(dict(start=start, end=end, text=segment.text.strip(),
                    asr_start=segment.start + args.start, asr_end=segment.end + args.start,
                    avg_logprob=segment.avg_logprob, no_speech_prob=segment.no_speech_prob, compression_ratio=segment.compression_ratio))
                print(f'ASR source time: {end:.2f}', flush=True)
            if file_hash(args.video) != checksum:
                raise ValueError('Video changed during recognition; use a fresh output directory')
            verify_evidence(out, metadata, transcript=False, audio=True)
            atomic_json(out / 'transcript.json', records)
            atomic_text(out / 'transcript.md', '# Автоматическое распознавание; требует проверки\n\n' +
                        '\n\n'.join(f'[{r["start"]:.2f}–{r["end"]:.2f}] {r["text"]}' for r in records))
            stages['recognition'] = dict(settings=settings, sha256=file_hash(out / 'transcript.json'),
                info=dict(model=str(model_path), language=info.language, segments=len(records),
                          feature_extraction='bounded_logmel_v1'))
            persist(out, state)
    metadata['recognition'] = stages['recognition']['info']
    if not metadata['recognition']['segments']:
        metadata['audio_quality'] = dict(metadata['audio_quality'],
            warnings=metadata['audio_quality']['warnings'] + ['no_speech_recognized_requires_review'])
    metadata['run_settings'] = stages['recognition']['settings']
    metadata['evidence_sha256']['transcript.json'] = stages['recognition']['sha256']
    verify_evidence(out, metadata, audio=True)
    if 'packet' in stages:
        if not (out / 'tutor_packet.json').is_file() or file_hash(out / 'tutor_packet.json') != stages['packet']['sha256']:
            raise ValueError('Packet integrity changed; use course.py prepare --rebuild')
        print(f'Reusing completed job: {out}', flush=True)
    else:
        atomic_json(out / 'manifest.json', metadata)
        from tutor_packet import build_packet
        build_packet(out)
        stages['packet'] = dict(sha256=file_hash(out / 'tutor_packet.json'))
    state.pop('last_error', None)
    state['status'] = 'complete'
    persist(out, state)
    return state
