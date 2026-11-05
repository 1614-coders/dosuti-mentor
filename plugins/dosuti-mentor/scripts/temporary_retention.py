"""Fail-closed cleanup for temporary trees written by external tools.

Even a pinned directory cannot prove ownership of every entry subsequently
created/replaced by another process. Never unlink, rename or traverse such a
tree during cleanup. This policy needs no platform-specific destructive fallback.
"""
import sys


def preserve_temporary(path):
    # The name is diagnostic only: it may now identify a different directory.
    # Do not stat/resolve it, enumerate it, or use it as a deletion target.
    print('Temporary files may remain at ' + str(path) +
          '; preserved without automatic deletion because ownership cannot be guaranteed. '
          'The directory or its parents may have moved or been replaced.', file=sys.stderr)
