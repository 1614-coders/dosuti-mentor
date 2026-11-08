"""Windows local UTF-8 homework adapter; normalized attach_material only.

The public course.attach byte-exact SingleFile contract stays unchanged.
One owner holds source/raw/text HANDLEs until Registry Update completes.
No HTTP, document extraction, pathname fallback, replay or residue deletion.
"""
from copy import deepcopy
import hashlib
from pathlib import Path
import sys
from types import SimpleNamespace
import uuid

import course
import registry_update as registry
from registry_windows import owned_io
from storage_paths import source_kind
import windows_managed_home as n
from windows_material_import import (SingleFile, Security, BoundRegistry, MAX_BYTES, component,
                                    READ, READ_CONTROL, DIRECTORY_WRITE, FILE_WRITE)
from windows_resource_text import ResourceText, utf8_text


def normalize(raw):
    return utf8_text(raw, MAX_BYTES)


def source_path(args):
    if (args.kind != 'homework' or source_kind(args.source) != 'local'
            or any(getattr(args, key, None) is not None
                   for key in ('material_text', 'material_origin', 'import_prepared'))):
        raise ValueError('Windows homework adapter requires one local, unprepared UTF-8 file')
    source = Path(args.source).expanduser().absolute()
    component(source.name)
    if source.suffix.lower() in ('.pdf', '.html', '.htm', '.zip'):
        raise ValueError('PDF/HTML/ZIP are outside the local UTF-8 homework contract')
    return source


