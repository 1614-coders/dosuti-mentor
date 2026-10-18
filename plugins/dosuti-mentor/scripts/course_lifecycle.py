"""Shared pending-cleanup guard for managed courses and runtimes."""
import json
from pathlib import Path
from profile_storage import checked

MARKER = '.cleanup-pending.json'


def pending(root):
    root = Path(root)
    candidates = [root / MARKER]
    if root.name == 'run': candidates.append(root.parent / MARKER)
    for path in candidates:
        checked(path)
        if not path.exists(): continue
        if path.stat().st_size > 16000: raise ValueError('Oversized cleanup marker; preserve it')
        value = json.loads(path.read_text(encoding='utf-8'))
        if not isinstance(value, dict) or not all(isinstance(value.get(key), str) for key in ('seal', 'owner')):
            raise ValueError('Invalid pending-cleanup marker; preserve it')
        return dict(pending=True, root=str(path.parent), seal=value['seal'],
                    next_action='A legacy cleanup marker was found. Preserve this folder and choose a new empty DoSuti data folder.')
    return None


def active(root):
    if pending(root):
        raise ValueError('legacy cleanup marker found; preserve this folder and choose a new empty DoSuti data folder')
