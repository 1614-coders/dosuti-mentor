"""One local PDF resource on Windows NTFS; raw bytes only, never extraction.

Standard local/cooperative model, using the accepted HANDLE/DACL primitives.
Unknown residue is retained; no pathname I/O, replay, retry or PDF parser.
"""
from copy import deepcopy
import hashlib
from pathlib import Path
from types import SimpleNamespace
import uuid

import course
import registry_update as registry
from registry_windows import owned_io
from storage_paths import source_kind
import windows_managed_home as n
from windows_material_import import (SingleFile, Security, BoundRegistry, MAX_BYTES,
                                    READ, READ_CONTROL, DIRECTORY_WRITE, FILE_WRITE, component)
from windows_resource_text import ResourceText, selected


def validate_pdf(payload):
    # Classification, not a claim that the PDF is syntactically valid or safe to render.
    if len(payload) > MAX_BYTES: raise ValueError('PDF exceeds 32 MiB')
    if not payload.startswith(b'%PDF-'): raise ValueError('Local PDF requires a PDF signature')


def source_path(args):
    if (args.kind != 'resources' or args.resource_type not in ('file', 'article')
            or source_kind(args.source) != 'local'
            or any(getattr(args, key, None) is not None for key in ('material_origin', 'material_text'))):
        raise ValueError('PDF intake only supports one local resource without extraction')
    path = Path(args.source).expanduser().absolute()
    component(path.name)
    if path.suffix.lower() != '.pdf': raise ValueError('PDF resource requires a .pdf filename')
    return path


def registered_pdf(item):
    origin = item.get('origin') if isinstance(item, dict) else None
    return (isinstance(origin, dict) and origin.get('media_type') == 'application/pdf'
            and item.get('kind') in ('file', 'article'))


class PDFIntake(ResourceText):
    def __init__(self, directory):
        super().__init__(directory, None, {})

    def prepare(self, source):
        self.lesson = self.pin(self.directory)
        self.api, self.security = self.lesson.api, Security(self.lesson.api)
        self.lesson_id = self.lesson.inspect(self.lesson.home, True)[0]
        initial = self.read_registry()
        self.expected_revision = registry.revision(initial)
        incoming = self.pin(source.parent)
        handle = self.open(incoming, incoming.home, source.name, False, n.READ_ACCESS)
        before = self.stamp(handle)
        if before[0][1] > MAX_BYTES: raise ValueError('PDF exceeds 32 MiB')
        payload = self.read(handle)
        if len(payload) != before[0][1] or self.stamp(handle) != before:
            raise ValueError('PDF source changed during read')
        incoming.check()
        validate_pdf(payload)
        self.retire(incoming, handle)

        self.category_name = 'resources'
        parent = self.lesson.own(self.api.open(self.lesson.home, self.category_name, directory=True, create=True))
        self.category, self.category_id = parent, self.lesson.inspect(parent, True)[0]
        self.snapshot_name = uuid.uuid4().hex[:12]
        self.folder = self.directory / self.category_name / self.snapshot_name
        snapshot = self.open(self.lesson, parent, self.snapshot_name, True, DIRECTORY_WRITE, create=True)
        self.residue = self.folder
        self.snapshot_id = self.lesson.inspect(snapshot, True)[0]
        self.name = source.name
        output = self.open(self.lesson, snapshot, self.name, False, FILE_WRITE, create=True)
        self.file_id = self.lesson.inspect(output, False)[0]
        self.api.write(output, payload)
        self.api.flush(output)
        self.size, self.checksum = len(payload), hashlib.sha256(payload).hexdigest()
        if self.read(output) != payload: raise OSError('PDF readback mismatch')
        self.security.seal(output, False)
        self.retire(self.lesson, output)
        self.denied(snapshot, self.name, False)
        self.file = self.open(self.lesson, snapshot, self.name, False, READ)
        self.security.seal(snapshot, True)
        self.retire(self.lesson, snapshot)
        self.denied(parent, self.snapshot_name, True)
        self.snapshot = self.open(self.lesson, parent, self.snapshot_name, True, n.DIR_ACCESS | READ_CONTROL)
        self.verify()

    def verify(self):
        payload = SingleFile.verify(self)
        validate_pdf(payload)
        return payload

    def commit(self, args, source):
        if self.writers: raise ValueError('Writable PDF HANDLE still owned before registry commit')
        target = str(self.folder / self.name)
        self.item = dict(id=uuid.uuid4().hex, source=target, kind=args.resource_type, status='downloaded',
            origin=dict(source=str(source), raw_file=target, sha256=self.checksum,
                        size=self.size, media_type='application/pdf'))
        def change(data):
            self.verify()
            if (not isinstance(data.get('id'), str)
                    or data['id'].casefold() != self.directory.name.casefold()):
                raise ValueError('PDF lesson registry identity changed')
            course.publish_attachment(data, args, None, self.item)
        self.receipt = registry.update(self.directory, change, expected_revision=self.expected_revision,
                                      registry=BoundRegistry(self.directory, self.lesson_id))
        try:
            self.verify()
            if selected(self.read_registry(), self.item['id']) != self.item:
                raise ValueError('PDF resource changed after confirmed commit')
        except BaseException as error:
            raise registry.CommitInterrupted(self.receipt, 'committed', phase='pdf-verification') from error
        return self.receipt


