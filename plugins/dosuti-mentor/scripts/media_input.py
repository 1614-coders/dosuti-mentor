"""Bounded-memory media inventory and timeline-preserving audio extraction."""
import math
from pathlib import Path
import subprocess
from preparation_control import run as preparation_run
import wave
import numpy as np

PIPELINE_VERSION = 7


def decoding_errors(stderr):
    # Called with loglevel=error (or showinfo for frames). Keep meaningful diagnostics.
    markers = ('partial file', 'invalid data', 'error while decoding', 'error submitting',
               'corrupt', 'packet too small', 'invalid nal', 'truncated', 'failed to read')
    return [line.strip() for line in stderr.splitlines() if any(m in line.lower() for m in markers)]


def probe_media(path):
    import av
    try:
        with av.open(str(path)) as container:
            origin = (container.start_time or 0) / 1_000_000
            tracks = {}
            for stream in container.streams:
                if stream.type not in ('audio', 'video'):
                    continue
                rate = float(stream.average_rate or 25) if stream.type == 'video' else None
                tracks[stream.index] = dict(index=stream.index, kind=stream.type, start=None, end=None,
                    channels=len(stream.codec_context.layout.channels) if stream.type == 'audio' else None,
                    title=stream.metadata.get('title'), language=stream.metadata.get('language'),
                    frame_duration=1 / rate if rate and rate > 0 else None,
                    declared_end=(float((stream.start_time or 0) + stream.duration) * float(stream.time_base) - origin)
                        if stream.duration is not None else None)
            for packet in container.demux():
                row = tracks.get(packet.stream.index)
                if row is None or packet.pts is None or not packet.size:
                    continue
                if packet.is_corrupt:
                    raise ValueError('Damaged media: corrupt packet; restore a complete source')
                left = float(packet.pts * packet.time_base) - origin
                length = float((packet.duration or 0) * packet.time_base)
                right = left + (length or row['frame_duration'] or 0)
                row['start'] = left if row['start'] is None else min(row['start'], left)
                row['end'] = right if row['end'] is None else max(row['end'], right)
            videos = [r for r in tracks.values() if r['kind'] == 'video' and r['end'] is not None]
            audible=[r for r in tracks.values() if r['kind']=='audio' and r['end'] is not None]
            if not videos and not audible:
                raise ValueError('No readable audio/video packets')
            video = videos[0] if videos else None
            if video and video['declared_end'] is not None and video['declared_end'] - video['end'] > max(.15, 3 * video['frame_duration']):
                raise ValueError('Damaged media: video ends before its declared duration; restore a complete source')
            return dict(origin=origin, video=video, audio=[r for r in tracks.values() if r['kind'] == 'audio'],
                        tracks=list(tracks.values()), verification='packet_inventory_not_semantic_review')
    except (OSError, RuntimeError) as error:
        raise ValueError(f'Cannot inspect media: {error}') from error


def timeline_end(media):
    if media.get('video'):return media['video']['end']
    ends=[r['end'] for r in media['audio'] if r.get('end') is not None]
    if not ends:raise ValueError('No readable audio/video timeline')
    return max(ends)


def signal_stats(path):
    total = 0
    squares = mono_squares = None
    peaks = None
    with wave.open(str(path), 'rb') as audio:
        channels = audio.getnchannels()
        while raw := audio.readframes(16000 * 10):
            values = np.frombuffer(raw, dtype='<i2').reshape(-1, channels).astype(np.float64)
            energy = (values * values).sum(axis=0)
            squares = energy if squares is None else squares + energy
            peak = np.abs(values).max(axis=0)
            peaks = peak if peaks is None else np.maximum(peaks, peak)
            mono = values.mean(axis=1)
            mono_squares = (mono_squares or 0) + float((mono * mono).sum())
            total += len(values)
    if not total:
        raise ValueError('Decoded audio is empty')
    return dict(channel_rms=np.sqrt(squares / total).tolist(), channel_peaks=peaks.tolist(),
                mono_rms=math.sqrt(mono_squares / total), samples=total, channels=channels)


