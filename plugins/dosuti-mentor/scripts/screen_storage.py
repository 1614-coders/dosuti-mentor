"""Small adapter over the existing owned-FD importer for screen output."""
from contextlib import contextmanager
import json
from pathlib import Path
from safe_material_import import Attempt


class ScreenStorage:
    def __init__(self, output):
        self.owner = Attempt(output, lesson=False, readonly=False, create_parents=True)
        self.folders = {output: self.owner.fd}

    def start(self, target):
        self.owner.paths.append(target)
        parent = self.owner.guard.pin(target.parent)
        self.folders[target] = self.owner.fresh_at(parent, target.name)

    def folder(self, parent, name):
        self.owner.guard.check()
        path = parent / name
        self.folders[path] = self.owner.fresh_at(self.folders[parent], name)
        return path

    @contextmanager
    def output(self, folder, name):
        self.owner.guard.check()
        with self.owner.output(self.folders[folder], name, folder / name) as stream:
            yield stream
        self.owner.guard.check()

    def blob(self, folder, name, content):
        with self.output(folder, name) as stream: stream.write(content)

    def input(self, path):
        return self.owner.input(path)

    def checksum(self, path):
        return self.owner.files[Path(path).absolute()][3]

    def metadata(self, folder, name, value):
        with self.output(folder, name) as stream:
            stream.write(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False).encode('utf-8'))

    def commit(self, folder, key):
        self.owner.check()
        self.metadata(folder, 'completion.json', dict(key=key, receipt_sha256=self.checksum(folder / 'screen.json')))
        self.owner.check()
        self.owner.published = True

    def close(self):
        # Never recursively remove a name that may now belong to another writer.
        self.owner.close()
