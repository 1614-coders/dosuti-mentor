"""Project reviewed r10 candidates into a lesson brief and a shareable bibliography.

Semantic fit stays with the mentor. The bibliography uses existing text snapshots;
learner context stays transient or in the existing profile lesson_context journal.
"""
from copy import deepcopy
import json
from pathlib import Path
from urllib.parse import urlsplit

MAX_REFERENCE_BYTES=24000
MAX_BRIEF_BYTES=32000



def bounded(result):
    if len(json.dumps(result,ensure_ascii=False,separators=(',',':')).encode())>MAX_BRIEF_BYTES:
        raise ValueError('Lesson brief budget exceeded; reduce sources or the transient context')
    return result


def text(value,name,maximum=1000,empty=False):
    if not isinstance(value,str) or (not empty and not value.strip()) or len(value)>maximum or '\0' in value:
        raise ValueError('Invalid lesson brief '+name)


def validate_request(request):
    fields={'goal','gap','learner_materials_sufficient','knowledge','reviews'}
    if not isinstance(request,dict) or set(request)!=fields:raise ValueError('Invalid lesson request fields')
    text(request['goal'],'goal',600);text(request['gap'],'gap',600,empty=request['learner_materials_sufficient'] is True)
    if type(request['learner_materials_sufficient']) is not bool:raise ValueError('Declare whether learner materials cover the goal')
    knowledge=request['knowledge']
    if not isinstance(knowledge,dict) or set(knowledge)!={'confirmed','reported','uncertain'}:raise ValueError('Separate confirmed, reported and uncertain knowledge')
    for values in knowledge.values():
        if not isinstance(values,list) or len(values)>8:raise ValueError('Use a small learner knowledge summary')
        for item in values:text(item,'knowledge item',300)
    reviews=request['reviews']
    if not isinstance(reviews,list) or len(reviews)>3:raise ValueError('Review at most three candidates')
    ids=set()
    for review in reviews:
        if not isinstance(review,dict) or set(review)!={'entry_id','verdict','prerequisites','basis','theory_plan','practice_plan'}:raise ValueError('Invalid fit review fields')
        for key in ('entry_id','basis','theory_plan','practice_plan'):text(review[key],key)
        if review['entry_id'] in ids:raise ValueError('Duplicate fit review')
        ids.add(review['entry_id'])
        if review['verdict'] not in ('suitable','conditional','unsuitable') or review['prerequisites'] not in ('confirmed','needs_check','missing'):
            raise ValueError('Invalid recorded fit/prerequisite status')
        if review['verdict']=='suitable' and review['prerequisites']!='confirmed':raise ValueError('Unverified prerequisites require a conditional plan')
    return request


def read_request(path):
    from profile_storage import checked
    path=Path(path);checked(path)
    with path.open('rb') as stream:raw=stream.read(16*1024+1)
    if len(raw)>16*1024:raise ValueError('Lesson request exceeds 16 KiB')
    try:return validate_request(json.loads(raw))
    except (RecursionError,UnicodeError) as error:raise ValueError('Invalid lesson request JSON') from error


def lesson_brief(selection,request):
    from source_library import R10_SHA256
    validate_request(request)
    result=dict(schema='library_lesson_brief_v1',status='needs_fit_review',next_action='review_fit',
        selected_entry_ids=[],omitted_count=0,ready_for_main_content=False,reference_markdown='',
        fact_verified=False,mastery_assessed=False,ai_content_import_allowed=False,
        fit_verification='assistant_recorded_judgement_not_semantic_or_independent_validation',
        readiness_basis='recorded_fit_and_prerequisite_review_not_automatic_knowledge_validation')
    if request['learner_materials_sufficient']:
        return bounded(result|dict(status='learner_materials_sufficient',next_action='use_learner_materials'))
    if request['reviews'] and all(r['verdict']=='unsuitable' for r in request['reviews']):
        return bounded(result|dict(status='no_suitable_material',next_action='find_material_for_gap'))
    if selection is None or selection.get('status')=='not_configured':
        return bounded(result|dict(status='not_configured',next_action='use_existing_research'))
    if selection.get('status')!='candidates' or selection.get('archive_sha256')!=R10_SHA256:
        raise ValueError('Use an eligible r10 candidate selection before preparing a lesson')
    candidates={c['entry']['entry_id']:c for c in selection['candidates']}
    selected=[];plans=[]
    for review in request['reviews']:
        if review['verdict']=='unsuitable':continue
        if len(selected)==2:result['omitted_count']+=1;continue
        candidate=candidates.get(review['entry_id'])
        if candidate is None:raise ValueError('Fit review refers to an unselected entry')
        entry,card=candidate['entry'],candidate['card']
        if (entry.get('ai_use_mode')!='reference_link' or card.get('ai_use_mode')!='reference_link'
                or card.get('priority') not in ('A','B') or entry.get('status') not in ('checked_text_excerpt','checked_task_excerpt')):
            raise ValueError('Fit judgement cannot override source rights or eligibility')
        url=entry.get('url','');parsed=urlsplit(url)
        if parsed.scheme not in ('http','https') or not parsed.hostname or parsed.username is not None or parsed.password is not None or any(c in url for c in '\r\n<>'):
            raise ValueError('Invalid bibliographic URL')
        # Persist library-owned annotations only, never personal knowledge or fit notes.
        selected.append(dict(entry=deepcopy(entry),card=deepcopy(card),card_notes=candidate['card_notes'],
            entry_path=candidate['entry_path'],card_path=candidate['card_path']))
        plans.append(deepcopy(review))
    result['adaptation']=dict(goal=request['goal'],gap=request['gap'],knowledge=deepcopy(request['knowledge']),reviews=plans,
        knowledge_is_recorded_context_not_new_evidence=True,practice_origin='learner_homework_first_otherwise_original_assistant_task')
    if not selected:
        if request['reviews']:result.update(status='no_suitable_material',next_action='find_material_for_gap')
        return bounded(result)
    lines=['# r10 — собственные библиографические аннотации',
        'Это ссылки и аннотации библиотеки, не текст внешних уроков и не доказательство истинности.',
        'reference_link не разрешает автоматически импортировать сайт, задачи, RAG или embeddings.',
        'Даты — исторические проверки r10; актуальность условий/версии/прав не подтверждена повторно.',
        'fact_verified=false; independent_check=false; ai_content_import_allowed=false.',
        'Archive SHA256: '+selection['archive_sha256']]
    maintenance=selection.get('maintenance',{})
    if maintenance:lines.append('Maintenance snapshot (not a current complete check): '+json.dumps(maintenance,ensure_ascii=False,separators=(',',':')))
    for candidate in selected:
        entry=candidate['entry']
        lines.extend(['',f"## {entry['entry_id']} / {entry['source']}",'URL: <'+entry['url']+'>',
            json.dumps(candidate,ensure_ascii=False,separators=(',',':'))])
    markdown='\n'.join(lines)+'\n'
    if len(markdown.encode())>MAX_REFERENCE_BYTES:raise ValueError('Reference budget exceeded; select fewer sources without truncating rights')
    conditional=any(r['verdict']=='conditional' for r in plans)
    result.update(status='reviewed_brief',next_action='check_prerequisites' if conditional else 'prepare_lesson',
        ready_for_main_content=not conditional,selected_entry_ids=[c['entry']['entry_id'] for c in selected],reference_markdown=markdown,
        persistence='Reference markdown only: existing role=assistant text source; personal adaptation uses the profile lesson_context')
    return bounded(result)
