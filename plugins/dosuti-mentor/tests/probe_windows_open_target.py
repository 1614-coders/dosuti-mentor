"""Native-only isolated class 10/65 rename matrix. No production changes/retries.

Exit 0: complete, consistent evidence, NOT a required class-10 failure or P1 acceptance.
Each case runs in a separate child process. Failed fixtures are preserved.
"""
import argparse
from contextlib import contextmanager
import ctypes as c
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import tempfile
import threading

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import windows_managed_home as n
from registry_windows import owned_io

OLD, NEW, FOREIGN = b'{"value":"old"}', b'{"value":"new"}', b'UNRELATED MUST STAY IDENTICAL'
PENDING, UNTOUCHED = 0x103, 0xDEADBEEF
READER_ACCESS = 0x80000000 | n.SYNC  # Explicit SYNCHRONIZE for synchronous native I/O.
CASES = ((10, 1, False), (10, 1, True), (65, 3, True))
UNSUPPORTED = {0xC0000002, 0xC0000003, 0xC00000BB}


def emit(row):
    print(json.dumps(row, ensure_ascii=True), flush=True)


def buffer_for(parent, flags):
    n.check_abi()
    name = 'lesson.json'.encode('utf-16-le')
    value = c.create_string_buffer(c.sizeof(n.FILE_RENAME_INFO) + len(name))
    info = n.FILE_RENAME_INFO.from_buffer(value)
    info.Flags, info.RootDirectory, info.FileNameLength = flags, parent, len(name)
    c.memmove(c.addressof(value) + n.FILE_RENAME_INFO.FileName.offset, name, len(name))
    if c.addressof(value) % c.alignment(n.FILE_RENAME_INFO): raise ValueError('Unaligned rename buffer')
    return value


def rename_once(api, source, parent, kind, flags):
    value, ios = buffer_for(parent, flags), n.IO_STATUS_BLOCK()
    ios.Result.Status = n.I32(UNTOUCHED).value
    returned = api.rename_info(source, c.byref(ios), value, len(value), kind) & 0xffffffff
    call = dict(information_class=kind, flags=flags, buffer_size=len(value), buffer_hex=value.raw.hex(),
                root_handle=parent, source_handle=source, ntstatus=returned,
                ntstatus_hex=f'0x{returned:08X}',
                iosb_status=ios.Result.Status & 0xffffffff, iosb_information=ios.Information)
    emit(dict(phase='api_return', call=call))  # Before waits or any final inspection.
    if returned == PENDING:
        # No Python unwind while the kernel might still reference value/ios.
        # The controller may terminate this entire isolated child on deadline.
        try:
            waited = api.wait_io(source, False, None)
            final = ios.Result.Status & 0xffffffff
            if waited != 0 or final in (PENDING, UNTOUCHED):
                raise OSError(f'Completion not established: wait=0x{waited & 0xffffffff:08X}')
        except BaseException as error:
            emit(dict(phase='incomplete_pending', diagnostic_complete=False, error=repr(error), call=call))
            os._exit(3)  # OS process teardown, never free pending buffers in live Python.
        call.update(wait_status=waited, completion_status=final, completion_confirmed=True,
                    final_iosb_status=final, final_iosb_information=ios.Information)
    else:
        call.update(completion_status=returned, completion_confirmed=True,
                    final_iosb_status=ios.Result.Status & 0xffffffff, final_iosb_information=ios.Information)
    emit(dict(phase='api_completed', call=call))
    return call


def describe(api, handle):
    file_id, standard, attrs = n.FILE_ID_INFO(), n.FILE_STANDARD_INFO(), n.FILE_ATTRIBUTE_TAG_INFO()
    for kind, value in ((18, file_id), (1, standard), (9, attrs)):
        api.boolean(api.info(handle, kind, c.byref(value), c.sizeof(value)))
    return dict(identity=[file_id.VolumeSerialNumber, bytes(file_id.FileId).hex()],
                size=standard.EndOfFile, links=standard.NumberOfLinks,
                delete_pending=bool(standard.DeletePending), directory=bool(standard.Directory),
                attributes=attrs.FileAttributes, reparse_tag=attrs.ReparseTag, file_type=api.file_type(handle))


