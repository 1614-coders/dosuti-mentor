"""Small read-only continuation snapshot; no inferred learner answers or grades."""
import json
from profile_storage import checked


def continuation(root, session=None):
    result=dict(session=session,open_activities=[],lessons=[],unit_checkpoints=[],saved={},errors=[])
    try:
        import pilot_activity
        result['open_activities']=[dict(id=a['id'],stage=a['stage'],session=a['session'],started=a['started'],method=a['method'],
            next_action='Continue the real operation, or recover as interrupted after verifying the worker stopped') for a in pilot_activity.read(root)['activities'] if a['status']=='running']
    except (ValueError,OSError,KeyError,TypeError) as error: result['errors'].append(str(error))
    try:
        import pilot_assessment as assessment
        data=assessment.read(root)
        for lesson in sorted({e['value']['lesson'] for e in data['events'] if e['type']=='plan'}):
            plan=assessment.latest(data,lesson);changed=assessment.stale(root,plan)
            answers={e['value']['question']:e for e in data['events'] if e['type']=='answer' and e['value']['lesson']==lesson and e['value']['plan_revision']==plan['revision']}
            pending=[q for q in plan['value']['questions'] if q['id'] not in answers]
            result['lessons'].append(dict(lesson=lesson,plan_revision=plan['revision'],stale=changed,
                next_question=None if changed or not pending else {k:pending[0][k] for k in ('id','kind','prompt')},
                answered=len(answers),remaining=len(pending),ungraded=[q for q,e in answers.items() if e['value']['verdict']=='ungraded'],
                note='Respect the ordinary learning checkpoint; do not restart onboarding or use a stale plan'))
    except (ValueError,OSError,KeyError,TypeError) as error: result['errors'].append(str(error))
    folder=root/'course/learners/pilot'
    try:
        for parent in (folder.parent,folder): checked(parent,True)
        for name in ('learning-intent.json','preferences.json','course-preferences.json','cursor.json'):
            path=folder/name;checked(path)
            if path.is_file():
                if path.stat().st_size>900000: raise ValueError('Continuation file too large: '+name)
                result['saved'][name]=json.loads(path.read_text(encoding='utf-8'))
    except (ValueError,OSError,KeyError,TypeError) as error: result['errors'].append(str(error))
    if session is not None:
        try:
            from course import slug
            slug(session)
            path=folder/'unit-learning.json';checked(path)
            if path.is_file():
                from unit_learning import load
                events=load(path,compact=True)['events']
                from study_units import load as load_units, checked_units
                registry=load_units(root/'course',include_history=False)
                for event in events:
                    if event['type']!='checkpoint' or event['session']!=session: continue
                    unit=checked_units(root/'course',registry,[event['unit']])[event['unit']]
                    valid=not unit['stale'] and unit['content_sha256']==event['unit_sha256']
                    result['unit_checkpoints'].append(dict(unit=event['unit'],revision=event['revision'],valid=valid,
                        continuation=event['value'] if valid else None,reason=None if valid else 'unit_or_source_changed'))
        except (ValueError,OSError,KeyError,TypeError) as error: result['errors'].append(str(error))
    return result
