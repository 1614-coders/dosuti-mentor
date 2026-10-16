"""Publish a coherent job snapshot; never edit a previously published fragment."""
import json
from pathlib import Path
import shutil
import uuid
from atomic_files import atomic_json
from source_integrity import file_hash, verify_evidence, source_matches
from storage_lock import file_lock
from tutor_packet import build_packet


def valid_publication(folder, key, checksum):
    try:
        marker = json.loads((folder / 'publication.json').read_text())
        manifest = json.loads((folder / 'manifest.json').read_text())
        if marker.get('job_key') != key or marker.get('packet_sha256') != file_hash(folder / 'tutor_packet.json'):
            return False
        source_matches(manifest, checksum)
        verify_evidence(folder, manifest, audio=True)
        return True
    except (OSError, ValueError, TypeError):
        return False


def publish_job(working, prepared, key, preferred=None):
    working, prepared = Path(working), Path(prepared)
    prepared.mkdir(parents=True, exist_ok=True)
    with file_lock(working / '.processing.lock'):
        checkpoint = json.loads((working / 'checkpoint.json').read_text())
        if checkpoint.get('status') != 'complete' or checkpoint['stages']['packet']['sha256'] != file_hash(working / 'tutor_packet.json'):
            raise ValueError('Processing job is not complete; nothing published')
        manifest = json.loads((working / 'manifest.json').read_text())
        verify_evidence(working, manifest, audio=True)
        with file_lock(prepared / ('.' + key + '.publish.lock')):
            target = prepared / ('v4-' + key[:24])
            if preferred:
                candidate = Path(preferred)
                if candidate.resolve().is_relative_to(prepared.resolve()) and valid_publication(candidate, key, manifest['source_sha256']):
                    return candidate
            if valid_publication(target, key, manifest['source_sha256']):
                return target
            if target.exists():
                target = target.with_name(target.name + '-' + uuid.uuid4().hex[:8])
            staging = prepared / ('.pending-' + uuid.uuid4().hex)
            staging.mkdir()
            try:
                for name in ['manifest.json', 'transcript.json', 'audio.wav']:
                    shutil.copyfile(working / name, staging / name)
                shutil.copytree(working / 'frames', staging / 'frames')
                for name in ['visual_notes.json', 'progress.json', 'transcript.md']:
                    if (working / name).is_file():
                        shutil.copyfile(working / name, staging / name)
                verify_evidence(staging, manifest, audio=True)
                build_packet(staging, display_folder=target)
                atomic_json(staging / 'publication.json', dict(job_key=key,
                            packet_sha256=file_hash(staging / 'tutor_packet.json')))
                staging.replace(target)
                return target
            finally:
                if staging.exists():
                    shutil.rmtree(staging)
