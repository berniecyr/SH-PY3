"""Snapshot selection helpers: membership is by physical file path."""
import os


def pathKey(path):
    return os.path.normcase(os.path.abspath(path))


def uniquePaths(paths):
    result, seen = [], set()
    for path in paths:
        key = pathKey(path)
        if key not in seen:
            result.append(path)
            seen.add(key)
    return result


def withinSelection(matches, selection):
    keys = {pathKey(path) for path in matches}
    return [path for path in uniquePaths(selection) if pathKey(path) in keys]


def remapSelections(saved, changes):
    changes = {pathKey(old): new for old, new in changes.items()}
    return {name: uniquePaths(changes.get(pathKey(path), path) for path in paths)
            for name, paths in saved.items()}
