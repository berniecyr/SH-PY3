"""Deterministic ordering shared by folder browsing and saved-media searches."""
import os

SORT_LABELS = ['Name A-Z', 'Name Z-A', 'Modified date: newest first',
               'Modified date: oldest first']


def sortPaths(paths, order=0):
    def nameKey(path):
        return (os.path.basename(path).casefold(), path.casefold(), path)

    paths = sorted(paths, key=nameKey, reverse=order == 1)
    if order in (2, 3):
        def dateKey(path):
            try:
                stamp = os.stat(path).st_mtime_ns
                return (False, -stamp if order == 2 else stamp)
            except OSError:
                return (True, 0)  # vanished/unreadable files sort last
        paths.sort(key=dateKey)
    return paths
