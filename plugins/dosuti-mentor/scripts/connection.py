"""Explicit course connections scoped to a project or a named chat; no background startup."""
import argparse
from copy import deepcopy
import json
from pathlib import Path
import sys
from types import SimpleNamespace
from atomic_files import atomic_json
from profile_storage import checked
from storage_lock import file_lock

KEY='dosuti_mentor_connections'
LEGACY_KEY='lecture_tutor_connections'
FIELDS={'course_path','profile','profiles_dir','runtime','model_path'}
SCHEMA='course_connections_v1'


def settings_path(project):
    current=project/'.dosuti-mentor.json'
    previous=project/'.lecture-tutor.json'
    checked(current)
    if current.exists():return current
    checked(previous)
    return previous if previous.exists() else current


def binding(value):
    import course
    if not isinstance(value,dict) or not {'course_path','profile'}<=set(value) or set(value)-FIELDS:
        raise ValueError('Connection requires course_path/profile and optional runtime/model_path/profiles_dir')
    course.slug(value['profile'])
    for key in set(value)-{'profile'}:
        if not isinstance(value[key],str) or not value[key].strip() or len(value[key])>4096 or '\0' in value[key]:
            raise ValueError('Invalid connection path: '+key)
        if '://' in value[key]: raise ValueError('Connection paths must be local')
    return value


def read(project):
    from learning_tools import read_json
    path=settings_path(project); checked(path)
    data=read_json(path,{})
    if LEGACY_KEY in data:
        if KEY in data:raise ValueError('Ambiguous old and new connection keys; preserve settings before repair')
        data[KEY]=data.pop(LEGACY_KEY)
    if KEY in data:
        state=data[KEY]
        if (not isinstance(state,dict) or set(state)!={'schema','revision','project_enabled','chats'}
                or state['schema']!=SCHEMA or type(state['revision']) is not int or state['revision']<0
                or type(state['project_enabled']) is not bool or not isinstance(state['chats'],dict)
                or len(state['chats'])>200):
            raise ValueError('Invalid course connections; preserve settings before repair')
        import course
        for name,item in state['chats'].items():
            course.slug(name)
            if item is not None:binding(item)
        if state['project_enabled']:
            binding({k:data[k] for k in FIELDS if k in data})
        elif 'course_path' in data:
            raise ValueError('Disabled project contains ambiguous legacy binding fields')
    else:
        enabled='course_path' in data
        if enabled:binding({k:data[k] for k in FIELDS if k in data} | {'profile':data.get('profile','default')})
        state=dict(schema=SCHEMA,revision=0,project_enabled=enabled,chats={})
    return data,deepcopy(state)


def resolve(project,value):
    result=deepcopy(value)
    for key in FIELDS-{'profile'}:
        if key in result:
            path=Path(result[key]).expanduser()
            result[key]=str((project/path).resolve() if not path.is_absolute() else path.resolve())
    return result


def course_ready(value):
    from learning_tools import read_json
    from profile_storage import profile_path
    root=Path(value['course_path'])
    checked(root,True); checked(root/'course.json')
    meta=read_json(root/'course.json')
    if (meta is None or type(meta.get('schema_version')) is not int or meta['schema_version']!=1
            or any(not isinstance(meta.get(k),str) or not meta[k].strip() for k in ('id','title'))):
        raise ValueError('Connected course is unavailable or has an unsupported schema')
    profile_path(root,value['profile'])


def status(project,chat=None):
    import course
    if chat is not None:course.slug(chat)
    data,state=read(project)
    value=None; scope=None; disabled=False
    if chat is not None and chat in state['chats']:
        value=state['chats'][chat]; scope='chat'; disabled=value is None
    elif state['project_enabled']:
        value={k:data[k] for k in FIELDS if k in data}; value.setdefault('profile','default'); scope='project'
    result=dict(revision=state['revision'],chat=chat,scope=scope,enabled=value is not None,
                chat_overrides_count=len(state['chats']),needs_chat_id=chat is None and bool(state['chats']),
                explicitly_disabled=disabled,binding=None,course_status='not_connected')
    if value is not None:
        result['binding']=resolve(project,value)
        try:course_ready(result['binding']); result['course_status']='available'
        except (ValueError,OSError) as error:
            result.update(course_status='unavailable',error=str(error))
    return result


