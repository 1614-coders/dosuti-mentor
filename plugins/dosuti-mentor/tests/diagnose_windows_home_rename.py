"""Isolated Win32/native 56/58-byte diagnostics, NOT a production fix.

Every call has a new private fixture. JSONL observations + final JSON comparison.
Exit 0 means complete, consistent diagnostic evidence, not Windows P1 acceptance.
"""
import argparse
import ctypes as c
import json
import os
from pathlib import Path
import platform
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import windows_managed_home as w

STAGE = '.diagnostic-stage'
PROGRESS = 'progress-sentinel.json'
PAYLOAD = b'{"schema":"dosuti_home_v1","id":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"}'
EXISTING = b'\xef\xbb\xbf{"schema":"dosuti_home_v1","id":"bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb","extra":"preserve"}'
PROGRESS_BYTES = b'{"answer":"independent learner sentinel"}'
PENDING = 0x103


def emit(value):
    print(json.dumps(value, ensure_ascii=False), flush=True)


def build_buffer(home, length):
    w.check_abi()
    name = w.MARKER.encode('utf-16-le')
    minimum = w.FILE_RENAME_INFO.FileName.offset + len(name) + 2
    if length < minimum: raise ValueError('Diagnostic buffer truncates the original input')
    buffer = c.create_string_buffer(length)
    header = w.FILE_RENAME_INFO.from_buffer(buffer)
    header.Flags, header.RootDirectory, header.FileNameLength = 0, home, len(name)
    c.memmove(c.addressof(buffer) + w.FILE_RENAME_INFO.FileName.offset, name, len(name))
    if c.addressof(buffer) % c.alignment(w.FILE_RENAME_INFO):
        raise RuntimeError('Unaligned diagnostic buffer')
    return buffer


def variant(kind, length):
    required = c.sizeof(w.FILE_RENAME_INFO) + len(w.MARKER.encode('utf-16-le'))
    return dict(label=('Native 56: original-buffer reproduction below documented size' if kind == 'native' and length == 56 else
                       'Native 58: documented-size control' if kind == 'native' and length == 58 else
                       'Win32: original-buffer reproduction'),
                native_documented_minimum=required,
                meets_native_documented_size=length >= required,
                caveat='Native acceptance of 56 bytes does not establish conformance to the documented size contract; size is not a proven cause of WinError 87.')


def parameters(buffer, home):
    header = w.FILE_RENAME_INFO.from_buffer(buffer)
    if header.RootDirectory != home or not home or header.Flags:
        raise ValueError('Unexpected diagnostic fields')
    offset = w.FILE_RENAME_INFO.FileName.offset
    name = buffer.raw[offset:offset + header.FileNameLength]
    if name != w.MARKER.encode('utf-16-le'): raise ValueError('Unexpected diagnostic name')
    normalized = bytearray(buffer.raw)
    start = w.FILE_RENAME_INFO.RootDirectory.offset
    normalized[start:start + c.sizeof(w.HANDLE)] = bytes(c.sizeof(w.HANDLE))
    return dict(buffer_size=len(buffer), buffer_hex=buffer.raw.hex(), root_handle=home,
                native_size_reference=dict(documented_minimum=c.sizeof(w.FILE_RENAME_INFO) + len(name),
                                           supplied_bytes=len(buffer),
                                           relation='below' if len(buffer) < c.sizeof(w.FILE_RENAME_INFO) + len(name) else 'meets',
                                           acceptance_is_not_documented_contract_guarantee=True),
                normalized_buffer_hex=normalized.hex(),
                fields=dict(root_role='held_home', source_role='held_stage', replace=False,
                            filename_utf16le=name.hex(), filename_length=len(name),
                            sizeof=c.sizeof(w.FILE_RENAME_INFO), alignment=c.alignment(w.FILE_RENAME_INFO),
                            root_offset=start, length_offset=w.FILE_RENAME_INFO.FileNameLength.offset,
                            name_offset=offset, directory_access=w.DIR_ACCESS, directory_share=3,
                            stage_access=w.STAGE_ACCESS, stage_share=1))


