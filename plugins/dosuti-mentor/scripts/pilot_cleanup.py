"""Explicit cleanup of a newly owned pilot tree, with an external report and receipt.

Does not uninstall Codex plugins, remove native chats or shared system dependencies.
Run from a Python installation and working directory outside the tree being removed.
"""
from contextlib import ExitStack
import hashlib
import json
import os
from pathlib import Path
import stat
import sys
from atomic_files import atomic_json, atomic_text
from pilot_assessment import digest
from profile_storage import checked
from storage_lock import file_lock
from pilot_lifecycle import protected, guard

MARKER='.cleanup-pending.json'


def seal(value): return hashlib.sha256(json.dumps(value,sort_keys=True,ensure_ascii=False).encode('utf-8')).hexdigest()


def inventory(root):
    result={}
    def visit(folder):
        for path in sorted(folder.iterdir()):
            if path==root/MARKER: continue
            name=path.relative_to(root).as_posix();info=path.lstat()
            if len(result)>=50000: raise ValueError('Cleanup inventory limit exceeded')
            if stat.S_ISLNK(info.st_mode): result[name]=dict(kind='link',target=os.readlink(path))
            elif stat.S_ISDIR(info.st_mode):
                # Junctions/reparse points must never be traversed on Windows.
                if getattr(info,'st_file_attributes',0)&0x400: raise ValueError('Reparse directories are not supported')
                result[name]=dict(kind='directory');visit(path)
            elif stat.S_ISREG(info.st_mode):
                if info.st_nlink!=1: raise ValueError('Hard-linked cleanup file; preserve it for manual review')
                result[name]=dict(kind='file',bytes=info.st_size,sha256=digest(path))
            else: raise ValueError('Special cleanup file; manual review required')
    visit(root);return result


def owned(pilot):
    from pilot import checked_root, load
    pilot=checked_root(pilot);data=load(pilot)
    if not data.get('cleanup_owner'): raise ValueError('Legacy pilot has no cleanup ownership marker; preserve it for manual cleanup')
    root=pilot
    setup=pilot.parent/'setup.json'
    if pilot.name=='run' and setup.exists():
        from pilot_setup import read
        setup_data=read(pilot.parent)
        if not setup_data.get('owner'): raise ValueError('Legacy setup root is not owned for automatic cleanup')
        root=pilot.parent
    plugin=Path(__file__).resolve().parents[1]
    if root==Path(root.anchor) or root.is_relative_to(plugin) or plugin.is_relative_to(root):
        raise ValueError('Unsafe cleanup root')
    return root,data['cleanup_owner']


def lock_paths(root):
    result=[]
    def visit(folder):
        for p in folder.iterdir():
            info=p.lstat()
            if stat.S_ISLNK(info.st_mode): continue
            if stat.S_ISDIR(info.st_mode):
                if getattr(info,'st_file_attributes',0)&0x400: raise ValueError('Reparse directory')
                visit(p)
            elif stat.S_ISREG(info.st_mode) and p.name.endswith('.lock'): result.append(p)
    visit(root)
    return sorted(result)


def idle(pilot):
    from pilot_activity import read
    if any(a['status']=='running' for a in read(pilot)['activities']):
        raise ValueError('Finish or mark interrupted activities before cleanup')


@protected(timeout=.1)
def plan(pilot, output):
    from pilot import checked_root, export
    pilot=checked_root(pilot);root,owner=owned(pilot)
    if output is None: raise ValueError('Choose an external --output cleanup-plan.json')
    output=checked_root(output)
    if output.is_relative_to(root): raise ValueError('Cleanup plan/report must be outside the test tree')
    # The exported report and receipt live beside the plan; never replace arbitrary existing files.
    report=output.with_name(output.stem+'-report.md');receipt=output.with_name(output.stem+'-receipt.json')
    if any(p.exists() for p in (output,report,receipt)): raise ValueError('Choose unused external output names')
    for p in (pilot/'.activities.lock',pilot/'.assessment.lock',pilot/'.pilot.lock'):
        checked(p)
        with file_lock(p,timeout=.1): pass
    with ExitStack() as stack:
        for path in lock_paths(root):
            if path!=pilot/'.pilot-use.lock': stack.enter_context(file_lock(path,timeout=.1,create_parent=False))
        idle(pilot)
        if root!=pilot:
            from pilot_setup import read
            if any(a['status']=='running' for a in read(root)['attempts']): raise ValueError('Setup is running')
        exported=export(pilot,report)
        if not exported['coverage']['complete']:
            raise ValueError('Incomplete report; cleanup refused. Preserve original journals and inspect the exported diagnostics')
        value=dict(schema='pilot_cleanup_v2',coverage=exported['coverage'],root=str(root),pilot=str(pilot),owner=owner,
                   report=str(report),report_sha256=digest(report),receipt=str(receipt),entries=inventory(root))
        value['seal']=seal(value);atomic_json(output,value)
    return dict(plan=str(output),report=str(report),files=len(value['entries']),
                bytes=sum(e.get('bytes',0) for e in value['entries'].values()),
                requires_explicit_apply=True,plugin_and_chat_history='Separate Codex removal required')


