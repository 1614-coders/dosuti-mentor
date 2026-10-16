"""Explicit vocabulary hints for local ASR, never transcript replacements."""
import json
from pathlib import Path


def recognition_prompt(prompt='', vocabulary_file=None):
    if vocabulary_file is None:
        return prompt
    path = Path(vocabulary_file)
    if path.stat().st_size > 32768:
        raise ValueError('Vocabulary file exceeds 32 KiB')
    terms = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(terms, list) or not 1 <= len(terms) <= 100:
        raise ValueError('Vocabulary must be a JSON list of 1..100 terms')
    if any(not isinstance(t, str) or not t.strip() or len(t) > 80
           or any(ord(c) < 32 for c in t) for t in terms):
        raise ValueError('Vocabulary terms must be nonempty single-line strings up to 80 characters')
    terms = list(dict.fromkeys(t.strip() for t in terms))
    hint = 'Technical vocabulary: ' + ', '.join(terms) + '.'
    if len(hint) > 4000:
        raise ValueError('Vocabulary exceeds 4000 characters')
    return '\n'.join(p for p in (prompt, hint) if p)
