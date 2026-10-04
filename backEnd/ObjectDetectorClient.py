#! /usr/local/bin/python

#*****************************************************************************
#
# ObjectDetectorClient.py
#     Used from within camera process to access classification service providing
#     recognition of tracked objects.
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

import time
from PIL import ImageDraw, ImageFont

from io import BytesIO
import base64
import http.client
import sys
import json
import os
import ssl
import threading
import traceback
from queue import Queue, Empty, Full

from vitaToolbox.math.Rect import Rect as VitaRect
from vitaToolbox.image.ImageConversion import convertProcFrameToPIL
from vitaToolbox.path.PathUtils import safeMkdir

# Bound on the per-camera detection backlog, in work items.
#
# This queue used to be unbounded.  The detection service serializes every
# camera's inference behind a single lock, so whenever it could not keep up
# the backlog grew without limit: each item's turn came later and later until
# the request died on the client's 30 s timeout, and because every queued item
# holds a full analysis frame the backlog also drove the CPU out-of-memory
# crashes seen in the camera logs.  Dropping the OLDEST item is the deliberate
# choice -- surveillance wants the freshest frame, and a detection result that
# arrives a minute late is worthless.
_kMaxPendingWorkItems = 4

# Items older than this are dropped at dequeue instead of being sent.  Past
# this age the recorder's matching full-res snapshot is rejected anyway (see
# _kSnapshotMatchToleranceMs in ObjectDetectorClientImageCheck), so the request
# would spend a slow CPU inference producing a result nobody can use.
_kMaxWorkItemAgeMs = 5000


_kGreen = (0, 255, 0)
_kRed = (255, 0, 0)
_kBlue = (0, 0, 255)
_kYellow = (255, 255, 0)
_kPink = (255, 132, 0)
_kCropMargin = 20 # number pixels around Sentry detection to send to the API
_kNetworkSize = 300 # This needs to be changed, if SIOWrapper changes the model to SSD500
_kMinOverlap = 0.3 # minimum overlap we allow between detection box and Sentry box
_kMinContainment = 0.9 # object needs to be well-contained within motion blob, for us to consider containment when overlap fails
_kCloud = False

################################################################################
# TODO: duplicated into ObjectDetectorClient.py, too small for its own module ... resolve the redundancy
def _getModelDir():
    if hasattr(sys, 'frozen'):
        path = os.path.dirname(sys.executable)
        if sys.platform == 'darwin':
            return os.path.join(path, "..", "Resources", "models" )
        return os.path.join(path, "models" )
    else:
        devLibFolder = os.getenv("SV_DEVEL_LIB_FOLDER_CONAN")
        if devLibFolder is not None and devLibFolder != "":
            res = os.path.join(devLibFolder, "..", "models")
            if os.path.isdir(res):
                return res
        # This likely won't work
        return os.path.join("..", "models")