def outcome(call):
    if call['api'] == 'win32':
        if type(call.get('return')) is not int or type(call.get('winerror')) is not int:
            raise ValueError('Incomplete Win32 evidence')
        if call['return']: return 'success'
        return 'collision' if call['winerror'] in (80, 183) else 'other_failure'
    if call['api'] != 'native' or type(call.get('returned_status')) is not int:
        raise ValueError('Incomplete native evidence')
    status = call['returned_status']
    if status == PENDING:
        if call.get('completion_confirmed') is not True or type(call.get('completion_status')) is not int:
            raise ValueError('Native operation has not completed')
        status = call['completion_status']
        if status == PENDING: raise ValueError('Native operation is still pending')
    # A non-pending immediate status is authoritative; initialized IOSB is not.
    if status == 0: return 'success'
    return 'collision' if status == w.COLLISION else 'other_failure'


def run_call(api, stage, home, buffer, kind):
    if kind == 'win32':
        returned = api.set_info(stage, 3, buffer, len(buffer))
        error = 0 if returned else c.get_last_error()
        return dict(api=kind, information_class=3, **{'return': int(returned)}, winerror=error)
    if kind != 'native': raise ValueError('Unknown diagnostic API')
    ntset = api.n.NtSetInformationFile
    ntset.argtypes = [w.HANDLE, c.POINTER(w.IO_STATUS_BLOCK), c.c_void_p, w.U32, w.U32]
    ntset.restype = w.I32
    ios = w.IO_STATUS_BLOCK()
    # Distinguish untouched IOSB from actual completion evidence in output.
    ios.Result.Status, ios.Information = w.I32(0xDEADBEEF).value, 0
    status = ntset(stage, c.byref(ios), buffer, len(buffer), 10) & 0xffffffff
    result = dict(api=kind, information_class=10, returned_status=status,
                  ios_status_observed=ios.Result.Status & 0xffffffff, ios_information=ios.Information)
    if status == PENDING:
        emit(dict(phase='pending', observation=result))
        wait = api.k.WaitForSingleObject
        wait.argtypes, wait.restype = [w.HANDLE, w.U32], w.U32
        waited = wait(stage, 30000)
        if waited != 0:
            # Keep buffers/handles alive until process exit. Do not unwind/free
            # memory while the kernel could still reference the pending IOSB.
            emit(dict(phase='incomplete_pending', wait_result=waited, diagnostic_complete=False))
            os._exit(3)  # OS releases handles; own fixture preserved for diagnosis.
        result.update(completion_confirmed=True, completion_status=ios.Result.Status & 0xffffffff,
                      ios_information=ios.Information)
        if result['completion_status'] in (PENDING, 0xDEADBEEF):
            emit(dict(phase='incomplete_pending', diagnostic_complete=False))
            os._exit(3)
    effective = result.get('completion_status', status)
    result['mapped_error'] = int(api.to_dos(w.I32(effective).value))
    return result


def snapshot(session, stage):
    """Read own named entries through held parents; use stage handle for its inode."""
    session.check(); session.sentinel()
    parent_entries = sorted(session.api.entries(session.parent))
    home_entries = sorted(session.api.entries(session.home))
    stage_id = session.inspect(stage, False)[0]
    files = {}
    for parent, prefix, names in ((session.parent, '', [n for n in parent_entries if n != 'fixture home']),
                                   (session.home, 'home/', home_entries)):
        for name in names:
            with session.opened(parent, name, metadata=True) as metadata:
                identity, size = session.inspect(metadata, False)
                if identity == stage_id:
                    raw = session.api.read(stage)
                else:
                    with session.opened(parent, name, directory=False) as readable:
                        if session.inspect(readable, False)[0] != identity:
                            raise ValueError('Snapshot binding changed')
                        raw = session.api.read(readable)
                        if session.inspect(readable, False) != (identity, size):
                            raise ValueError('Snapshot file changed')
                if len(raw) != size: raise ValueError('Snapshot length mismatch')
                files[prefix + name] = dict(identity=[identity[0], identity[1].hex()], size=size, bytes_hex=raw.hex())
    session.check()
    return dict(parent_entries=parent_entries, home_entries=home_entries, files=files)


