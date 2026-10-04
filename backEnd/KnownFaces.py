#! /usr/local/bin/python

#*****************************************************************************
#
# KnownFaces.py
#     Load the enrolled-face library and match an embedding against it.
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
Name a face by matching its ArcFace embedding against the enrolled library.

`known_faces.dat` is a pickle of {"encodings": [...], "names": [...]} written by
the enrollment path (see backEnd/FaceEnrollment.py).  This module reads it and
answers "who is this?" for one embedding.

It exists as its own module because the Image view needs the same answer the
cameras get, and the camera-side copy lives inside a method of a per-camera
process class.  That copy is deliberately NOT changed by this file -- the live
detection path is left exactly as it was -- so the two are, for now, parallel
implementations of the same rule.  Recorded in REVIEW-BACKLOG.md.

The difference here is arithmetic, not behaviour: the known matrix is
L2-normalised ONCE at load, so a query is a single dot product against the whole
library instead of a Python loop that recomputes every known vector's norm for
every query face.  Matching one camera frame against a handful of identities did
not care; matching a photo library does.
"""

# Python imports...
import os
import pickle
import threading

# Common 3rd-party imports...
import numpy as np

# Toolbox imports...

# Local imports...


# Constants...

# Guards the module-level cache: the Image view matches on a worker thread
# while the UI thread can ask for a reload.
_kLock = threading.Lock()

# Cached library: (matrix of unit row-vectors, names, mtime of the file it
# came from).  The mtime is what lets a re-enrollment be picked up without a
# restart -- enrollment rewrites the whole file.
_cache = {"matrix": None, "names": [], "mtime": None, "path": None}


##############################################################################
def _l2Normalise(rows):
    """Scale each row to unit length.

    @param  rows  (n, d) float array.
    @return       The same shape, each row unit length.  A zero row stays zero
                  rather than becoming NaN.
    """
    norms = np.linalg.norm(rows, axis=1, keepdims=True)
    # Not a tolerance knob -- purely to keep a zero-length row from dividing
    # by zero.  Such a row then scores 0 against everything, which is right.
    norms[norms == 0] = 1e-9
    return rows / norms


##############################################################################
def loadKnownFaces(datPath, logger=None, force=False):
    """Load (or re-use) the enrolled-face library.

    @param  datPath  Path to known_faces.dat.
    @param  logger   Optional logger.
    @param  force    Reload even if the file has not changed.
    @return count    Number of identities available.
    """
    with _kLock:
        if not datPath or not os.path.isfile(datPath):
            if _cache["path"] != datPath and logger is not None:
                logger.info("KnownFaces: no library at %r; faces will be "
                            "detected but not named" % (datPath,))
            _cache.update(matrix=None, names=[], mtime=None, path=datPath)
            return 0

        try:
            mtime = os.path.getmtime(datPath)
        except OSError:
            mtime = None

        if (not force and _cache["matrix"] is not None
                and _cache["path"] == datPath and _cache["mtime"] == mtime):
            return len(_cache["names"])

        try:
            with open(datPath, "rb") as f:
                data = pickle.load(f)
            encodings = data.get("encodings", [])
            names = data.get("names", [])
        except Exception as e:
            # A library we cannot read means unnamed faces, not a failed
            # analysis -- everything else about the file is still worth having.
            if logger is not None:
                logger.warning("KnownFaces: cannot read %r: %s" % (datPath, e))
            _cache.update(matrix=None, names=[], mtime=mtime, path=datPath)
            return 0

        if not encodings:
            _cache.update(matrix=None, names=[], mtime=mtime, path=datPath)
            return 0

        matrix = _l2Normalise(
            np.asarray([np.asarray(e).flatten() for e in encodings],
                       dtype=np.float32))
        _cache.update(matrix=matrix, names=list(names), mtime=mtime,
                      path=datPath)
        if logger is not None:
            logger.info("KnownFaces: loaded %d identities from %r"
                        % (len(names), datPath))
        return len(names)


##############################################################################
def matchEmbedding(embedding, threshold):
    """Name one face.

    @param  embedding  The face's ArcFace embedding (any shape, flattened).
    @param  threshold  Minimum cosine similarity to accept, i.e. FACEMATCH_CONF.
    @return (name, conf)  The best match above the threshold and its
                          similarity, or ("", None) when nothing qualifies.
    """
    if embedding is None:
        return ("", None)

    with _kLock:
        matrix = _cache["matrix"]
        names = _cache["names"]

    if matrix is None or not len(names):
        return ("", None)

    query = np.asarray(embedding, dtype=np.float32).flatten()
    norm = float(np.linalg.norm(query))
    if norm == 0:
        return ("", None)
    query = query / norm

    # One dot product against the whole library; both sides are unit vectors,
    # so the product IS the cosine similarity.
    sims = matrix.dot(query)
    idx = int(np.argmax(sims))
    conf = float(sims[idx])
    if conf < threshold:
        return ("", None)

    # Enrollment stores display names as "Name (extra)"; rules and the UI use
    # the bare name.
    return (names[idx].split(" (")[0], conf)


##############################################################################
def matchBest(faces, threshold):
    """Name a group of faces, keeping the most confident identification.

    @param  faces      Objects with an `embedding` attribute, as returned by
                       DetectionServiceClient.face().
    @param  threshold  Minimum cosine similarity to accept.
    @return (name, conf)  Best match across all of them, or ("", None).
    """
    bestName = ""
    bestConf = None
    for face in faces or []:
        name, conf = matchEmbedding(getattr(face, "embedding", None),
                                    threshold)
        if name and (bestConf is None or conf > bestConf):
            bestName, bestConf = name, conf
    return (bestName, bestConf)
