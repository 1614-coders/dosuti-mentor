"""Local preferences, learning cursors, evidence-linked topics and project navigation."""
import json
import math
from pathlib import Path
import re
from atomic_files import atomic_json
from storage_lock import file_lock

DEFAULTS = dict(style='teacher', format='steps', level='beginner', detail='balanced', checks='each_block')
CHOICES = dict(style=('teacher','partner','reviewer'), format=('steps','blocks','file'),
               level=('beginner','intermediate','advanced'), detail=('concise','balanced','detailed'),
               checks=('each_block','end','on_request'))
ONBOARDING_STATES = ('not_asked','in_progress','complete','skipped')
ONBOARDING_QUESTIONS = dict(
    outcome=('topics','skill','course','profession','growth','undecided'),
    subject=None,
    self_assessment=('zero','basics','basic_experience','confident','significant_experience','unknown'),
    learning_format=('theory_then_practice','brief_then_practice','example_then_explanation','tasks_projects','step_by_step','adaptive'),
    assistant_role=('teacher','mentor','partner','examiner','flexible'),
    check_frequency=('each_block','end','on_request','scheduled','adaptive'),
    explanation_detail=('concise','balanced','detailed','adaptive'),
    constraints=('deadline','study_time','existing_materials','free_pace','undecided'))


def onboarding_answers(value):
    if not isinstance(value,dict) or set(value)-set(ONBOARDING_QUESTIONS):
        raise ValueError('Invalid onboarding answers')
    for question, record in value.items():
        if not isinstance(record,dict):raise ValueError('Invalid onboarding answer record')
        if record == dict(status='skipped'):continue
        if (not {'status','value'}<=set(record) or set(record)-{'status','value','text','area','direction'}
                or record['status'] not in ('answered','clarifying')
                or (question!='subject' and set(record)&{'area','direction'})):
            raise ValueError('Invalid onboarding answer record')
        for field in ('text','area','direction'):
            if field in record and (not isinstance(record[field],str) or not record[field].strip() or len(record[field])>4000):
                raise ValueError('Invalid onboarding '+field)
        if record['status']=='clarifying' and 'direction' in record:
            raise ValueError('A known direction does not need subject clarification')
        answer=record['value']; choices=ONBOARDING_QUESTIONS[question]
        if answer is None:
            if 'text' not in record:raise ValueError('Free onboarding answer requires actual text')
            continue
        if question=='subject':
            if not isinstance(answer,str) or not answer.strip() or len(answer)>4000:
                raise ValueError('Invalid onboarding subject')
        elif question in ('outcome','constraints'):
            if question=='constraints':
                if not isinstance(answer,dict) or set(answer)-{'selected','details'}:
                    raise ValueError('Invalid onboarding constraints')
                details=answer.get('details','')
                if not isinstance(details,str) or len(details)>4000:
                    raise ValueError('Invalid onboarding constraint details')
                answer=answer.get('selected',[])
            else:details=''
            if (not isinstance(answer,list) or len(answer)>len(choices)
                    or any(not isinstance(item,str) or item not in choices for item in answer)
                    or len(answer)!=len(set(answer)) or not (answer or details.strip())):
                raise ValueError('Invalid onboarding selection')
        else:
            selected=answer if isinstance(answer,list) else [answer]
            if (not selected or len(selected)>len(choices)
                    or any(not isinstance(v,str) or v not in choices for v in selected)
                    or len(set(selected))!=len(selected)):
                raise ValueError('Invalid onboarding choice: '+question)
    return value


def onboarding_record(answer):
    if isinstance(answer,dict) and set(answer)&{'value','text','area','direction','pending'}:
        if set(answer)-{'value','text','area','direction','pending'} or type(answer.get('pending',False)) is not bool:
            raise ValueError('Invalid onboarding answer fields')
        return dict(status='clarifying' if answer.get('pending') else 'answered',
                    value=answer.get('value'),**{k:v for k,v in answer.items() if k not in ('value','pending')})
    return dict(status='answered',value=answer)


