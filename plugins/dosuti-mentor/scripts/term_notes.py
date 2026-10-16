"""Evidence-bound terminology interpretations; never rewrite ASR or assert verbatim speech."""
from copy import deepcopy
from pathlib import Path
import hashlib
import json
from source_integrity import digest, verify_snapshot

FIELDS = {'id','start','end','asr_text','term','basis','notes','frames','code'}


def validate(value):
    import course
    from lecture_context import number
    if not isinstance(value,dict) or set(value)!=FIELDS:
        raise ValueError('Term note requires exactly: '+', '.join(sorted(FIELDS)))
    course.slug(value['id'])
    for key in ('asr_text','term','notes'):
        if not isinstance(value[key],str) or not value[key].strip() or len(value[key])>4000:
            raise ValueError('Term note '+key+' must be a nonempty string up to 4000 characters')
    number(value['start'],'start');number(value['end'],'end')
    if value['start']<0 or value['end']<=value['start']:
        raise ValueError('Require 0 <= start < end')
    if value['basis'] not in ('screen','attached_code','hypothesis'):
        raise ValueError('Term basis must be screen, attached_code or hypothesis')
    if not isinstance(value['frames'],list) or len(value['frames'])>50:
        raise ValueError('frames must be a list of at most 50 items')
    for frame in value['frames']:
        if not isinstance(frame,dict) or set(frame)!={'time','sha256'}:
            raise ValueError('Frame requires time and sha256')
        number(frame['time'],'frame time');digest(frame['sha256'])
        if not value['start']<=frame['time']<value['end']:
            raise ValueError('Term frame must be inside note interval')
    code=value['code']
    if code is not None:
        if not isinstance(code,dict) or set(code)!={'sha256','file'} or not isinstance(code['file'],str):
            raise ValueError('code requires sha256 and file')
        digest(code['sha256'])
    if value['basis']=='screen' and (not value['frames'] or code is not None):
        raise ValueError('screen basis requires frames and no code')
    if value['basis']=='attached_code' and (code is None or value['frames']):
        raise ValueError('attached_code basis requires code and no frames')
    if value['basis']=='hypothesis' and (code is not None or value['frames']):
        raise ValueError('hypothesis must not claim evidence')


def evidence(directory,data,value):
    from lecture_context import assemble
    result=assemble(directory,data,value['start'],value['end']-value['start'],include_code_stages=False,include_term_notes=False)
    if not result['coverage']['complete']:
        raise ValueError('Term interval must be fully prepared')
    speech=[{key:s[key] for key in ('start','end','text')} for s in result['speech']]
    if not any(value['asr_text'] in s['text'] for s in speech):
        raise ValueError('asr_text is not present in an overlapping speech segment')
    frames={(f['approx_seconds'],f['sha256']) for f in result['frames']}
    if any((f['time'],f['sha256']) not in frames for f in value['frames']):
        raise ValueError('Term frame is missing or changed')
    if value['code'] is not None:
        from code_repositories import current
        code=next((c for c in current(data).values() if c.get('content_sha256')==value['code']['sha256']), {})
        verify_snapshot(code)
        if code.get('content_sha256')!=value['code']['sha256'] or value['code']['file'] not in code.get('inventory',{}):
            raise ValueError('Term code snapshot is no longer current or file is unknown')
        text=(Path(code['snapshot'])/value['code']['file']).read_text(encoding='utf-8')
        if value['term'] not in text:
            raise ValueError('Proposed term is absent from referenced code file')
    checksum=hashlib.sha256(json.dumps(speech,sort_keys=True,ensure_ascii=False).encode()).hexdigest()
    return checksum


def checked(directory,data,start=None,stop=None):
    rows=[]
    for original in data.get('term_notes',{}).values():
        row=deepcopy(original)
        try:
            value={key:row[key] for key in FIELDS};validate(value)
            if start is not None and not (row['start']<stop and row['end']>start):continue
            if row.get('source_sha256')!=data['video'].get('sha256'):
                raise ValueError('Term video identity changed')
            if row.get('speech_sha256')!=evidence(directory,data,value):
                raise ValueError('Term speech changed; review again')
            row.update(stale=False)
        except (ValueError,OSError,KeyError,TypeError) as error:
            row.update(stale=True,reason=str(error))
        row['verbatim_speech_confirmed']=False
        row['interpretation_status']='requires_review' if row['stale'] else ('hypothesis' if row.get('basis')=='hypothesis' else 'supported_term_interpretation')
        rows.append(row)
    return rows


def term_note(args):
    import course
    directory=course.lesson_path(args)
    if getattr(args,'note_file',None):
        value=json.loads(Path(args.note_file).read_text(encoding='utf-8'));validate(value)
        def update(data):
            notes=data.setdefault('term_notes',{})
            if value['id'] in notes:raise ValueError('Term note ID already exists; remove explicitly before replacing')
            checksum=evidence(directory,data,value)
            notes[value['id']]=dict(value,source_sha256=data['video']['sha256'],speech_sha256=checksum,updated=course.now())
        course.update_lesson(directory,update)
    elif getattr(args,'remove',None):
        course.slug(args.remove)
        def remove(data):
            if args.remove not in data.get('term_notes',{}):raise ValueError('Unknown term note ID')
            del data['term_notes'][args.remove]
        course.update_lesson(directory,remove)
    return dict(lesson=args.lesson,term_notes=checked(directory,course.read(directory/'lesson.json')))
