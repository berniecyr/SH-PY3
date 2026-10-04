#! /usr/local/bin/python

#*****************************************************************************
#
# ExifData.py
#     Read and write file metadata, without touching the pixels.
#
#
#*****************************************************************************
#
#
# Copyright 2013-2022 Sighthound, Inc.
#
# Licensed under the GNU GPLv3 license found at
# https://www.gnu.org/licenses/gpl-3.0.txt
#
# Alternative licensing available from Sighthound, Inc.
# by emailing opensource@sighthound.com
#
# This file is part of the Sighthound Video project which can be found at
# https://github.com/sighthoundinc/SighthoundVideo
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; using version 3 of the License.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston, MA  02111, USA.
#
#
#*****************************************************************************

"""
## @file
Metadata for the user's own files, read and written through ExifTool.

### Why not Pillow, which is already here

`img.save(path, exif=...)` **re-encodes the image**.  For a JPEG that is a
generational quality loss, and it silently drops everything Pillow does not
model -- XMP, IPTC, MakerNotes, colour profiles.  That is a fine trade for a
snapshot we generated ourselves, which is why
DataManager.saveEventSnapshot uses exactly that call.  It is not a fine trade
for somebody's original photograph, and this module exists because the Image
view edits originals.

ExifTool rewrites only the metadata segment.  The pixel data is byte-identical
afterwards, and it handles the formats a photo library actually contains --
HEIC, RAW, PNG, and video containers -- none of which Pillow will write.

### Reading works without it

`readTags` falls back to Pillow for plain EXIF on formats Pillow can open, so
the metadata pane still shows something on a machine where ExifTool was not
staged.  Writing has no fallback and says so: `canWrite()` is what the UI
should ask before offering an editable field.
"""

# Python imports...
import json
import os
import subprocess
import sys

# Common 3rd-party imports...

# Toolbox imports...

# Local imports...
from appCommon.InstallPaths import getExifToolExe


# Constants...

# Never flash a console window, matching every other subprocess in this tree.
_kNoWindow = (subprocess.CREATE_NO_WINDOW
              if sys.platform == "win32" else 0)

# ExifTool is fast, but a network path or a very large video can still stall.
_kTimeoutSecs = 30

# Groups worth showing first in a metadata pane.  Everything else follows.
kPreferredGroups = ["EXIF", "XMP", "IPTC", "Composite", "QuickTime", "File"]

# Tags that are ours to compute, not the user's to edit: changing them in the
# file would just make the file lie about itself.
kReadOnlyTags = {
    "File:FileSize", "File:FileModifyDate", "File:FileAccessDate",
    "File:FileInodeChangeDate", "File:FilePermissions",
    "File:FileType", "File:FileTypeExtension", "File:MIMEType",
    "File:ImageWidth", "File:ImageHeight", "File:Directory",
    "File:FileName", "ExifTool:ExifToolVersion", "SourceFile",
}


##############################################################################
def canWrite():
    """Is metadata editing available?

    @return bool  True when ExifTool was found.
    """
    return getExifToolExe() is not None


##############################################################################
def unavailableReason():
    """Why editing is unavailable, for showing to the user.

    @return str  An explanation, or "" when it is available.
    """
    if canWrite():
        return ""
    return ("Metadata editing needs ExifTool, which is not installed. "
            "Reading still works for common image formats.")