def onboarding_message(onboarding, answers, next_question):
    if next_question is None:return None
    copy=(Path(__file__).parents[1]/'references/onboarding-dialog.md').read_text(encoding='utf-8')
    def block(heading):
        return copy.split(heading+'\n',1)[1].split('\n### ',1)[0].split('\n## ',1)[0].strip()
    index=list(ONBOARDING_QUESTIONS).index(next_question)+1
    heading=f'### Вопрос {index} из 8'
    message=heading+'\n\n'+block(heading)
    record=answers.get(next_question,{})
    if record.get('status')=='clarifying':
        area=record.get('area',record.get('value'))
        clarify='### Уточнение: '+(area if isinstance(area,str) else 'Другая область')
        if clarify+'\n' not in copy:clarify='### Уточнение: Другая область'
        message=heading+'\n\n'+(block(clarify) if next_question=='subject' else
            'Расскажи свой вариант своими словами. Можно также написать «Пропустить».')
    if onboarding=='not_asked' and not answers:
        message=block('## Приветствие')+'\n\n'+message
    return message


def onboarding_comment(value):
    if not isinstance(value,str) or len(value)>4000:
        raise ValueError('Invalid onboarding comment')
    return value


from read_snapshot import observed

@observed
def read_json(path, default=None):
    if not path.exists():
        return default
    if path.is_symlink():
        raise ValueError('Learning data must not be symbolic links')
    if path.stat().st_size > 1_000_000:
        raise ValueError('Learning data file is too large')
    value = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(value, dict):
        raise ValueError('Expected a JSON object')
    return value


def learner(args):
    import course as c
    root = c.root(args)
    c.read(root/'course.json')
    from profile_storage import profile_path
    return profile_path(root, c.slug(getattr(args,'profile','default')))


def pref_file(args, scope):
    import course as c
    path = learner(args)
    if scope == 'profile' and getattr(args,'profiles_dir',None):
        path = args.profiles_dir.expanduser().resolve()/c.slug(getattr(args,'profile','default'))
    if path.is_symlink():
        raise ValueError('Profile folders must not be symbolic links')
    if path.resolve().is_relative_to(Path(__file__).resolve().parents[1]):
        raise ValueError('Preferences must stay outside the installed plugin')
    return path/('preferences.json' if scope=='profile' else 'course-preferences.json')


def validate_preferences(value):
    if not isinstance(value,dict) or set(value)-set(CHOICES):
        raise ValueError('Unknown preference; evidence and progress rules cannot be overridden')
    for key, val in value.items():
        if val not in CHOICES[key]:
            raise ValueError('Invalid preference: '+key)
    return value


def revision(data):
    value=data.get('revision',0)
    if type(value) is not int or value<0:
        raise ValueError('Invalid learning data revision')
    return value


def preferences(args):
    result = dict(DEFAULTS); layers = {}; onboarding = 'not_asked'; answers = {}; comment = ''
    for scope in ('profile','course'):
        path = pref_file(args,scope)
        data = read_json(path, {})
        values = validate_preferences(data.get('values',{}))
        result.update(values); layers[scope] = dict(values=values,path=str(path),revision=revision(data))
        if scope=='profile':
            onboarding = data.get('onboarding','not_asked')
            answers = onboarding_answers(data.get('onboarding_answers',{}))
            comment = onboarding_comment(data.get('onboarding_comment',''))
            if onboarding not in ONBOARDING_STATES:
                raise ValueError('Invalid onboarding state')
    pending = [q for q in ONBOARDING_QUESTIONS if answers.get(q,{}).get('status')=='clarifying'
                or (onboarding not in ('complete','skipped') and q not in answers)]
    return dict(effective=result,layers=layers,onboarding=onboarding,
                onboarding_answers=answers,onboarding_comment=comment,
                onboarding_pending=pending,onboarding_next=pending[0] if pending else None,
                onboarding_message=onboarding_message(onboarding,answers,pending[0] if pending else None))


