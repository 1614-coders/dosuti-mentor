"""Validate profile storage before SQLite writes; reject links and detectable path swaps."""
import os
from pathlib import Path
import sqlite3
import stat


def checked(path, directory=False):
    try:s=path.lstat()
    except FileNotFoundError:return None
    if stat.S_ISLNK(s.st_mode) or (directory and not stat.S_ISDIR(s.st_mode)) or (not directory and not stat.S_ISREG(s.st_mode)):
        raise ValueError('Profile storage must contain ordinary directories and files, not links')
    if not directory and s.st_nlink!=1:
        raise ValueError('Profile database hard links are not supported')
    return (s.st_dev,s.st_ino)


def profile_path(root, profile):
    from course import slug
    path=Path(root)/'learners'/slug(profile)
    checked(path.parent,True);checked(path,True)
    if not path.resolve().is_relative_to(Path(root).resolve()):raise ValueError('Profile escapes course')
    return path


def open_progress(root, profile):
    folder=profile_path(root,profile)
    # One component at a time; no writes through an already-present parent link.
    for directory in (folder.parent,folder):
        try:directory.mkdir()
        except FileExistsError:pass
        checked(directory,True)
    parents=[(p,checked(p,True)) for p in (folder.parent,folder)]
    path=folder/'progress.sqlite3'
    for p in [path]+[Path(str(path)+suffix) for suffix in ('-journal','-wal','-shm')]:checked(p)
    flags=os.O_RDWR|os.O_CREAT|getattr(os,'O_NOFOLLOW',0)
    fd=os.open(path,flags,0o600)
    connection=None
    try:
        identity=(os.fstat(fd).st_dev,os.fstat(fd).st_ino)
        if checked(path)!=identity or any(checked(p,True)!=i for p,i in parents):raise ValueError('Profile storage changed while opening')
        connection=sqlite3.connect(path.as_uri()+'?mode=rw',uri=True,timeout=30)
        if checked(path)!=identity or any(checked(p,True)!=i for p,i in parents):raise ValueError('Profile storage changed while opening')
        for suffix in ('-journal','-wal','-shm'):checked(Path(str(path)+suffix))
        return connection
    except BaseException:
        if connection is not None:connection.close()
        raise
    finally:os.close(fd)
