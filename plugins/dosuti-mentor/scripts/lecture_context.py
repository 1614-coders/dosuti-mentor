"""Merge source evidence without claiming that sampling or ASR is complete."""
from copy import deepcopy
import hashlib
import json
import math
from pathlib import Path
from asr_quality import diagnose
from source_integrity import (file_hash, source_matches, verify_evidence,
                              check_local_video, verify_snapshot, digest)
from read_snapshot import watch


def read(path):
    try:
        return json.loads(Path(path).read_text(encoding='utf-8'))
    except (OSError, ValueError) as error:
        raise ValueError(f'Cannot read context evidence: {path}: {error}') from error


def number(value, label):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f'{label} must be a finite number')
    return value


def interval(value, label):
    if not isinstance(value, dict):
        raise ValueError(f'{label} must contain start and end')
    start = number(value.get('start'), label + '.start')
    end = number(value.get('end'), label + '.end')
    if start < 0 or end <= start:
        raise ValueError(f'{label} requires 0 <= start < end')
    return start, end


def coverage(ranges, start, stop):
    merged = []
    for left, right in sorted(ranges):
        left, right = max(left, start), min(right, stop)
        if right <= left:
            continue
        if merged and left <= merged[-1]['end']:
            merged[-1]['end'] = max(merged[-1]['end'], right)
        else:
            merged.append(dict(start=left, end=right))
    gaps, cursor = [], start
    for item in merged:
        if cursor < item['start']:
            gaps.append(dict(start=cursor, end=item['start']))
        cursor = item['end']
    if cursor < stop:
        gaps.append(dict(start=cursor, end=stop))
    return dict(start=merged[0]['start'] if merged else None,
                end=merged[-1]['end'] if merged else None,
                ranges=merged, complete=not gaps), gaps


def load_fragment(fragment, directory, expected_hash):
    folder = Path(fragment['folder']).expanduser().resolve()
    packet_file = folder / 'tutor_packet.json'
    packet = read(packet_file)
    if not isinstance(packet, dict):
        raise ValueError(f'Invalid context packet: {packet_file}')
    if fragment.get('packet_sha256') != file_hash(packet_file):
        raise ValueError('Packet integrity changed or legacy packet lacks a hash; prepare again')
    bounds = interval(packet.get('coverage'), f'Packet coverage in {packet_file}')
    if bounds != interval(fragment.get('coverage'), 'Registered coverage'):
        raise ValueError(f'Packet coverage differs from registered coverage: {packet_file}')
    for kind in ['speech', 'frames']:
        if not isinstance(packet.get(kind), list):
            raise ValueError(f'Packet {kind} must be a list: {packet_file}')
    if not isinstance(packet.get('source'), str):
        raise ValueError(f'Packet source is missing: {packet_file}')
    try:
        identity = str(folder.relative_to(directory.resolve()))
    except ValueError:
        identity = str(folder)
    fragment_id = 'p' + hashlib.sha256(identity.encode()).hexdigest()[:16]
    # The manifest supplies descriptive provenance, not proof of video identity.
    manifest = read(folder / 'manifest.json') if (folder / 'manifest.json').exists() else {}
    source_matches(manifest, expected_hash)
    source_matches(packet, expected_hash)
    if fragment.get('source_sha256') != expected_hash:
        raise ValueError('Registered fragment source SHA256 does not match video')
    verify_evidence(folder, manifest, frame_names=[])
    source = dict(fragment_id=fragment_id, source=packet['source'],
                  source_path=manifest.get('source_path'),
                  source_size=manifest.get('source_size'),
                  source_sha256=manifest.get('source_sha256'),
                  identity_verification='pinned_source_hash_matches',
                  _evidence_sha256=manifest['evidence_sha256'],
                  folder=str(folder), packet_file=str(packet_file),
                  rules_file=str(folder / 'TUTOR_RULES.md'),
                  notes_file=str(folder / 'visual_notes.json'), coverage=packet['coverage'],
                  recognition=packet.get('recognition'), limitations=packet.get('limitations', []),
                  asr_quality=diagnose(packet['speech']),
                  audio_quality=packet.get('audio_quality', {'status': 'legacy_unverified', 'warnings': ['legacy_media_timeline_requires_rebuild']}))
    if not Path(source['rules_file']).is_file():
        raise ValueError(f'Missing teaching rules: {source["rules_file"]}')
    return packet, source