def _apply(pilot, plan_file):
    from pilot import checked_root, load
    if plan_file is None: raise ValueError('--plan-file required')
    path=checked_root(plan_file)
    if not path.is_file() or path.stat().st_size>12_000_000: raise ValueError('Missing or oversized cleanup plan')
    value=json.loads(path.read_text(encoding='utf-8'));signature=value.pop('seal',None)
    if value.get('schema')!='pilot_cleanup_v2' or seal(value)!=signature: raise ValueError('Cleanup plan changed or legacy plan; generate a new plan with export coverage')
    coverage=value.get('coverage')
    if (not isinstance(coverage,dict) or coverage.get('schema')!='pilot_export_coverage_v1' or coverage.get('complete') is not True
            or not coverage.get('entries') or any(e.get('status')!='complete' or e.get('detected')!=e.get('exported') for e in coverage['entries'])):
        raise ValueError('Incomplete export coverage; cleanup refused')
    root=checked_root(value['root']);pilot=checked_root(pilot)
    if str(pilot)!=value['pilot'] or not pilot.is_relative_to(root) or path.is_relative_to(root): raise ValueError('Cleanup path mismatch')
    for name in ('report','receipt'):
        if checked_root(value[name]).is_relative_to(root): raise ValueError('Report/receipt must remain outside cleanup root')
    report=checked_root(value['report']);receipt=checked_root(value['receipt'])
    if not report.is_file() or digest(report)!=value['report_sha256']: raise ValueError('Exported report missing or changed')
    if Path(sys.executable).resolve().is_relative_to(root) or Path.cwd().resolve().is_relative_to(root):
        raise ValueError('Run cleanup from a Python and working directory outside the test folder')
    plugin=Path(__file__).resolve().parents[1]
    if plugin.is_relative_to(root) or root.is_relative_to(plugin): raise ValueError('Cleanup must not delete executing plugin')
    if not root.exists():
        if receipt.is_file() and json.loads(receipt.read_text(encoding='utf-8')).get('seal')==signature:
            return dict(status='already_removed',receipt=str(receipt))
        raise ValueError('Cleanup root missing without matching receipt')
    marker=root/MARKER;checked(marker)
    continuing=marker.exists()
    if continuing:
        if json.loads(marker.read_text(encoding='utf-8'))!={'seal':signature,'owner':value['owner']}:
            raise ValueError('Another cleanup is pending')
    else:
        if receipt.exists(): raise ValueError('Unexpected existing cleanup receipt')
        actual,owner=owned(pilot)
        if actual!=root or owner!=value['owner']: raise ValueError('Cleanup ownership changed')
    with ExitStack() as stack:
        if not continuing: stack.enter_context(guard(pilot,timeout=.1))
        for lock in lock_paths(root):
            if continuing or lock!=pilot/'.pilot-use.lock':
                stack.enter_context(file_lock(lock,timeout=.1,create_parent=False))
        if not continuing: idle(pilot)
        current=inventory(root);expected=value['entries']
        if any(name not in expected or expected[name]!=item for name,item in current.items()) or (not continuing and current!=expected):
            raise ValueError('Test files changed after the plan; nothing removed')
        atomic_json(marker,dict(seal=signature,owner=value['owner']))
        atomic_json(receipt,dict(schema='pilot_cleanup_receipt_v1',seal=signature,status='removing',root=str(root),
            report=str(report),plugin_removed=False,chat_history_removed=False))
    # Locks are released before deleting lock files (required on Windows). The durable marker blocks new pilot commands.
    for name in sorted(current,key=lambda n:(len(Path(n).parts),n),reverse=True):
        item=current[name];target=root/name
        # Recheck each entry immediately before removal; never follow symlinks.
        if item['kind']=='directory':
            if target.is_symlink(): raise ValueError('Directory changed during cleanup')
            target.rmdir()
        elif item['kind']=='link':
            if not target.is_symlink() or os.readlink(target)!=item['target']: raise ValueError('Link changed during cleanup')
            target.unlink()
        else:
            if target.is_symlink() or not target.is_file() or target.stat().st_nlink!=1 or digest(target)!=item['sha256']:
                raise ValueError('File changed during cleanup')
            target.unlink()
    if any(p!=marker for p in root.iterdir()): raise ValueError('Unexpected file appeared during cleanup; preserve the pending marker')
    marker.unlink()
    try: root.rmdir()
    except OSError:
        atomic_json(marker,dict(seal=signature,owner=value['owner']))
        raise
    atomic_json(receipt,dict(schema='pilot_cleanup_receipt_v1',seal=signature,status='test_data_removed',
        root=str(root),report=str(report),plugin_removed=False,chat_history_removed=False))
    return dict(status='test_data_removed',receipt=str(receipt),report=str(report),plugin_removed=False,chat_history_removed=False)


def apply(pilot, plan_file):
    if plan_file is None: raise ValueError('--plan-file required')
    from pilot import checked_root
    path=checked_root(plan_file)
    if not path.is_file(): raise ValueError('Cleanup plan missing')
    lock=path.with_name(path.name+'.apply.lock');checked(lock)
    with file_lock(lock,timeout=.1,create_parent=False):
        return _apply(pilot,path)