def open_reader(api, parent, name):
    raw = name.encode('utf-16-le')
    value = c.create_string_buffer(raw + b'\0\0')
    text = n.UNICODE_STRING(len(raw), len(raw) + 2, c.cast(value, n.HANDLE))
    oa = n.OBJECT_ATTRIBUTES(c.sizeof(n.OBJECT_ATTRIBUTES), parent, c.pointer(text), 0x1040, None, None)
    ios, handle = n.IO_STATUS_BLOCK(), n.HANDLE()
    ios.Result.Status = n.I32(UNTOUCHED).value
    result = api.create(c.byref(handle), READER_ACCESS, c.byref(oa), c.byref(ios), None,
                        0x80, 7, 1, 0x200000 | 0x20 | 0x40, None, 0)
    emit(dict(phase='reader_open_return', call=dict(
        name=name, root_handle=parent, desired_access=READER_ACCESS, share_access=7,
        create_options=0x200000 | 0x20 | 0x40, ntstatus=result & 0xffffffff,
        ntstatus_hex=f'0x{result & 0xffffffff:08X}', iosb_status=ios.Result.Status & 0xffffffff,
        iosb_information=ios.Information, handle=handle.value)))
    api.status(result, 'Probe reader NtCreateFile')
    try:
        api.status(ios.Result.Status, 'Probe reader IOSB')
        if ios.Information != 1: raise ValueError('Reader did not open an existing file')
        info = describe(api, handle.value)
        if info['attributes'] & 0x400 or info['directory'] or info['file_type'] != 1 or info['links'] != 1:
            raise ValueError('Unsafe reader object')
        return handle.value
    except BaseException:
        api.close(handle)
        raise


@contextmanager
def reader_handle(api, parent, name):
    handle = open_reader(api, parent, name)
    try: yield handle
    finally: api.close(handle)


def read_bytes(api, handle):
    # Small fixtures only. Unlike marker validation, an OLD replaced HANDLE may
    # legitimately be DeletePending/have zero links and must still be readable.
    api.boolean(api.seek(handle, 0, None, 0))
    value, count = c.create_string_buffer(4096), n.U32()
    api.boolean(api.read_file(handle, value, len(value), c.byref(count), None))
    if count.value > len(value): raise ValueError('Invalid native read count')
    return value.raw[:count.value]


def check_effects(before, after, call, opened):
    success = call['completion_status'] == 0
    assert after['foreign'] == before['foreign'], 'Unrelated file changed'
    assert after['target_bytes'] == (NEW if success else OLD).hex(), 'Invalid new target bytes'
    assert after['target']['identity'] == before['source' if success else 'target']['identity']
    assert after['source_bytes'] == NEW.hex(), 'Source HANDLE data changed'
    assert after['entries'] == (['foreign.bin', 'lesson.json'] if success else ['foreign.bin', 'lesson.json', 'source.tmp'])
    if opened:
        assert after['reader_bytes'] == OLD.hex(), 'Old reader did not retain old bytes'
        assert after['reader']['identity'] == before['target']['identity'], 'Reader identity changed'


def handle_count(api):
    current = api.k.GetCurrentProcess
    current.argtypes, current.restype = [], n.HANDLE
    count = api.k.GetProcessHandleCount
    count.argtypes, count.restype = [n.HANDLE, c.POINTER(n.U32)], n.I32
    value = n.U32()
    api.boolean(count(current(), c.byref(value)))
    return value.value