class Homework(ResourceText):
    def __init__(self, directory):
        super().__init__(directory, None, {})
        self.residues = []
        self.source_handle = None
        self.preparing_homework = False

    def verify_source(self):
        self.source_session.check()
        if self.stamp(self.source_handle) != self.source_stamp:
            raise ValueError('Homework source changed during import')
        current = self.open(self.source_session, self.source_session.home, self.source_name, False, n.READ_ACCESS)
        try:
            if self.source_session.inspect(current, False)[0] != self.source_stamp[0][0]:
                raise ValueError('Homework source changed its identity')
        finally: self.retire(self.source_session, current)

    def verify(self):
        self.verify_source()
        return super().verify()

    def write_homework(self, payload):
        # Same exclusive create / descriptor / seal protocol as the accepted
        # ResourceText writer, in a distinct homework directory and ownership record.
        self.preparing_homework = True
        self.output_ready = False
        self.category_name = 'homework'
        self.category = self.lesson.own(self.api.open(self.lesson.home, self.category_name, directory=True, create=True))
        self.category_id = self.lesson.inspect(self.category, True)[0]
        self.snapshot_name = uuid.uuid4().hex[:12]
        self.folder = self.directory / self.category_name / self.snapshot_name
        snapshot = self.open(self.lesson, self.category, self.snapshot_name, True, DIRECTORY_WRITE, create=True)
        self.residue = self.folder
        self.residues.append(self.folder)
        self.snapshot_id = self.lesson.inspect(snapshot, True)[0]
        self.name = 'extracted-' + uuid.uuid4().hex + '.txt'
        handle = self.open(self.lesson, snapshot, self.name, False, FILE_WRITE, create=True)
        self.file_id = self.lesson.inspect(handle, False)[0]
        self.api.write(handle, payload)
        self.api.flush(handle)
        self.size, self.checksum = len(payload), hashlib.sha256(payload).hexdigest()
        if self.read(handle) != payload: raise OSError('Homework readback mismatch')
        self.security.seal(handle, False)
        self.retire(self.lesson, handle)
        self.denied(snapshot, self.name, False)
        self.file = self.open(self.lesson, snapshot, self.name, False, READ)
        self.security.seal(snapshot, True)
        self.retire(self.lesson, snapshot)
        self.denied(self.category, self.snapshot_name, True)
        self.snapshot = self.open(self.lesson, self.category, self.snapshot_name, True, n.DIR_ACCESS | READ_CONTROL)
        self.output_ready = True
        self.verify()
        return str(self.folder / self.name)

    def prepare(self, source):
        self.lesson = self.pin(self.directory)
        self.api, self.security = self.lesson.api, Security(self.lesson.api)
        self.lesson_id = self.lesson.inspect(self.lesson.home, True)[0]
        initial = self.read_registry()
        self.expected_revision = registry.revision(initial)
        self.expected_homework = deepcopy(initial.get('homework'))
        self.source_session = self.pin(source.parent)
        self.source_name = source.name
        self.source_handle = self.open(self.source_session, self.source_session.home, source.name, False, n.READ_ACCESS)
        self.source_stamp = self.stamp(self.source_handle)
        if self.source_stamp[0][1] > MAX_BYTES: raise ValueError('Homework exceeds 32 MiB')
        raw = self.read(self.source_handle)
        if len(raw) != self.source_stamp[0][1]: raise ValueError('Homework source changed size')
        self.verify_source()
        normalize(raw)  # Validate UTF-8/signatures and both size bounds before allocating a snapshot.
        try:
            # Retain the accepted raw writer unchanged, then pin a distinct proof
            # before its current-output fields are reused for prepared homework.
            raw_file = super().write_text(raw)
        finally:
            if self.residue is not None: self.residues.append(self.residue)
        raw_checksum = self.checksum
        raw_input = self.checked_input(raw_file, raw_checksum)
        prepared = normalize(self.verify_input(raw_input))
        target = self.write_homework(prepared)
        self.material = dict(status='provided', file=target, sha256=self.checksum,
            origin=dict(source=str(source), effective_url=None, media_type='', raw_file=raw_file,
                        sha256=raw_checksum, size=len(raw), extraction='utf8_text'))
        self.material['revision'] = course.homework_revision(self.material)
        self.verify()

    def commit(self):
        if self.writers: raise ValueError('Writable homework HANDLE still owned before registry commit')
        def change(data):
            self.verify()
            if (not isinstance(data.get('id'), str)
                    or data['id'].casefold() != self.directory.name.casefold()):
                raise ValueError('Homework lesson identity changed')
            if data.get('homework') != self.expected_homework:
                raise registry.RevisionConflict('Homework changed concurrently; read current state')
            course.publish_attachment(data, SimpleNamespace(kind='homework'), self.material)
        self.receipt = registry.update(self.directory, change, expected_revision=self.expected_revision,
                                      registry=BoundRegistry(self.directory, self.lesson_id))
        try:
            self.verify()
            if self.read_registry().get('homework') != self.material:
                raise ValueError('Homework changed after confirmed commit')
        except BaseException as error:
            raise registry.CommitInterrupted(self.receipt, 'committed', phase='homework-verification') from error
        return self.receipt

    def close(self):
        if self.closed: return
        try: SingleFile.close(self)
        finally:
            if self.receipt is None:
                others = [path for path in self.residues if path != self.residue]
                if others:
                    print('Homework raw residue preserved; no automatic cleanup: '
                          + ', '.join(map(str, others)), file=sys.stderr)


def attach_homework(args, directory):
    source = source_path(args)
    attempt = Homework(directory)
    def run():
        from material_import import _CURRENT
        state = _CURRENT.get()
        if state is not None:
            if state['directory'] != attempt.directory or state.get('safe') is not None:
                raise ValueError('Homework import context already owns another attempt')
            state['safe'] = attempt
        try:
            attempt.prepare(source)
            receipt = attempt.commit()
        except BaseException as primary:
            try: attempt.close()
            except BaseException as cleanup:
                primary.add_note('Homework cleanup failure: ' + repr(cleanup))
                if isinstance(primary, registry.CommitInterrupted): primary.cleanup_errors += (cleanup,)
            raise
        else:
            try: attempt.close()
            except BaseException as cleanup:
                raise registry.CommitInterrupted(receipt, 'committed', phase='homework-close') from cleanup
            return receipt
    result = owned_io(run)
    print('Attached: homework')
    return result
