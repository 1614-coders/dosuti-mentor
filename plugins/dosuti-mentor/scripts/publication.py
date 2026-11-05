"""Publish a coherent job snapshot; never edit a previously published fragment."""
import json
import hashlib
import os
import re
from pathlib import Path
import shutil
import uuid
from atomic_files import atomic_json
from source_integrity import file_hash, verify_evidence, source_matches
from storage_lock import file_lock
from tutor_packet import build_packet


def valid_publication(folder, key, checksum):
    """Return False for an invalid cached publication; never repair it in place."""
    try:
        marker = json.loads((folder / 'publication.json').read_text())
        manifest = json.loads((folder / 'manifest.json').read_text())
        if not isinstance(marker, dict) or not isinstance(manifest, dict):
            return False
        if not isinstance(marker.get('job_key'), str) or not isinstance(marker.get('packet_sha256'), str):
            return False
        frames = manifest.get('frames', [])
        if not isinstance(frames, list) or any(not isinstance(frame, dict)
                or not isinstance(frame.get('file'), str) for frame in frames):
            return False
        if marker.get('job_key') != key or marker.get('packet_sha256') != file_hash(folder / 'tutor_packet.json'):
            return False
        source_matches(manifest, checksum)
        verify_evidence(folder, manifest, audio=True)
        return True
    except (OSError, ValueError, TypeError):
        return False


def publish_job(working, prepared, key, preferred=None):
    from safe_material_import import Attempt
    from safe_zip_import import supported
    if not isinstance(key, str) or not key or len(key) > 200 or any(c in key for c in ('/', '\\', '\0')) or key in ('.', '..'):
        raise ValueError('Publication key must be one nonempty filename component')
    if not supported():
        raise ValueError('Safe job publication is unavailable on this platform; no unsafe fallback')
    working, prepared = Path(working).absolute(), Path(prepared).absolute()
    attempt = Attempt(prepared, lesson=False, readonly=False)
    try:
        working_fd = attempt.guard.pin(working)
        with file_lock(working / '.processing.lock', directory_fd=working_fd):
            try:
                with attempt.input(working / 'checkpoint.json') as stream:
                    checkpoint = json.load(stream)
            except (json.JSONDecodeError, UnicodeDecodeError) as error:
                raise ValueError('Invalid checkpoint.json: unreadable JSON; nothing published') from error
            if not isinstance(checkpoint, dict) or not isinstance(checkpoint.get('status'), str):
                raise ValueError('Invalid checkpoint.json: expected an object with status; nothing published')
            stages = checkpoint.get('stages')
            packet = stages.get('packet') if isinstance(stages, dict) else None
            packet_checksum = packet.get('sha256') if isinstance(packet, dict) else None
            if not isinstance(packet_checksum, str) or not re.fullmatch(r'[a-fA-F0-9]{64}', packet_checksum):
                raise ValueError('Invalid checkpoint.json: expected stages.packet.sha256; nothing published')
            with attempt.input(working / 'tutor_packet.json') as stream:
                packet_hash = hashlib.file_digest(stream, 'sha256').hexdigest()
            if checkpoint.get('status') != 'complete' or packet_checksum != packet_hash:
                raise ValueError('Processing job is not complete; nothing published')
            with attempt.input(working / 'manifest.json') as stream: manifest = json.load(stream)
            verify_evidence(working, manifest, audio=True)
            with file_lock(prepared / ('.' + key + '.publish.lock'), directory_fd=attempt.fd):
                target = prepared / ('v4-' + key[:24])
                candidates = [target]
                if preferred:
                    candidate = Path(os.path.abspath(preferred))
                    if candidate.is_relative_to(prepared): candidates.insert(0, candidate)
                for candidate in candidates:
                    try: attempt.guard.pin(candidate)
                    except FileNotFoundError: continue
                    attempt.guard.check()
                    if valid_publication(candidate, key, manifest['source_sha256']):
                        attempt.guard.check()
                        return candidate
                if target.exists(): target = target.with_name(target.name + '-' + uuid.uuid4().hex[:8])
                attempt.paths.append(target)
                target_fd = attempt.fresh_at(attempt.fd, target.name)
                for name in ('manifest.json', 'transcript.json', 'audio.wav'):
                    attempt.copy(working / name, target_fd, name, destination=target / name)
                from safe_prepared_import import copy_frames
                copy_frames(attempt, working, target, target_fd, manifest)
                for name in ('visual_notes.json', 'progress.json', 'transcript.md'):
                    if (working / name).exists():
                        attempt.copy(working / name, target_fd, name, destination=target / name)
                verify_evidence(target, manifest, audio=True)

                def write(path, text):
                    attempt.check()
                    attempt.text(target, target_fd, path.name, text)

                build_packet(target, display_folder=target, writer=write)
                attempt.check()
                checksum = attempt.files[(target / 'tutor_packet.json').absolute()][3]
                # No second rename of a publicly replaceable source directory.
                # The marker is the commit record; without it no partial folder
                # passes valid_publication or enters the lesson registry.
                write(target / 'publication.json', json.dumps(dict(job_key=key,
                      packet_sha256=checksum), ensure_ascii=False, indent=2, allow_nan=False))
                attempt.check()
                attempt.published = True
                return target
    finally:
        # A name may now belong to a peer. Never recursively discard it.
        attempt.close()
