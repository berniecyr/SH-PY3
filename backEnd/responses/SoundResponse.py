#!/usr/bin/env python

#*****************************************************************************
#
# SoundResponse.py
#     Response: play a local sound
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


# Python imports...
import logging
import os
from subprocess import Popen, PIPE
import sys
import time
import wave

# Common 3rd-party imports...
try:
    import pyaudio
except ImportError:
    pyaudio = None

try:
    import winsound as _winsound
except ImportError:
    _winsound = None

# Local imports...
from .BaseResponse import BaseResponse

from frontEnd.GetLaunchParameters import getLaunchParameters
from appCommon.InstallPaths import resolveSoundPath
from appCommon.ResponseSubstitution import substituteResponseVars, faceNameForObjs

_kSoundPathLookup = "soundPath"


###############################################################
class SoundResponse(BaseResponse):
    """Sound response class."""
    ###########################################################
    def __init__(self, paramDict, ruleName=None, camLoc=None, dataMgr=None):
        """Initializer for SoundResponse class

        @param  paramDict      A dictionary of parameters for the response.
        @param  ruleName       Name of the owning rule (for TTS substitution
                               variables); may be None.
        @param  camLoc         The camera location (ditto); may be None.
        @param  dataMgr        DataManager for resolving {SvRuleFace} at fire
                               time (we run on the back end's search thread, so
                               using it here is safe); may be None.
        """
        super(SoundResponse, self).__init__()

        self._logger = logging.getLogger(__name__)

        self._ruleName = ruleName
        self._camLoc = camLoc
        self._dataMgr = dataMgr
        self._svLookFor = paramDict.get('svLookFor', '')

        # This is a dict of object ids and the last frame number they were seen
        # in.  We won't alert a second time for an object until it begins
        # re-triggering after a break.
        self._itemDict = {}

        soundPath = paramDict.get(_kSoundPathLookup, '')

        # Ensure we have a str path (bytes would come from old pickled data).
        if isinstance(soundPath, bytes):
            soundPath = soundPath.decode('utf-8')

        # A sound from the app's own folder is stored relative to the install
        # (and older rules hold the full path of whatever copy of the app saved
        # them), so find the file in THIS install before anything opens it.
        soundPath = resolveSoundPath(soundPath)

        self._soundPath = soundPath
        # Where to play the WAV: 'local' machine or 'chromecast' network speaker
        # (the speaker target is shared with TTS — self._ttsCcIp/_ttsCcName).
        self._soundOutput = paramDict.get('soundOutput', 'local')
        self._lastAlertTime = 0
        self._fileDuration = 0

        if soundPath:
            self._popenParamList = getLaunchParameters()
            self._popenParamList.extend(["--sound", soundPath])
            try:
                waveFile = wave.open(soundPath, 'rb')
                self._fileDuration = 1.0*waveFile.getnframes()/waveFile.getframerate()
            except Exception:
                pass
        else:
            self._popenParamList = None

        # TTS params
        self._ttsEnabled  = bool(paramDict.get('ttsEnabled', False))
        self._ttsText     = paramDict.get('ttsText', 'Alert')
        self._ttsVoice    = paramDict.get('ttsVoice', 'af_heart')
        self._ttsSpeed    = float(paramDict.get('ttsSpeed', 1.0))
        self._ttsOutput   = paramDict.get('ttsOutput', 'local')
        self._ttsCcIp     = paramDict.get('ttsChromecastIP', '')
        self._ttsCcName   = paramDict.get('ttsChromecastName', '')

        # Per-rule cooldown: minimum time between firings of this whole Sound
        # response (played WAV and/or spoken text) when a rule triggers
        # repeatedly (minutes, fractional; 0 = no extra limit).  Folded into the
        # alert gate in addRanges so it throttles the entire response.
        self._ttsCooldownSecs = float(paramDict.get('ttsCooldownMins', 0.0)) * 60.0

        # Minimum cooldown for TTS-only mode (no WAV duration to pace from)
        if self._ttsEnabled and self._fileDuration == 0:
            self._fileDuration = 3.0


    ###########################################################
    def addRanges(self, ms, rangeDict):
        """Add ranges generated from processing.

        @param  ms         The most recent time in milliseconds that has been
                           processed.
        @param  rangeDict  A dictionary of response ranges.  Key = objId,
                           value = list of ((firstFrame, firstTime),
                                            (lastFrame, lastTime)).
        """
        _ = ms

        alert = False

        prevSeenObjs = list(self._itemDict.keys())
        curObjs = list(rangeDict.keys())

        for objId in curObjs:
            numRanges = len(rangeDict[objId])
            if not numRanges:
                assert False, "Must have entries in the range list"

            elif numRanges == 1:
                # There is only one range. We'll check to see if it is a
                # continuation of the previous triggered events.  If not we'll
                # send an alert.
                (firstFrame, _), (lastFrame, _) = rangeDict[objId][0]

                if (objId not in self._itemDict) or \
                   (firstFrame != self._itemDict[objId]+1):
                    alert = True

                self._itemDict[objId] = lastFrame
            else:
                # If there are multiple ranges here we know the object triggered
                # after taking a break so we'll send an alert.  If the object
                # hasn't been previously tracked, we'll also send an alert.
                alert = True
                for (_, _), (lastFrame, _) in rangeDict[objId]:
                    # Set the last frame seen to the last frame seen.
                    self._itemDict[objId] = max(self._itemDict.get(objId, 0),
                                                lastFrame)

        # Clean up objects that aren't around anymore.
        for objId in prevSeenObjs:
            if objId not in curObjs:
                del self._itemDict[objId]

        if alert:
            now = time.time()
            if now < self._lastAlertTime + max(self._fileDuration,
                                               self._ttsCooldownSecs):
                # Don't re-fire until the previous play has finished AND the
                # per-rule cooldown (which applies to the whole Sound response)
                # has elapsed.
                return

            self._lastAlertTime = now

            # Play the WAV — locally (subprocess) or cast to the shared network
            # speaker, depending on this rule's sound output setting.
            if self._soundPath:
                if (self._soundOutput == 'chromecast' and
                        (self._ttsCcIp or self._ttsCcName)):
                    try:
                        from backEnd.responses.TtsManager import get_tts_manager
                        get_tts_manager(self._logger).queue_cast_file(
                            self._soundPath, self._ttsCcIp,
                            cc_name=self._ttsCcName)
                    except Exception as e:
                        self._logger.warning(
                            "[SoundResponse] sound cast error: %s" % e)
                elif self._popenParamList:
                    subProc = Popen(self._popenParamList, stdin=PIPE,
                                    stdout=PIPE, stderr=PIPE,
                                    close_fds=(sys.platform=='darwin'))
                    subProc.stdin.close()
                    subProc.stdout.close()
                    subProc.stderr.close()

            # TTS alert (queued to background thread).  The per-rule cooldown is
            # enforced by the alert gate above, so it already covers both the
            # WAV and the speech.
            if self._ttsEnabled and self._ttsText:
                try:
                    from backEnd.responses.TtsManager import get_tts_manager

                    # Per-event substitution variables in the spoken text...
                    text = self._ttsText
                    if '{Sv' in text:
                        eventTimeMs = None
                        try:
                            eventTimeMs = min(rangeDict[o][0][0][1]
                                              for o in curObjs)
                        except Exception:
                            pass
                        text = substituteResponseVars(
                            text, self._ruleName, self._camLoc, eventTimeMs,
                            self._svLookFor,
                            faceNameForObjs(self._dataMgr, curObjs))

                    mgr = get_tts_manager(self._logger)
                    mgr.queue_tts(text, self._ttsVoice,
                                  self._ttsSpeed, self._ttsOutput,
                                  self._ttsCcIp, cc_name=self._ttsCcName)
                except Exception as e:
                    self._logger.warning("[SoundResponse] TTS error: %s" % e)


    ###########################################################
    def startNewSession(self):
        """Do anything necessary to respond to a new camera session."""
        return


#####################################################################
def playSound(soundPath, catchExceptions=True):
    """Play a wave file.

    @param  soundPath        The absolute path to the wave file to play.
    @param  catchExceptions  If True exceptions will be caught and ignored.
    """
    try:
        if isinstance(soundPath, bytes):
            soundPath = soundPath.decode('utf-8')

        if pyaudio is not None:
            pyAudioInst = pyaudio.PyAudio()
            waveFile = wave.open(soundPath, 'rb')

            channels, width, framerate, _, _, _ = waveFile.getparams()
            fmt = pyAudioInst.get_format_from_width(width)
            stream = pyAudioInst.open(rate=framerate, channels=channels,
                                      format=fmt, output=True)

            data = waveFile.readframes(4096)
            while data:
                stream.write(data)
                data = waveFile.readframes(4096)

            stream.close()
            pyAudioInst.terminate()
        elif _winsound is not None:
            _winsound.PlaySound(soundPath, _winsound.SND_FILENAME)
    except:
        if not catchExceptions:
            raise