def check_effects(before, after, call, existing):
    result = outcome(call)
    expected_before = {STAGE, 'home/' + PROGRESS} | ({'home/' + w.MARKER} if existing else set())
    if (set(before['files']) != expected_before or
            before['parent_entries'] != [STAGE, 'fixture home'] or
            set(before['home_entries']) != {PROGRESS} | ({w.MARKER} if existing else set())):
        raise ValueError('Incomplete initial filesystem evidence')
    if (before['files'][STAGE]['bytes_hex'] != PAYLOAD.hex() or
            before['files']['home/' + PROGRESS]['bytes_hex'] != PROGRESS_BYTES.hex() or
            (existing and before['files']['home/' + w.MARKER]['bytes_hex'] != EXISTING.hex())):
        raise ValueError('Incorrect fixture payloads')
    for item in before['files'].values():
        if not item.get('identity') or item.get('size') != len(bytes.fromhex(item['bytes_hex'])):
            raise ValueError('Incomplete file identity/size evidence')
    expected = json.loads(json.dumps(before))
    if result == 'success':
        if existing: raise ValueError('No-replace violation: existing marker was replaced')
        expected['parent_entries'].remove(STAGE)
        expected['home_entries'] = sorted(expected['home_entries'] + [w.MARKER])
        expected['files']['home/' + w.MARKER] = expected['files'].pop(STAGE)
    if after != expected:
        raise ValueError('Unexpected filesystem side effects')
    return dict(operation_outcome=result, effects_verified=True,
                existing_marker_unchanged=existing and result != 'success',
                no_replace_verified=existing and result == 'collision',
                no_replace_control=('collision_rejected' if result == 'collision' else 'inconclusive_failure') if existing else 'not_applicable',
                collision_observed=result == 'collision', progress_unchanged=True)


def experiment(kind, length, existing):
    record = dict(api=kind, buffer_size=length, variant=variant(kind, length),
                  preexisting_marker=existing, phase='preparation')
    try:
        with tempfile.TemporaryDirectory(prefix='dosuti-rename87-') as temporary:
            root = Path(temporary)
            record['fixture_root'] = str(root)
            with w.HomeSession(root / 'fixture home', create=True) as session:
                if session.classification(False) is not None:
                    raise RuntimeError('Marker unexpectedly exists in a fresh fixture')
                api = session.api
                filesystem = c.create_unicode_buffer(32)
                api.boolean(api.volume_info(session.root, None, 0, None, None, None, filesystem, len(filesystem)))
                if filesystem.value != 'NTFS': raise ValueError('Diagnostic filesystem is not NTFS')
                root_identity = session.inspect(session.root, True)[0]
                record['environment'] = dict(filesystem=filesystem.value, windows=platform.platform(),
                                             python=sys.version, pointer_size=c.sizeof(w.HANDLE),
                                             volume_identity=root_identity[0], volume_mapping=list(session.mapping))
                for name, payload in [(PROGRESS, PROGRESS_BYTES)] + ([(w.MARKER, EXISTING)] if existing else []):
                    with session.opened(session.home, name, directory=False, stage=True) as h:
                        api.write(h, payload); api.flush(h)
                        if api.read(h) != payload: raise ValueError('Fixture preparation readback mismatch')
                stage = session.own(api.open(session.parent, STAGE, directory=False, stage=True))
                identity = session.inspect(stage, False)[0]
                api.write(stage, PAYLOAD); api.flush(stage)
                session.verify_stage(stage, identity, PAYLOAD)
                buffer = build_buffer(session.home, length)
                record.update(parameters=parameters(buffer, session.home), stage_handle=stage,
                              before=snapshot(session, stage), phase='rename')
                emit(dict(event='before_call', experiment=record))
                call = run_call(api, stage, session.home, buffer, kind)
                record.update(call=call, phase='verification')
                emit(dict(event='call_result', fixture_root=record['fixture_root'], api=kind,
                          variant=record['variant'], parameters=record['parameters'],
                          phase='verification', call=call))
                record['after'] = snapshot(session, stage)
                record['checks'] = check_effects(record['before'], record['after'], call, existing)
            # All session handles must be closed BEFORE filesystem cleanup. Test
            # releasability in the same process rather than relying on process exit.
            if session.handles: raise ValueError('Diagnostic handles remain registered')
            moved = root.with_name(root.name + '-released')
            root.rename(moved)
            try: moved.rename(root)
            except BaseException:
                # Do not lose the owned temporary root if the probe cannot restore.
                record['release_probe_path'] = str(moved)
                raise
            record.update(handles_released=True, phase='complete', diagnostic_complete=True)
    except Exception as error:
        record.update(diagnostic_complete=False, error_type=type(error).__name__, error=str(error))
    return record