def evidence(source, local_id):
    return dict(fragment_id=source['fragment_id'], local_id=local_id,
                packet_file=source['packet_file'])


def deduplicate(events, key):
    """Exact matches only. Distinct words/times may represent an actual correction."""
    unique, aliases = {}, {}
    for event in events:
        identity = key(event)
        if identity in unique:
            event['provenance'] = unique[identity]['provenance'] + event['provenance']
        unique[identity] = event  # Last registered recognition owns the display ID.
    for event in unique.values():
        for origin in event['provenance']:
            aliases[origin['fragment_id'] + ':' + origin['local_id']] = event['id']
    return list(unique.values()), aliases


def note_ids(item, fragment_id, aliases):
    if isinstance(item, list):
        return [note_ids(value, fragment_id, aliases) for value in item]
    if not isinstance(item, dict):
        return item
    result = {}
    for key, value in item.items():
        if key in ('frame_id', 'speech_id') and isinstance(value, str):
            scoped = fragment_id + ':' + value
            result[key] = aliases.get(scoped, scoped)
        elif key in ('evidence_frames', 'frame_ids', 'speech_ids') and isinstance(value, list):
            result[key] = [aliases.get(fragment_id + ':' + v, fragment_id + ':' + v)
                           if isinstance(v, str) else v for v in value]
        else:
            result[key] = note_ids(value, fragment_id, aliases)
    return result


def overlaps(speech, limit=100):
    pairs, active = [], []
    for current in speech:
        active = [other for other in active if other['end'] > current['start']]
        current_sources = {p['fragment_id'] for p in current['provenance']}
        for other in active:
            other_sources = {p['fragment_id'] for p in other['provenance']}
            if current_sources.isdisjoint(other_sources):
                if len(pairs) == limit:
                    return pairs, True
                pairs.append(dict(speech_ids=[other['id'], current['id']],
                    start=max(other['start'], current['start']),
                    end=min(other['end'], current['end']),
                    verification='overlap_needs_review'))
        active.append(current)
    return pairs, False


