"""Run a local, checkpointed lecture-processing job."""
import argparse
import math
import os
from pathlib import Path
import subprocess  # Shared module retained for controlled failure injection in tests.
from processing_job import run_job


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('video', type=Path)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--start', type=float, default=0)
    parser.add_argument('--duration', type=float, default=1200)
    parser.add_argument('--frame-every', type=float, default=10,
                        help='Seconds between navigation frames (default: 10); use scan-screen for brief changes')
    parser.add_argument('--model', default='small')
    parser.add_argument('--language', default='ru')
    parser.add_argument('--prompt', default='')
    parser.add_argument('--audio-track', type=int, help='Absolute audio stream index; required when several nonempty tracks exist')
    parser.add_argument('--audio-channel', type=int, help='Zero-based channel index; otherwise detect destructive cancellation')
    parser.add_argument('--prepare-only', action='store_true')
    parser.add_argument('--resume', action='store_true')
    args = parser.parse_args()
    if not args.video.is_file():
        parser.error('Video file does not exist')
    if (not all(math.isfinite(v) for v in [args.start, args.duration, args.frame_every])
            or args.start < 0 or args.duration <= 0 or args.frame_every <= 0):
        parser.error('Start must be nonnegative; duration and frame interval positive')
    if any(v is not None and v < 0 for v in (args.audio_track, args.audio_channel)):
        parser.error('Audio track/channel indexes must be nonnegative')
    import imageio_ffmpeg
    import certifi
    os.environ.setdefault('SSL_CERT_FILE', certifi.where())
    os.environ['HF_HUB_OFFLINE'] = '1'
    try:
        run_job(args, imageio_ffmpeg.get_ffmpeg_exe())
    except ValueError as error:
        parser.error(str(error))
    except (OSError, RuntimeError, subprocess.CalledProcessError) as error:
        parser.exit(1, f'Processing interrupted: {error}; rerun with --resume\n')


if __name__ == '__main__':
    main()
