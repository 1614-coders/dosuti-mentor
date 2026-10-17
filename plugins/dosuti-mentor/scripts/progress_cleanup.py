"""Conservative recovery of abandoned unit journal writes under writer locks."""
import hashlib
import json
from pathlib import Path
import re
from profile_storage import checked, profile_path
from storage_lock import file_lock
import unit_learning
import learning_segments


def inspect(folder):
    """Only the managed unit journal namespace is eligible; unknown files stay."""
    path=folder/'unit-learning.json'
    journal=unit_learning.load(path)
    manifest=learning_segments.read_manifest(path)
    references={entry['sha256'] for entry in (manifest or {}).get('segments',[])}
    candidates=[];preserved=[];unknown=False
    children=[]
    import itertools
    children=list(itertools.islice(folder.iterdir(),2001))
    if len(children)>2000:raise ValueError('Profile cleanup scan limit reached')
    for p in children:
        if not re.fullmatch(r'unit-learning\.json\.[0-9a-f]{32}\.tmp',p.name):continue
        try:
            checked(p)
            if p.stat().st_size>learning_segments.LIMIT:raise ValueError('Temporary journal too large')
            unit_learning.load(p)
            candidates.append(p)
        except (OSError,ValueError,KeyError,TypeError):
            preserved.append(dict(path=p.name,reason='Unverified temporary journal'));unknown=True
    segments=folder/'unit-learning-segments';checked(segments,True)
    if segments.exists():
        entries=list(itertools.islice(segments.iterdir(),2001))
        if len(entries)>2000:raise ValueError('Segment cleanup scan limit reached')
        for p in entries:
            if not re.fullmatch(r'[0-9a-f]{64}\.json',p.name):continue
            if p.stem in references:continue
            try:
                checked(p)
                if unknown:raise ValueError('Unverified temporary journals may reference this segment')
                if p.stat().st_size>learning_segments.LIMIT:raise ValueError('Segment too large')
                raw=p.read_bytes()
                if hashlib.sha256(raw).hexdigest()!=p.stem:raise ValueError('Checksum mismatch')
                events=json.loads(raw)
                if not isinstance(events,list) or not events:raise ValueError('Invalid segment')
                # Validate through the same reader, without publishing a fake manifest.
                first=events[0].get('revision')
                if type(first) is not int or first<1:raise ValueError('Invalid segment revision')
                for index,event in enumerate(events,first):
                    if event.get('revision')!=index:raise ValueError('Invalid segment order')
                    unit_learning.validate(event['value'],event['type'])
                candidates.append(p)
            except (OSError,ValueError,KeyError,TypeError,AttributeError):
                preserved.append(dict(path=p.relative_to(folder).as_posix(),reason='Unverified orphan segment'))
    return journal['revision'],candidates,preserved


def cleanup(args):
    import course
    root=course.root(args);course.read(root/'course.json')
    if root.is_relative_to(Path(__file__).resolve().parents[1]):raise ValueError('Cleanup must stay outside installed plugin')
    from pilot_lifecycle import active
    active(root)
    folder=profile_path(root,getattr(args,'profile','default'))
    apply=bool(getattr(args,'apply',False))
    result=dict(schema='progress_cleanup_v1',read_only=not apply,profile=getattr(args,'profile','default'),
                candidates=[],removed=[],preserved=[],reclaimable_bytes=0)
    if not folder.exists():return result
    # Same order as learn-unit. Never infer inactivity from time or process IDs.
    for path in (root/'.study-units.lock',folder/'unit-learning.lock'):checked(path)
    with file_lock(root/'.study-units.lock',timeout=.1,create_parent=False):
        with file_lock(folder/'unit-learning.lock',timeout=.1,create_parent=False):
            active(root)
            for lock in (root/'.study-units.lock',folder/'unit-learning.lock'):checked(lock)
            profile_path(root,getattr(args,'profile','default'))
            revision,candidates,preserved=inspect(folder)
            result.update(revision=revision,preserved=preserved)
            identities={p:(checked(p),p.stat().st_size,hashlib.sha256(p.read_bytes()).hexdigest()) for p in candidates}
            result['candidates']=[p.relative_to(root).as_posix() for p in candidates]
            result['reclaimable_bytes']=sum(size for identity,size,sha in identities.values())
            if apply:
                for p,(identity,size,sha) in identities.items():
                    if checked(p)!=identity or p.stat().st_size!=size or hashlib.sha256(p.read_bytes()).hexdigest()!=sha:raise ValueError('Cleanup candidate changed; retry inspection')
                    p.unlink();result['removed'].append(p.relative_to(root).as_posix())
            result['note']='Only validated abandoned unit journal temporaries and unreferenced segments; active journal, answers and referenced segments preserved.'
    return result
