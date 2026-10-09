#! /usr/local/bin/python

"""
## @file
File work behind the Images tab's right-click actions on a selection.

Everything here works on COPIES in a new folder under the system temp
folder; the user's originals are only ever read.  No wx here, so the copy
rules can be tested on their own.
"""

# Python imports...
import os
import re
import shutil
import subprocess
import tempfile
import time

# Local imports...
from appCommon.InstallPaths import getExifToolExe


# Hides the console window ExifTool would otherwise flash up.
_kNoWindow = getattr(subprocess, 'CREATE_NO_WINDOW', 0)

# ExifTool is given this many files per run, keeping the command line short.
_kExifToolBatch = 100


##############################################################################
def makeTempFolder(root=None, now=None):
    """A new, empty, timestamped folder for one action's copies.

    @param  root  Parent folder; defaults to %TEMP%\\Sighthound.
    @param  now   time.struct_time for the name; defaults to now.
    @return str   Absolute path, e.g. ...\\Sighthound\\20261009-161940.
    """
    root = root or os.path.join(tempfile.gettempdir(), 'Sighthound')
    stamp = time.strftime('%Y%m%d-%H%M%S', now or time.localtime())
    folder = os.path.join(root, stamp)
    n = 2
    while os.path.exists(folder):
        folder = os.path.join(root, '%s-%d' % (stamp, n))
        n += 1
    os.makedirs(folder)
    return folder


##############################################################################
def uniqueName(folder, name, taken):
    """A file name in folder that neither exists nor was handed out already.

    report.jpg, then report (2).jpg, report (3).jpg ...

    @param  folder  Destination folder.
    @param  name    Wanted file name.
    @param  taken   Set of lower-cased names already used; updated.
    @return str     The name to use.
    """
    stem, ext = os.path.splitext(name)
    candidate, n = name, 2
    while (candidate.lower() in taken
           or os.path.exists(os.path.join(folder, candidate))):
        candidate = '%s (%d)%s' % (stem, n, ext)
        n += 1
    taken.add(candidate.lower())
    return candidate


##############################################################################
def structuredPath(folder, path):
    """Where a file goes when its original folder path is kept.

    The drive becomes a top folder (G:\\Photos\\a.jpg -> <folder>\\G\\Photos\\
    a.jpg); a network path keeps its server and share
    (\\\\nas\\photos\\a.jpg -> <folder>\\nas\\photos\\a.jpg).

    @param  folder  Destination root.
    @param  path    Absolute path of the original.
    @return str     Destination path.
    """
    drive, rest = os.path.splitdrive(os.path.abspath(path))
    drive = drive.replace(':', '').strip('\\/').replace('\\', os.sep).replace('/', os.sep)
    parts = [p for p in (drive,) + tuple(rest.replace('/', '\\').split('\\')) if p]
    return os.path.join(folder, *parts)


##############################################################################
def copyFiles(paths, folder, keepStructure=False, progressFn=None):
    """Copy files into folder, flat or keeping their folder structure.

    @param  paths          Absolute paths of the originals.
    @param  folder         Destination root (normally from makeTempFolder).
    @param  keepStructure  True to recreate each file's original folders.
    @param  progressFn     Optional f(done, total, path) -> False to cancel.
    @return (copied, failed)  copied: destination paths; failed: list of
                              (original path, reason).
    """
    copied, failed, taken = [], [], set()
    for done, path in enumerate(paths):
        if progressFn is not None and progressFn(done, len(paths), path) is False:
            break
        try:
            if keepStructure:
                target = structuredPath(folder, path)
                os.makedirs(os.path.dirname(target), exist_ok=True)
            else:
                target = os.path.join(folder, uniqueName(
                    folder, os.path.basename(path), taken))
            shutil.copy2(path, target)
            copied.append(target)
        except OSError as e:
            failed.append((path, e.strerror or str(e)))
    if progressFn is not None:
        progressFn(len(paths), len(paths), None)
    return copied, failed


##############################################################################
def exifToolMissingMessage():
    """What to tell the user when ExifTool cannot be found.

    @return str
    """
    from appCommon.InstallPaths import getInstallRoot, kToolsDirName, kExifToolExe
    return ('Removing metadata needs ExifTool, which was not found.\n\n'
            'Put exiftool.exe in:\n%s\n\nor anywhere on the PATH, then try again.'
            % os.path.join(getInstallRoot(), kToolsDirName, 'exiftool', kExifToolExe))


##############################################################################
def _batchFailures(batch, returncode, stdout, stderr):
    """Which files of one ExifTool run were not cleaned.

    Counted, not just searched for: ExifTool's summary must account for
    every file as updated or unchanged (already had nothing), or the batch
    is not trusted.  A copy reported clean that still held GPS data would be
    shared believing it stripped.

    @return list  (path, reason) pairs.
    """
    counts = {}
    for number, what in re.findall(
            r'(\d+) (?:image )?files? (updated|unchanged)', stdout):
        counts[what] = counts.get(what, 0) + int(number)
    if returncode == 0 and sum(counts.values()) == len(batch):
        return []
    folded = stderr.replace('/', '\\').casefold()
    named = [(path, 'ExifTool could not rewrite it') for path in batch
             if path.casefold() in folded]
    if named and sum(counts.values()) + len(named) == len(batch):
        return named
    reason = stderr.strip().splitlines()[-1] if stderr.strip() else 'ExifTool failed'
    return [(path, reason) for path in batch]


##############################################################################
def stripMetadata(paths, progressFn=None):
    """Remove all metadata (EXIF, XMP, IPTC, GPS...) from copies, in place.

    Never pass originals: ExifTool rewrites the files it is given.

    @param  paths       Absolute paths of the COPIES.
    @param  progressFn  Optional f(done, total) -> False to cancel.
    @return list        (path, reason) for each file ExifTool could not clean.
    @raise  FileNotFoundError if ExifTool is not installed.
    """
    exe = getExifToolExe()
    if exe is None:
        raise FileNotFoundError(exifToolMissingMessage())
    failed = []
    for start in range(0, len(paths), _kExifToolBatch):
        if progressFn is not None and progressFn(start, len(paths)) is False:
            break
        batch = paths[start:start + _kExifToolBatch]
        # File names go in a UTF-8 argument file: on the command line Windows
        # would hand ExifTool the ANSI code page, which garbles any name
        # outside it (Japanese, emoji...) so the copy is never opened.
        fd, argFile = tempfile.mkstemp(suffix='.args')
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as stream:
                stream.write('\n'.join(batch) + '\n')
            # -all= removes every writable tag; -overwrite_original leaves no
            # "_original" backup beside each copy.
            proc = subprocess.run(
                [exe, '-charset', 'filename=utf8', '-all=',
                 '-overwrite_original', '-@', argFile],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                creationflags=_kNoWindow)
        finally:
            os.remove(argFile)
        failed += _batchFailures(batch, proc.returncode,
                                 proc.stdout.decode('utf-8', 'replace'),
                                 proc.stderr.decode('utf-8', 'replace'))
    if progressFn is not None:
        progressFn(len(paths), len(paths))
    return failed


##############################################################################
def showInExplorer(folder):
    """Open a folder in Explorer.

    @param  folder  Absolute path.
    """
    # No shell: spaces and punctuation stay part of the path.
    subprocess.Popen(['explorer.exe', os.path.normpath(folder)])
