"""Folder registration with content-based duplicate linking, optionally analysis."""
import os
from pathlib import Path

from backEnd import UserMediaAnalysis


def excludedFolder(name):
    return name.startswith('.') or 'raw' in name.casefold()


def folderFiles(root, cancelled=lambda: False, onError=None):
    """Yield supported media recursively, pruning excluded folders and links."""
    root = os.path.abspath(root)
    if any(excludedFolder(part) for part in Path(root).parts[1:]):
        return
    for directory, folders, files in os.walk(root, onerror=onError):
        if cancelled():
            return
        folders[:] = sorted(name for name in folders
                            if not excludedFolder(name)
                            and not os.path.islink(os.path.join(directory, name))
                            and not os.path.isjunction(os.path.join(directory, name)))
        for name in sorted(files):
            if cancelled():
                return
            path = os.path.join(directory, name)
            if (not name.startswith('.') and os.path.isfile(path)
                    and name.lower().endswith(UserMediaAnalysis.kImageExts
                                              + UserMediaAnalysis.kVideoExts)):
                yield path


def availableConfig(cfg, caps):
    """Return a private configuration plus the optional detectors being omitted."""
    cfg = dict(cfg)
    missing = []
    for key, cap, label in (('RUN_FACE', 'face', 'Face'),
                            ('RUN_NUDITY', 'nudity', 'Nudity')):
        if not caps.get(cap) or not cfg.get(key):
            missing.append(label)
            cfg[key] = False
    return cfg, missing


def importFolder(root, db, lock, cfg, client=None, analyze=False,
                 cancelled=lambda: False, progress=lambda counts: None,
                 logger=None, pause=lambda: None):
    """Register first, then reuse successful analysis of identical content.

    Filenames-only never contacts a detector or clears shared results/text.
    The caller owns the connection and serializes its other access with lock.
    """
    counts = dict(registered=0, analyzed=0, reused=0, failed=0)
    signature = UserMediaAnalysis.modelSignature(cfg) if analyze else None

    def failed(exc):
        counts['failed'] += 1
        if logger:
            logger.warning('Folder import: %s' % exc)

    for path in folderFiles(root, cancelled, failed):
        try:
            with lock:
                db.registerContent(path)
                counts['registered'] += 1
                row = db.getFile(path)
                needed = analyze and (db.needsAnalysis(path, signature)
                                      or bool(row.get('error')))
            if cancelled():
                break
            if needed:
                result = UserMediaAnalysis.analyzeFile(
                    path, client, cfg, logger=logger,
                    **({'cancelFn': cancelled} if UserMediaAnalysis.isVideo(path) else {}))
                # Cancelled videos contain partial results; do not mark complete.
                if cancelled():
                    break
                with lock:
                    db.saveResult(path, result)
                if result.get('error'):
                    failed('%s: %s' % (path, result['error']))
                else:
                    counts['analyzed'] += 1
                pause()
            elif analyze:
                counts['reused'] += 1
        except Exception as exc:
            failed('%s: %s' % (path, exc))
        progress(dict(counts))
    return counts
