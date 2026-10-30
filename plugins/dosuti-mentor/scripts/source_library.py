"""Read-only r10 Markdown navigation. Source annotations are not factual evidence."""
import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
from zipfile import ZipFile, BadZipFile

R10_SHA256='05fb6aa4a7acc9b4618021d1ab32b7621466245b92f2a7dd78e1858f8ee18b62'
ROOT='dosuti-source-library/'
MAX_ARCHIVE_BYTES=5*1024*1024
MAX_READ_CHARS=40000  # Grouped card files are charged in full; only the selected section enters context.
MAX_FILES=10  # Three navigation files, maintenance queue, up to three entry/card pairs.
MAX_CONTEXT_BYTES=40000
LEVELS={'basic':'Н','applied':'С','transfer':'П'}
LINK=re.compile(r'\[([^\]]+)\]\(([^)]+)\)')
BLOCK=re.compile(r'```yaml\s*\n(.*?)\n```',re.S)


class BudgetExceeded(ValueError):pass



def archive_digest(stream):
    """Bound the same open file even when it grows after the path size check."""
    stream.seek(0);checksum=hashlib.sha256();size=0
    while block:=stream.read(min(256*1024,MAX_ARCHIVE_BYTES+1-size)):
        size+=len(block)
        if size>MAX_ARCHIVE_BYTES:raise ValueError('Library exceeds the r10 archive size limit')
        checksum.update(block)
    return checksum.hexdigest()


def metadata(text):
    """Only the documented JSON-valued YAML subset; never load YAML tags or code."""
    result=[]
    for block in BLOCK.findall(text):
        row={}
        for line in block.splitlines():
            if not line.strip():continue
            key,sep,value=line.partition(': ')
            if not sep or not re.fullmatch(r'[a-z][a-z0-9_]*',key) or key in row:raise ValueError('Invalid library metadata')
            row[key]=json.loads(value)
        result.append(row)
    return result


def relative(base,link):
    """Normalize archive links without ever extracting or accessing external paths."""
    link=link.split('#',1)[0]
    if not link or '\\' in link or ':' in link or link.startswith('/'):raise ValueError('Unsafe library link')
    parts=list(PurePosixPath(base).parent.parts)
    for part in PurePosixPath(link).parts:
        if part=='..':
            if not parts:raise ValueError('Library link escapes root')
            parts.pop()
        elif part!='.':parts.append(part)
    return '/'.join(parts)



def card_section(text,identity):
    sections=re.split(r'(?=<a id=")',text)
    section=next((s for s in sections if f'<a id="{identity.lower()}"' in s),None)
    if section is None:
        if len(metadata(text))!=1:raise ValueError('Grouped source card requires an explicit anchor')
        section=text
    # Dated corrections can be appended after a different card in a shared file.
    updates=re.findall(r'(?:^|\n)(## r10 — '+re.escape(identity)+r':[^\n]*\n.*?)(?=\n## |\Z)',text,re.S)
    section=section.split('\n## r10 — ',1)[0]
    preamble=sections[0] if len(sections)>1 and not sections[0].startswith('<a id=') else ''
    return section, '\n\n'.join([preamble,BLOCK.sub('',section).strip()]+updates).strip()


def languages(value):
    # Only the explicit leading language list; negative translation caveats stay caveats.
    match=re.match(r'^(?:RU|EN|DE|FR|JA)(?:\s*[+/,]\s*(?:RU|EN|DE|FR|JA))*',value.strip())
    return re.findall(r'RU|EN|DE|FR|JA',match[0]) if match else []


