"""Replace individual files only after a complete, flushed write."""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import uuid


@contextmanager
def atomic_writer(path, mode='w', **options):
    """Write/flush/close before replacement; caller owns mkdir, encoding and locks."""
    path = Path(path)
    temporary = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        with temporary.open(mode, **options) as stream:
            yield stream
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def atomic_text(path, text):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with atomic_writer(path, encoding='utf-8', newline='\n') as stream:
        stream.write(text)


def atomic_json(path, value):
    atomic_text(path, json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False))


def atomic_bytes(path, raw):
    """Publish binary document parts without text/newline conversion."""
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    with atomic_writer(path, 'wb') as stream:stream.write(raw)


def atomic_chunks(path, chunks):
    """Publish a report incrementally; never expose a half-written destination."""
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    with atomic_writer(path, encoding='utf-8', newline='\n') as stream:
        for chunk in chunks:stream.write(chunk)