def run_case(fixture, index):
    kind, flags, opened = CASES[index]
    session = n.HomeSession(fixture)
    api, parent = session.api, session.home
    cleanup_errors, row = [], dict(case=index, information_class=kind, flags=flags, reader_requested=opened,
        filesystem='NTFS', windows=platform.platform(), python=sys.version, pointer_size=c.sizeof(n.HANDLE),
        masks=dict(source_access=n.STAGE_ACCESS, source_share=1, reader_access=READER_ACCESS, reader_share=7,
                   directory_access=n.DIR_ACCESS, directory_share=3, create_options=0x200000 | 0x20 | 0x40))
    ready, inspect_after, reader_done = threading.Event(), threading.Event(), threading.Event()
    observed, reader_errors, thread = {}, [], None
    def retained_reader():
        try:
            with reader_handle(api, parent, 'lesson.json') as handle:
                observed.update(before=describe(api, handle), before_bytes=read_bytes(api, handle).hex(), handle=handle)
                ready.set()  # Actual open/read completed; writer cannot guess from timing.
                inspect_after.wait()  # Never release the reader merely to unblock a pending rename.
                observed.update(after=describe(api, handle), after_bytes=read_bytes(api, handle).hex())
        except BaseException as error: reader_errors.append(repr(error))
        finally:
            ready.set(); reader_done.set()
    try:
        for name, raw in (('source.tmp', NEW), ('lesson.json', OLD), ('foreign.bin', FOREIGN)):
            handle = api.open(parent, name, directory=False, stage=True)
            try:
                api.write(handle, raw); api.flush(handle)
                assert read_bytes(api, handle) == raw
                if name == 'source.tmp': source = session.own(handle); handle = None
            finally:
                if handle is not None: api.close(handle)
        with reader_handle(api, parent, 'lesson.json') as target, reader_handle(api, parent, 'foreign.bin') as foreign:
            before = dict(directory=describe(api, parent), source=describe(api, source), target=describe(api, target),
                          source_bytes=read_bytes(api, source).hex(), target_bytes=read_bytes(api, target).hex(),
                          foreign=dict(info=describe(api, foreign), bytes=read_bytes(api, foreign).hex()))
        assert before['source']['identity'] != before['target']['identity'], 'Fixture files must be independent'
        if opened:
            thread = threading.Thread(target=retained_reader, name='probe-target-reader', daemon=False)
            thread.start()
            if not ready.wait(10) or reader_errors: raise RuntimeError(f'Reader open barrier failed: {reader_errors}')
            if reader_done.is_set(): raise RuntimeError('Reader exited before writer operation')
            assert observed['before']['identity'] == before['target']['identity']
            assert observed['before_bytes'] == OLD.hex()
            assert observed['handle'] != source, 'Reader must hold an independent HANDLE'
        row.update(before=before, reader_barrier_confirmed=opened,
                   reader_before=observed.copy() if opened else None)
        emit(dict(phase='prepared', observation=row))
        session.check()
        call = rename_once(api, source, parent, kind, flags)  # Exactly one request.
        row['call'] = call
        inspect_after.set()
        if opened:
            if not reader_done.wait(10): raise TimeoutError('Reader did not finish post-rename inspection')
            thread.join(timeout=5)
            if thread.is_alive() or reader_errors: raise RuntimeError(f'Reader cleanup failed: {reader_errors}')
        with reader_handle(api, parent, 'lesson.json') as target, reader_handle(api, parent, 'foreign.bin') as foreign:
            after = dict(target=describe(api, target), target_bytes=read_bytes(api, target).hex(),
                         foreign=dict(info=describe(api, foreign), bytes=read_bytes(api, foreign).hex()),
                         source_bytes=read_bytes(api, source).hex(), entries=sorted(api.entries(parent)))
        if opened: after.update(reader=observed['after'], reader_bytes=observed['after_bytes'])
        check_effects(before, after, call, opened)
        row.update(after=after, effects_verified=True, diagnostic_complete=True,
                   class_unavailable=kind == 65 and call['completion_status'] in UNSUPPORTED)
    except BaseException as error:
        row.update(diagnostic_complete=False, error=repr(error))
    finally:
        inspect_after.set()
        if thread is not None and thread.ident is not None:
            thread.join(timeout=10)
            if thread.is_alive():
                emit(dict(phase='reader_still_live', diagnostic_complete=False, observation=row))
                os._exit(3)  # Do not close its parent or free any possible live I/O.
        # Do not use HomeSession.close's exception-time suppression. One attempt
        # per owned numeric HANDLE; failures are diagnostic failures, never PASS.
        for handle in reversed(session.handles):
            try: api.close(handle)
            except BaseException as error: cleanup_errors.append(repr(error))
        session.handles.clear()
        row.update(cleanup_errors=cleanup_errors, reader_thread_released=thread is None or not thread.is_alive())
        if cleanup_errors: row['diagnostic_complete'] = False
    return row