def extract_audio(video, folder, start, duration, media, ffmpeg, audio_track=None, audio_channel=None, threads=None):
    """Track numbers are absolute stream indexes; channel numbers are zero based."""
    tracks = media['audio']
    if audio_track is not None:
        tracks = [r for r in tracks if r['index'] == audio_track]
        if not tracks:
            raise ValueError('Selected audio track does not exist; choose an index from the media inventory')
    if not tracks and audio_channel is not None:
        raise ValueError('Cannot select a channel without audio')
    candidates = []
    paths = []
    try:
        for track in tracks:
            if not track['channels'] or track['channels'] > 8:
                raise ValueError('Audio layout is unsupported; provide a mono/stereo source')
            target = folder / f'audio-track-{track["index"]}.staging.wav'
            paths.append(target)
            # first_pts=0 pads a late first packet after input seeking; apad keeps the visual tail.
            result = preparation_run([ffmpeg, '-hide_banner', '-loglevel', 'error', '-xerror', '-y',
                *(['-threads', str(threads), '-filter_threads', str(threads)] if threads else []),
                '-ss', str(start), '-i', str(video), '-map', f'0:{track["index"]}', '-vn',
                '-af', f'aresample=16000:async=1:first_pts=0,apad=whole_dur={duration}',
                '-ar', '16000', '-t', str(duration), '-c:a', 'pcm_s16le',
                *(['-threads', str(threads)] if threads else []), str(target)],
                check=True, capture_output=True, text=True, process_group=False, timeout=max(120, duration * 10))
            errors = decoding_errors(result.stderr)
            if errors:
                raise ValueError('Damaged audio: ' + '; '.join(errors)[:600])
            candidates.append(dict(track=track, file=target, signal=signal_stats(target)))
        # Near-zero threshold detects empty channels, not speech; music also needs explicit choice.
        audible = [r for r in candidates if max(r['signal']['channel_peaks']) > 8]
        if audio_track is None and len(audible) > 1:
            raise ValueError('Multiple nonempty audio tracks; choose --audio-track: ' +
                             ', '.join(str(r['track']['index']) for r in audible))
        selected = (audible or candidates or [None])[0]
        output = folder / 'audio.staging.wav'
        warnings = []
        channel = audio_channel
        if selected is None:
            with wave.open(str(output), 'wb') as wav:
                wav.setparams((1, 2, 16000, 0, 'NONE', 'not compressed'))
                remaining = round(duration * 16000)
                while remaining:
                    count = min(remaining, 160000); wav.writeframes(bytes(count * 2)); remaining -= count
            return output, dict(status='no_audio_track', selected_track=None, selected_channel=None,
                warnings=['visual_only_no_audio_track'], tracks=media['audio'], signal=None)
        stats = selected['signal']
        if channel is not None and not 0 <= channel < stats['channels']:
            raise ValueError('Selected audio channel is outside track layout')
        if channel is None and stats['channels'] > 1 and stats['mono_rms'] < .1 * max(stats['channel_rms']):
            # Strong cancellation means averaging destroys information. Pick the strongest channel and disclose.
            channel = int(np.argmax(stats['channel_rms']))
            warnings.append('channel_cancellation_using_single_channel')
        with wave.open(str(selected['file']), 'rb') as source, wave.open(str(output), 'wb') as target:
            target.setparams((1, 2, 16000, 0, 'NONE', 'not compressed'))
            while raw := source.readframes(160000):
                values = np.frombuffer(raw, dtype='<i2').reshape(-1, stats['channels']).astype(np.float64)
                mono = values[:, channel] if channel is not None else values.mean(axis=1)
                target.writeframes(np.rint(mono).clip(-32768, 32767).astype('<i2').tobytes())
        final_stats = signal_stats(output)
        silent = max(final_stats['channel_peaks']) <= 8
        if silent: warnings.append('selected_audio_is_silent')
        return output, dict(status='silent' if silent else 'signal_present_unverified',
            selected_track=selected['track']['index'], selected_channel=channel,
            selection='explicit' if audio_track is not None else 'only_nonempty_or_first_silent',
            warnings=warnings, tracks=media['audio'], signal=stats, output_signal=final_stats,
            timeline=dict(source_start=start, samples_per_second=16000, padding_preserves_source_time=True))
    finally:
        for path in paths:
            path.unlink(missing_ok=True)
