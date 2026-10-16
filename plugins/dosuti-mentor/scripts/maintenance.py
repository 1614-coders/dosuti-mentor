"""Read-only environment diagnosis and conservative cleanup review plan."""
import importlib.util
import json
import platform
from pathlib import Path
import shutil,subprocess,sys
from storage_report import inventory


def ocr_capability():
    """Report prerequisites, without compiling or claiming successful inference."""
    supported = platform.system() == 'Darwin'
    compiler = supported and Path('/usr/bin/swiftc').is_file()
    return dict(name='screen_ocr', backend='apple_vision', optional=True,
                status=('prerequisites_present' if compiler else
                        'missing_swift' if supported else 'unsupported_platform'),
                execution_verified=False, blocks_teaching=False,
                next_step='Inspect original frames in chat and compare with supplied code; automatic local OCR is optional. No cloud fallback.')


def doctor(args):
    import course
    root=course.root(args);course.read(root/'course.json')
    checks=[]
    checks.append(dict(name='python',available=sys.version_info>=(3,11),version=sys.version.split()[0]))
    for module in ('imageio_ffmpeg','faster_whisper','yt_dlp','numpy'):
        try:present=importlib.util.find_spec(module) is not None
        except (ImportError,ValueError):present=False
        checks.append(dict(name=module,available=present))
    for binary in ('curl','git','ffprobe'):
        checks.append(dict(name=binary,available=shutil.which(binary) is not None))
    try:
        import imageio_ffmpeg
        p=subprocess.run([imageio_ffmpeg.get_ffmpeg_exe(),'-version'],capture_output=True,timeout=10)
        checks.append(dict(name='ffmpeg_executable',available=p.returncode==0))
    except (OSError,ImportError,RuntimeError,subprocess.TimeoutExpired):checks.append(dict(name='ffmpeg_executable',available=False))
    model=getattr(args,'model_path',None)
    model_report=dict(status='not_requested')
    if model is not None:
        from model_readiness import inspect_model
        try:
            inspect_model(model);model_report=dict(status='structurally_ready',note='File readiness does not prove model inference or speech accuracy.')
        except (ValueError,OSError,RuntimeError):model_report=dict(status='not_ready',next_step='Use the documented local model setup/repair workflow.')
    for check in checks:
        if not check['available']:
            check['next_step']='Use setup_local.py in the chosen private runtime for Python packages; install system tools only for the workflows that need them.'
    return dict(read_only=True,network_used=False,checks=checks,model=model_report,
                environment=dict(system=platform.system(),machine=platform.machine()),
                capabilities=[ocr_capability()],filesystem_free_bytes=shutil.disk_usage(root).free,
                note='ffprobe/git/curl support specific workflows; missing optional tools do not mean all local teaching is blocked. No package installation or model inference performed.')


def strings(value):
    if isinstance(value,str):yield value
    elif isinstance(value,dict):
        for child in value.values():yield from strings(child)
    elif isinstance(value,list):
        for child in value:yield from strings(child)


def cleanup_plan(args):
    import course
    root=course.root(args);course.read(root/'course.json')
    report=inventory(root);rows=[];refs=[];unknown=False
    for index,lesson_file in enumerate((root/'lessons').glob('*/lesson.json')):
        if index>=2000:unknown=True;break
        try:
            if lesson_file.is_symlink() or lesson_file.parent.is_symlink() or lesson_file.stat().st_size>4*1024*1024:raise ValueError('Unbounded or linked metadata')
            data=course.read(lesson_file)
            # Preserve every stored path, including unknown future fields/history.
            refs.extend(Path(v).resolve() for v in strings(data) if Path(v).is_absolute())
        except (OSError,ValueError,RecursionError):unknown=True
    for entry in report['groups']:
        if entry['category'] not in ('processing_work','download_staging'):continue
        lesson=entry['lesson'];folder=root/'lessons'/lesson/('.jobs' if entry['category']=='processing_work' else 'video')
        if folder.is_symlink():continue
        try:
            import itertools
            children=list(itertools.islice(folder.iterdir(),2001))
        except OSError:unknown=True;continue
        if len(children)>2000:unknown=True
        for child in children[:2000]:
            if entry['category']=='download_staging' and not child.name.startswith('.fetch-'):continue
            if child.is_symlink() or not child.is_dir():continue
            if len(rows)>=2000:unknown=True;break
            referenced=any(p==child.resolve() or p.is_relative_to(child.resolve()) for p in refs)
            # Never infer inactivity from status/age alone. A complete publication must
            # still be independently checked under locks before future removal.
            state='unknown'
            if entry['category']=='processing_work':
                marker=child/'checkpoint.json'
                try:
                    if marker.is_symlink() or marker.stat().st_size>1024*1024:raise ValueError()
                    d=json.loads(marker.read_text());state=d.get('status','unknown') if isinstance(d,dict) else 'unknown'
                    if not isinstance(state,str):state='unknown'
                except (ValueError,OSError,RecursionError):pass
            rows.append(dict(path=child.relative_to(root).as_posix(),kind=entry['category'],recorded_state=state,
                metadata_reference_detected=referenced,decision='preserve',
                reason='Recorded dependency' if referenced else 'Inactivity, complete publication and recovery dependencies have not been proven'))
    return dict(read_only=True,deletion_supported=False,scan_complete=report['scan_complete'] and not unknown,
        areas=rows,proposed_deletions=[],reclaimable_bytes=None,
        protected=['video sources and pinned cache','prepared/retired evidence','code/homework/resources including history','learner progress and checkpoints'],
        next_step='Review dependencies and lock ownership before implementing cleanup. No directory is approved for deletion by this report.',
        limitations=['References in lesson metadata inspected conservatively; arbitrary external/chat references and live process ownership cannot be established.',
                    'This plan preserves all areas; it does not promise disk recovery or prove jobs are unused.'])


def dispatch(args):
    if args.command=='download-plan':
        from download_planning import download_plan
        return download_plan(args)
    if args.command=='doctor':return doctor(args)
    return cleanup_plan(args)