def settings(args):
    update_file = getattr(args,'onboarding_file',None)
    skips = getattr(args,'onboarding_skip',[]) or []
    if args.set or args.reset or args.onboarding or update_file or skips:
        path = pref_file(args,args.scope)
        updates = {}
        for item in args.set:
            key,sep,val = item.partition('=')
            if not sep:raise ValueError('Use --set key=value')
            updates[key]=val
        validate_preferences(updates)
        if (args.onboarding or update_file or skips) and args.scope!='profile':
            raise ValueError('Onboarding belongs to the profile')
        update = read_json(update_file) if update_file else {}
        if not isinstance(update,dict) or set(update)-{'answers','comment'}:
            raise ValueError('Invalid onboarding update')
        raw_answers=update.get('answers',{})
        if not isinstance(raw_answers,dict):raise ValueError('Invalid onboarding answers')
        additions={key:onboarding_record(value) for key,value in raw_answers.items()}
        if (not isinstance(skips,list) or any(not isinstance(q,str) or q not in ONBOARDING_QUESTIONS for q in skips)
                or len(skips)!=len(set(skips)) or set(skips)&set(additions)):
            raise ValueError('Invalid onboarding skips')
        additions.update({question:dict(status='skipped') for question in skips})
        onboarding_answers(additions)
        if 'comment' in update:onboarding_comment(update['comment'])
        if args.onboarding=='skipped' and (update_file or skips):
            raise ValueError('Skip all onboarding separately from individual answers')
        if args.onboarding is not None and args.onboarding not in ONBOARDING_STATES:
            raise ValueError('Invalid onboarding state')
        with file_lock(path.with_suffix('.lock')):
            current = read_json(path,dict(values={},revision=0,onboarding='not_asked'))
            values = {} if args.reset else dict(validate_preferences(current.get('values',{})))
            values.update(updates)
            onboarding = args.onboarding or current.get('onboarding','not_asked')
            if onboarding not in ONBOARDING_STATES:raise ValueError('Invalid onboarding state')
            answers=dict(onboarding_answers(current.get('onboarding_answers',{})))
            comment=onboarding_comment(current.get('onboarding_comment',''))
            legacy_finished=onboarding in ('complete','skipped') and len(answers)<len(ONBOARDING_QUESTIONS)
            previous=answers.get('subject',{}); incoming=additions.get('subject',{})
            if previous.get('status')=='clarifying' and incoming.get('status') in ('answered','clarifying'):
                incoming=dict(incoming)
                if 'area' in previous:incoming.setdefault('area',previous['area'])
                if previous.get('text'):
                    incoming.setdefault('text',previous['text'])
                    if incoming['text']!=previous['text'] and not incoming['text'].startswith(previous['text']+'\n'):
                        incoming['text']=previous['text']+'\n'+incoming['text']
                additions['subject']=incoming
            answers.update(additions)
            onboarding_answers(answers)
            if args.onboarding=='skipped':
                for question in ONBOARDING_QUESTIONS:
                    answers.setdefault(question,dict(status='skipped'))
            elif additions:
                # Editing one legacy answer must not restart a finished survey.
                onboarding=(onboarding if legacy_finished else
                            'in_progress' if any(r['status']=='clarifying' for r in answers.values()) else
                            'complete' if len(answers)==len(ONBOARDING_QUESTIONS) else
                            onboarding if args.onboarding is None and onboarding in ('complete','skipped') else 'in_progress')
            if args.onboarding=='complete' and (len(answers)!=len(ONBOARDING_QUESTIONS)
                    or any(r['status']=='clarifying' for r in answers.values())):
                raise ValueError('Cannot complete unresolved onboarding questions')
            if 'comment' in update:comment=update['comment']
            saved=dict(values=values,revision=revision(current)+1,onboarding=onboarding)
            if answers:saved['onboarding_answers']=answers
            if comment or 'onboarding_comment' in current or 'comment' in update:
                saved['onboarding_comment']=comment
            atomic_json(path,saved)
    return preferences(args)


def lessons(args):
    import course as c
    c.read(c.root(args)/'course.json')
    return sorted([(p.parent,c.read(p)) for p in (c.root(args)/'lessons').glob('*/lesson.json')],
                  key=lambda item:(item[1]['order'],item[1]['id']))


def text(value, label, limit=4000):
    if not isinstance(value,str) or len(value)>limit:
        raise ValueError(label+' must be text within length limit')
    return value


def strings(value,label):
    if not isinstance(value,list) or len(value)>50:
        raise ValueError(label+' must contain at most 50 strings')
    return [text(v,label,500) for v in value]


def evidence_signature(directory, data):
    from lecture_context import load_fragment
    from source_integrity import check_local_video
    check_local_video(data['video'])
    signatures=[]
    for fragment in data.get('prepared_fragments',[]):
        load_fragment(fragment,directory,data['video'].get('sha256'))
        signatures.append(fragment['packet_sha256'])
    return sorted(signatures)


def topic_signature(directory, data, start, end):
    """Bind only intersecting packets and the preceding frame source at the seam."""
    from lecture_context import assemble
    from source_integrity import file_hash
    result = assemble(directory, data, start, end-start, include_code_stages=False)
    if not result['coverage']['complete']:
        raise ValueError('Topic interval is no longer fully prepared')
    return sorted(file_hash(source['packet_file']) for source in result['sources'])