##############################################################################
def _run(args, timeout=_kTimeoutSecs):
    """Run ExifTool and return its stdout.

    @param  args     Arguments after the executable.
    @param  timeout  Seconds before giving up.
    @return          (ok, stdout, stderr)
    """
    exe = getExifToolExe()
    if exe is None:
        return (False, "", "ExifTool is not installed.")
    try:
        proc = subprocess.run(
            [exe] + list(args), timeout=timeout, creationflags=_kNoWindow,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except Exception as e:
        return (False, "", str(e))
    return (proc.returncode == 0,
            proc.stdout.decode("utf-8", "replace"),
            proc.stderr.decode("utf-8", "replace"))


##############################################################################
def readTags(path):
    """Read every metadata tag from a file.

    @param  path  Absolute path.
    @return dict  {"Group:Tag": value}, possibly empty.
    """
    if getExifToolExe() is not None:
        # -G1 groups by the specific family (EXIF, XMP, IPTC...) rather than
        # lumping everything under one heading; -n keeps numbers numeric so a
        # value round-trips unchanged rather than being reformatted.
        ok, out, _err = _run(["-json", "-G1", "-a", "-u", path])
        if ok and out.strip():
            try:
                records = json.loads(out)
            except ValueError:
                records = []
            if records:
                record = records[0]
                return {k: v for k, v in record.items() if k != "SourceFile"}

    return _readTagsWithPillow(path)


##############################################################################
def _readTagsWithPillow(path):
    """Fallback reader for when ExifTool is not installed.

    Plain EXIF only, and only for formats Pillow can open -- but a metadata
    pane that shows the capture date and camera model beats one that shows an
    error because a binary was not staged.

    @param  path  Absolute path.
    @return dict  {"EXIF:Tag": value}.
    """
    try:
        from PIL import Image, ExifTags
    except ImportError:
        return {}

    tags = {}
    try:
        with Image.open(path) as img:
            exif = img.getexif()
            if not exif:
                return {}
            for tagId, value in exif.items():
                name = ExifTags.TAGS.get(tagId, str(tagId))
                tags["EXIF:%s" % name] = _readable(value)
            # DateTimeOriginal lives in the Exif sub-IFD (0x8769), not IFD0.
            # Reading only the top level is the mirror of the writing trap
            # documented in DataManager's snapshot writer, and it is why a
            # naive reader shows no capture date at all.
            try:
                sub = exif.get_ifd(0x8769)
            except Exception:
                sub = None
            for tagId, value in (sub or {}).items():
                name = ExifTags.TAGS.get(tagId, str(tagId))
                tags["EXIF:%s" % name] = _readable(value)
    except Exception:
        return {}
    return tags


##############################################################################
def _readable(value):
    """Coerce a Pillow EXIF value into something printable.

    @param  value  Whatever Pillow returned.
    @return        A str, int or float.
    """
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace").strip("\x00")
    if isinstance(value, (int, float, str)):
        return value
    return str(value)


##############################################################################
def writeTags(path, tags):
    """Change metadata in place, leaving the image data untouched.

    @param  path  Absolute path.
    @param  tags  {"Group:Tag": value}; a value of "" clears the tag.
    @return       (ok, message)
    """
    if getExifToolExe() is None:
        return (False, unavailableReason())
    if not os.path.isfile(path):
        return (False, "That file no longer exists.")
    if not tags:
        return (True, "Nothing to change.")

    args = []
    for name, value in tags.items():
        if name in kReadOnlyTags:
            continue
        # ExifTool's own syntax: -TAG=value, and -TAG= to delete.
        args.append("-%s=%s" % (name, "" if value is None else value))

    if not args:
        return (True, "Nothing to change.")

    # -overwrite_original: edit in place rather than leaving a _original copy
    # beside every photo the user touches.  This is safe BECAUSE ExifTool
    # rewrites only the metadata -- the pixels are not re-encoded, so there is
    # no generational loss to undo.
    args.append("-overwrite_original")
    args.append(path)

    ok, _out, err = _run(args)
    if not ok:
        return (False, err.strip() or "ExifTool could not write that.")
    return (True, "Saved.")


##############################################################################
def groupOf(tagName):
    """The group part of a "Group:Tag" name.

    @param  tagName  e.g. "EXIF:DateTimeOriginal".
    @return str      e.g. "EXIF".
    """
    return tagName.split(":", 1)[0] if ":" in tagName else "Other"


##############################################################################
def shortName(tagName):
    """The tag part of a "Group:Tag" name.

    @param  tagName  e.g. "EXIF:DateTimeOriginal".
    @return str      e.g. "DateTimeOriginal".
    """
    return tagName.split(":", 1)[1] if ":" in tagName else tagName


##############################################################################
def sortedTags(tags):
    """Order tags for display: preferred groups first, then alphabetically.

    @param  tags  The dict from readTags.
    @return list  [(name, value)] in display order.
    """
    def key(item):
        group = groupOf(item[0])
        try:
            rank = kPreferredGroups.index(group)
        except ValueError:
            rank = len(kPreferredGroups)
        return (rank, group, shortName(item[0]))

    return sorted(tags.items(), key=key)


##############################################################################
def isEditable(tagName):
    """Should the UI let this tag be edited?

    @param  tagName  "Group:Tag".
    @return bool
    """
    if tagName in kReadOnlyTags:
        return False
    # Composite tags are derived by ExifTool from other tags; writing one
    # writes nothing and confuses the person who tried.
    return groupOf(tagName) not in ("Composite", "ExifTool", "File")
