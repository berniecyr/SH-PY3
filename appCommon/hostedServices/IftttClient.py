#*****************************************************************************
#
# IftttClient.py
#   IFTTT Webhooks API Client
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

import http.client
import json
import ssl

_kIftttHost = "maker.ifttt.com"
_kTimeout = 20


##############################################################################
class IftttClient(object):
    """Sends events directly to the IFTTT Webhooks service (maker.ifttt.com).

    Each instance is bound to a specific webhook key and event name.
    The key is found at ifttt.com → Services → Webhooks → Settings.
    """

    ###########################################################
    def __init__(self, logger, key, eventName):
        """Constructor.

        @param  logger     Logger instance.
        @param  key        IFTTT Webhooks key or full key URL from the Settings page.
        @param  eventName  Name of the IFTTT event to trigger.
        """
        self._logger = logger
        # Accept either the bare key or the full URL IFTTT shows in Settings
        _prefix = "https://maker.ifttt.com/use/"
        if key.startswith(_prefix):
            key = key[len(_prefix):].rstrip('/')
        self._key = key
        self._eventName = eventName


    ###########################################################
    def trigger(self, camLoc, ruleName, triggerTime):
        """Trigger the IFTTT event via the Webhooks service.

        Sends value1=camera, value2=rule, value3=timestamp.

        @param  camLoc       Camera location name.
        @param  ruleName     Rule name.
        @param  triggerTime  Epoch seconds.
        @return              True if the request returned HTTP 200.
        """
        path = "/trigger/%s/with/key/%s" % (self._eventName, self._key)
        payload = json.dumps({
            'value1': camLoc,
            'value2': ruleName,
            'value3': str(int(triggerTime)),
        }).encode('utf-8')
        headers = {
            'Content-Type': 'application/json',
            'Content-Length': len(payload),
        }
        self._logger.info("IFTTT trigger: event=%s cam=%s rule=%s" % (
            self._eventName, camLoc, ruleName))
        try:
            ctx = ssl._create_unverified_context()
            conn = http.client.HTTPSConnection(_kIftttHost, 443,
                                              timeout=_kTimeout,
                                              context=ctx)
            conn.request('POST', path, payload, headers)
            resp = conn.getresponse()
            status = resp.status
            body = resp.read().decode('utf-8', errors='replace')
            conn.close()
            self._logger.info("IFTTT response: %d %s" % (status, body[:200]))
            return status == 200
        except Exception as e:
            self._logger.error("IFTTT trigger failed: %s" % str(e))
            return False