def validate_interval(directory,data,start,end):
    from lecture_context import assemble, number
    number(start,'start');number(end,'end')
    if start<0 or end<=start:raise ValueError('Require 0 <= start < end')
    result=assemble(directory,data,start,end-start)
    if not result['coverage']['complete']:
        raise ValueError('Topic interval is not fully prepared; prepare the missing range first')
    return result


def cursor(args):
    import course as c
    path=learner(args)/'cursor.json'
    try:
        state=read_json(path)
        if state is None:return dict(revision=0,state=None,valid=False,reason='no_checkpoint')
        rev=revision(state)
        stored=state.get('state')
        fields={'lesson','time','topic','recap','next_step','pending_question','misconceptions','source_sha256','packet_hashes','updated'}
        optional={'dependency_topics','dependency_evidence','dependency_materials','material_evidence'}
        if not isinstance(stored,dict) or not fields <= set(stored) or set(stored)-fields-optional:raise ValueError('Invalid checkpoint fields')
        for left,right in [('dependency_topics','dependency_evidence'),('dependency_materials','material_evidence')]:
            if (left in stored)!=(right in stored):raise ValueError('Incomplete checkpoint dependency fields')
        c.slug(stored['lesson'])
        position=stored['time']
        if type(position) not in (int,float) or not math.isfinite(position) or position<0:raise ValueError('Invalid checkpoint time')
        for key in ('topic','recap','next_step','pending_question','updated'):text(stored[key],key)
        strings(stored['misconceptions'],'misconceptions')
        from source_integrity import digest
        digest(stored['source_sha256'])
        if not isinstance(stored['packet_hashes'],list) or not stored['packet_hashes']:raise ValueError('Invalid packet hashes')
        for value in stored['packet_hashes']:digest(value)
        if 'dependency_topics' in stored:
            from cursor_dependencies import topic_ids, validate_evidence, packet_hashes
            validate_evidence(stored['dependency_evidence'],topic_ids(stored['dependency_topics']))
            if stored['packet_hashes'] != packet_hashes(stored['dependency_evidence']):
                raise ValueError('Checkpoint packet dependencies do not match')
        if 'dependency_materials' in stored:
            from material_dependencies import validate
            validate(stored['material_evidence'],stored['dependency_materials'])
    except (ValueError,OSError):
        return dict(revision=None,state=None,valid=False,reason='invalid_checkpoint',recovery='Preserve and repair or archive cursor.json before saving a new checkpoint')
    lesson=c.root(args)/'lessons'/stored['lesson']
    try:
        data=c.read(lesson/'lesson.json')
        if 'dependency_topics' in stored:
            from cursor_dependencies import collect
            valid=(stored['source_sha256']==data['video'].get('sha256') and
                   stored['dependency_evidence']==collect(lesson,data,stored['dependency_topics'],position))
        else:
            valid=(stored['source_sha256']==data['video'].get('sha256') and
                   stored['packet_hashes']==evidence_signature(lesson,data))
        if 'dependency_materials' in stored:
            from material_dependencies import collect
            valid=valid and stored['material_evidence']==collect(lesson,data,stored['dependency_materials'])
    except (OSError,ValueError,KeyError,TypeError,AttributeError):valid=False
    return dict(revision=rev,state=stored,valid=valid,
                reason=None if valid else 'source_or_preparation_changed')


def resume(args):
    import course as c
    path=learner(args)/'cursor.json'
    if getattr(args,'state_file',None):
        value=read_json(args.state_file)
        required={'lesson','time','topic','recap','next_step','pending_question','misconceptions'}
        if not isinstance(value,dict) or not required <= set(value) or set(value)-required-{'dependency_topics','dependency_materials'}:raise ValueError('Checkpoint requires: '+', '.join(sorted(required))+'; optional dependency_topics, dependency_materials')
        if 'dependency_topics' in value:
            from cursor_dependencies import topic_ids
            topic_ids(value['dependency_topics'])
        if 'dependency_materials' in value:
            from material_dependencies import selection
            selection(value['dependency_materials'])
        c.slug(value['lesson']);position=value['time']
        if isinstance(position,bool) or not isinstance(position,(int,float)) or not math.isfinite(position) or position<0:
            raise ValueError('Checkpoint time must be finite and nonnegative')
        for key in ('topic','recap','next_step','pending_question'):text(value[key],key)
        strings(value['misconceptions'],'misconceptions')
        directory=c.root(args)/'lessons'/value['lesson']
        with file_lock(directory/'.lesson.lock'):
            data=c.read(directory/'lesson.json')
            if 'dependency_topics' in value:
                from cursor_dependencies import collect, packet_hashes
                dependencies=collect(directory,data,value['dependency_topics'],position)
                signature=packet_hashes(dependencies)
                value=dict(value,dependency_evidence=dependencies)
            else:
                signature=evidence_signature(directory,data)
                if not signature or not any(f['coverage']['start']<=position<=f['coverage']['end'] for f in data['prepared_fragments']):
                    raise ValueError('Checkpoint position must belong to prepared material')
            if 'dependency_materials' in value:
                from material_dependencies import collect
                value=dict(value,material_evidence=collect(directory,data,value['dependency_materials']))
            value=dict(value,source_sha256=data['video']['sha256'],packet_hashes=signature,updated=c.now())
            with file_lock(path.with_suffix('.lock')):
                if path.exists() and cursor(args).get('reason')=='invalid_checkpoint':
                    raise ValueError('Invalid checkpoint; preserve and repair or archive cursor.json first')
                old=read_json(path,dict(revision=0))
                if args.expected_revision!=revision(old):
                    raise ValueError('Checkpoint changed in another chat; read resume before saving')
                atomic_json(path,dict(revision=revision(old)+1,state=value))
    result=cursor(args)
    from learning_intent import view as intent_view
    result['learning_progress']=intent_view(args)['learning_progress']
    return result


