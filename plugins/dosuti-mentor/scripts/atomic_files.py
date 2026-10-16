"""Replace individual files only after a complete, flushed write."""
import json
import os
from pathlib import Path
import uuid


def atomic_text(path, text):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        with temporary.open('w', encoding='utf-8') as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def atomic_json(path, value):
    atomic_text(path, json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False))


def atomic_bytes(path, raw):
    """Publish binary document parts without text/newline conversion."""
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    temporary=path.with_name(path.name+'.'+uuid.uuid4().hex+'.tmp')
    try:
        with temporary.open('wb') as stream:
            stream.write(raw);stream.flush();os.fsync(stream.fileno())
        temporary.replace(path)
    finally:temporary.unlink(missing_ok=True)