def assemble(directory, data, start, window, include_code_stages=True, include_term_notes=True, include_verbal_homework=True):
    number(start, 'Time')
    number(window, 'Window')
    stop = number(start + window, 'Window end')
    if start < 0 or window <= 0 or stop <= start:
        raise ValueError('Time must be nonnegative and window must advance time')
    packets, ranges, speech, frames, prior_frames = [], [], [], [], []
    seen = set()
    video_state = None
    snapshot_state = verify_snapshot(data['code'])
    from code_repositories import describe as repositories
    teacher_repositories = repositories(data)
    for fragment in data['prepared_fragments']:
        left, right = interval(fragment.get('coverage'), 'Registered coverage')
        intersects = left < stop and right > start
        # An adjacent earlier packet may supply a preceding sample at the seam.
        anchor_only = right == start
        if not intersects and not anchor_only:
            continue
        if video_state is None:
            video_state = check_local_video(data['video'])
        packet, source = load_fragment(fragment, directory, data['video']['sha256'])
        if source['fragment_id'] in seen:
            continue
        seen.add(source['fragment_id'])
        source['role'] = 'window_evidence' if intersects else 'preceding_sample_only'
        packets.append((packet, source))
        if intersects:
            ranges.append((left, right))
        local_ids = set()
        for i, record in enumerate(packet['speech'], 1):
            if not isinstance(record, dict):
                raise ValueError('Speech record must be an object')
            rs, re = interval(record, 'Speech timestamp')
            if rs < left or re > right or not isinstance(record.get('text'), str):
                raise ValueError('Speech timestamp or text is outside packet coverage')
            local_id = record.get('id', f's{i:05d}')
            if not isinstance(local_id, str) or not local_id or local_id in local_ids:
                raise ValueError('Speech ID must be a nonempty unique string')
            local_ids.add(local_id)
            if intersects and rs < stop and re > start:
                row = deepcopy(record)
                row.update(id=source['fragment_id'] + ':' + local_id,
                           local_id=local_id, provenance=[evidence(source, local_id)],
                           selected_interval=dict(start=max(rs, start), end=min(re, stop)))
                speech.append(row)
        for i, record in enumerate(packet['frames'], 1):
            if not isinstance(record, dict):
                raise ValueError('Frame record must be an object')
            seconds = number(record.get('approx_seconds'), 'Frame timestamp')
            if seconds < left or seconds > right:
                raise ValueError('Frame timestamp is outside packet coverage')
            local_id = record.get('id', f'f{i:05d}')
            if not isinstance(local_id, str) or not local_id or local_id in local_ids:
                raise ValueError('Frame ID must be a nonempty unique string')
            local_ids.add(local_id)
            in_window = intersects and start <= seconds < stop
            preceding = left <= start <= right and seconds < start
            if not in_window and not preceding:
                continue
            row = deepcopy(record)
            row.update(id=source['fragment_id'] + ':' + local_id, local_id=local_id,
                       selection='in_window' if in_window else 'preceding_sample',
                       screen_state_confirmed=False,
                       provenance=[evidence(source, local_id)])
            row['_folder'] = source['folder']
            row['_expected_sha256'] = source['_evidence_sha256'].get(row.get('file'))
            if preceding:
                row['age_seconds'] = start - seconds
                prior_frames.append(row)
            else:
                frames.append(row)
    # Preserve all different samples tied for nearest preceding time.
    if prior_frames and not any(f['approx_seconds'] == start for f in frames):
        nearest = max(f['approx_seconds'] for f in prior_frames)
        frames.extend(f for f in prior_frames if f['approx_seconds'] == nearest)
    for row in frames:
        if not isinstance(row.get('file'), str):
            raise ValueError('Frame file must be a string')
        folder = Path(row.pop('_folder'))
        path = (folder / row['file']).resolve()
        if not path.is_relative_to(folder) or not path.is_file():
            raise ValueError(f'Missing frame or frame outside fragment folder: {path}')
        watch(path)
        with path.open('rb') as stream:
            checksum = hashlib.file_digest(stream, 'sha256').hexdigest()
        watch(path)
        if checksum != digest(row.pop('_expected_sha256'), 'Selected frame SHA256'):
            raise ValueError(f'Selected frame integrity changed: {path}')
        row.update(absolute_path=str(path), sha256=checksum)
    from screen_evidence import context_scans
    screen_frames,screen_sources=context_scans(directory,data,start,stop)
    if screen_sources and video_state is None:
        video_state=check_local_video(data['video'])
    frames.extend(screen_frames)
    speech, speech_aliases = deduplicate(speech, lambda r: (r['start'], r['end'], r['text'], r.get('speaker')))
    frames, frame_aliases = deduplicate(frames, lambda f: (f['approx_seconds'], f['sha256']))
    speech.sort(key=lambda r: (r['start'], r['end'], r['id']))
    frames.sort(key=lambda f: (f['approx_seconds'], f['id']))
    notes = dict(observations=[], uncertainties=[], sources=[])
    aliases = {**speech_aliases, **frame_aliases}
    for packet, source in packets:
        notes['sources'].append(dict(fragment_id=source['fragment_id'], file=source['notes_file']))
        original = read(source['notes_file'])
        if not isinstance(original, dict):
            raise ValueError(f'Invalid visual notes: {source["notes_file"]}')
        for kind in ['observations', 'uncertainties']:
            if not isinstance(original.get(kind, []), list):
                raise ValueError(f'Visual notes {kind} must be a list')
            for item in original.get(kind, []):
                if not isinstance(item, dict):
                    raise ValueError('Visual note must be an object')
                bounds = item.get('range')
                if isinstance(bounds, list) and len(bounds) == 2:
                    ns, ne = interval(dict(start=bounds[0], end=bounds[1]), 'Visual note range')
                    if not (ns < stop and ne > start):
                        continue
                row = note_ids(item, source['fragment_id'], aliases)
                row['provenance'] = dict(fragment_id=source['fragment_id'], notes_file=source['notes_file'])
                if row.get('source_kind') == 'attached_code':
                    old_hash=row.get('source_sha256')
                    from code_repositories import resolve
                    try:
                        code=resolve(data,row.get('source_repository','default'))
                    except (ValueError,OSError,KeyError,TypeError):
                        code={}
                    current_hash=code.get('content_sha256')
                    current=bool(old_hash and old_hash==current_hash)
                    historical=any(old_hash and old_hash==item.get('content_sha256') for item in code.get('history',[]))
                    row['stale']=not current
                    row['source_status']='current' if current else ('historical' if historical else 'unverifiable')
                    if not current:
                        row['original_verification']=row.get('verification')
                        row['verification']='requires_review'
                        row['usable_as_current_evidence']=False
                    else:
                        row['usable_as_current_evidence']=True
                notes[kind].append(row)
    covered, gaps = coverage(ranges, start, stop)
    conflicting, truncated = overlaps(speech)
    warnings = ['periodic_frames_may_miss_changes',
                'asr_and_speakers_need_verification']
    if any(n.get('stale') for n in notes['observations']+notes['uncertainties']):
        warnings.append('attached_code_notes_stale_or_unverifiable')
    for packet, source in packets:
        for warning in source['audio_quality'].get('warnings', []) + source['asr_quality']['warnings']:
            if warning not in warnings:
                warnings.append(warning)
    for field in ('processing_jobs','screen_jobs'):
        if any(job.get('status') in ('pending','failed') for job in data.get(field,{}).values()):
            warnings.append(field+'_have_unfinished_or_failed_work')
    if screen_sources:
        warnings.append('screen_scan_candidates_do_not_verify_meaning')
    if video_state is None:
        warnings.append('no_prepared_source_identity')
    elif video_state == 'original_unavailable_using_pinned_hash':
        warnings.append('original_video_unavailable')
    if gaps:
        warnings.append('unprepared_intervals')
    if conflicting:
        warnings.append('overlapping_transcripts_need_review')
    if any(f['selection'] == 'preceding_sample' for f in frames):
        warnings.append('preceding_sample_may_no_longer_match_screen')
    if len({f['approx_seconds'] for f in frames}) < len(frames):
        warnings.append('different_frames_at_same_timestamp')
    sources = [{key: value for key, value in source.items() if not key.startswith('_')}
               for packet, source in packets]
    from material_sources import describe
    from semantic_context import boundary_speech
    crossings = boundary_speech(speech, start, stop)
    if crossings: warnings.append('speech_crosses_window_boundary')
    from code_stages import checked
    stages = checked(directory, data, start, stop) if include_code_stages else []
    if any(row['stale'] for row in stages): warnings.append('stale_code_stage')
    if any(row['overlapping_stage_ids'] for row in stages): warnings.append('ambiguous_code_stages')
    from term_notes import checked as checked_terms
    terms = checked_terms(directory, data, start, stop) if include_term_notes else []
    if any(row['stale'] for row in terms): warnings.append('stale_term_interpretation')
    from compact_lesson import memory_context
    memory = memory_context(directory, start, stop, {f['id'] for f in frames})
    result = dict(schema_version=2, source=data['video']['source'], registered_source=deepcopy(data['video']),
                video_integrity=video_state, code_integrity=snapshot_state,
                coverage=covered, gaps=gaps, requested_window=[start, stop],
                sources=sources,screen_scan_sources=screen_sources, rules=[source['rules_file'] for source in sources],
                speech=speech, boundary_speech=crossings, speech_overlaps=conflicting,
                speech_overlap_details_truncated=truncated,
                frames=frames, visual_notes=notes, warnings=warnings,
                term_notes=terms, code_stages=stages, teacher_code=data['code'], teacher_repositories=teacher_repositories,
                homework=data['homework'], resources=describe(data['resources']))
    if include_verbal_homework:
        from spoken_homework import resolve
        result['homework'] = resolve(directory, data)
        from course import homework_revision
        saved_revision = (memory or {}).get('review_scope', {}).get('homework_revision')
        if saved_revision is not None and saved_revision != homework_revision(result['homework']):
            result['warnings'].append('lecture_memory_homework_review_is_outdated_use_current_homework')
    if memory is not None: result['lecture_memory'] = memory
    from material_quality import summarize
    result['quality_summary'] = summarize(result)
    return result