def topic(args):
    import course as c
    directory=c.lesson_path(args)
    if getattr(args,'topic_file',None):
        value=read_json(args.topic_file)
        fields={'id','title','summary','keywords','start','end','code_files'}
        if not isinstance(value,dict) or not fields <= set(value) or set(value)-fields-{'boundary_review','code_references'}:raise ValueError('Topic requires fields: '+', '.join(sorted(fields)))
        if 'boundary_review' in value:
            from semantic_context import validate_boundaries
            validate_boundaries(value['boundary_review'])
        c.slug(value['id']);text(value['title'],'title',200);text(value['summary'],'summary')
        strings(value['keywords'],'keywords');strings(value['code_files'],'code_files')
        def update(data):
            validate_interval(directory,data,value['start'],value['end'])
            if value['code_files']:
                if not data['code'].get('snapshot'):
                    raise ValueError('Attach a code snapshot before linking topic files')
                c.verify_snapshot(data['code'])
                if any(name not in data['code']['inventory'] for name in value['code_files']):raise ValueError('Topic references an unknown snapshot file')
            record=dict(value,origin='assistant_annotation',source_sha256=data['video']['sha256'],
                        packet_hashes=topic_signature(directory,data,value['start'],value['end']),
                        evidence_scope='topic_interval_v1',code_sha256=data['code'].get('content_sha256'),updated=c.now())
            if 'code_references' in value:
                from code_repositories import references
                record['code_references']=references(value['code_references'],data)
            data.setdefault('topics',{})[value['id']]=record
        c.update_lesson(directory,update)
    data=c.read(directory/'lesson.json')
    return dict(lesson=args.lesson,topics=checked_topics(directory,data))


def checked_topics(directory,data):
    import course as c
    records=list(data.get('topics',{}).values())
    if not records:return []
    for row in records:
        try:
            scope=row.get('evidence_scope')
            if scope == 'topic_interval_v1':
                signature=topic_signature(directory,data,row['start'],row['end'])
            elif scope is None:
                signature=evidence_signature(directory,data)
            else:
                raise ValueError('Unknown topic evidence scope')
            row['stale']=(row.get('source_sha256')!=data['video'].get('sha256') or row.get('packet_hashes')!=signature)
        except (ValueError,OSError,KeyError):
            row['stale']=True
        if row.get('code_files'):
            try:c.verify_snapshot(data['code'])
            except (ValueError,OSError):row['stale']=True
            if row.get('code_sha256')!=data['code'].get('content_sha256'):row['stale']=True
        if 'code_references' in row:
            try:
                from code_repositories import references
                references(row['code_references'],data,stored=True)
            except (ValueError,OSError,KeyError,TypeError):row['stale']=True
    return records


def tokens(value):return set(re.findall(r'\w+',value.casefold()))