def search(path=None,*,direction=None,topic=None,level=None,language=None,entries=None):
    result=dict(schema='source_library_r10_v1',status='not_configured',candidates=[],
        learner_materials_first=True,fact_verified=False,independent_check=False,
        ai_content_import_allowed=False,limited=False,gap=False,
        limitations=['Library selection is conditional on the learner goal and prerequisites.',
            'reference_link permits URL/own annotation only, not automatic content ingestion.',
            'Ratings and recorded checks are not independent fact verification.',
            'Maintenance snapshot is not a current external check; selected conditions require rechecking.'])
    path=path or os.environ.get('DOSUTI_SOURCE_LIBRARY')
    if not path:return result
    if level is not None and level not in LEVELS:raise ValueError('Choose basic/applied/transfer after self-assessment or calibration')
    if language is not None and language not in ('RU','EN','DE','FR','JA'):raise ValueError('Choose an explicit material language')
    if entries is not None and (not isinstance(entries,list) or not 1<=len(entries)<=3 or len(set(entries))!=len(entries)):
        raise ValueError('Select at most three distinct entry IDs')
    if topic is not None and direction is None:raise ValueError('Topic requires a direction')
    if entries is not None and topic is None:raise ValueError('Entries require a topic')
    if topic and (level is None or language is None):raise ValueError('Topic selection requires learner level and material language')
    path=Path(path).expanduser()
    from profile_storage import checked
    checked(path)
    if not path.is_file() or path.stat().st_size>MAX_ARCHIVE_BYTES:raise ValueError('Provide the verified r10 ZIP (up to 5 MiB)')
    # Open/hash/parse the same descriptor; no persistent cache or adjacent-folder fallback.
    with path.open('rb') as stream:
        identity=archive_digest(stream)
        if identity!=R10_SHA256:raise ValueError('Library does not match the confirmed r10 archive')
        stream.seek(0)
        try:z=ZipFile(stream)
        except BadZipFile as error:raise ValueError('Invalid r10 ZIP') from error
        with z:
            names=z.namelist()
            if len(names)!=len(set(names)) or len(names)>1000 or sum(i.file_size for i in z.infolist())>8*1024*1024:
                raise ValueError('Invalid library archive bounds')
            io=dict(files=0,characters=0,bytes=0,archive_hash_bytes=2*os.fstat(stream.fileno()).st_size,archive_hash_passes=2,paths=[])
            result.update(status='navigation',revision='r10',archive_sha256=identity,io=io)
            cache={}
            def read(name):
                if name in cache:return cache[name]
                if name.startswith('/') or '..' in PurePosixPath(name).parts:raise ValueError('Unsafe library member')
                info=z.getinfo(ROOT+name)
                if io['files']>=MAX_FILES or io['characters']>=MAX_READ_CHARS:raise BudgetExceeded('Library read budget reached')
                if info.file_size>64*1024:raise BudgetExceeded('Library member exceeds bounded navigation size')
                raw=z.read(info);value=raw.decode('utf-8')
                io['files']+=1;io['bytes']+=len(raw);io['characters']+=len(value);io['paths'].append(name)
                if io['characters']>MAX_READ_CHARS:raise BudgetExceeded('Library character budget reached; reduce candidates')
                cache[name]=value;return value
            try:
                index=read('INDEX.md')
                directions={p.split('/')[1]:p for _,p in LINK.findall(index) if re.fullmatch(r'search/[a-z-]+/INDEX.md',p)}
                if direction is None:result['directions']=list(directions)
                else:
                    if direction not in directions:raise ValueError('Unknown library direction')
                    nav=read(directions[direction]);topics=[]
                    for line in nav.splitlines():
                        for label,link in LINK.findall(line):
                            if re.fullmatch(r'S\d+-\d+',label):topics.append(dict(id=label,path=relative(directions[direction],link),navigation=line))
                    result['topics']=topics
                    if topic is not None:
                        match=next((r for r in topics if r['id']==topic),None)
                        if match is None:raise ValueError('Topic is not in this direction')
                        text=read(match['path']);result['topic_notes']=text
                        queue=read('maintenance/QUEUE.md');result['maintenance']=dict(snapshot_only=True,queue_excerpt=queue)
                        rows=[]
                        for line in text.splitlines():
                            links=LINK.findall(line);entry=next(((i,p) for i,p in links if re.fullmatch(r'E\d+-\d+',i)),None)
                            source=next(((i,p) for i,p in links if re.fullmatch(r'[A-Z]+-\d+',i) and p.startswith('../../domains/')),None)
                            if not entry or not source:continue
                            fields=line.split('|')
                            if len(fields)<4:raise ValueError('Invalid topic table')
                            grade,sep,lang=fields[2].partition('/')
                            if not sep:raise ValueError('Invalid level/language navigation')
                            row=dict(entry=entry,source=source)
                            if LEVELS[level] in grade and language in languages(lang):rows.append(row)
                        if entries is not None:
                            if not set(entries)<={i for i,_ in LINK.findall(text) if re.fullmatch(r'E\d+-\d+',i)}:raise ValueError('Entry is not in this topic')
                            rows=[r for r in rows if r['entry'][0] in entries]
                        used=set()
                        for row in rows:
                            if len(result['candidates'])==3:break
                            eid,ep=row['entry'];sid,sp=row['source']
                            if sid in used:continue
                            entry=metadata(read(relative(match['path'],ep)))
                            if len(entry)!=1 or entry[0].get('entry_id')!=eid or entry[0].get('source')!=sid:raise ValueError('Entry/source binding mismatch')
                            entry=entry[0]
                            if entry.get('status') not in ('checked_text_excerpt','checked_task_excerpt') or entry.get('ai_use_mode')!='reference_link':continue
                            if language not in languages(entry.get('language','')) or LEVELS[level] not in entry.get('level',''):continue
                            cardpath=relative(match['path'],sp);cardtext=read(cardpath)
                            section,notes=card_section(cardtext,sid)
                            cards=[c for c in metadata(section) if c.get('id')==sid]
                            if len(cards)!=1:raise ValueError('Invalid source card binding')
                            card={k:v for k,v in cards[0].items() if k not in ('tags','topics','r2_learning_tasks_before_correction')}
                            if card.get('priority') not in ('A','B') or card.get('ai_use_mode')!='reference_link':continue
                            candidate=dict(entry=entry,card=card,card_notes=notes,entry_path=relative(match['path'],ep),card_path=cardpath,
                                selection='topic_level_language_candidate_not_semantic_approval',requires_goal_and_prerequisite_review=True)
                            if len(json.dumps(result|dict(candidates=result['candidates']+[candidate]),ensure_ascii=False).encode())>MAX_CONTEXT_BYTES:
                                raise BudgetExceeded('Library context budget reached; no card or rights text truncated')
                            result['candidates'].append(candidate);used.add(sid)
                        result.update(status='candidates',gap=len(result['candidates'])<2)
                        result.pop('topics',None)  # The selected topic already retains its full caveats.
                        ids={r['entry'][k] for r in result['candidates'] for k in ('entry_id','source')}
                        result['maintenance']['queue_excerpt']='\n'.join(line for line in queue.splitlines()
                            if not line.startswith('|') or any(re.search(r'(?<![\w-])'+re.escape(i)+r'(?![\w-])',line) for i in ids))
            except BudgetExceeded as error:result.update(limited=True,gap=True,stop_reason=str(error))
            stream.seek(0)
            if archive_digest(stream)!=identity:raise ValueError('Library changed during selection')
    if len(json.dumps(result,ensure_ascii=False).encode())>MAX_CONTEXT_BYTES:
        raise ValueError('Library navigation exceeds context budget; select a narrower direction')
    return result


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--library',type=Path);parser.add_argument('--direction');parser.add_argument('--topic')
    parser.add_argument('--level',choices=list(LEVELS));parser.add_argument('--language',choices=['RU','EN','DE','FR','JA'])
    parser.add_argument('--entries',nargs='+')
    args=parser.parse_args()
    try:result=search(args.library,direction=args.direction,topic=args.topic,level=args.level,language=args.language,entries=args.entries)
    except (OSError,ValueError,KeyError,BadZipFile) as error:parser.exit(2,str(error)+'\n')
    print(json.dumps(result,ensure_ascii=False,separators=(',',':')))

if __name__=='__main__':main()
