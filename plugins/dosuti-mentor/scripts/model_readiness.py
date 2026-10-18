"""Check all model files; a successful check does not prove inference quality."""
import hashlib
import json
from pathlib import Path
import re
from source_integrity import file_hash
from storage_lock import file_lock

FILES = ('model.bin', 'config.json', 'tokenizer.json', 'vocabulary.txt')


class ModelNotReady(ValueError):
    pass


def model_lock(folder):
    folder = Path(folder).resolve()
    return file_lock(folder.parent / ('.' + folder.name + '.model.lock'))


def inspect_model(folder):
    folder = Path(folder)
    try:
        inventory = {}
        for name in FILES:
            path = folder / name
            if not path.is_file() or path.stat().st_size == 0:
                raise ModelNotReady(f'Model file missing or empty: {name}')
            inventory[name] = dict(sha256=file_hash(path), size=path.stat().st_size)
        config = json.loads((folder / 'config.json').read_text(encoding='utf-8'))
        tokenizer = json.loads((folder / 'tokenizer.json').read_text(encoding='utf-8'))
        if not isinstance(config, dict) or not isinstance(config.get('lang_ids'), list) or not config['lang_ids']:
            raise ModelNotReady('Invalid model config.json')
        if not isinstance(tokenizer, dict) or not isinstance(tokenizer.get('model'), dict) or not tokenizer['model'].get('vocab'):
            raise ModelNotReady('Invalid model tokenizer.json')
        (folder / 'vocabulary.txt').read_text(encoding='utf-8')
        marker = folder / 'model-ready.json'
        saved = None
        if marker.exists():
            saved = json.loads(marker.read_text(encoding='utf-8'))
            if saved.get('files') != inventory:
                raise ModelNotReady('Model inventory changed')
        journal = folder / '.model-installing.json'
        if journal.exists():
            pending = json.loads(journal.read_text(encoding='utf-8'))
            if saved is None or pending.get('files') != inventory or saved.get('fingerprint') != pending.get('fingerprint'):
                raise ModelNotReady('Model installation is incomplete; rerun downloader')
        provenance = folder / 'download.json'
        origin = json.loads(provenance.read_text(encoding='utf-8')) if provenance.exists() else {}
        if origin.get('sha256') and (origin['sha256'] != inventory['model.bin']['sha256'] or
                                    origin.get('size') != inventory['model.bin']['size']):
            raise ModelNotReady('Model weights fail the saved download checksum')
        fingerprint = hashlib.sha256(json.dumps(inventory, sort_keys=True).encode()).hexdigest()
        return dict(files=inventory, fingerprint=fingerprint, origin=origin)
    except (OSError, ValueError, TypeError, AttributeError) as error:
        if isinstance(error, ModelNotReady):
            raise
        raise ModelNotReady(f'Model files are unreadable or invalid: {error}') from error


def checked_model(folder):
    with model_lock(folder):
        return inspect_model(folder)


def repair_type(folder, fallback):
    try:
        origin = json.loads((Path(folder) / 'download.json').read_text(encoding='utf-8')).get('repository', '')
        match = re.search(r'faster-whisper-(tiny|base|small|medium|large-v3)/', origin)
        return match.group(1) if match else fallback
    except (OSError, ValueError, AttributeError):
        return fallback