##############################################################################
class ObjectDetectorClient(threading.Thread):
    """ Threaded HTTP client object, responsible for sequentially running work
        items against an external entity.
    """
    #---------------------------------------------------------------------------
    def __init__(self, id, logger):
        threading.Thread.__init__(self)
        self._id = id
        self._visualizeDebug = False
        self.setDebugFolder(None)

        self._logger = logger
        self._workItemsQueue = Queue(maxsize=_kMaxPendingWorkItems)
        self._resultsQueue = Queue()
        self._droppedWorkItems = 0     # cumulative, for the drop log

        self._httpConn = None
        self._httpHeaders = {}

        self._httpTimeout = 20
        self._lastHttpRequest = time.time() - 2*self._httpTimeout

    #---------------------------------------------------------------------------
    def setDebugFolder(self, folder):

        if folder is not None:
            self._outputFolderOriginal = os.path.join(folder, self._id, "original")
            safeMkdir(self._outputFolderOriginal)
            self._outputFolderBoxes = os.path.join(folder, self._id, "boxes")
            safeMkdir(self._outputFolderBoxes)
        else:
            self._outputFolderOriginal = None
            self._outputFolderBoxes = None

        self._visualizeDebug = folder is not None

    #---------------------------------------------------------------------------
    def setAnalyticsPort(self, port):
        pass

    #---------------------------------------------------------------------------
    def _doRefreshHTTPConnection(self):
        """ Establish HTTP/S connection
        """
        raise NotImplementedError("_doRefreshHTTPConnection is abstract in parent class")

    #---------------------------------------------------------------------------
    def _mustRefreshHTTPConnection(self):
        return self._httpConn is None or \
            self._lastHttpRequest + self._httpTimeout <= time.time()

    #---------------------------------------------------------------------------
    def _refreshHTTPConnection(self):
        """ Refresh HTTP connectiion if needed
        """
        if self._mustRefreshHTTPConnection():
            self._doRefreshHTTPConnection()

    #---------------------------------------------------------------------------
    def _getTextOrigin(self, image, draw, box, text, lineNo, lineCount):
        try:
            txtW, txtH = draw.textsize(text)
        except AttributeError:
            bbox = draw.textbbox((0, 0), text)
            txtW, txtH = bbox[2] - bbox[0], bbox[3] - bbox[1]
        txtH += 1 #include spacing
        if box.br().y-box.tl().y > lineCount*txtH:
            y = box.tl().y + (lineNo-1)*txtH + 1
        elif box.tl().y > lineCount*txtH:
            y = box.tl().y - (lineNo-1)*txtH - 1
        else:
            y = box.br().y + (lineNo-1)*txtH + 1

        if box.tl().x + txtW < image.size[0]:
            x = box.tl().x
        else:
            x = image.size[0] - txtW
        return (x,y)

    #---------------------------------------------------------------------------
    def _visualizeSentryBoxes(self, image, sentryBoxes):
        """ Draw Sentry rectangles on the provided PIL image in green
        """
        if not self._visualizeDebug:
            return
        draw = ImageDraw.Draw(image)
        for obj, id, sentryRect in sentryBoxes:
            color = _kYellow if obj.sentryObjType == "person" else _kGreen
            label = "ID="+str(obj._idNum)+" frame="+str(id)
            x, y = self._getTextOrigin(image, draw, sentryRect, label, 1, 2)
            draw.text((x, y), label, fill=_kPink)
            draw.rectangle((sentryRect.tl().x, \
                            sentryRect.tl().y, \
                            sentryRect.br().x+1, \
                            sentryRect.br().y+1), \
                            outline=color)

    #---------------------------------------------------------------------------
    def _visualizeResults(self, image, sentryRect, objects, offset=(0,0)):
        """ Draw Cloud result rectangles on the provided PIL image in red
        """
        if not self._visualizeDebug:
            return
        draw = ImageDraw.Draw(image)
        label = ""
        for type, box, score in objects:
            color = _kRed if type == "person" else _kBlue
            draw.rectangle((box.tl().x+offset[0], \
                            box.tl().y+offset[1], \
                            box.br().x+1+offset[0], \
                            box.br().y+1+offset[1]),
                            outline=color)
            label = "%s%s-%.2f;" % (label, type, score)
        x, y = self._getTextOrigin(image, draw, sentryRect, label, 2, 2)
        draw.text((x,y), label, fill=_kPink)

    #---------------------------------------------------------------------------
    def _processSingleImage(self, image):
        """ Invoke HTTP API on a single PIL image object

        @param  image    PIL Image
        @return objects  list of (objectType, objectRectangle)
        """
        raise NotImplementedError("_processSingleImage is abstract in parent class")

    #---------------------------------------------------------------------------
    def _scaleUpSentryBoxes(self, sentryBoxes, sizeRatio):
        """ Translate Sentry box sizes into full image domain
        """
        scaledUpBoxes = []
        for obj, id, sBox in sentryBoxes:
            sentryRect = VitaRect(sBox[0],sBox[1],sBox[2]-sBox[0],sBox[3]-sBox[1])
            scaledUpSentryRect = sentryRect * ( 1/sizeRatio )
            scaledUpBoxes.append((obj,id,scaledUpSentryRect))
        return scaledUpBoxes

    #---------------------------------------------------------------------------
    def _getBestCoord(self, p1, p2, minSize, margin, dimSize):
        """ Determine the best coordinates for a bounding box
        @param  p1          lower point of the dimension
        @param  p2          upper point of the dimension
        @param  minSize     minimal size of the resulting box
        @param  margin      minimal margin we'd want to add to the existing box
                            on each size
        @param  dimSize     size of the dimension -- can't grow the box outside of (0, dimSize-1
        @return (newP1,newP2) new dimension constraints
        """
        curSize = p2 - p1

        # if growing to minSize will not generate a sufficient margin, we'll
        # need a new minSize
        minSize = max(curSize + margin*2, minSize)

        # if the size if sufficient, keep the box as is
        if curSize >= minSize:
            return (p1, p2)

        # add required growth on both sides, as much as the dimensions allow
        toGrow = minSize - curSize
        growTop = min( toGrow//2, dimSize - p2 - 1 )
        growBottom = min( toGrow//2, p1 )

        newP1 = p1 - growBottom
        newP2 = p2 + growTop

        # add whatever is left to add, if we can
        remainingToGrow = toGrow - ( growTop + growBottom )
        if remainingToGrow > 0:
            if newP1 > 0:
                newP1 = max ( 0, newP1 - remainingToGrow )
            if newP2 < dimSize - 1:
                newP2 = min ( newP2 + remainingToGrow, dimSize - 1)
        return ( newP1, newP2 )



    #---------------------------------------------------------------------------
    def _getCropRect(self, imageSize, box):
        """ Get best crop rectangle for the image, with the network size in mind
        """
        left, right = self._getBestCoord(box.tl().x, box.br().x, _kNetworkSize, _kCropMargin, imageSize[0])
        top, bottom = self._getBestCoord(box.tl().y, box.br().y, _kNetworkSize, _kCropMargin, imageSize[1])
        return (left, top, right, bottom), VitaRect(box.x-left, box.y-top, box.width, box.height)

    #---------------------------------------------------------------------------
    def _processWorkItem(self, timestamp, fullSizeFrame, sentryBoxes, sizeRatio):
        """ Run cloud detection on the frame, if a full-size frame exists for this timestamp
        """
        allTypes = ""

        start = time.time()
        scaledUpBoxes = self._scaleUpSentryBoxes(sentryBoxes, sizeRatio)
        image = convertProcFrameToPIL(fullSizeFrame)
        if self._visualizeDebug:
            outputFileOriginal = os.path.join(self._outputFolderOriginal, str(timestamp) + ".jpg")
            image.save(outputFileOriginal)
            outputFileBase = os.path.join(self._outputFolderBoxes, str(timestamp))

        output = []
        for obj, id, box in scaledUpBoxes:
            cropRect, sentryRectInCrop = self._getCropRect(image.size, box)
            imageCrop = image.crop(cropRect)
            objects = self._processSingleImage(imageCrop)
            cropOutput = self._processJSONResult(objects, (obj,id,sentryRectInCrop))

            if cropOutput:
                for match in cropOutput:
                    allTypes = allTypes + "-" + match[1]
                    output.append(match)

            if self._visualizeDebug:
                self._visualizeResults(image, box, objects, (cropRect[0], cropRect[1]))

        detCallTime = int((time.time() - start)*1000)

        if output is not None:
            self._logger.debug("Detection for %d done in %s ms, processed %d cloud / %d sentry results, %d matched, queue size is %d" % \
                    (timestamp, str(detCallTime), len(objects), len(sentryBoxes), len(output), self._workItemsQueue.qsize()) )
            self._lastHttpRequest = time.time()

        if self._visualizeDebug:
            self._visualizeSentryBoxes(image, scaledUpBoxes)
            image.save(outputFileBase+allTypes+".jpg")

        self._resultsQueue.put( (timestamp, output) )

    #---------------------------------------------------------------------------
    def _processJSONResult(self, objects, sentryBox):
        if objects is None:
            return None

        output = []

        # match the box to Sentry box
        objId, frameId, sentryRect = sentryBox

        # Iterate on external results, and match them up with Sentry results
        for type, boxRect, score in objects:
            # when using 'ss', a person passing in front of the car, may receive a 'vehicle' label
            # when using 'jaccard', a group of people in a single bounding box may receive an 'unknown' label
            # ... choose your poison.
            kOverlapMethod = 'jaccard' # 'ss'
            overlap = sentryRect.overlap(boxRect, kOverlapMethod)
            # ONE-WAY on purpose: the detection inside the blob.  The reverse
            # direction was tried 2026-08-14 and removed 2026-08-19 -- it can
            # only fire where jaccard already judged the blob too small, and
            # measured as 0 gain on 196 real matches against 5 of 6 false
            # `vehicle` labels on a parked car.  See the ImageCheck override.
            containment = boxRect.containment(sentryRect)
            if overlap >= _kMinOverlap or containment >= _kMinContainment:
                status = "match"
                output.append( (objId, type, score, overlap) )
            else:
                status = "ignored, insufficient overlap"
            self._logger.debug( "SentryID=%d, frame=%d: Detection %s: %s rect=%s sentryRect=%s score=%.2f overlap=%.2f" %
                                (objId._idNum, frameId, status, type, str(boxRect), str(sentryRect), score, overlap) )

        return output

    #---------------------------------------------------------------------------
    def _sendHTTPRequest(self, url, params):
        """ Invoke HTTP request

        @return response if successful, or None
        """
        kMaxHTTPRetries = 5
        retries = 0
        response = None

        while response is None:
            if retries >= kMaxHTTPRetries:
                raise Exception("Couldn't send HTTP request")

            try:
                retries += 1
                self._refreshHTTPConnection()
                self._httpConn.request("POST", url, params, self._httpHeaders)
                response = self._httpConn.getresponse()
            except:
                self._httpConn = None # start with a fresh connection object for the next attempt
                self._logger.warning("Failed to send HTTP request. Retrying ..." + str(retries) + "..." + traceback.format_exc())
        return response


    #---------------------------------------------------------------------------
    def _callDetectionAPI(self, params):
        """ Invoke HTTP API with given params

        @param  image    PIL Image
        @return objects  list of (objectType, objectRectangle), or None if error
        """
        raise NotImplementedError("_callDetectionAPI is abstract in parent class")

    #---------------------------------------------------------------------------
    def _dropWorkItem(self, item, why):
        """Discard a work item, still publishing a result for it.

        A dropped item MUST still produce a result.  The owner counts one
        result per enqueued item (_outstandingDetectionRequests in
        QueuedDataManagerCloud) and its terminate() spins with no timeout
        until that count reaches zero, so a silently dropped item would hang
        shutdown.  An empty result is exactly what the existing failure paths
        publish: the frame's objects fall back to 'unknown'.
        """
        self._resultsQueue.put( (item[0], []) )
        self._droppedWorkItems += 1
        # Loud once, then occasional: a backlog drops many frames in a row.
        if self._droppedWorkItems == 1 or self._droppedWorkItems % 50 == 0:
            self._logger.info(
                "Dropped detection work item (%s); %d dropped so far, queue %d"
                % (why, self._droppedWorkItems, self._workItemsQueue.qsize()))

    #---------------------------------------------------------------------------
    def enqueWorkItem(self, timestamp, image, sentryBoxes, sizeRatio):
        """
        Submit next item for external detection.

        The queue is bounded (_kMaxPendingWorkItems).  When it is full the
        OLDEST pending item is discarded to make room, so a camera that
        outruns the detection service loses the stalest frames rather than
        accumulating a backlog that times out and exhausts memory.
        """
        item = (timestamp, image, sentryBoxes, sizeRatio)
        while True:
            try:
                self._workItemsQueue.put_nowait(item)
                return
            except Full:
                try:
                    oldest = self._workItemsQueue.get_nowait()
                except Empty:
                    continue      # drained by run() in the meantime; retry
                self._workItemsQueue.task_done()
                if oldest is None:
                    # Terminate sentinel -- put it back and drop THIS item
                    # instead; losing it would wedge the worker thread.
                    self._workItemsQueue.put_nowait(oldest)
                    self._dropWorkItem(item, "shutting down")
                    return
                self._dropWorkItem(oldest, "backlog full")

    #---------------------------------------------------------------------------
    def getNextResult(self):
        """
        Get the next (timestamp, output) tuple, if available, or None otherwise
        """
        if self._resultsQueue.empty():
            return None
        return self._resultsQueue.get()

    #---------------------------------------------------------------------------
    def pendingWorkItems(self):
        """
        How many work items are queued for the detector right now.

        Approximate by construction (Queue.qsize()), and that is fine: the
        caller uses this to decide whether an OPTIONAL extra frame is worth
        enqueueing, so being off by one costs at most one frame either way.
        """
        return self._workItemsQueue.qsize()

    #---------------------------------------------------------------------------
    def droppedWorkItems(self):
        """
        Cumulative count of work items dropped without being processed.

        _dropWorkItem's own log fires on the 1st drop and every 50th after --
        deliberately quiet, but that cannot be differenced over a window and it
        resets with the camera process.  Every drop publishes an empty result
        and therefore costs one abstention vote for every object in the dropped
        frame, so the RATE is what matters; the owner samples this into its
        periodic stats line.
        """
        return self._droppedWorkItems

    #---------------------------------------------------------------------------
    def run(self):
        """
        Process queue and execute connection requests
        """
        while True:
            tuple = self._workItemsQueue.get()
            if tuple is None:
                self._workItemsQueue.task_done()
                break

            timestamp = tuple[0]
            fullSizeFrame = tuple[1]
            sentryBoxes = tuple[2]
            sizeRatio = tuple[3]

            # Timestamps are epoch ms (same clock the snapshot matcher
            # compares against os.path.getmtime).
            if (_kMaxWorkItemAgeMs > 0 and
                    (time.time() * 1000.0) - timestamp > _kMaxWorkItemAgeMs):
                self._dropWorkItem(tuple, "stale")
                self._workItemsQueue.task_done()
                continue

            try:
                self._processWorkItem(timestamp, fullSizeFrame, sentryBoxes, sizeRatio)
            except:
                self._logger.error("Exception while processing detection request:")
                self._logger.error(traceback.format_exc())
                # Nothing will process this item again -- this will cause every
                # tracked item to default to 'unknown' in this frame
                self._resultsQueue.put( (timestamp, []) )

            self._workItemsQueue.task_done()

    #---------------------------------------------------------------------------
    def terminate(self):
        """ Terminate the thread

        The queue is bounded, so a plain blocking put() could stall here
        while the worker is mid-inference on a full backlog.  Make room by
        discarding the oldest pending item instead: we are shutting down, so
        its detection result is moot -- but it still has to be published, or
        the owner's outstanding-request count never reaches zero and its own
        terminate() spins forever.
        """
        while True:
            try:
                self._workItemsQueue.put_nowait(None)
                return
            except Full:
                try:
                    oldest = self._workItemsQueue.get_nowait()
                except Empty:
                    continue      # drained by run(); retry
                self._workItemsQueue.task_done()
                if oldest is not None:
                    self._dropWorkItem(oldest, "shutting down")

##############################################################################
class ObjectDetectorClientCloud(ObjectDetectorClient):
    """ Cloud version of the client
    """

    _kCloudToken = "c8n5V5bFSUIM9VNIXS3ey7Rui72jqV5vg6pl"

    #---------------------------------------------------------------------------
    def __init__(self, id, logger):
        super(ObjectDetectorClientCloud, self).__init__(id, logger)

    #---------------------------------------------------------------------------
    def _doRefreshHTTPConnection(self):
        """ Establish HTTP/S connection
        """
        self._httpHeaders = {"Content-type": "application/json", "X-Access-Token": ObjectDetectorClient._kCloudToken}
        self._httpConn = http.client.HTTPSConnection("dev.sighthoundapi.com",
            context=ssl.SSLContext(ssl.PROTOCOL_TLSv1),
            timeout=self._httpTimeout)

    #---------------------------------------------------------------------------
    def _processSingleImage(self, image):
        """ Invoke HTTP API on a single PIL image object

        @param  image    PIL Image
        @return objects  list of (objectType, objectRectangle)
        """
        with BytesIO() as f:
            image.save(f, format='JPEG')
            image_data= base64.b64encode(f.getvalue())

        params = json.dumps({"image": image_data})
        try:
            objects = self._callDetectionAPI(params)
        except:
            # reset the state every time exception occurs
            self._httpConn = None
            raise
        return objects

    #---------------------------------------------------------------------------
    def _callDetectionAPI(self, params):
        """ Invoke HTTP API with given params

        @param  image    PIL Image
        @return objects  list of (objectType, objectRectangle), or None if error
        """

        output = []

        response = self._sendHTTPRequest( "/v1/detections?type=person", params )

        if response.status == 200:
            result = response.read()
            jsonResult = json.loads(result)
            objects = jsonResult.get("objects", None)
            for x in objects:
                type = x["type"]
                box = x["boundingBox"]
                boxRect = VitaRect(box["x"], box["y"], box["width"], box["height"])
                output.append((type, boxRect))
            return output
        else:
            self._logger.error("Error %d in detections: reason=%s msg=%s" % (response.status, response.reason, response.msg))
            output = None
        return output


##############################################################################
class ObjectDetectorClientLocal(ObjectDetectorClient):
    """ Local version of the client
    """

    #---------------------------------------------------------------------------
    def __init__(self, id, logger):
        super(ObjectDetectorClientLocal, self).__init__(id, logger)
        self._skippedDetections = 0
        self._analyticsPort = None
        self._nextAnalyticsPort = None

    #---------------------------------------------------------------------------
    def setAnalyticsPort(self, port):
        self._nextAnalyticsPort = port
        self._logger.info("Set detection port to " + str(port) + "; " + str(self._skippedDetections) + " had been skipped")
        self._skippedDetections = 0

    #---------------------------------------------------------------------------
    def _mustRefreshHTTPConnection(self):
        return super(ObjectDetectorClientLocal, self)._mustRefreshHTTPConnection() or \
            self._analyticsPort != self._nextAnalyticsPort

    #---------------------------------------------------------------------------
    def _doRefreshHTTPConnection(self):
        """ Establish HTTP/S connection
        """
        self._httpConn = http.client.HTTPConnection("127.0.0.1", self._nextAnalyticsPort,
                                                timeout=self._httpTimeout)
        self._analyticsPort = self._nextAnalyticsPort

    #---------------------------------------------------------------------------
    def _processSingleImage(self, image):
        """ Invoke HTTP API on a single PIL image object

        @param  image    PIL Image
        @return objects  list of (objectType, objectRectangle)
        """
        output = []

        if self._nextAnalyticsPort is None:
            if self._skippedDetections == 0:
                self._logger.warning("Analytics port isn't set! Not attempting detection.")
            self._skippedDetections += 1
            return output

        binData = image.tobytes()
        self._httpHeaders = { "Content-Length":str(len(binData)), "X-Width":str(image.size[0]), "X-Height":str(image.size[1]) }
        response = self._sendHTTPRequest("/", binData)
        if response.status == 200:
            result = response.read()
            # self._logger.debug("Got output! " + str(result))
            jsonResult = json.loads(result)
            objects = jsonResult.get("objects", None)
            for x in objects:
                type = x["type"]
                box = x["boundingBox"]
                score = x["score"] if "score" in x else 0
                boxRect = VitaRect(box["x"], box["y"], box["width"], box["height"])
                output.append((type, boxRect, score))
            return output
        else:
            self._logger.error("Error %d in detections: reason=%s msg=%s" % (response.status, response.reason, response.msg))
            output = []
        return output

##############################################################################
class ObjectDetectorClientInProcess(ObjectDetectorClient):
    """ In-process version of the client
    """

    #---------------------------------------------------------------------------
    def __init__(self, id, logger):
        from SIOWrapper.SIOWrapper import SIOWrapper
        super(ObjectDetectorClientInProcess, self).__init__(id, logger)
        self._sio = SIOWrapper(self._logger, _getModelDir(), False)
        if self._sio.init() <= 0:
            raise Exception("Failed to init SIO!")

    #---------------------------------------------------------------------------
    def _doRefreshHTTPConnection(self):
        """ Establish HTTP/S connection
        """
        self._httpConn = None

    #---------------------------------------------------------------------------
    def _processSingleImage(self, image):
        """ Invoke HTTP API on a single PIL image object

        @param  image    PIL Image
        @return objects  list of (objectType, objectRectangle)
        """
        w, h = image.size
        idIn = self._sio.addInput(image.tobytes(), w, h)
        if idIn < 0:
            self._logger.error("Failed to submit input for processing")
            return None

        idOut, detections = self._sio.getOutput(True)
        if idOut != idIn:
            # TODO: keep this until we do multi-threaded HTTP request processing
            self._logger.error("Something went terribly wrong")
            return None

        output = []
        objects = detections.get("objects", None)
        for x in objects:
            type = x["type"]
            box = x["boundingBox"]
            score = x["score"] if "score" in x else 0
            boxRect = VitaRect(box["x"], box["y"], box["width"], box["height"])
            output.append((type, boxRect, score))
        return output
