"""Additional named teacher repositories, independent of the legacy primary code."""
from copy import deepcopy
from source_integrity import verify_snapshot


def repository_id(value):
    import course
    course.slug(value)
    if value == 'default':
        raise ValueError('default is reserved for the legacy primary repository; omit --repository')
    return value


def current(data):
    return {'default': data['code'], **data.get('code_repositories', {})}


def resolve(data, name='default'):
    if name != 'default':
        repository_id(name)
    code = current(data).get(name)
    if not isinstance(code, dict) or code.get('status') != 'provided' or not code.get('snapshot'):
        raise ValueError('Missing code snapshot for repository: ' + str(name))
    verify_snapshot(code)
    return code


def references(value, data, stored=False):
    """Bind explicit topic references to current snapshots; never infer a repository."""
    from source_integrity import digest
    from learning_tools import strings
    if not isinstance(value, list) or not 1 <= len(value) <= 50:
        raise ValueError('code_references requires 1..50 repositories')
    result = []; seen = set()
    for row in value:
        fields = {'repository', 'files'} | ({'code_sha256'} if stored else set())
        if not isinstance(row, dict) or set(row) != fields:
            raise ValueError('Invalid code reference fields')
        name = row['repository']
        code = resolve(data, name)
        if name in seen:
            raise ValueError('Duplicate code reference repository')
        seen.add(name)
        strings(row['files'], 'code reference files')
        if not row['files'] or len(set(row['files'])) != len(row['files']):
            raise ValueError('Code reference files must be nonempty and unique')
        if any(path not in code['inventory'] for path in row['files']):
            raise ValueError('Code reference contains an unknown snapshot file')
        if stored:
            digest(row['code_sha256'])
            if row['code_sha256'] != code['content_sha256']:
                raise ValueError('Code reference snapshot changed')
        result.append(dict(repository=name, files=row['files'], code_sha256=code['content_sha256']))
    return result


def describe(data):
    result = {}
    for name, code in current(data).items():
        if name != 'default':
            repository_id(name)
        verify_snapshot(code)
        result[name] = deepcopy(code)
    return result


def snapshots(data):
    for code in current(data).values():
        yield code
        yield from code.get('history', [])
