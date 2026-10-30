"""Relocation anchors for managed course files; external paths stay external."""
from copy import deepcopy
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
from urllib.parse import urlsplit


def source_kind(source):
    if not isinstance(source, str) or not source.strip():
        raise ValueError('Source must be a nonempty path or URL')
    # urlsplit interprets a Windows drive letter as a URL scheme.
    if PureWindowsPath(source).drive:
        return 'local'
    parsed = urlsplit(source)
    if parsed.scheme.lower() in ('https', 'http') and parsed.hostname:
        return 'remote'
    if parsed.scheme:
        raise ValueError('Use an HTTP(S) URL or a local file path')
    return 'local'


def local_path(source):
    if os.name != 'nt' and PureWindowsPath(str(source)).drive:
        raise ValueError('Windows path requires Windows; provide this computer\'s local path')
    return Path(source).expanduser().resolve()


def path_fields(data):
    yield ('video', 'path')
    if source_kind(data['video']['source']) == 'local':
        yield ('video', 'source')
    for kind, field in [('code', 'snapshot'), ('homework', 'file')]:
        yield (kind, field)
        yield (kind, 'origin', 'raw_file')
        for index, _ in enumerate(data.get(kind, {}).get('history', [])):
            yield (kind, 'history', index, field)
            yield (kind, 'history', index, 'origin', 'raw_file')
    for index, item in enumerate(data.get('resources', {}).get('items', [])):
        if source_kind(item['source']) == 'local':
            yield ('resources', 'items', index, 'source')
        yield ('resources', 'items', index, 'origin', 'raw_file')
        yield ('resources', 'items', index, 'extracted_file')
        yield ('resources', 'items', index, 'visuals', 'folder')
        visual=item.get('visuals')
        if isinstance(visual,dict) and isinstance(visual.get('images'),list) and len(visual['images'])<=24:
            for image_index,_ in enumerate(visual['images']):
                yield ('resources','items',index,'visuals','images',image_index,'path')
    for name, code in data.get('code_repositories', {}).items():
        yield ('code_repositories', name, 'snapshot')
        yield ('code_repositories', name, 'origin', 'raw_file')
        for index, _ in enumerate(code.get('history', [])):
            yield ('code_repositories', name, 'history', index, 'snapshot')
            yield ('code_repositories', name, 'history', index, 'origin', 'raw_file')
    for kind in ['prepared_fragments', 'retired_fragments', 'screen_scans', 'retired_screen_scans']:
        for index, _ in enumerate(data.get(kind, [])):
            yield (kind, index, 'folder')


def parent_value(data, keys):
    try:
        for key in keys[:-1]:
            data = data[key]
        return data, data[keys[-1]]
    except (KeyError, IndexError, TypeError):
        return None, None


def anchored(data, lesson_file):
    data = deepcopy(data)
    base = Path(lesson_file).resolve().parents[2]
    anchors = []
    for keys in path_fields(data):
        _, value = parent_value(data, keys)
        if not isinstance(value, str) or not Path(value).is_absolute():
            continue
        path = Path(value).resolve()
        if path.is_relative_to(base):
            anchors.append(dict(keys=list(keys), relative=path.relative_to(base).as_posix()))
    data['storage_paths'] = {'version': 1, 'anchors': anchors}
    return data


def resolved(data, lesson_file):
    data = deepcopy(data)
    info = data.get('storage_paths')
    if info is None:
        return data
    if not isinstance(info, dict) or info.get('version') != 1 or not isinstance(info.get('anchors'), list):
        raise ValueError('Invalid course path anchors')
    base = Path(lesson_file).resolve().parents[2]
    allowed = set(path_fields(data))
    seen = set()
    for item in info['anchors']:
        if not isinstance(item, dict) or not isinstance(item.get('keys'), list):
            raise ValueError('Invalid course path anchor')
        if any(type(key) not in (str, int) for key in item['keys']):
            raise ValueError('Invalid managed path key')
        keys = tuple(item['keys'])
        relative = item.get('relative')
        if keys not in allowed or keys in seen or not isinstance(relative, str):
            raise ValueError('Invalid or duplicate managed path field')
        seen.add(keys)
        posix = PurePosixPath(relative)
        if not relative or posix.is_absolute() or '..' in posix.parts or '\\' in relative or PureWindowsPath(relative).drive:
            raise ValueError('Managed path must stay within the course')
        path = base.joinpath(*posix.parts).resolve()
        if not path.is_relative_to(base):
            raise ValueError('Managed path escapes the course')
        parent, value = parent_value(data, keys)
        if parent is None or not isinstance(value, str):
            raise ValueError('Managed path field is missing')
        parent[keys[-1]] = str(path)
    return data
