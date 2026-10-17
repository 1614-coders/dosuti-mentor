"""Read-only part inventory for one document; unreferenced does not mean unused."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import sys

import document_store as ds
from profile_storage import checked

NAME = re.compile(r'[0-9a-f]{64}\.part\Z')


def manifest_bytes(path):
    checked(path)
    with path.open('rb') as stream:
        raw = stream.read(ds.INLINE_BYTES + 1)
    if len(raw) > ds.INLINE_BYTES:
        raise ValueError('Document manifest exceeds inline limit')
    return raw


def references(value, kind):
    if not isinstance(value, dict):
        raise ValueError('Document must be a JSON object')
    if value.get('schema') != ds.SCHEMA:
        return None
    if (set(value) != {'schema', 'kind', 'bytes', 'sha256', 'parts'}
            or value['kind'] != kind or type(value['bytes']) is not int
            or not 0 < value['bytes'] <= ds.MAX_BYTES
            or not isinstance(value['parts'], list) or not 1 <= len(value['parts']) <= 256):
        raise ValueError('Invalid document manifest')
    from source_integrity import digest
    digest(value['sha256'])
    for item in value['parts']:
        if (not isinstance(item, dict) or set(item) != {'sha256', 'bytes'}
                or type(item['bytes']) is not int or not 0 < item['bytes'] <= ds.PART_BYTES):
            raise ValueError('Invalid document part')
        digest(item['sha256'])
    if sum(item['bytes'] for item in value['parts']) != value['bytes']:
        raise ValueError('Document size changed')
    return value['parts']


def inventory(path, kind, max_entries=4096):
    if type(max_entries) is not int or not 1 <= max_entries <= 100000:
        raise ValueError('max-entries must be between 1 and 100000')
    path = Path(path).absolute()
    # No resolution through symlink ancestors. No file/lock is created by a report.
    for parent in reversed(path.parents):
        checked(parent, True)
    raw = manifest_bytes(path)
    value = json.loads(raw)
    parts = references(value, kind)
    names = {item['sha256'] + '.part' for item in (parts or [])}
    directory = ds.folder(path)
    checked(directory, True)
    before = directory.stat() if directory.exists() else None
    checksum = hashlib.sha256()
    # Verify each ordered part, not just unique filenames; repeated parts count
    # multiple times in the logical document but once in disk totals below.
    for item in parts or []:
        p = directory / (item['sha256'] + '.part')
        checked(p)
        with p.open('rb') as stream:
            data = stream.read(ds.PART_BYTES + 1)
        if len(data) != item['bytes'] or hashlib.sha256(data).hexdigest() != item['sha256']:
            raise ValueError('Document part checksum or size changed')
        checksum.update(data)
    if parts is not None and checksum.hexdigest() != value['sha256']:
        raise ValueError('Document checksum changed')
    counts = dict(referenced_files=0, referenced_bytes=0,
                  unreferenced_files=0, unreferenced_bytes=0, unknown_entries=0)
    rows = []
    visited = 0
    complete = True
    if before is not None:
        with os.scandir(directory) as entries:
            for entry in entries:
                if visited >= max_entries:
                    complete = False
                    break
                visited += 1
                try:
                    checked(Path(entry.path))
                    info = entry.stat(follow_symlinks=False)
                    if not NAME.fullmatch(entry.name) or not 0 < info.st_size <= ds.PART_BYTES:
                        raise ValueError('Unknown entry')
                except (ValueError, OSError):
                    counts['unknown_entries'] += 1
                    continue
                category = 'referenced' if entry.name in names else 'unreferenced'
                counts[category + '_files'] += 1
                counts[category + '_bytes'] += info.st_size
                if category == 'unreferenced' and len(rows) < 100:
                    rows.append(dict(name=entry.name, bytes=info.st_size))
    checked(directory, True)
    after = directory.stat() if directory.exists() else None
    signature = lambda s: None if s is None else (s.st_dev, s.st_ino, s.st_mtime_ns, s.st_ctime_ns)
    stable = manifest_bytes(path) == raw and signature(before) == signature(after)
    return dict(schema='document_report_v1', read_only=True,
                format='parts' if parts is not None else 'inline', kind=kind,
                manifest_sha256=hashlib.sha256(raw).hexdigest(),
                current_bytes_verified=parts is not None,
                logical_json_verified=False, observed_unchanged=stable,
                scan_complete=complete, entries_visited=visited, totals=counts,
                unreferenced_sample=rows,
                sample_truncated=counts['unreferenced_files'] > len(rows),
                reclaimable_bytes=None, deletion_supported=False,
                limitations=[
                    'Unreferenced means absent from this manifest, not safe to delete: older readers may still need these parts.',
                    'Current part bytes and aggregate hash are verified; logical JSON/schema and unreferenced content hashes are not checked.',
                    'No atomic snapshot or protection against hostile concurrent path replacement; unchanged observations do not prove absence of activity.',
                    'Totals cover visited ordinary single-link part files only; unknown entries are excluded.'])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--document', type=Path, required=True)
    parser.add_argument('--kind', required=True)
    parser.add_argument('--max-entries', type=int, default=4096)
    args = parser.parse_args()
    try:
        print(json.dumps(inventory(args.document, args.kind, args.max_entries), ensure_ascii=False, indent=2))
    except (ValueError, OSError, RecursionError) as error:
        print('Error:', error, file=sys.stderr)
        sys.exit(1)


if __name__ == '__main__':
    main()
