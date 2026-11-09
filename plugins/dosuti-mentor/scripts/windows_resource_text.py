"""UTF-8 extraction/review of a registered local NTFS resource; no acquisition.

Reuse the accepted single-file HANDLE/DACL primitives. Registry, source bytes,
unknown leftovers and the single-file implementation are never rewritten here.
"""
from copy import deepcopy
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path, PureWindowsPath
import re
import uuid

import course
import registry_update as registry
from registry_windows import owned_io
from storage_paths import source_kind
import windows_managed_home as n
from windows_material_import import (SingleFile, Security, BoundRegistry, READ,
                                    DIRECTORY_WRITE, FILE_WRITE, READ_CONTROL)


def eligible(item, args):
    if not isinstance(item, dict) or item.get('kind') == 'video': return False
    if args.action not in ('extract', 'review') or getattr(args, 'visual_pages', None) is not None:
        return False
    origin = item.get('origin')
    if not isinstance(origin, dict) or not isinstance(origin.get('raw_file'), str): return False
    try:
        if source_kind(item.get('source')) != 'local' or source_kind(origin.get('source')) != 'local': return False
    except ValueError: return False
    if any(PureWindowsPath(value).suffix.lower() in ('.pdf', '.html', '.htm', '.zip')
           for value in (item['source'], origin['raw_file'], origin['source'])): return False
    media = origin.get('media_type', '')
    return (isinstance(media, str) and media.split(';')[0].strip().lower() in ('', 'text/plain')
            and item.get('extraction') in (None, 'utf8_text'))


def utf8_text(raw, maximum):
    if len(raw) > maximum: raise ValueError('Text material exceeds import size limit')
    if raw.startswith((b'%PDF', b'PK\x03\x04')) or b'\0' in raw:
        raise ValueError('Resource is not plain UTF-8 text')
    try: value = raw.decode('utf-8-sig')
    except UnicodeDecodeError as error: raise ValueError('Resource is not UTF-8 text') from error
    if value.lstrip().lower().startswith(('<!doctype html', '<html')):
        raise ValueError('HTML extraction is outside the Windows text resource contract')
    if not value.strip(): raise ValueError('No readable text extracted')
    result = (value.removesuffix('\n').removesuffix('\r') + '\n').encode('utf-8')
    if len(result) > maximum: raise ValueError('Extracted text exceeds import size limit')
    return result


def selected(data, identity):
    resources = data.get('resources')
    if not isinstance(resources, dict) or not isinstance(resources.get('items'), list):
        raise ValueError('Invalid resource registry')
    return next((row for row in resources['items'] if isinstance(row, dict) and row.get('id') == identity), None)


def compare_and_update(data, identity, original, proposed):
    row = selected(data, identity)
    if row != original: raise ValueError('Resource changed concurrently; read its current state')
    row.clear(); row.update(deepcopy(proposed))


@dataclass
class CheckedInput:
    path: Path
    session: object
    directory: int
    handle: int
    directory_id: tuple
    file_id: tuple
    stamp: tuple
    sha256: str


