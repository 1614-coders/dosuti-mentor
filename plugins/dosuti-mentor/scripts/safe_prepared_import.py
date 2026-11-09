"""Copy and build an imported fragment without reopening output paths for writing."""
import uuid
from safe_material_import import current


def stage(args, directory, source):
    from tutor_packet import build_packet
    attempt = current(directory)
    target, fd = attempt.fresh('prepared', 'import-' + uuid.uuid4().hex[:8])
    for name in ('manifest.json', 'transcript.json'):
        attempt.copy(source / name, fd, name, destination=target / name)
    import json
    with attempt.input(target / 'manifest.json') as stream: manifest = json.load(stream)
    copy_frames(attempt, source, target, fd, manifest)
    if (source / 'visual_notes.json').exists():
        attempt.copy(source / 'visual_notes.json', fd, 'visual_notes.json',
                     destination=target / 'visual_notes.json')

    def write(path, text):
        # Packet generation may read by pathname, but every write stays pinned.
        # The guard also prevents publishing a replaced or detached snapshot.
        attempt.check()
        attempt.text(target, fd, path.name, text)

    build_packet(target, writer=write)
    attempt.check()
    return target


def metadata(attempt, fd, name, value):
    """Status metadata uses the same pinned-parent atomic replacement contract."""
    import json
    import os
    from safe_zip_import import new_file
    attempt.guard.check()
    temporary = name + '.' + uuid.uuid4().hex + '.tmp'
    with os.fdopen(new_file(fd, temporary), 'w', encoding='utf-8', newline='\n') as stream:
        stream.write(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False))
        stream.flush(); os.fsync(stream.fileno())
    attempt.guard.check()
    os.rename(temporary, name, src_dir_fd=fd, dst_dir_fd=fd)


def copy_frames(attempt, source, target, fd, manifest):
    """Copy only indexed evidence, never orphaned frame attempts or foreign leftovers."""
    from pathlib import PurePosixPath
    frames = manifest.get('frames', [])
    if len(frames) > 5000: raise ValueError('Frame count exceeds import limit')
    directories = {('frames',): attempt.fresh_at(fd, 'frames')}
    total = 0
    for row in frames:
        parts = PurePosixPath(row['file']).parts
        if not parts or parts[0] != 'frames' or len(parts) < 2 or len(parts) > 64 or '..' in parts or '\\' in row['file']:
            raise ValueError('Invalid frame evidence path')
        for depth in range(2, len(parts)):
            prefix = parts[:depth]
            if prefix not in directories:
                directories[prefix] = attempt.fresh_at(directories[prefix[:-1]], prefix[-1])
        copied = attempt.copy(source.joinpath(*parts), directories[parts[:-1]], parts[-1], destination=target.joinpath(*parts))
        total += copied['size']
        if total > 256*1024*1024: raise ValueError('Frames exceed 256 MiB')
        if copied['sha256'] != manifest['evidence_sha256'].get(row['file']): raise ValueError('Frame integrity changed during copying')