def dispatch(args):
    project=args.project.expanduser().resolve()
    if not project.is_dir(): raise ValueError('Choose an existing project directory')
    if project.is_relative_to(Path(__file__).resolve().parents[1]):
        raise ValueError('Connections must stay outside the installed plugin')
    action=getattr(args,'action','status'); chat=getattr(args,'chat',None)
    if chat is not None:
        import course
        course.slug(chat)
    if action in ('connect','disconnect'):
        scope=getattr(args,'scope',None)
        if scope not in ('chat','project') or (scope=='chat' and chat is None):
            raise ValueError('Choose --scope project or --scope chat with --chat ID')
        expected=getattr(args,'expected_revision',None)
        if type(expected) is not int or expected<0:raise ValueError('Connection change requires --expected-revision')
        value=None
        if action=='connect':
            selected=getattr(args,'course',None)
            if selected is None:raise ValueError('Connect requires --course')
            # Explicit relative CLI paths are relative to --project, like stored paths.
            value=dict(course_path=str(selected),profile=getattr(args,'profile','default'))
            for key in ('runtime','model_path','profiles_dir'):
                path=getattr(args,key,None)
                if path is not None:value[key]=str(path)
            value=resolve(project,binding(value)); course_ready(value)
        read(project)  # Reject malformed/linked settings before creating a lock file.
        # Existing installations retain their file/lock location, avoiding parallel
        # independent writers during the rename. New projects use the new names.
        path=settings_path(project)
        lock=path.with_suffix('.lock'); checked(lock)
        with file_lock(lock):
            checked(lock); data,state=read(project)
            if state['revision']!=expected:raise ValueError('Connections changed; read status before updating')
            if scope=='project':
                for key in FIELDS:data.pop(key,None)
                state['project_enabled']=value is not None
                if value is not None:data.update(value)
            else:
                if chat not in state['chats'] and len(state['chats'])>=200:raise ValueError('Chat connection limit reached')
                state['chats'][chat]=value
            # Normalize a legacy default profile when adding the first chat override.
            if state['project_enabled']:data.setdefault('profile','default')
            state['revision']+=1; data[KEY]=state
            if len(json.dumps(data,ensure_ascii=False,indent=2).encode())>900_000:raise ValueError('Connection settings too large')
            if path.name=='.lecture-tutor.json':
                data[LEGACY_KEY]=data.pop(KEY)
            atomic_json(path,data)
    result=status(project,chat)
    if action=='check':
        if result['course_status']!='available':
            result['environment']=dict(checked=False,reason='No available connected course')
        else:
            from maintenance import doctor
            b=result['binding']
            report=doctor(SimpleNamespace(course=Path(b['course_path']),model_path=Path(b['model_path']) if b.get('model_path') else None))
            report['interpreter']=sys.executable
            report['configured_runtime']=b.get('runtime')
            report['runtime_note']='Checks describe this interpreter; configured runtime is not executed automatically.'
            report['web_search']=dict(status='unknown',next_step='Codex must inspect tools available in this chat')
            report['codex_teaching']=dict(status='requires_current_chat',note='Local package checks do not verify model access or usage limits.')
            result['environment']=report
    return result


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project',type=Path,required=True)
    parser.add_argument('--action',choices=['status','connect','disconnect','check'],default='status')
    parser.add_argument('--scope',choices=['chat','project'])
    parser.add_argument('--chat')
    parser.add_argument('--course',type=Path)
    parser.add_argument('--profile',default='default')
    for name in ('runtime','model-path','profiles-dir'):parser.add_argument('--'+name,type=Path)
    parser.add_argument('--expected-revision',type=int)
    args=parser.parse_args()
    if sys.version_info<(3,11):parser.error('Python 3.11+ required; 3.12 is tested')
    try:print(json.dumps(dispatch(args),ensure_ascii=False,indent=2))
    except (ValueError,OSError,RuntimeError) as error:
        print('Error:',error,file=sys.stderr); sys.exit(1)


if __name__=='__main__':main()
