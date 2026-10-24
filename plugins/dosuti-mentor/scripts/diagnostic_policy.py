"""Objective ordering constraints for opt-in adaptive_v2; no semantic grader."""
POLICY='adaptive_v2'
LEVELS=('basic','applied','transfer')


def validate_graph(names,graph):
    if not isinstance(graph,dict) or set(graph)-set(names):raise ValueError('Invalid competence prerequisites')
    for name,parents in graph.items():
        if (not isinstance(parents,list) or any(not isinstance(p,str) or p not in names or p==name for p in parents)
                or len(set(parents))!=len(parents)):raise ValueError('Invalid prerequisite reference')
    visiting=set();done=set()
    def visit(name):
        if name in visiting:raise ValueError('Cyclic diagnostic prerequisites')
        if name in done:return
        visiting.add(name)
        for parent in graph.get(name,[]):visit(parent)
        visiting.remove(name);done.add(name)
    for name in names:visit(name)


def recovery(current):
    """Reconstruct failed-skill obligations from actual assessments, not last answer alone."""
    stack=[]
    for row in sorted(current['answers']+current.get('skips',[]),key=lambda r:r['revision']):
        if row.get('action')!='answer':stack=[];continue
        assessment=row.get('assessment',{});result=assessment.get('result')
        if result=='incorrect':
            failed=dict(competence=row['competence'],difficulty=row['difficulty'],revision=row['revision'])
            if stack and stack[-1]['competence']==row['competence']:
                failed['difficulty']=max((failed['difficulty'],stack[-1]['difficulty']),key=LEVELS.index)
                stack[-1]=failed
            else:stack.append(failed)
        elif (result=='correct' and assessment['assistance']['status']=='independent' and stack
              and row['competence']==stack[-1]['competence']
              and LEVELS.index(row['difficulty'])>=LEVELS.index(stack[-1]['difficulty'])):stack.pop()
    return stack


def eligible(current):
    # Considered prerequisites order the interview; skips/insufficient never prove knowledge.
    covered={r['competence'] for r in current['answers']+current.get('skips',[])}
    return [name for name in current['competences'] if set(current['competence_prerequisites'].get(name,[]))<=covered]


def check_prompt(current,competence):
    stack=recovery(current)
    allowed=set(eligible(current))
    if stack:
        failed=stack[-1]['competence'];allowed.add(failed)
        allowed.update(current['competence_prerequisites'].get(failed,[]))
    if competence is not None and competence not in allowed:
        raise ValueError('Check prerequisite competences before dependent topics')


def check_answer(current,prompt,value):
    following=value.get('next_competence')
    if following is None or current['question_budget']['remaining']==0:return
    assessment=value['assessment'];result=assessment['result']
    rank=LEVELS.index(prompt['difficulty']);next_rank=LEVELS.index(value['next_difficulty'])
    rows=current['answers']+current.get('skips',[])+[prompt]
    rows=sorted(rows,key=lambda r:r['revision'])
    covered={r['competence'] for r in current['answers'] if r.get('assessment',{}).get('result') in ('correct','partial','incorrect')}
    if (len(rows)>=3 and all(r['competence']==prompt['competence'] for r in rows[-3:])
            and following!=prompt['competence'] and following in eligible(current) and following not in covered):
        return  # Preserve recovery, but allow coverage after repeated unsuccessful probes.
    if result=='incorrect':
        parents=current['competence_prerequisites'].get(prompt['competence'],[]) or [prompt['competence']]
        if following not in parents or next_rank>max(0,rank-1):
            raise ValueError('After an incorrect answer check a concrete prerequisite at a lower difficulty')
    elif result=='correct' and assessment['assistance']['status']=='independent':
        row=dict(value,revision=current['revision']+1,competence=prompt['competence'],difficulty=prompt['difficulty'])
        stack=recovery(dict(current,answers=current['answers']+[row]))
        if stack:
            failed=stack[-1]
            if following!=failed['competence'] or next_rank<max(LEVELS.index(failed['difficulty']),min(2,rank+1)):
                raise ValueError('A prerequisite success requires a harder check of the failed skill')
        elif following==prompt['competence'] and next_rank<min(2,rank+1):
            raise ValueError('Independent success requires higher difficulty or another competence')


def guidance(current):
    stack=recovery(current)
    assessment=current['answers'][-1].get('assessment',{}) if current['answers'] else {}
    action={'incorrect':'check_prerequisite','partial':'probe_gap','insufficient':'clarify_response'}.get(assessment.get('result'))
    if assessment.get('result')=='correct':
        action=('challenge_failed_skill_after_prerequisite_success' if assessment['assistance']['status']=='independent' else 'independent_recheck')
    return dict(selection_policy=POLICY,competence_prerequisites=current['competence_prerequisites'],
                eligible_competences=eligible(current),recovery=stack,
                recovery_action=action if stack else None,
                limitations=['Prerequisite graph and semantic task choice are mentor judgements',
                             'An easier correct answer only confirms its own criteria and difficulty'])