def search(args):
    from lecture_context import load_fragment,assemble
    from source_integrity import check_local_video
    query=tokens(text(args.query,'query',500))
    if not query or not 1<=args.limit<=50:raise ValueError('Search requires words and limit 1..50')
    hits=[];warnings=[]
    for directory,data in lessons(args):
        if getattr(args,'lesson',None) and args.lesson!=data['id']:continue
        try:
            check_local_video(data['video'])
            records=[dict(kind='topic',text=r['title']+' '+r['summary']+' '+' '.join(r['keywords']),
                          start=r['start'],end=r['end'],topic_id=r['id'],code_files=r.get('code_files',[]),
                          **({'code_references':r['code_references']} if 'code_references' in r else {})) for r in checked_topics(directory,data) if not r['stale']]
            for fragment in data.get('prepared_fragments',[]):
                packet,_=load_fragment(fragment,directory,data['video'].get('sha256'))
                records.extend(dict(kind='speech',text=r['text'],start=r['start'],end=r['end']) for r in packet['speech'])
            for row in records:
                score=len(query & tokens(row['text']))
                if score:hits.append(dict(row,score=score,lesson=data['id'],directory=directory,data=data))
        except (ValueError,OSError,KeyError) as error:
            warnings.append(dict(lesson=data['id'],error=str(error)))
    hits.sort(key=lambda r:(-r['score'],r['lesson'],r['start'],r['kind']))
    results=[];seen=set()
    for row in hits:
        key=(row['lesson'],row['start'],row['end'],row['text'])
        if key in seen:continue
        seen.add(key)
        if len(results)>=args.limit:break
        try:
            context=assemble(row['directory'],row['data'],row['start'],max(.001,row['end']-row['start']))
        except (ValueError,OSError) as error:
            warnings.append(dict(lesson=row['lesson'],error=str(error)));continue
        result={key:value for key,value in row.items() if key not in ('directory','data')}
        result['text']=result['text'][:1500]
        result['frames']=context['frames'];result['warnings']=context['warnings']
        if row.get('code_files'):
            result['code_snapshot']=row['data']['code']['snapshot']
            result['code_lecture_match']='assistant_topic_link_requires_video_review'
        if row.get('code_references'):
            from code_repositories import resolve
            result['code_snapshots']={ref['repository']:resolve(row['data'],ref['repository'])['snapshot'] for ref in row['code_references']}
            result['code_lecture_match']='assistant_topic_link_requires_video_review'
        result['source_sha256']=row['data']['video']['sha256']
        results.append(result)
    return dict(query=args.query,method='keyword_and_reviewed_topic_navigation',results=results,warnings=warnings,
                limitations=['No embedding model: rephrase or expand queries in chat.', 'ASR and assistant topic annotations are not verified quotations.'])


def project_map(args):
    import course as c
    nodes=[];previous=None
    for directory,data in lessons(args):
        node=dict(lesson=data['id'],title=data['title'],order=data['order'],lecture_match='unverified',
                  topics=checked_topics(directory,data),files=[],changes=[],dependencies=[])
        from code_repositories import describe as repositories
        node['repositories']=repositories(data)
        code=data['code']
        if not code.get('snapshot'):
            node['code_status']=code.get('status','not_asked');previous=None;nodes.append(node);continue
        c.verify_snapshot(code);inventory=code['inventory'];snapshot=Path(code['snapshot']);definitions={};texts={}
        for name,entry in inventory.items():
            f=snapshot/name;symbols=[]
            if f.suffix in ('.java','.py','.js','.ts','.tsx','.kt','.cs') and f.stat().st_size<=1_000_000:
                try:
                    content=f.read_text(encoding='utf-8');symbols=re.findall(r'\b(?:class|interface|enum|record|def|function)\s+([A-Za-z_]\w*)',content);texts[name]=tokens(content)
                except UnicodeDecodeError:pass
            for symbol in symbols:definitions.setdefault(symbol.casefold(),set()).add(name)
            node['files'].append(dict(file=name,sha256=entry['sha256'],symbols=symbols))
        for name,words in texts.items():
            for symbol,targets in definitions.items():
                if symbol in words:
                    for target in sorted(targets-{name}):node['dependencies'].append(dict(from_file=name,to_file=target,symbol=symbol,confidence='lexical_candidate_not_call_graph'))
        if previous is not None:
            for name in sorted(set(previous['inventory'])|set(inventory)):
                before=previous['inventory'].get(name);after=inventory.get(name)
                if before!=after:node['changes'].append(dict(file=name,status='added' if before is None else ('removed' if after is None else 'modified')))
            node['compared_to']=previous['lesson']
        node['code_status']='snapshot_verified';node['snapshot']=str(snapshot)
        previous=dict(lesson=data['id'],inventory=inventory);nodes.append(node)
    return dict(lessons=nodes,limitations=['Snapshot changes do not establish when code changed on video.',
                'Symbols and dependency candidates use lexical matching; comments and dynamic code can mislead.'])