def interpretation(rows):
    if len(rows) != 3 or any(not row.get('diagnostic_complete') for row in rows): return 'incomplete'
    statuses = [row['call']['completion_status'] for row in rows]
    if statuses[0] != 0: return 'closed-target control failed; hypothesis inconclusive'
    if statuses[1] == 0: return 'class 10 replaced open target; hypothesis refuted on this runner'
    if statuses[2] == 0 and statuses[1] in (0xC0000022, 0xC0000043):
        return 'consistent with class-10 open-target limitation; class 65 succeeded on this runner'
    return 'inconclusive; inspect exact statuses, no fallback or production fix justified'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--case', type=int, choices=range(3))
    parser.add_argument('--fixture', type=Path)
    args = parser.parse_args()
    if os.name != 'nt' or platform.machine().upper() not in ('AMD64', 'X86_64') or c.sizeof(n.HANDLE) != 8:
        parser.exit(2, 'Windows x64/local NTFS required; no native PASS, skip or fallback\n')
    n.check_abi()
    if args.case is not None:
        if args.fixture is None: parser.error('--case requires --fixture')
        api = n.Native()
        owned_io(lambda: None)  # Warm Python worker before measuring handles.
        for thread in threading.enumerate():
            if thread.name == 'dosuti-registry-update': thread.join(timeout=5)
        before = handle_count(api)
        row = owned_io(lambda: run_case(args.fixture, args.case))
        workers = [thread for thread in threading.enumerate() if thread.name == 'dosuti-registry-update']
        for thread in workers: thread.join(timeout=5)
        row.update(phase='result', fixture=str(args.fixture), handles_before=before, handles_after=handle_count(api))
        row['handles_released'] = row['handles_before'] == row['handles_after']
        row['owning_threads_released'] = not any(thread.is_alive() for thread in workers)
        if not row['handles_released'] or not row['owning_threads_released']: row['diagnostic_complete'] = False
        emit(row)
        # The closed-target control and proposed Ex mechanism must succeed.
        # Class 10 with an open target is observational: either result is valid.
        accepted = row.get('diagnostic_complete') and (args.case == 1 or row['call']['completion_status'] == 0)
        return 0 if accepted else 1
    rows, healthy = [], True
    for index in range(3):
        fixture = Path(tempfile.mkdtemp(prefix=f'dosuti-rename-{index}-')).resolve()
        command = [sys.executable, '-B', str(Path(__file__).resolve()), '--case', str(index), '--fixture', str(fixture)]
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            output, error = process.communicate(timeout=60)
        except subprocess.TimeoutExpired:
            process.kill()
            try: output, error = process.communicate(timeout=10)
            except subprocess.TimeoutExpired:
                emit(dict(phase='unreaped_child', case=index, pid=process.pid, fixture=str(fixture),
                          diagnostic_complete=False, reason='Child did not exit after termination request'))
                return 1
            healthy = False
        sys.stdout.write(output.decode('utf-8', errors='replace')); sys.stdout.flush()
        sys.stderr.write(error.decode('utf-8', errors='replace'))
        results = [json.loads(line) for line in output.decode('utf-8').splitlines() if line.startswith('{')]
        result = next((row for row in reversed(results) if row.get('phase') == 'result'), None)
        ok = process.returncode == 0 and result is not None and result.get('diagnostic_complete')
        if result is not None: rows.append(result)
        healthy = healthy and ok
        emit(dict(phase='child_exit', case=index, returncode=process.returncode, process_reaped=process.poll() is not None,
                  fixture=str(fixture), fixture_preserved=not ok))
        if ok: shutil.rmtree(fixture)  # Own isolated tree, only after child exit/verification.
    emit(dict(phase='summary', interpretation=interpretation(rows), diagnostic_complete=healthy))
    return 0 if healthy else 1


if __name__ == '__main__':
    raise SystemExit(main())
