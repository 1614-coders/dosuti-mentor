"""Export and remove only an owned failed installation that has no QA run yet."""
import argparse
from contextlib import ExitStack
import json
from pathlib import Path
import re
from atomic_files import atomic_json, atomic_text
from managed_storage import validate
from pilot import checked_root, literal
import pilot_cleanup as cleanup
from pilot_assessment import digest
from storage_lock import file_lock

DIAGNOSTICS=re.compile(r'(setup\.json|SETUP_REPORT\.md|attempt-\d{2}-[a-z-]+\.(json|log))$')
ALLOWED={'.dosuti-home.json','.bootstrap.lock','.setup.lock','.storage.lock','.heavy.lock',
         'runtime','models','cache','temp','reports'}


def owner(root):
    root=validate(root)
    plugin=Path(__file__).resolve().parents[1]
    if root in (Path(root.anchor),Path.home()) or root.is_relative_to(plugin) or plugin.is_relative_to(root):
        raise ValueError('Unsafe early-cleanup root')
    if (root/'run').exists(): raise ValueError('QA run exists; use the full pilot cleanup/export')
    for path in root.iterdir():
        if path.name not in ALLOWED and not DIAGNOSTICS.fullmatch(path.name) and not re.fullmatch(r'smoke-\d{2}',path.name):
            raise ValueError('Unknown installation entry; preserve it before cleanup: '+path.name)
    if (root/'setup.json').exists():
        from pilot_setup import read
        read(root)  # Corrupt/unknown journals are not silently discarded.
    return json.loads((root/'.dosuti-home.json').read_text(encoding='utf-8-sig'))['id']


def plan(root, output):
    root=checked_root(root);identity=owner(root)
    if output is None: raise ValueError('Choose an external cleanup-plan.json')
    output=checked_root(output)
    if output.is_relative_to(root): raise ValueError('Plan/report must be outside the installation')
    report=output.with_name(output.stem+'-report.md');receipt=output.with_name(output.stem+'-receipt.json')
    if any(p.exists() for p in (output,report,receipt)): raise ValueError('Choose unused external output names')
    # Make cooperative entry points lockable even when Python never got to setup.json.
    for name in ('.bootstrap.lock','.setup.lock','.storage.lock','.heavy.lock'):
        path=root/name
        from profile_storage import checked
        checked(path)
        with file_lock(path,timeout=.1): pass
    with ExitStack() as locks:
        for path in cleanup.lock_paths(root): locks.enter_context(file_lock(path,timeout=.1,create_parent=False))
        if owner(root)!=identity: raise ValueError('Setup ownership changed')
        entries=cleanup.inventory(root)
        lines=['# DoSuti Mentor — незавершённая установка','',
               'Курс/QA-run не создан. Это диагностика установки, не отчёт о пройденном обучении.',
               'Время/ресурсы до запуска Python могут отсутствовать; неизвестные показатели не равны нулю.',
               'План удаляет только перечисленную папку; общий Python, плагин и чаты удаляются отдельно.',
               'Просмотри личные пути и журналы перед передачей. Автоматической отправки нет.','',literal(str(root)),'']
        coverage=dict(schema='setup_export_coverage_v1',complete=True,entries=[])
        used=0
        for name,item in entries.items():
            diagnostic=(name=='.dosuti-home.json' or DIAGNOSTICS.fullmatch(name) or
                        name.startswith('reports/'))
            if not diagnostic: continue
            record=dict(path=name,status='complete',detected=1,exported=0)
            coverage['entries'].append(record)
            try:
                if item['kind']!='file' or item['bytes']>2_000_000: raise ValueError('Diagnostic is not a bounded regular file')
                raw=(root/name).read_bytes();used+=len(raw)
                if used>30_000_000: raise ValueError('Diagnostic export budget exceeded')
                if digest(root/name)!=item['sha256']: raise ValueError('Diagnostic changed during export')
                lines += [literal(name),literal(raw.decode('utf-8-sig')),'']
                record.update(exported=1,sha256=item['sha256'])
            except (OSError,ValueError,UnicodeError) as error:
                record.update(status='error',error=str(error));coverage['complete']=False
                lines += [literal(name),literal(record),'']
        if not (root/'setup.json').exists(): lines+=['setup.json отсутствует: Python-подготовка не началась либо прервалась до журнала.','']
        lines += ['## Опись удаления','',literal(json.dumps(entries,ensure_ascii=False)),
                  '', '## Полнота диагностики','',literal(json.dumps(coverage,ensure_ascii=False))]
        atomic_text(report,'\n'.join(lines))
        if not coverage['complete']: raise ValueError('Incomplete setup report; data preserved; inspect '+str(report))
        # Snapshot includes all binaries too; export contains full diagnostic text plus inventory hashes.
        if cleanup.inventory(root)!=entries: raise ValueError('Installation changed during export; create a new plan')
        value=dict(schema='setup_cleanup_v1',coverage=coverage,root=str(root),pilot=str(root),owner=identity,
                   report=str(report),report_sha256=digest(report),receipt=str(receipt),entries=entries)
        value['seal']=cleanup.seal(value)
        if len(json.dumps(value,ensure_ascii=False).encode('utf-8'))>12_000_000: raise ValueError('Cleanup plan budget exceeded; preserve the installation')
        atomic_json(output,value)
    return dict(plan=str(output),report=str(report),files=len(entries),requires_explicit_apply=True)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=['plan','apply'])
    parser.add_argument('--root',type=Path,required=True)
    parser.add_argument('--output',type=Path)
    parser.add_argument('--plan-file',type=Path)
    args=parser.parse_args()
    try:
        result=plan(args.root,args.output) if args.action=='plan' else cleanup.apply(args.root,args.plan_file,setup_only=True)
        print(json.dumps(result,ensure_ascii=False,indent=2))
    except (ValueError,OSError,RuntimeError) as error: parser.exit(1,str(error)+'\n')


if __name__=='__main__':main()