def compare(records):
    expected = {(api, length, existing) for api, length in [('win32', 56), ('native', 56), ('native', 58)]
                for existing in (False, True)}
    keys = [(r.get('api'), r.get('buffer_size'), r.get('preexisting_marker')) for r in records]
    if len(keys) != 6 or set(keys) != expected: raise ValueError('Missing/duplicate diagnostic experiments')
    if len({r.get('fixture_root') for r in records}) != 6: raise ValueError('Fixtures were reused')
    for record in records:
        if record.get('diagnostic_complete') is not True or record.get('handles_released') is not True:
            raise ValueError('Incomplete diagnostic experiment')
        if record['environment'].get('filesystem') != 'NTFS' or record['environment'].get('pointer_size') != 8:
            raise ValueError('Unverified native environment')
        if record.get('variant') != variant(record['api'], record['buffer_size']):
            raise ValueError('Missing/incorrect native size-contract label')
        capture = record['parameters']
        raw = bytes.fromhex(capture['buffer_hex'])
        if len(raw) != record['buffer_size']:
            raise ValueError('Captured buffer size mismatch')
        original = build_buffer(capture['root_handle'], len(raw))
        if raw != original.raw or parameters(original, capture['root_handle']) != capture:
            raise ValueError('Captured buffer fields/normalization mismatch')
        if record['call'].get('api') != record['api'] or record['call'].get('information_class') != (3 if record['api'] == 'win32' else 10):
            raise ValueError('Call API/class mismatch')
        actual = check_effects(record['before'], record['after'], record['call'], record['preexisting_marker'])
        if record.get('checks') != actual: raise ValueError('Inconsistent diagnostic verification')
    environment_keys = ('filesystem', 'windows', 'python', 'pointer_size', 'volume_identity', 'volume_mapping')
    environments = [{key: r['environment'][key] for key in environment_keys} for r in records]
    if any(environment != environments[0] for environment in environments):
        raise ValueError('Environment/volume differs between diagnostic experiments')
    fields = [r['parameters']['fields'] for r in records]
    if any(f != fields[0] for f in fields): raise ValueError('Semantic rename parameters differ')
    normalized = [bytes.fromhex(r['parameters']['normalized_buffer_hex']) for r in records]
    if any(b[:56] != normalized[0][:56] or any(b[56:]) for b in normalized):
        raise ValueError('Unexpected differences beyond RootDirectory role and buffer padding')
    initial = {}
    for record in records:
        state = {p: v['bytes_hex'] for p, v in record['before']['files'].items()}
        group = record['preexisting_marker']
        if group in initial and initial[group] != state: raise ValueError('Initial states differ')
        initial[group] = state
    return dict(diagnostic_complete=True, semantic_fields_equal=True, initial_states_equivalent=True,
                buffer_differences='Only buffer size/tail padding differ between 56 and 58; HANDLE values are fixture-local',
                observations=[dict(api=r['api'], size=r['buffer_size'], variant=r['variant'], existing=r['preexisting_marker'],
                                   call=r['call'], checks=r['checks']) for r in records],
                interpretation='API/size outcome differences are observations, not proof of internal filename transformation or of the cause of WinError 87. Native acceptance of 56 bytes does not guarantee documented size-contract conformance.',
                excluded='Caller-side name/field/mask differences and incomplete stage payloads are checked; snapshots reject unexpected captured-file changes. This does not inspect kernel-side arguments.',
                remaining='Win32-wrapper conversion, size requirements, access/OS/filter behavior require native observations and trace.',
                further_trace='WinDbg at ntdll!NtSetInformationFile: actual incoming name/root/length and returned NTSTATUS.',
                windows_p1_fixed=False)


def diagnose():
    if sys.platform != 'win32': raise RuntimeError('Native Windows x64/NTFS required; mock tests are not native evidence')
    w.check_abi()
    emit(dict(event='environment', windows=platform.platform(), python=sys.version,
              pointer_size=c.sizeof(w.HANDLE), filesystem_requirement='local NTFS enforced by HomeSession'))
    records = []
    for kind, length in [('win32', 56), ('native', 56), ('native', 58)]:
        for existing in (False, True):
            record = experiment(kind, length, existing)
            records.append(record)
            emit(dict(event='experiment_result', experiment=record))
    summary = compare(records)
    emit(dict(event='summary', summary=summary))
    return summary


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--compare-native', action='store_true', help='Compatibility alias: all six isolated variants now always run')
    parser.parse_args()
    try: diagnose()
    except Exception as error:
        emit(dict(event='diagnostic_error', diagnostic_complete=False, error_type=type(error).__name__, error=str(error)))
        raise SystemExit(2)