class PDFReader(ResourceText):
    def prepare_pdf(self):
        self.lesson = self.pin(self.directory)
        self.api, self.security = self.lesson.api, Security(self.lesson.api)
        self.lesson_id = self.lesson.inspect(self.lesson.home, True)[0]
        data = self.read_registry()
        if selected(data, self.original['id']) != self.original:
            raise ValueError('PDF resource changed concurrently')
        origin = self.original['origin']
        if (self.original['source'] != origin.get('raw_file')
                or source_kind(origin.get('source')) != 'local'
                or type(origin.get('size')) is not int or origin['size'] < 0):
            raise ValueError('Invalid registered PDF origin')
        value = self.checked_input(origin['raw_file'], origin.get('sha256'))
        self.payload = self.verify_input(value)
        if len(self.payload) != origin['size']: raise ValueError('PDF snapshot size changed')
        validate_pdf(self.payload)
        latest = self.read_registry()
        if selected(latest, self.original['id']) != self.original:
            raise ValueError('PDF resource changed concurrently')
        # No new snapshot and no registry write/replay for an existing raw resource.
        self.result = deepcopy(latest['resources'])
        for row in self.result['items']:
            if row['id'] == self.original['id']: row['source_integrity'] = 'verified'


def execute(attempt, function):
    def run():
        from material_import import _CURRENT
        state = _CURRENT.get()
        if state is not None:
            if state['directory'] != attempt.directory or state.get('safe') is not None:
                raise ValueError('PDF operation already owns another import attempt')
            state['safe'] = attempt
        try:
            result = function()
        except BaseException as primary:
            try: attempt.close()
            except BaseException as cleanup:
                primary.add_note('PDF cleanup failure: ' + repr(cleanup))
                if isinstance(primary, registry.CommitInterrupted): primary.cleanup_errors += (cleanup,)
            raise
        else:
            try: attempt.close()
            except BaseException as cleanup:
                if attempt.receipt is not None:
                    raise registry.CommitInterrupted(attempt.receipt, 'committed', phase='pdf-close') from cleanup
                raise
            return result
    return owned_io(run)


def attach_pdf(args, directory):
    source = source_path(args)
    attempt = PDFIntake(directory)
    def ingest():
        attempt.prepare(source)
        return attempt.commit(args, source)
    result = execute(attempt, ingest)
    print('Attached: resources')
    return result


def fetch_pdf(args, directory, original):
    if args.action != 'fetch' or not registered_pdf(original):
        raise ValueError('Only raw PDF fetch is supported; extraction is outside PDF intake')
    attempt = PDFReader(directory, SimpleNamespace(resource_id=original['id']), original)
    execute(attempt, attempt.prepare_pdf)
    return attempt.result