class ResourceText(SingleFile):
    def __init__(self, directory, args, original):
        super().__init__(directory)
        self.args, self.original = args, deepcopy(original)
        self.item = deepcopy(original)
        self.inputs = []
        self.output_ready = False

    def read_registry(self):
        handle = self.open(self.lesson, self.lesson.home, 'lesson.json', False, n.READ_ACCESS)
        try: data = json.loads(self.read(handle).decode('utf-8-sig'))
        finally: self.retire(self.lesson, handle)
        if (not isinstance(data, dict) or not isinstance(data.get('id'), str)
                or data['id'].casefold() != self.directory.name.casefold()):
            raise ValueError('Resource lesson identity changed')
        self.lesson.check()
        return course.resolved(data, self.directory / 'lesson.json')

    def checked_input(self, value, checksum):
        if not isinstance(value, str) or not isinstance(checksum, str) or not re.fullmatch('[0-9a-f]{64}', checksum):
            raise ValueError('Invalid resource snapshot path or SHA256')
        path = Path(value)
        if not path.is_absolute() or not path.is_relative_to(self.directory):
            raise ValueError('Resource snapshot must stay inside its lesson')
        parts = path.relative_to(self.directory).parts
        if len(parts) != 3 or parts[0] not in ('resources', 'materials') or '..' in parts:
            raise ValueError('Unsupported resource snapshot layout')
        session = self.pin(path.parent)
        directory = self.open(session, session.parent, path.parent.name, True, n.DIR_ACCESS | READ_CONTROL)
        handle = self.open(session, directory, path.name, False, READ)
        value = CheckedInput(path, session, directory, handle, session.inspect(directory, True)[0],
                             session.inspect(handle, False)[0], self.stamp(handle), checksum)
        self.inputs.append(value)
        self.verify_input(value)
        return value

    def verify_input(self, value):
        value.session.check()
        if value.session.inspect(value.directory, True)[0] != value.directory_id:
            raise ValueError('Resource snapshot directory identity changed')
        current = self.open(value.session, value.directory, value.path.name, False, n.READ_ACCESS)
        try:
            if value.session.inspect(current, False)[0] != value.file_id or self.stamp(value.handle) != value.stamp:
                raise ValueError('Resource snapshot identity changed')
        finally: self.retire(value.session, current)
        self.security.verify_seal(value.directory, True)
        self.security.verify_seal(value.handle, False)
        if self.api.entries(value.directory) != {value.path.name}:
            raise ValueError('Resource snapshot inventory changed')
        raw = self.read(value.handle)
        if len(raw) != value.stamp[0][1] or hashlib.sha256(raw).hexdigest() != value.sha256:
            raise ValueError('Resource snapshot SHA256 changed')
        if self.stamp(value.handle) != value.stamp: raise ValueError('Resource snapshot changed during read')
        return raw

    def write_text(self, payload):
        # Allocation/seal sequence is the accepted SingleFile protocol, without
        # changing its prepare() contract or creating/reopening a pathname temp.
        self.category_name = 'materials'
        self.category = self.lesson.own(self.api.open(self.lesson.home, self.category_name, directory=True, create=True))
        self.category_id = self.lesson.inspect(self.category, True)[0]
        self.snapshot_name = uuid.uuid4().hex[:12]
        self.folder = self.directory / self.category_name / self.snapshot_name
        snapshot = self.open(self.lesson, self.category, self.snapshot_name, True, DIRECTORY_WRITE, create=True)
        self.residue = self.folder
        self.snapshot_id = self.lesson.inspect(snapshot, True)[0]
        self.name = 'text-' + uuid.uuid4().hex + '.txt'
        handle = self.open(self.lesson, snapshot, self.name, False, FILE_WRITE, create=True)
        self.file_id = self.lesson.inspect(handle, False)[0]
        self.api.write(handle, payload); self.api.flush(handle)
        self.size, self.checksum = len(payload), hashlib.sha256(payload).hexdigest()
        if self.read(handle) != payload: raise OSError('Resource text readback mismatch')
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

    def verify(self):
        self.lesson.check()
        for value in self.inputs: self.verify_input(value)
        if self.output_ready: return super().verify()

    def prepare_resource(self):
        from material_sources import MAX_BYTES, resource_revision
        self.lesson = self.pin(self.directory)
        self.api, self.security = self.lesson.api, Security(self.lesson.api)
        self.lesson_id = self.lesson.inspect(self.lesson.home, True)[0]
        if selected(self.read_registry(), self.args.resource_id) != self.original:
            raise ValueError('Resource changed concurrently; read its current state')
        if Path(self.item['source']) != Path(self.item['origin']['raw_file']):
            raise ValueError('Local resource snapshot binding changed')
        source = self.checked_input(self.item['origin']['raw_file'], self.item['origin']['sha256'])
        if self.item.get('extracted_file'):
            text = self.checked_input(self.item['extracted_file'], self.item.get('text_sha256'))
            if text.directory_id == source.directory_id or text.file_id == source.file_id:
                raise ValueError('Extracted text must have its own snapshot')
        elif self.args.action == 'review':
            raise ValueError('Extract and read the resource text before review')
        else:
            payload = utf8_text(self.verify_input(source), MAX_BYTES)
            target = self.write_text(payload)
            self.item.update(extracted_file=target, text_sha256=self.checksum,
                             extraction='utf8_text', status='extracted_not_reviewed')
        if self.args.action == 'review':
            if self.args.expected_revision != resource_revision(self.item):
                raise ValueError('Resource revision changed; read the current extracted text')
            if not isinstance(self.args.notes, str) or not self.args.notes.strip() or len(self.args.notes) > 4000:
                raise ValueError('Review requires notes within 4000 characters')
            self.item.update(status='reviewed', review=dict(revision=self.args.expected_revision,
                notes=self.args.notes, updated=course.now(), kind='assistant_recorded_review'))
        else: self.item['revision'] = resource_revision(self.item)
        self.verify()

    def described(self, resources):
        from material_sources import describe, resource_revision
        result = deepcopy(resources)
        for index, row in enumerate(result['items']):
            if row['id'] != self.args.resource_id:
                # Preserve the independent, existing UI treatment of other rows.
                result['items'][index] = describe(dict(items=[row]))['items'][0]
                continue
            row.update(source_integrity='verified', text_integrity='verified')
            if row.get('review'):
                row['review_valid'] = row['review'].get('revision') == resource_revision(row)
        return result

    def commit(self):
        if self.writers: raise ValueError('Writable resource HANDLE still owned before registration')
        def change(data):
            self.verify()
            compare_and_update(data, self.args.resource_id, self.original, self.item)
        self.receipt = registry.update(self.directory, change,
            registry=BoundRegistry(self.directory, self.lesson_id))
        try:
            self.verify()
            data = self.read_registry()
            if selected(data, self.args.resource_id) != self.item:
                raise ValueError('Resource changed after confirmed commit')
            # Selected-row integrity comes from held HANDLE proofs, never a
            # pathname rehash fallback. Other rows keep their existing UI policy.
            self.result = self.described(data['resources'])
            self.verify()
        except BaseException as error:
            raise registry.CommitInterrupted(self.receipt, 'committed', phase='resource-verification') from error
        return self.receipt


def resource_text(args, directory, original):
    if not eligible(original, args): raise ValueError('Windows extraction requires a registered local UTF-8 resource')
    attempt = ResourceText(directory, args, original)
    def execute():
        from material_import import _CURRENT
        state = _CURRENT.get()
        if state is not None:
            if state['directory'] != attempt.directory or state.get('safe') is not None:
                raise ValueError('Resource operation already owns another material attempt')
            state['safe'] = attempt
        try:
            attempt.prepare_resource()
            result = attempt.commit()
        except BaseException as primary:
            try: attempt.close()
            except BaseException as cleanup:
                primary.add_note('Resource cleanup failure: ' + repr(cleanup))
                if isinstance(primary, registry.CommitInterrupted): primary.cleanup_errors += (cleanup,)
            raise
        else:
            try: attempt.close()
            except BaseException as cleanup:
                raise registry.CommitInterrupted(result, 'committed', phase='resource-close') from cleanup
            return result
    owned_io(execute)
    return attempt.result
