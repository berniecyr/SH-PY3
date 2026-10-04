"""StreamReader implementation using OpenCV for RTSP/camera capture."""

import ctypes
import logging
import os
import queue
import re
import shutil
import socket
import struct
import subprocess
import sys
import threading
import time
import traceback
import numpy as np
from collections import deque
from datetime import datetime

# Never let an ffmpeg child pop its own console window.  A console child
# inherits its parent's console, but ALLOCATES A NEW, VISIBLE ONE when the
# parent has none -- and the back end frequently has none (started detached, or
# by the service).  See videoLib2\python\ClipReader.py for the full note.
_kNoWindow = subprocess.CREATE_NO_WINDOW if sys.platform == 'win32' else 0

try:
    import cv2
    _cv2_available = True
except ImportError:
    _cv2_available = False

try:
    from videoLib2.python.AudioRelay import AudioRingWriter
except Exception:
    AudioRingWriter = None

def _stub(*a, **kw): raise NotImplementedError("videoLib2 not available")

def getLocalCameraNames(*a, **kw): return []
def getHardwareDevicesList(*a, **kw): return []
def getTimestampFlags(*a, **kw): return 0

kPacketCaptureErrCodes = {}

_kClipDurationMs = 2 * 60 * 1000   # 2-minute clip segments
_kCacheStatus_Cache = 1
_kCacheStatus_NonCache = 0

# Gap fill: frames are anchored to real wall-clock time so the archived clip
# keeps its real-world duration regardless of camera fps quirks or dropped
# frames (see _record_frame).  When real time runs ahead of the frames written,
# the clip is padded: short gaps hold the last frame frozen (a brief stutter,
# not distracting); longer gaps show black to visibly mark missing footage.
# A gap larger than the fill cap is treated as a stop/restart — we roll a new
# clip rather than padding a huge black stretch.
_kGapFreezeMaxMs = 500         # <= this: hold last frame (absorbs jitter); above: black screen
_kGapFillMaxMs   = 30 * 1000  # gap beyond this rolls a new clip instead of padding

# FPS auto-measurement: cameras frequently mis-report CAP_PROP_FPS (e.g. a 30fps
# camera reporting 25), which would make the recorder drop or pad frames to hit
# the wrong rate.  For network cameras we ignore the reported value and measure
# the true delivery rate from the first few seconds before recording starts.
_kFpsWarmupMs         = 500    # ignore the initial connect burst
_kFpsMeasureWindowMs  = 2000   # average frame arrivals over this window...
_kFpsMeasureMinFrames = 15     # ...and require at least this many frames
_kFpsMin, _kFpsMax    = 1.0, 60.0   # clamp the measured value to sane bounds

# Rolling window for the published live-view frame rate (see _publish_fps).
# Long enough that a single slow decode doesn't swing it, short enough that a
# camera genuinely falling over shows up well inside the monitor view's ~17.5s
# warning trigger.
_kPublishFpsWindowSecs = 5.0

# How long close() waits for the capture thread to come out of read() before
# leaving it to release the stream on its own.  Short on purpose: the caller is
# usually reconnecting a stalled camera and a dead RTSP read can sit in FFmpeg
# for tens of seconds.  Nothing is leaked by giving up -- the thread still
# releases -- and the generation check keeps it off the new connection.
_kCaptureJoinSecs = 2.0

_kTargetBitrate = '1024k'   # H.264 target bitrate — matches original Py2 behaviour

# --- Capture timestamping (see _stampFrameMs) -------------------------------
# Frames used to be stamped with the wall-clock time they were READ, but a
# frame's content is older than its read time by the whole pipeline latency
# (camera encode + network + decoder buffering + any backlog when the capture
# loop falls behind).  Measured on a real camera that error drifts by over a
# second in seconds, and grows to multiple seconds under load — making the
# program's timeline disagree with the camera's burned-in clock and pushing
# recorded audio ahead of video.  Instead we stamp frames with the camera's
# own PTS (CAP_PROP_POS_MSEC) anchored to the wall clock: the anchor is the
# minimum of (wall - pts) over a sliding window, i.e. the moment we were most
# caught-up.  Backlog then no longer distorts timestamps at all; the residual
# error is only the fixed camera/network latency (a few hundred ms).
_kPtsWindowFrames = 600      # sliding min window (~40s at 15fps)

# ANCHORING ON THE ANALYSIS PATH.  The sliding window above tracks a moving
# offset, which is what a camera whose PTS drifts against the wall clock needs.
# The analysis stream is not that: measured on this fleet, its offset holds to
# 0.01s for a whole segment and steps only when the recorder's ffmpeg respawns.
# Sliding a window across it therefore ADDS noise rather than removing any --
# 0.38s of within-segment jitter where there had been 0.01s, measured live on
# 2026-08-31.  So on that path the anchor is settled once per connection and
# then held until the PTS restart that marks the next one.
#
# The opening frames are skipped: they were buffered before we attached and
# arrive in a burst, so their (wall - pts) is far below the steady-state
# latency and would anchor the whole connection too early.  This is the same
# hazard _measureFps avoids with _kFpsWarmupMs, for the same reason.
#
# 1.5s of warmup and a plain min() were not enough.  Measured 2026-08-31 with a
# walking subject: within one connection the frozen anchor held to 0.01s, but
# BETWEEN connections it stepped by 1-2.4s, and the step size tracked how
# unsettled the stream still was while the anchor was being taken -- 542ms of
# variation on 08_FrontStep_hr (which landed well) against 4.7-5.2s on
# 09_Jungle (which landed 1s worse than before the change).  min() is the
# statistic most exposed to that: one unusually prompt frame moves it, and it
# can only ever be biased one way, stamping the whole connection early.
#
# So: skip more of the startup, judge the anchor on a LOW PERCENTILE rather
# than the extreme, and refuse to freeze while the window is still obviously
# turbulent -- but freeze anyway rather than never settling, because a
# provisional anchor keeps moving and that is worse than a mediocre fixed one.
_kPtsAnchorWarmupMs = 4000      # ignore the connect burst and the fill after it
_kPtsAnchorSettleFrames = 200   # offsets considered for the anchor
_kPtsAnchorPercentile = 0.10    # near the caught-up end, but not the extreme
# What "steady" means, measured rather than guessed: with the 4s warmup above
# excluding the startup fill, every camera on this fleet reports a p10-p90
# offset spread of 504-689 ms (2026-08-31, 15 streams).  That is the genuine
# frame-to-frame jitter of the socket path and it does not go away, so a
# threshold below it can never be met -- the first attempt used 400 ms and
# every camera fell through to the cap, spending ~40s of each 60-120s
# connection on a provisional, still-moving anchor.  1000 ms admits normal
# jitter while still refusing the 4-10s turbulence that the pre-warmup windows
# showed, which is all this gate is now for.
_kPtsAnchorCalmMs = 1000        # p10..p90 spread that counts as steady
_kPtsAnchorMaxFrames = 600      # stop waiting for calm and settle regardless
_kPtsMaxLagMs     = 30000    # stamped time this far behind wall = pathological, reset
_kPtsBackJumpMs   = 5000     # pts moving backward by this much = stream restarted
_kPtsStaleLimit   = 50       # this many identical pts values = pts is dead, use wall

# Stream-copy recorder tuning.  Segments are 60s (vs the legacy 2-min clips)
# so footage becomes searchable quickly WITHOUT on-demand cuts: cutting means
# cycling the recorder ffmpeg, which costs ~1s of footage and fragments the
# archive — and searches of recent time flush EVERY searched camera
# (SearchUtils), so uncontrolled cuts fragment everything.  flush() is
# therefore heavily debounced and skipped entirely when the current segment
# is young (the previous segment already satisfies "recent footage").
# A hole in the camera's delivery at least this long is an OUTAGE, not the
# normal ~0.4-0.5s burst jitter, and gets black frames in the archive rather
# than being smoothed away.  Smearing the surviving frames evenly (what the
# constant-rate re-stamp does on its own) hides the outage completely: the
# clip plays smoothly while the camera's burned-in clock jumps forward, so
# footage looks continuous when seconds or minutes of it are simply missing.
#
# 2.0s is measured, not guessed: brief LAN hiccups on this network drop
# 1.1-1.5s simultaneously across several cameras and recover on the next
# segment (verified in RAW segments, before any of our processing), while the
# outages worth seeing run 3s to 24s.  Blacking the blips would re-encode
# nearly every segment on 15 cameras for ~2% of missing footage.  Lower this
# if you want even the blips marked.
_kGapBlackMinSecs = 2.0

# A segment cannot REPRESENT more elapsed time than the wall clock during
# which its file was written.  The segment muxer opens the next file the
# instant the next packet arrives, so (next file's strftime name - this one's)
# is a hard ceiling on this segment's span.
#
# Cameras violate it constantly, because the ceiling is measured against OUR
# clock and the span comes from THEIR packet timestamps: 01_South_Gate
# 2026-08-08 09:23:42 reported a 44.8s outage inside a file that was written
# in 15s.  No camera can go silent for 45s inside a 15s window -- its PTS
# jumped.  We believed it, black-filled 44.8s of footage that never existed,
# registered a 58.2s span, and the next segment then overlapped it by 43.2s.
# That single mechanism produced 64% of the main-vs-main overlaps measured.
#
# Slack is generous because the ceiling itself is only as good as the moment
# ffmpeg got round to creating the next file; only clear violations are
# rejected.
_kSegSpanSlackSecs = 5.0

_kRemuxSegmentSecs        = 60

# Segment length used after a run died before completing a segment.  A restart
# discards the in-progress segment, so this caps that loss at ~15s instead of a
# full minute on cameras that keep dropping.  Since segments became fragmented
# (see _kFragmentSecs) a killed segment is no longer a total loss, so this is
# now belt-and-braces rather than the main defence -- kept because short
# segments also bound how much a genuinely CORRUPT tail can cost.
_kRemuxShortSegmentSecs   = 15

# How long a run must stay healthy before SHORT segments are handed back to
# normal length.  Segment length is fixed when ffmpeg spawns, so the verdict
# reached at one run's death otherwise governs the whole of the next run -- and
# on a GPU-less machine the FIRST run always dies in ~3s probing the hardware
# decoder, so a recorder that then runs perfectly for days stayed on 15s
# segments for all of it (Hyper-V VM, 2026-08-14: 45 min and counting off a
# single 3s failure).  A camera that has recorded this long without dying is
# not the "keeps dropping" case short segments exist for, so promote it: clear
# the flag and cycle ffmpeg through the FLUSH path, which closes the current
# segment gracefully and costs the same ~1s a search-driven flush does.
# Generous, because the cycle is only free-ish and re-testing a genuinely
# marginal camera should be rare: at worst such a camera pays ~1s per interval
# to retry a normal segment, and drops straight back to SHORT when the attempt
# dies early.
_kRemuxPromoteAfterSecs   = 5 * _kRemuxSegmentSecs

# How often the in-flight segment closes a fragment.  Each fragment makes what
# came before it independently readable, so this is exactly how much footage a
# kill can destroy.  Cheap: one moof header per second per track, against
# ~700 KB/s of 4K video.  Do NOT raise it to the GOP length -- these cameras
# use smart encoding and were measured at 4 keyframes in a 47s segment.
_kFragmentSecs            = 1.0

# Where a segment goes when it cannot be read.  A SUBDIRECTORY of the segment
# dir on purpose: _listSegments only looks at files, so nothing in here can be
# picked up as a clip again, and the bytes stay available to
# tools/recover_stub.py instead of being deleted.
_kBrokenSubdir            = '_broken'
# ...but not forever.  Long enough to notice and investigate, short enough that
# a camera stuck in a reconnect loop cannot fill the disk with stubs.
_kBrokenKeepSecs          = 24 * 3600

# The recorder's ffmpeg can stay ALIVE while producing nothing -- connected to a
# camera that has stopped sending -- and the supervise loop below only ends when
# the process exits, so nothing noticed.  Measured on 08_FrontStep 2026-08-01:
# holes of 23, 11 and 17 minutes with no segment at all, while every other
# camera recorded normally; one only ended because the app happened to restart.
# If no segment has completed in this long, the recorder is wedged: restart it,
# and log the hole so the missing time is visible rather than silent.
_kRecorderStallSecs = 3 * _kRemuxSegmentSecs

# How long stop() waits for the supervise thread before killing ffmpeg itself.
# It used to wait 45s and then return REGARDLESS, which is how orphaned
# recorders came to squat camera sessions.  Short, because the only work worth
# waiting for is closing the current segment; anything left unswept is
# recovered by the next start() ("recovered N unswept segment(s)").
_kRecorderStopJoinSecs = 8.0

# How long the supervise loop waits for a graceful 'q' to close the current
# segment.  Must stay UNDER _kRecorderStopJoinSecs, or stop()'s join expires
# first and the process has to be killed from there instead.
_kRecorderQuitWaitSecs = 4.0

# How long a recorder may be between ffmpeg runs and still count as "running"
# (see _RemuxRecorder.isRunning).  Longer than the reconnect backoff ceiling so
# an ordinary respawn is not mistaken for a dead recorder, but short enough
# that a supervise thread stuck in a slow closing sweep is replaced rather than
# re-used -- which is how 08_FrontStep recorded nothing for half an hour while
# live view looked healthy.
_kRecorderRespawnGraceSecs = 45.0

# Time budget for the sweep that runs while a recorder is shutting down.
# Finalizing can re-timestamp and even re-encode gap fills, which on a 4K
# segment takes seconds -- long enough to push stop() past its join and orphan
# the ffmpeg.  Whatever is not finalized in time is left on disk and picked up
# by the next start(), so overrunning costs nothing but a short delay.
_kClosingSweepBudgetSecs = 4.0

# Analysis frames have stopped while ffmpeg is still alive and segments are
# still landing: the recording half is demonstrably fine, so respawn ffmpeg
# (~1s) instead of letting CameraCapture's 15s stream timeout tear down the
# whole camera.  Must stay comfortably under that 15s.
_kAnalysisStallSecs = 7.0
# ...but only while the recorder is provably working, i.e. a segment reached
# the archive this recently.  Otherwise the camera itself is down and the
# normal reconnect/backoff path owns the problem.
_kAnalysisStallNeedsArchiveSecs = 90.0

# Ceiling for the gap fill's output frame rate.  Every camera in this fleet is
# 25fps or below, so nothing above this can carry real detail -- it only makes
# the re-encode slower and the file bigger.
_kGapFillMaxFps = 30.0

# The black fill re-encodes a whole segment, and on a 4K camera that can take
# MINUTES.  It used to get a flat 300s, so a run of gappy segments put the
# finalize sweep further and further behind -- 08_FrontStep 2026-08-07 was
# finalizing 18:35-18:40 segments at 19:11, a ~30 minute backlog, with the
# supervise thread stuck in it.
#
# The bound is a rate argument, not a guess: the recorder produces 60s of
# footage every 60s, so any per-segment finalize costing more than the segment
# covers can never catch up.  Give a fill a FRACTION of its own span; if it
# overruns, abandon it (the segment survives untouched -- best effort, as
# everywhere else in this class) and register the real, shorter length instead.
# The outage is then represented as missing time rather than black frames,
# which is also the more honest thing for a coverage measurement.
_kGapFillTimeoutFrac = 0.5
_kGapFillMinTimeoutSecs = 15.0

# ...and if fills keep overrunning on a camera, stop trying: this stream is
# simply too heavy to gap-fill on this machine, and burning the timeout on
# every segment is the backlog all over again, just smaller.
_kGapFillMaxSlowRuns = 2
# Socket-I/O timeout for the recorder's RTSP connection, in seconds.  ffmpeg
# exits with ETIMEDOUT (logged as "Error number -138") when no data arrives for
# this long, and the supervise loop then reconnects -- throwing away the
# in-progress segment.
#
# It was 5s, which is far too eager for this fleet.  Measured 2026-08-04 across
# ~11,000 frame gaps that recovered with NO reconnect at all: 2,948 were longer
# than 5s, 705 longer than 8s, 163 longer than 12s, and the longest
# self-healing stall was 34.8s.  So the old value was killing streams that
# would have come back on their own -- 100 demux timeouts on 12_Ravine alone in
# one 4-hour window, plus 83 on X_Bedroom and 51 on 09_West_Terrace.
#
# 15s covers all but ~1.5% of the self-healing stalls.  NOTE this same option
# is also the TCP CONNECT timeout (it appears as "?timeout=" on the tcp:// URL),
# so a genuinely dead camera now takes 15s rather than 5s to fail a connect --
# acceptable because CameraCapture backs off between reconnects and the 180s
# stall watchdog still catches a wedged recorder.
_kRemuxSocketTimeoutSecs = 15

_kRemuxFlushDebounceSecs  = 60.0

# previsouly set at 10.0 seconds this avoided cutting a clip shorter than 10 seconds
# due to a detection so that save type events can trigger faster
# such as send an email,  save a snapshot, and even push notification 
# (although I think that has been stubbed and removed) 
# there is no reason to trigger those events so quickly. 
# other events such as ihost, ifttt, tapo, all happen as they are recorded.
_kRemuxMinSegAgeForCutSecs = 60.0

# Non-actionable ffmpeg stderr chatter to drop from the per-camera remux log.
# We record with -c:v copy (no decode), so h264 decoder-level gripes about a
# quirky camera bitstream (malformed SEI, start-of-stream ref/PPS complaints)
# have zero effect on the recorded file -- but a single quirky camera can emit
# them every GOP and bloat its log 50x (observed: 06_Garage "SEI type ...
# truncated").  These are all well-known-benign; genuine failures (open/mux/
# permission errors, "does not contain any stream") are NOT listed and still
# get logged.
_kBenignRemuxNoise = (
    'non monotonically increasing dts',   # raw-PCM tee timestamp spam
    'SEI type',                           # "SEI type NNN size NN truncated at NN"
    'non-existing PPS',                   # benign at (re)connect / GOP boundary
    'Could not find ref with POC',        # benign at stream start
    'co located POCs unavailable',        # benign at stream start
    'mmco: unref short failure',          # benign reference-management chatter
    # Teardown of the single-stream ANALYSIS output.  Closing that socket is
    # how the camera is stopped, so ffmpeg always notices its reader vanished
    # and reports it six ways ("Error submitting a packet to the muxer",
    # "Error writing trailer", WSAECONNABORTED -10053...) on every close and
    # every reconnect.  None of it touches the archive -- the segment muxer is
    # out#0 and never says "rawvideo".  This output's real health is the
    # delivered-frame count, not its stderr, and the decode-fault hints below
    # are matched BEFORE this filter so a genuine decoder failure still shows.
    '/rawvideo @',
)

# stderr signatures that indict the ANALYSIS DECODER for this camera rather
# than the camera itself -- observed live on 08_FrontStep, whose stream kills
# ffmpeg's h264 parser under hardware decode while recording (stream-copy) is
# perfectly fine.  Seeing one of these with zero analysis frames delivered is
# what drops the decode rung; see _RemuxRecorder._rungs.
_kDecodeFaultHints = (
    'Error initializing filters',
    'Error setting option video_size',
    'missing picture in access unit',   # decoder can't parse this bitstream
    'Device creation failed',
    'hwaccel initialisation returned error',
    'Impossible to convert between the formats',
    'Function not implemented',
    # No usable GPU at all -- a machine with no NVIDIA card or driver.  Seen on
    # a Hyper-V VM 2026-08-14: every run failed here and the camera never
    # displayed, because the rung below vetoed the demotion to software.
    'Cannot load nvcuda.dll',
    'Could not dynamically load CUDA',
    'No device available for decoder',
    'Hardware device setup failed for decoder',
)

# ...but a camera that is simply UNREACHABLE also produces no analysis frames,
# and on a flaky stream can emit decoder complaints on the way down.  Demoting
# then would cost GPU memory and CPU for the life of the process for no
# reason, so a run that failed to connect never indicts the decoder.
# ('no frame!' is deliberately NOT a fault hint above: X_Bedroom emits it
# routinely over wifi while decoding perfectly well.)
#
# 'Operation not permitted' is deliberately NOT listed on its own.  It is only
# ffmpeg's rendering of EPERM, which it also emits for a failed HARDWARE DEVICE
# setup -- "Hardware device setup failed for decoder: Operation not permitted",
# "Error opening output files: Operation not permitted".  Matching it bare made
# every GPU-less machine set _connect_failed on a purely local decoder failure,
# which vetoed the demotion to software decode: the camera retried nvdec
# forever and never displayed (Hyper-V VM, 2026-08-14 -- 30 identical attempts
# in 9 minutes).  A genuine unreachable camera still matches, because ffmpeg
# prefixes those with the input error: "Error opening input: Operation not
# permitted" / "Error opening input files: Operation not permitted", both of
# which contain 'Error opening input' below.
_kConnectFailHints = (
    'Error opening input',
    'Connection refused',
    'Connection timed out',
    'No route to host',
    'Server returned 4',                # 401/403/404 on the RTSP setup
    'Immediate exit requested',
)

# Full-res snapshot ring: keep this many seconds of ~1/s keyframe JPEGs so
# the detector can match a snapshot to the ANALYZED frame's timestamp even
# when detection runs many seconds behind capture.
_kSnapshotRingSecs = 40.0

# ---------------------------------------------------------------------------
# Single main-stream connection per camera (reliability redesign, 2026-08-03;
# the two-session shape and its opt-out were removed 2026-08-16).
#
# The two-session shape -- recorder stream-copying the MAIN stream, analysis
# decoding the SUBSTREAM -- let the two halves disagree: the substream could
# keep delivering detections, attributes and thumbnails through a window in
# which the main stream recorded nothing.  That is the "detection with no
# footage" the user keeps hitting.  With ONE connection the halves share fate:
# analysis frames exist only while a recording ffmpeg exists, so a detection
# without footage becomes impossible by construction.  It also halves RTSP
# sessions per camera and removes the substream-URI guessing.
#
# Measured 2026-08-03 before committing to this (app stopped, real cameras):
#   * `-c:v copy` segments are BYTE-IDENTICAL with a decode output attached --
#     1295 packets, same per-packet md5, same file bytes as a copy-only run.
#   * 15 concurrent NVDEC sessions cost 1409 MiB; co-resident with the warm
#     DetectionService (939 MiB) the combined peak was 2321 of 4096 MiB with
#     every camera at 98-100% of its frame rate.
#   * ffmpeg CPU for all 15 was 2.81 cores against a 5.48-core whole-app
#     baseline: decoding 7x the pixels costs LESS, because the GPU absorbs it.
#
# THE ONLY SHAPE (2026-08-16)
# This ran behind SV_SINGLE_STREAM for two weeks, reached 100% coverage on
# every camera at lower CPU, and became the default.  The switch and the
# two-session code behind it are now gone: an experiment nobody would choose
# was only a way to run the product in a configuration that is worse in every
# measured respect.  The substream gap-fill archive and the cross-stream delta
# correction went with it -- both existed solely to reconcile two timelines
# that no longer exist, and at 100% coverage there are no holes left to fill.
#
# Webcams and local files still decode their own capture: they have no
# recorder, so there is nothing for analysis to share.  That is the `single`
# flag's only remaining job.

# Analysis frames are capped to this width (aspect preserved).  Same cap the
# two-session path applied after decoding; here the GPU does it BEFORE the
# host download, so the pixels above it are never paid for at all.
#
# KNOWN CONSEQUENCE: the live-view mmap is resized from this frame, and in the
# two-session shape it was resized from the raw substream instead -- which on
# the four cameras whose substream is 720p/960p meant a sharper enlarged view
# than 640 wide can give.  Detection is unaffected (it was already capped at
# 640 either way).  The fix, if that view matters, is a THIRD scaled output
# from the same ffmpeg at live-view size -- NOT raising this cap to follow the
# live view, which is what made the old NVDEC capture re-open in a loop and
# got it disabled in July.
_kAnalysisMaxWidth = 640


# How long a read on the PTS transport may block with no frame arriving.  It
# bounds shutdown: the pump thread owns the capture and can only tear it down
# between reads, and until it does, the camera's port stays taken.  Generous
# against these cameras (5-25 fps, delivered in ~500ms bursts) but far below
# the 45s connect wait -- a ten-second gap in analysis frames is a dead stream,
# and treating it as one just sends the pump back to waiting for a reconnect,
# which is recoverable.
_kAnalysisReadTimeoutSecs = 10.0

# How long the analysis reader waits for the recorder's ffmpeg to (re)connect.
# The recorder deliberately cycles ffmpeg -- flush, watchdog, stream drop --
# and the reader must ride through that rather than tearing the camera down:
# a respawn is ~1s, a camera-process reconnect is tens of seconds.  Only when
# nothing connects for this long is the stream genuinely gone.
_kAnalysisConnectWaitSecs = 45.0

# Cap the full-res snapshot ring's width (0 = the camera's native size).  The
# ring feeds face/nudity analysis, whose whole benefit is having MORE pixels
# than the 640-wide analysis frame -- 08_Pool matched a face from a 456x1190
# crop taken from a 2560x1440 snapshot.  Lower this only if GPU memory gets
# tight; anything above _kAnalysisMaxWidth still beats the analysis frame.
_kSnapshotMaxWidth = 0

# Snapshot ring cadence, in frames per second.  The old ring emitted one file
# per KEYFRAME because the decoder ran `-skip_frame nokey` -- a fixed rate
# there would have duplicated the last I-frame into files with fresh mtimes,
# forging timestamps onto stale content (the bug that broke the fullres face
# path in July).  Under single-stream every frame is genuinely decoded, so a
# fixed rate emits real, current pixels and mtime == content time still holds.
# A fixed rate is also STEADIER than keyframes: this fleet's cameras use smart
# encoding with GOPs up to ~10s when a scene is static.
_kSnapshotFps = 1.0

# Cameras whose stream is this wide or narrower decode in SOFTWARE.
#
# Counter-intuitive but measured across the whole fleet (14 cameras, real
# streams, 2026-08-03).  A hardware session is not free: it carries a CUDA
# context plus the CUDA frame pools the per-output scalers need, ~300 MiB
# each, and a fixed CPU overhead of its own.  Below roughly 1080p, software
# decode is cheaper on BOTH counts:
#
#   threshold   gpu sessions   VRAM        ffmpeg CPU
#   640                   11   2380 MiB    2.46 cores
#   1280                   9   2129 MiB    1.86 cores
#   1920                   4   1226 MiB    2.08 cores
#
# So only the genuinely large cameras (4K, 2688x1520, 2560x1440) use the GPU.
# That leaves ~1.7 GB of the 4 GB card free -- which matters because the front
# end spawns its OWN NVDEC decoder for clip playback, and at 11 sessions there
# was nothing left for it (measured: 37 MiB free, and a 15th session could not
# be created at all).  Raise this only if CPU becomes the binding constraint.
_kSoftwareDecodeMaxWidth = 1920 # removing the cap 2026-08-13 for testing.
# _kSoftwareDecodeMaxWidth = 3840


# Per-codec NVDEC decoders.  The generic `-hwaccel cuda` path works without
# knowing the codec, but costs 2-2.6x the GPU memory per session (measured
# 2026-08-03: 4K 533 vs 207 MiB, 1440p 303 vs 147) -- and with 14 cameras that
# is the difference between fitting on the 4 GB card and filling it.  So name
# the decoder when we know it, and fall back to the generic path when we
# don't or when it fails.  Getting this wrong is not subtle: h264_cuvid
# pointed at an HEVC stream dies with "missing picture in access unit" /
# "no frame!", which is exactly what 08_FrontStep did during the spike.
_kCuvidByCodec = {
    'h264':  'h264_cuvid',
    'hevc':  'hevc_cuvid',
    'h265':  'hevc_cuvid',
    'av1':   'av1_cuvid',
    'vp9':   'vp9_cuvid',
    'vp8':   'vp8_cuvid',
    'mjpeg': 'mjpeg_cuvid',
}

# Fixed video pipeline latency (ms) subtracted from frame timestamps.  The
# PTS anchor removes all VARIABLE latency (backlog), but the fixed part —
# camera encoder buffering + network + demuxer — is invisible to it: stamps
# end up "capture time + fixed latency".  That fixed part is what remains of
# any difference between the program's timeline and the camera's burned-in
# OSD clock.  Calibrate once per install with the SV_VIDEO_LATENCY_MS env
# var: if the program timeline reads N seconds AHEAD of the camera's burned-
# in clock in saved video, set SV_VIDEO_LATENCY_MS to about N*1000.  This
# also delays recorded audio by the same amount automatically (the audio
# offset is computed against these stamps), keeping A/V in step.
_kDefaultVideoLatencyMs = 0

# ANALYSIS LAG PROBE -- measurement only, applies no correction.
#
# Detection boxes are stamped when the analysis frame arrives over the loopback
# socket, while the archive is dated from the recorder's segment filenames,
# written at DEMUX.  Both outputs come from the SAME ffmpeg on substream
# cameras, so the gap between them is not connection latency -- it is how far
# the decode/scale/hwdownload/socket path trails the demux.  Measured against a
# walking subject on 07_Back_Yard it was ~2s, which is invisible on a
# stationary subject and displaces a walker by more than their own width.
#
# The two are comparable by FRAME COUNT: the sub recorder's segments hold every
# demuxed frame, and _AnalysisFrameSource counts every frame delivered, so
# (demuxed - delivered) / fps is the lag in seconds with no clock, no vision
# and no calibration.  Counting is only valid within one analysis connection,
# so the totals reset whenever the recorder's ffmpeg respawns.
#
# OFF by default because it costs one extra ffmpeg probe per sub segment (20s
# per camera), which is real money on this machine -- see the finalize-cost
# notes.  Turn it on for a few minutes with SV_ANALYSIS_LAG_PROBE=1, read the
# per-camera numbers out of the camera logs, then turn it off again.
_kDefaultAnalysisLagProbe = 0
# Don't log on every segment; once per this many seconds per camera is plenty
# to build a per-camera picture without flooding the log.
_kAnalysisLagLogSecs = 60.0
# The probe reads the socket counter at wall times it chooses, then interpolates
# back to a segment's OPEN instant, because _sweep can only notice a segment on
# the tick AFTER it appeared -- up to a whole sweep interval late, which at
# 15 fps is tens of frames, the same size as the lag being measured.  This many
# (wall_ms, frames) samples at ~1-3s per sweep is several minutes of history,
# far more than the one sweep back any lookup needs.
_kLagSampleRing = 256

# APPLY the probe's measurement to the analysis timestamps, per camera, live.
#
# Validated 2026-08-08 against drawbox.py on a walking subject: the probe's
# per-camera figure IS the shift that makes the detection box land on the
# subject -- 07_Back_Yard 1.81s/-1.8, 06_Garage 0.9s/-0.9 (and -1.8 visibly
# overshoots it), 08_FrontStep 0.88s/-0.9.  09_West_Terrace, which has no
# substream and so no socket hop, needs ZERO and gets zero because no sub
# recorder exists to report a lag for it.
#
# DEFAULT OFF SINCE 2026-08-09: IT FAILED ITS OWN VALIDATION WALK.  Left in
# place, and still switchable with SV_ANALYSIS_LAG_CORRECT=1, because the
# machinery is sound -- the ESTIMATOR feeding it is not:
#
#     (demuxed - delivered) = delay*fps + LOST
#           probe_seconds   = true_delay + LOST/fps
#
# A frame the socket never received is indistinguishable, BY COUNTING ALONE,
# from a frame still in flight, and LOST only grows.  So the estimate creeps up
# with run age: 07_Back_Yard measured +1.81s on a fresh run (drawbox agreed at
# -1.8) and +3.21s after 10 hours, and on the 06:00 walk its box landed at
# +1.4 -- over-corrected by the drift.  11_Fire Pit shows the floor of it,
# reading ~58 frames behind at demuxed=300 on a FRESH run, because frames are
# demuxed before the analysis socket finishes connecting.
#
# Re-anchoring more often does NOT fix this: mid-run it measures the CHANGE in
# backlog, so a camera at a steady real delay would report ~0.  The absolute
# value needs a known zero-backlog instant, and the only one is a respawn --
# which is exactly where the startup loss happens.  Anything better has to
# measure the LOSS independently, or drop frame-counting for a time-domain
# match.  Enabling this forces the lag probe on, since it is the input.
_kDefaultAnalysisLagCorrect = 0
# Never move a stamp further than the pipeline could plausibly hold.  Measured
# backlogs are 0.9-1.9s fleet-wide; the lossy cameras' clip-DATING errors (11s
# on 09_Jungle, 21s on 02_BigTree) are a SEPARATE defect and this must not try
# to absorb them.
_kMaxLagCorrectionMs = 4000
# Median over this many recent per-segment estimates.  One segment can step on
# a transient, so three tracks a real sustained change within ~40s while
# rejecting a lone outlier.
_kLagCorrectionWindow = 3
# A segment's measured rate must look like a real camera before the seconds
# derived from it are trusted.  These cameras run 11-25 fps; anything outside
# this came from a bad duration probe on a gappy segment.
_kMinPlausibleFps = 5.0
_kMaxPlausibleFps = 32.0

# Feed the analysis output every demuxed frame exactly once, instead of letting
# ffmpeg conform the rawvideo stream to the input's ADVERTISED frame rate.  See
# the -fps_mode passthrough comment in _spawn for what the default costs.
# SV_ANALYSIS_PASSTHROUGH=0 restores the old CFR behaviour.
_kDefaultAnalysisPassthrough = 1

# CARRY THE FRAME'S OWN TIMESTAMP ON THE ANALYSIS STREAM.
#
# Headerless `rawvideo` carries pixels and nothing else, so an analysis frame
# had no time of its own and could only be stamped when it came off the socket
# -- after decode, scale, hwdownload and the socket itself.  The archive is
# dated at demux.  The two timelines therefore disagreed by whatever backlog
# sat in between: measured -0.72s to -2.25s per camera across this fleet on
# 2026-08-31 (scripts/measure_analysis_skew.py), which is enough to walk a
# subject clean out of their own detection box.
#
# Muxing the same frames into NUT gives every frame its real PTS for 0.01% more
# bytes, and _stampFrameMs already knows what to do with a PTS: its anchor is
# the sliding minimum of (wall - pts), so a frame held up by backlog gets a
# correspondingly older -- correct -- stamp.  Nothing is estimated and nothing
# needs calibrating; a frame delayed three seconds still reports the moment it
# was captured.  The PTS restart at every recorder respawn is already handled
# (_kPtsBackJumpMs), as is a source whose PTS never advances (_kPtsStaleLimit).
#
# The two sides must agree, so ONE flag picks the transport for both the ffmpeg
# output in _spawn and the reader in _AnalysisFrameSource.  DEFAULT OFF for its
# first outing: this is the recording path on a live fleet, and the codebase's
# habit with transport changes (SV_NVDEC, SV_HW_DECODE) is to prove them behind
# a switch first.  Turn on with SV_ANALYSIS_PTS=1 and confirm with
# scripts/measure_analysis_skew.py that the residual skew is ~0.
_kDefaultAnalysisPts = 0


def _analysisPtsEnabled():
    """Whether the analysis stream carries per-frame timestamps.

    THREE places need the same answer -- the recorder builds the ffmpeg output,
    _AnalysisFrameSource demuxes it, and StreamReader decides whether to trust
    the PTS it gets -- so it is read here once instead of being copied into each
    of them.  (It was briefly defined on one class and read from another, which
    took every camera down with an AttributeError.)
    """
    try:
        return bool(int(os.environ.get('SV_ANALYSIS_PTS',
                                       str(_kDefaultAnalysisPts))))
    except Exception:
        return bool(_kDefaultAnalysisPts)

# Hardware (GPU) video decode for the analysis/live-view stream via D3D11VA.
# Offloads the per-camera H.264 software decode to the GPU's video engine;
# frames still arrive as normal host BGR Mats, so the rest of the pipeline is
# unchanged.  Each camera validates HW decode with a probe read and falls back
# to software automatically.  Disable globally with SV_HW_DECODE=0.
_kDefaultHwDecode = 1

# NVDEC pipe decode for the analysis/live stream: decodes AND downscales on the
# GPU so the host only ever receives the small analysis-sized frame.
#
# DEFAULT OFF (2026-07-26).  It is ~2.8x cheaper per frame in isolation, but in
# production it destabilised camera connections: the probe opens an EXTRA RTSP
# session (these cameras are session-limited, and two probes timed out), and
# re-opening to follow the live-view size fought the camera-restart cycle,
# which resets the live size -- an endless reconnect loop when a camera was
# selected.  Its aggregate CPU win was also unproven once the child ffmpeg
# processes were counted.  Enable with SV_NVDEC=1 for experiments only.
_kDefaultNvdec = 0

# Sentinel pushed onto the record queue to finalize the current clip on demand
# (when a snapshot/export response needs the recorded video right away), without
# stopping recording.  Distinct from None, which tells the writer thread to exit.
_kFlushSentinel = object()
# Minimum wall-clock seconds between on-demand finalizes, so several responses
# firing for one event don't fragment the archive into many tiny clips.
_kFlushDebounceSecs = 2.0

def _probe_duration_ms(path):
    """Return a media file's duration in ms by parsing `ffmpeg -i` output,
    or None if it can't be determined (imageio-ffmpeg bundles no ffprobe).
    """
    try:
        proc = subprocess.run(
            [_get_ffmpeg(), '-hide_banner', '-i', path],
            capture_output=True, timeout=15,
            creationflags=_kNoWindow)
        text = proc.stderr.decode('utf-8', 'replace')
        for line in text.splitlines():
            line = line.strip()
            if line.startswith('Duration:'):
                stamp = line.split('Duration:', 1)[1].split(',', 1)[0].strip()
                if stamp.startswith('N/A'):
                    return None
                hh, mm, rest = stamp.split(':')
                return int((int(hh) * 3600 + int(mm) * 60 + float(rest)) * 1000)
    except Exception:
        pass
    return None


def _get_ffmpeg():
    """Return path to ffmpeg binary, preferring imageio-ffmpeg's bundled copy.

    See ClipUtils._get_ffmpeg: an installed build uses a Sighthound-named copy
    of the same binary.
    """
    try:
        from appCommon.InstallPaths import getFfmpegExe
        return getFfmpegExe()
    except Exception:
        pass
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return 'ffmpeg'


class _AudioCapture:
    """Persistent per-camera audio capture using ONE RTSP session.

    Decodes the camera's audio to PCM continuously and fans it out to:
      * a shared-memory ring buffer (AudioRingWriter) that the front-end live
        view reads — so live audio needs NO extra camera session (many cameras
        cap at 2 concurrent RTSP sessions, already used by the backend video
        capture and this audio capture), and
      * an optional per-clip sink file the recorder muxes into archived clips.

    One continuous session (vs the old per-clip reconnect) avoids the session
    spikes that intermittently hit the camera's connection limit and produced
    choppy / missing recorded audio.  It reconnects automatically on drops.
    """

    def __init__(self, uri, force_tcp, is_network, ring_path, location=''):
        self._uri         = uri
        self._force_tcp   = force_tcp
        self._is_network  = is_network
        self._ring_path   = ring_path
        self._location    = location or ''
        self._stop        = threading.Event()
        self._thread      = None
        self._proc        = None
        self._lock        = threading.Lock()
        self._writer      = None      # AudioRingWriter (live relay)
        self._sink        = None      # open per-clip PCM file (recording)
        self._sink_path   = None
        self._clip_first_ms  = 0.0   # clip_first_ms from most recent begin_clip
        self._first_write_ms = None  # wall time of first PCM byte written to current sink
        # Prefer the camera's substream for audio: it carries the same audio at
        # lower bandwidth and is served as a SEPARATE RTSP session, so cameras
        # that allow only one session per stream (e.g. some TP-Link models) can
        # still provide audio alongside the video session OpenCV holds on the
        # main stream.  We fall back to the main URI on failure.
        self._uris        = self._audioUriCandidates(uri)
        self._uri_idx     = 0

    @staticmethod
    def _audioUriCandidates(uri):
        if not uri:
            return []
        cands = []
        # Common main->sub stream path conventions across camera vendors.
        for a, b in (('stream1', 'stream2'),
                     ('/h264Preview_01_main', '/h264Preview_01_sub'),
                     ('/Streaming/Channels/101', '/Streaming/Channels/102'),
                     ('subtype=0', 'subtype=1'),
                     ('/ch01/0', '/ch01/1')):
            if a in uri:
                sub = uri.replace(a, b)
                if sub != uri:
                    cands.append(sub)
                break
        cands.append(uri)
        return cands

    def start(self):
        if not self._uris:
            return False
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        return True

    def _log(self, msg):
        # Log clean text straight to the per-camera Python logger (registered as
        # "<location>.log").  We avoid getCLogFn()'s ctypes (int, c_char_p)
        # callback, which forced bytes and rendered as b'...' in the logs.
        try:
            logging.getLogger(self._location + '.log').info("audio-capture: " + msg)
        except Exception:
            pass

    def _spawn(self):
        uri = self._uris[self._uri_idx % len(self._uris)]
        cmd = [_get_ffmpeg(), '-loglevel', 'error', '-fflags', 'nobuffer']
        if self._is_network:
            if self._force_tcp:
                cmd += ['-rtsp_transport', 'tcp']
            # Bound socket I/O so a stalled session fails fast and we reconnect
            # rather than hanging (5s, in microseconds).  NOTE: this ffmpeg build
            # rejects -rw_timeout ("Option not found"); the rtsp socket option is
            # -timeout.
            cmd += ['-timeout', '5000000']
        # Pull the full stream but keep only audio (-vn).  We deliberately do NOT
        # use -allowed_media_types audio: on some cameras it turns a rejected
        # session into a hang.  Receiving (and discarding) the video RTP is the
        # reliable behaviour and still just ONE RTSP session.
        cmd += ['-i', uri, '-vn',
                '-f', 's16le', '-acodec', 'pcm_s16le', '-ac', '2', '-ar', '44100',
                'pipe:1']
        self._log("starting (%s): %s" % (uri, ' '.join(cmd)))
        return subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE,
                                creationflags=_kNoWindow)

    def _drain_stderr(self, proc):
        try:
            for line in iter(proc.stderr.readline, b''):
                if not line:
                    break
                s = line.decode('utf-8', 'replace').rstrip()
                # Drop the harmless raw-PCM muxer timestamp spam; keep real
                # errors (connection refused, EPERM, etc).  Must still drain the
                # pipe so ffmpeg doesn't block.
                if not s or 'non monotonically increasing dts' in s:
                    continue
                self._log("ffmpeg: " + s)
        except Exception:
            pass

    def _run(self):
        if AudioRingWriter is not None and self._ring_path:
            try:
                self._writer = AudioRingWriter(self._ring_path, 44100, 2)
            except Exception as e:
                self._log("ring writer create failed: %r" % e)
                self._writer = None
        backoff = 0.5
        while not self._stop.is_set():
            try:
                proc = self._spawn()
            except Exception as e:
                self._log("spawn failed: %r" % e)
                if self._stop.wait(backoff):
                    break
                continue
            with self._lock:
                self._proc = proc
            threading.Thread(target=self._drain_stderr, args=(proc,),
                             daemon=True).start()
            got = False
            try:
                while not self._stop.is_set():
                    data = proc.stdout.read(8192)
                    if not data:
                        break
                    if not got:
                        got = True
                        self._log("connected - receiving audio")
                    if self._writer is not None:
                        try:
                            self._writer.write(data)
                        except Exception:
                            pass
                    with self._lock:
                        if self._sink is not None:
                            if self._first_write_ms is None:
                                self._first_write_ms = time.time() * 1000.0
                            try:
                                self._sink.write(data)
                            except Exception:
                                pass
            except Exception:
                pass
            finally:
                try:
                    proc.kill()
                except Exception:
                    pass
            if not got:
                # This candidate URI gave no audio (rejected session, or no
                # audio track) — try the next one (substream <-> main) next time.
                if len(self._uris) > 1:
                    self._uri_idx = (self._uri_idx + 1) % len(self._uris)
                self._log("no audio produced; will retry")
            if self._stop.wait(backoff):
                break

        with self._lock:
            if self._sink is not None:
                try:
                    self._sink.close()
                except Exception:
                    pass
                self._sink = None
        if self._writer is not None:
            try:
                self._writer.close()
            except Exception:
                pass
            self._writer = None

    def begin_clip(self, path, clip_first_ms):
        """Start writing captured audio to a per-clip PCM sink file."""
        with self._lock:
            if self._sink is not None:
                try:
                    self._sink.close()
                except Exception:
                    pass
            try:
                self._sink = open(path, 'wb')
                self._sink_path = path
            except Exception:
                self._sink = None
                self._sink_path = None
                return
            # Offset is derived from the wall time of the first actual PCM byte
            # written (tracked in _run), not from now: audio may not have
            # connected yet (e.g. after a stream restart), so opening the file
            # and writing the first byte can be seconds apart.
            self._clip_first_ms  = float(clip_first_ms)
            self._first_write_ms = None

    def end_clip(self):
        """Close the per-clip sink; return (pcm_path or None, offset_seconds)."""
        with self._lock:
            sink, self._sink = self._sink, None
            path, self._sink_path = self._sink_path, None
            first_write_ms = self._first_write_ms
            clip_first_ms  = self._clip_first_ms
        if sink is not None:
            try:
                sink.close()
            except Exception:
                pass
        if path and os.path.isfile(path) and os.path.getsize(path) > 0:
            if first_write_ms is not None:
                offset = max(0.0, (first_write_ms - clip_first_ms) / 1000.0)
            else:
                offset = 0.0
            return path, offset
        return None, 0.0

    def stop(self):
        self._stop.set()
        with self._lock:
            proc, self._proc = self._proc, None
        if proc is not None:
            try:
                proc.kill()
            except Exception:
                pass
        t, self._thread = self._thread, None
        if t is not None and t is not threading.current_thread():
            t.join(timeout=5)


class _RemuxRecorder:
    """Records the camera's own H.264/H.265 by STREAM-COPY — no decode, no
    re-encode — the way the original Py2 native pipeline did.

    One ffmpeg per camera pulls the MAIN stream and writes 2-minute mp4
    segments with `-c:v copy` (video passthrough) + AAC audio, and tees the
    decoded audio as PCM to the live-view ring buffer.  This replaces BOTH the
    old raw-frames->libx264 recording pipeline (the single largest CPU cost:
    a full software re-encode per camera) and _AudioCapture's separate audio
    session.  A/V sync is exact by construction: both streams are muxed by
    ffmpeg from ONE camera session using the camera's own RTP timestamps —
    there is nothing left to calibrate.

    Segment lifecycle: ffmpeg names each segment from the wall clock at open
    (strftime, second precision — the same naming the legacy recorder used).
    A watcher finalizes a segment as soon as the next one appears, dating it
    (end - probed duration).  The end comes from the next segment's filename
    ONLY when the same ffmpeg wrote both — across a run boundary that name
    carries the new run's RTSP setup latency, so the run's recorded end time
    is used instead (see _sweep).  Finalize = the owner's callback (move to
    the archive + register in ClipManager).

    Failure handling: the supervisor restarts ffmpeg with backoff on stream
    drops.  If ffmpeg dies quickly twice in a row (typically a camera with no
    audio track making the PCM output invalid), it retries without the audio
    outputs and records video-only.  Graceful stop ('q' on stdin) lets ffmpeg
    close the segment properly; a hard-killed segment has no moov atom, fails
    the duration probe, and is deleted rather than registered.
    """

    def __init__(self, uri, force_tcp, seg_dir, ring_path, location,
                 segment_secs, finalize_cb, log_fn, snapshot_path=None,
                 analysis_port=None, source_size=None, source_codec='',
                 analysis_frames_fn=None, want_audio=True,
                 retimestamp=True, analysis_dropped_fn=None):
        """@param finalize_cb    fn(tmp_path, first_ms, last_ms) — move+register.
           @param log_fn         fn(msg) — per-camera logger.
           @param snapshot_path  optional JPEG path continuously updated with
                                 the latest FULL-RESOLUTION keyframe — used by
                                 face/nudity analysis, which otherwise only
                                 sees the small analysis stream.
           @param analysis_port  loopback TCP port of the _AnalysisFrameSource.
                                 Set => single-stream mode: this ffmpeg also
                                 decodes and emits the analysis frames, so the
                                 camera needs no second RTSP session.
           @param source_size    (w, h) of the camera's stream if known, used
                                 to size the analysis scale.  See analysisSize.
           @param analysis_frames_fn  fn() -> frames delivered so far.  A run
                                 that records fine but delivers NO frames
                                 indicts the decode rung, not the camera.
           @param retimestamp    smooth each finished segment to its true
                                 constant frame rate (and black-fill its
                                 outages).  False for the substream BUFFER
                                 recorder — see _sweep."""
        self._uri          = uri
        self._force_tcp    = force_tcp
        self._seg_dir      = seg_dir
        self._ring_path    = ring_path
        self._snapshot     = snapshot_path
        self._location     = location or ''
        self._segment_secs = max(10, int(segment_secs))
        # An in-progress segment killed mid-write has no moov atom and is
        # discarded, so EVERY recorder restart costs up to a full segment of
        # footage.  On an unstable camera that dominates the loss: measured
        # 2026-08-02 over 4.5h, 08_FrontStep 68% coverage / 30 holes,
        # 06_Garage 82% / 24 holes, 05_Gate 88% / 25 holes -- almost all holes
        # 1-2 minutes, i.e. one segment each, against 99% on the stable
        # cameras.  So when a run dies before completing a segment, the next
        # run records in SHORT segments: the same instability then costs
        # seconds instead of a minute.  Reverts as soon as a run survives.
        self._unstable = False
        self._finalize_cb  = finalize_cb
        self._log          = log_fn
        self._stop         = threading.Event()
        self._flush_req    = threading.Event()
        self._lock         = threading.Lock()
        self._proc         = None
        self._thread       = None
        self._writer       = None      # AudioRingWriter (live audio relay)
        # want_audio=False for the substream buffer recorder: the MAIN recorder
        # owns this camera's audio and its live ring, so a second one would
        # duplicate the work and fight for the ring file.
        self._want_audio   = bool(want_audio)
        self._audio_ok     = bool(want_audio)  # False => no usable audio
        self._audio_bad_hint = False   # stderr suggested the AUDIO outputs
                                       # are what killed ffmpeg (see below)
        self._fast_fails   = 0
        self._last_flush   = 0.0
        self._final_fails  = {}        # path -> consecutive finalize failures
        # When a segment last actually LANDED in the archive.  The watchdog
        # measures against this, not against ffmpeg rolling files -- see _run.
        self._lastArchivedMs = 0
        self._run_id       = 0         # bumped per ffmpeg run; segment dating
        self._seg_run      = {}        # basename -> run that wrote it
        self._run_start_ms = {}        # run -> epoch ms ffmpeg was spawned
        self._run_end_ms   = {}        # run -> epoch ms it stopped writing
        self._run_first_seg = {}       # run -> basename of its first segment
        # End of the last span registered, and the run it came from.  Segments
        # of ONE ffmpeg run are contiguous by construction -- no packet is in
        # two of them -- so their registered rows must not overlap either.
        self._last_reg_end_ms = None
        self._last_reg_run    = None
        self._fill_slow_runs = 0       # consecutive black fills that overran
        self._fill_disabled  = False   # ...too many: stop gap-filling here
        self._retimestamp    = bool(retimestamp)
        self._rt_safe      = None      # is per-segment re-timestamp safe for
                                       # this stream? None=undetermined, True=
                                       # I/P (safe), False=has B-frames (skip)
        # -- analysis frames this recorder also emits -----------------------
        self._analysis_port = analysis_port
        self._analysis_frames_fn = analysis_frames_fn
        # Queue overflow, reported alongside the lag because they are the two
        # halves of one story: the lag is what the pipeline holds BEFORE the
        # socket, this is what the consumer could not keep up with AFTER it.
        self._analysis_dropped_fn = analysis_dropped_fn
        try:
            self._analysis_passthrough = bool(int(os.environ.get(
                'SV_ANALYSIS_PASSTHROUGH',
                str(_kDefaultAnalysisPassthrough))))
        except Exception:
            self._analysis_passthrough = bool(_kDefaultAnalysisPassthrough)
        self._analysis_pts = _analysisPtsEnabled()
        # Analysis lag probe state (measurement only; see the constants block).
        # Sampling the socket counter at REGISTRATION was wrong -- finalizing
        # trails the demux by the sweep plus the finalize work, so the socket
        # legitimately ran ahead and the probe reported "delivered > demuxed",
        # i.e. -26s.  Reading it when a segment is first SEEN on disk was still
        # wrong for the same reason in miniature: the file appeared some part of
        # a sweep interval earlier, so the count is up to ~3s of frames too
        # high.  The counter is now SAMPLED ON A SCHEDULE and interpolated back
        # to the open instant in the segment's -strftime name, which is the
        # real demux boundary -- everything before that file is fully demuxed.
        try:
            self._lag_probe = bool(int(os.environ.get(
                'SV_ANALYSIS_LAG_PROBE', str(_kDefaultAnalysisLagProbe))))
        except Exception:
            self._lag_probe = bool(_kDefaultAnalysisLagProbe)
        try:
            self._lag_correct = bool(int(os.environ.get(
                'SV_ANALYSIS_LAG_CORRECT',
                str(_kDefaultAnalysisLagCorrect))))
        except Exception:
            self._lag_correct = bool(_kDefaultAnalysisLagCorrect)
        # The correction IS the probe's output, so it cannot run without it.
        if self._lag_correct:
            self._lag_probe = True
        # Only the recorder that OWNS the analysis socket can run the probe.
        # In the two-session shape the MAIN recorder has no analysis_frames_fn,
        # so its counter reads a flat 0 and every line it logged said "behind
        # <every frame demuxed so far>" -- half the probe output was noise that
        # grew without bound (+576s on 07_Back_Yard) and looked like a finding.
        if analysis_frames_fn is None:
            self._lag_probe = False
            self._lag_correct = False
        # A list, not a deque: the lookup binary-searches it, and indexing a
        # deque is O(n) per step.
        self._lagSamples = []         # [(wall_ms, frames delivered)], ordered
        self._lagRun = None           # run these totals belong to
        self._lagBase = None          # delivered at that run's first seg open
        self._lagCum = 0              # frames in this run's finalized segments
        self._lagLoggedAt = 0.0
        self._lagRecent = deque(maxlen=_kLagCorrectionWindow)  # recent secs
        self._src_size = tuple(source_size) if source_size else (0, 0)
        self._analysis_wh = None       # computed once by analysisSize()
        # Decode rungs for the analysis output, cheapest first:
        #   cuvid -- the named per-codec NVDEC decoder.  2-2.6x less GPU
        #            memory than the generic path, which decides whether 14
        #            cameras fit on the 4 GB card at all.  Needs the codec.
        #   nvdec -- ffmpeg's generic CUDA hwaccel.  Codec-agnostic, verified
        #            live on every camera in this fleet, but memory-hungry.
        #   sw    -- software decode.  No GPU memory, ~7x the CPU.
        # A rung is demoted for the life of this process once it fails: a
        # retry costs a dead run per respawn, and nothing about a camera's
        # bitstream changes mid-session.
        self._src_codec = (source_codec or '').lower()
        self._cuvid = _kCuvidByCodec.get(self._src_codec)
        rungs = ['cuvid', 'nvdec', 'sw']
        if not self._cuvid:
            # Unknown codec (no local recording to probe yet): the named
            # decoder can't be chosen, and guessing it is the failure mode
            # that killed 08_FrontStep during the spike.
            rungs.remove('cuvid')
        sw, sh = self._src_size if self._src_size else (0, 0)
        if sw and sw <= _kSoftwareDecodeMaxWidth:
            rungs = ['sw']
        self._rungs = tuple(rungs)
        self._rung  = 0
        # Last moment an ffmpeg of ours was confirmed alive; isRunning() uses
        # it to tell a normal respawn from a wedged supervise thread.
        self._lastProcAliveAt = time.time()
        self._decode_bad_hint = False  # stderr blamed the DECODE/filter path
        self._connect_failed  = False  # ...or the camera was simply unreachable
        # Was the previous stderr line dropped as benign noise?  Decides
        # whether ffmpeg's "Last message repeated N times" is spam or signal;
        # see _drainStderr.
        self._lastStderrDropped = False
        # Has ffmpeg's own stderr told us the camera's real codec/size yet?
        # Only matters when the probe above came up empty -- see
        # _noteSourceInfo().
        self._src_known = bool(self._src_size and self._src_size[0])

    # -- public ---------------------------------------------------------

    def _killOrphans(self):
        """Kill leftover recorder ffmpegs from a previous camera process.

        A hard-terminated camera process orphans its ffmpeg child, which
        keeps holding the camera's RTSP session (tripping per-camera session
        limits — new spawns then fail with 'Operation not permitted') and
        Windows file locks on the segment dir.  The segment dir path in the
        command line uniquely identifies OUR recorder for this camera."""
        try:
            import psutil
        except Exception:
            return
        try:
            from appCommon.InstallPaths import isOurFfmpegProcess
        except Exception:
            # Source checkout without appCommon on the path: the binary is
            # imageio-ffmpeg's, which is named ffmpeg-*.
            isOurFfmpegProcess = \
                lambda name: (name or '').lower().startswith('ffmpeg')
        for p in psutil.process_iter(['name', 'cmdline', 'pid']):
            try:
                if not isOurFfmpegProcess(p.info['name']):
                    continue
                cmd = ' '.join(p.info['cmdline'] or [])
                if self._seg_dir and self._seg_dir in cmd:
                    self._log("remux: killing orphan ffmpeg pid %d "
                              "(held a stale camera session)" % p.pid)
                    p.kill()
            except Exception:
                continue

    def start(self):
        try:
            self._killOrphans()
            os.makedirs(self._seg_dir, exist_ok=True)
            if self._snapshot:
                os.makedirs(self._snapshot, exist_ok=True)
            # Leftovers from the previous run.  These are NOT all crash
            # debris: only the file ffmpeg was actively writing lacks a moov
            # atom, and every segment the muxer already closed is a complete,
            # playable recording that simply never got swept into the archive.
            # Deleting the lot cost real footage -- 544 segments on one camera
            # in a single night, because an unstable camera reconnects every
            # few minutes and each restart wiped whatever was still in flight.
            # So: rescue anything that probes as playable, delete only the rest.
            rescued = dropped = 0
            for f in sorted(os.listdir(self._seg_dir)):
                if not f.endswith('.mp4') or f.endswith(('.gap.mp4', '.rt.mp4',
                                                         '.df.mp4')):
                    if f.endswith(('.gap.mp4', '.rt.mp4', '.df.mp4')):
                        try:
                            os.remove(os.path.join(self._seg_dir, f))
                        except Exception:
                            pass
                    continue
                p = os.path.join(self._seg_dir, f)
                dur = _probe_duration_ms(p)
                name_ms = self._parseSegMs(p)
                if dur and dur >= 1000 and name_ms is not None:
                    # Dated from its own filename: the run that could place it
                    # more precisely is gone.  Not re-timestamped either --
                    # raw footage beats deleted footage, and the smoothing is
                    # cosmetic next to losing the recording.
                    self._finalize(p, name_ms, name_ms + dur)
                    rescued += 1
                else:
                    try:
                        os.remove(p)
                    except Exception:
                        pass
                    dropped += 1
            if rescued or dropped:
                self._log("remux: recovered %d unswept segment(s) from the "
                          "previous run, discarded %d unplayable"
                          % (rescued, dropped))
        except Exception as e:
            self._log("remux: cannot prepare segment dir: %r" % e)
            return False
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        return True

    def stop(self):
        """Stop recording.  MUST NOT return while our ffmpeg is still alive.

        A survivor holds the camera's RTSP session, so the next open() gets no
        stream, times out after 15s, tears the camera down again -- and leaves
        another survivor.  That loop cost 45-100s of footage per cycle on
        2026-08-03 (`killing orphan ffmpeg (held a stale camera session)`
        appearing 11x an hour on 08_FrontStep) and dropped its coverage to
        45%.  The supervise thread is given a short window to close the
        segment gracefully; after that the process is killed regardless of
        what that thread is still doing.
        """
        self._stop.set()
        self._signalQuit()
        t, self._thread = self._thread, None
        if t is not None and t is not threading.current_thread():
            t.join(timeout=_kRecorderStopJoinSecs)
            if t.is_alive():
                self._log("remux: supervise thread still busy after %.0fs; "
                          "killing ffmpeg anyway so the camera session is "
                          "released" % _kRecorderStopJoinSecs)
        with self._lock:
            proc = self._proc
        if proc is not None and proc.poll() is None:
            for fn in (proc.kill, lambda: proc.wait(timeout=5)):
                try:
                    fn()
                except Exception:
                    pass
        # Belt and braces: anything still holding OUR segment dir dies here
        # rather than being discovered by the next start().
        self._killOrphans()

    def isRunning(self):
        """True while this recorder is actually RECORDING (or briefly between
        ffmpeg runs), not merely while its thread exists.

        A live thread is not enough.  After ffmpeg exits, the supervise loop
        runs `_sweep(closing=True)`, and finalizing can black-fill outages by
        RE-ENCODING -- on a 4K camera with many outages that takes minutes per
        segment.  08_FrontStep 2026-08-07: ffmpeg gone, thread alive grinding
        a ~30 minute backlog (segments from 18:35-18:40 finalized at 18:44,
        18:55, 19:00, 19:11), so `open()` kept "re-using the running recorder"
        and never built a working one.  **No footage was recorded for over half
        an hour while live view looked perfectly fine.**  Before change 1 an
        analysis timeout happened to replace the recorder and hid this.

        So: alive thread AND (an ffmpeg right now, or one recently enough that
        this is just the normal respawn backoff).
        """
        t = self._thread
        if not (t is not None and t.is_alive() and not self._stop.is_set()):
            return False
        with self._lock:
            proc = self._proc
        if proc is not None and proc.poll() is None:
            return True
        return (time.time() - self._lastProcAliveAt) < _kRecorderRespawnGraceSecs

    def matchesUri(self, uri):
        return self._uri == uri

    def flush(self):
        """Cycle ffmpeg so the in-progress segment closes and becomes a clip
        now (searches/responses wanting recent footage).

        Cycling costs ~1s of recording and a segment fragment, and searches
        near "now" call this for every camera involved — so it's heavily
        debounced, and skipped when the newest segment is young (whatever
        just closed already covers "recent")."""
        now = time.time()
        if now - self._last_flush < _kRemuxFlushDebounceSecs:
            return
        segs = self._listSegments()
        if segs:
            newest_ms = self._parseSegMs(segs[-1])
            if newest_ms is not None and \
               (now * 1000 - newest_ms) < _kRemuxMinSegAgeForCutSecs * 1000:
                return
        self._last_flush = now
        self._flush_req.set()
        self._signalQuit()

    # -- internals ------------------------------------------------------

    def _signalQuit(self):
        """Ask the running ffmpeg to exit gracefully (closes segment moov)."""
        with self._lock:
            proc = self._proc
        if proc is not None and proc.poll() is None:
            try:
                proc.stdin.write(b'q\n')
                proc.stdin.flush()
            except Exception:
                pass

    def _probeVideoFrameCount(self, path):
        """Count video frames in a finished segment (fast: demux only, no
        decode).  Returns int, or None on failure."""
        try:
            proc = subprocess.run(
                [_get_ffmpeg(), '-hide_banner', '-i', path, '-map', '0:v:0',
                 '-c', 'copy', '-f', 'null', '-'],
                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=60,
                creationflags=_kNoWindow)
        except Exception:
            return None
        m = None
        for m in re.finditer(rb'frame=\s*(\d+)', proc.stderr or b''):
            pass
        return int(m.group(1)) if m else None

    def _probeAudioContentMs(self, path):
        """How much audio a segment actually holds, from its SAMPLE COUNT.

        Not the audio PTS span: the camera delivers RTP in ~0.4-0.5s bursts, so
        the stamped span understates or overstates what arrived.  The sample
        count is paced by the camera's own audio oscillator and measured (on
        healthy segments from three cameras) to sit within 0.06s of real
        elapsed, which makes it the steadiest clock in the file.  asetpts
        re-derives the timeline from that count, so ffmpeg's final time= IS the
        content length.  Returns ms, or None.
        """
        try:
            proc = subprocess.run(
                [_get_ffmpeg(), '-hide_banner', '-nostdin', '-i', path,
                 '-map', '0:a:0', '-af', 'asetpts=N/SR/TB', '-f', 'null', '-'],
                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=60,
                creationflags=_kNoWindow)
        except Exception:
            return None
        m = None
        for m in re.finditer(rb'time=(\d+):(\d+):([0-9.]+)', proc.stderr or b''):
            pass
        if not m:
            return None
        secs = (int(m.group(1)) * 3600 + int(m.group(2)) * 60
                + float(m.group(3)))
        return int(secs * 1000) if secs > 0 else None

    def _probePacketTimestampsMs(self, path):
        """Every video packet's timestamp in ms, without decoding a frame.

        The mkvtimestamp_v2 muxer just writes the timestamps it is handed, so
        with -c copy this is a demux-only pass (~0.25s on a 57s 4K segment)
        rather than the ~45s a showinfo decode would cost.  Cheap enough to run
        on every segment, which is what lets the gap check be unconditional.

        @return list of ints (ascending), or None if the probe failed.
        """
        try:
            proc = subprocess.run(
                [_get_ffmpeg(), '-hide_banner', '-nostdin', '-i', path,
                 '-map', '0:v:0', '-c', 'copy', '-f', 'mkvtimestamp_v2', '-'],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=60,
                creationflags=_kNoWindow)
        except Exception:
            return None
        out = []
        for line in (proc.stdout or b'').splitlines():
            line = line.strip()
            if not line or line.startswith(b'#'):
                continue
            try:
                out.append(int(float(line)))
            except ValueError:
                continue
        return out or None

    def _findGaps(self, stampsMs, minSecs=_kGapBlackMinSecs):
        """Holes in delivery, as [(startSec, endSec)] on the segment timeline.

        A "gap" is measured between consecutive PACKETS, so it is the camera
        not sending -- not us dropping anything.
        """
        gaps = []
        minMs = minSecs * 1000.0
        base = stampsMs[0] if stampsMs else 0
        for i in range(1, len(stampsMs)):
            delta = stampsMs[i] - stampsMs[i - 1]
            if delta >= minMs:
                gaps.append(((stampsMs[i - 1] - base) / 1000.0,
                             (stampsMs[i] - base) / 1000.0))
        return gaps

    def _segmentHasBframes(self, path):
        """True/False whether the segment's video uses B-frames, or None if the
        probe failed (so the caller can retry on a later segment)."""
        try:
            proc = subprocess.run(
                [_get_ffmpeg(), '-hide_banner', '-i', path, '-map', '0:v:0',
                 '-vf', 'showinfo', '-frames:v', '60', '-f', 'null', '-'],
                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=60,
                creationflags=_kNoWindow)
        except Exception:
            return None
        if proc.returncode != 0:
            return None
        return b'type:B' in (proc.stderr or b'')

    def _fillGapsWithBlack(self, path, gaps, stampsMs):
        """Rewrite a segment so its outages are BLACK, at their real duration.

        The constant-rate re-stamp used elsewhere spreads whatever frames
        arrived evenly across the segment, which silently deletes an outage
        from the timeline -- playback stays smooth while the camera's burned-in
        clock jumps (measured on 08_FrontStep: 537 frames covering 57s of wall
        time, so ~35s of missing footage compressed out of existence).

        Here the real timing is kept instead: `fps` resamples onto a uniform
        grid honouring the input timestamps (which also irons out the camera's
        burst jitter, the reason the re-stamp exists), and `drawbox` paints the
        frames inside each gap black so an outage is unmistakable in the file
        itself.  This costs a re-encode, so it runs ONLY on segments that
        actually have a gap.

        @return the new duration in ms, or None if the rewrite failed.
        """
        if self._fill_disabled:
            return None
        spanSecs = (stampsMs[-1] - stampsMs[0]) / 1000.0
        gapSecs = sum(e - s for s, e in gaps)
        liveSecs = spanSecs - gapSecs
        if spanSecs <= 0 or liveSecs <= 0:
            return None
        # The rate the camera actually delivered at WHILE it was working -- not
        # frames/span, which is deflated by the outage and would play the live
        # footage in slow motion.
        rate = (len(stampsMs) - 1) / liveSecs
        # Burst delivery makes the live-stretch average exceed what the camera
        # actually produces (measured: 34 fps on a 24.8 fps camera), and the
        # output grid is that dense everywhere -- ~37% more frames to encode
        # and store than exist, on an encoder already running minutes behind.
        # It does NOT affect playback speed: `fps` honours the input timing, so
        # the clip's duration is the same at any rate (verified 12/25/40 fps ->
        # identical duration).  This is purely about not doing pointless work.
        rate = min(rate, _kGapFillMaxFps)
        if not (1.0 <= rate <= 120.0):
            return None

        enable = '+'.join(r'between(t\,%.3f\,%.3f)' % (s, e) for s, e in gaps)
        vf = ('fps=%.6g,drawbox=x=0:y=0:w=iw:h=ih:color=black:t=fill:enable=%s'
              % (rate, enable))

        tmp = path + '.gap.mp4'
        # CPU encode, deliberately -- NOT NVENC.  This GPU (4GB) already hosts
        # the detection service's CUDA context, 15 cameras' D3D11VA decode and
        # NVDEC playback decode.  Adding an NVENC session for a gap fill drove
        # the display driver into fault (nvlddmkm event 153) within seconds on
        # 2026-08-02, and once that happens the front end's GL contexts are
        # poisoned: every texture upload returns GL_OUT_OF_MEMORY and neither
        # live view nor clip playback renders again until the app restarts.
        # Filling gaps is a background chore; it must never contend with the
        # things the user is actually looking at.  Mostly-black frames encode
        # cheaply, and this only runs on segments that actually have a gap.
        cmd = [_get_ffmpeg(), '-y', '-loglevel', 'error', '-i', path,
               '-map', '0:v:0', '-vf', vf,
               '-c:v', 'libx264', '-preset', 'veryfast', '-crf', '23',
               '-maxrate', '9000k', '-bufsize', '12000k',
               '-pix_fmt', 'yuv420p']
        if self._audio_ok:
            cmd += ['-map', '0:a?', '-af', 'asetpts=N/SR/TB',
                    '-c:a', 'aac', '-b:a', '96k']
        cmd += ['-movflags', '+faststart', tmp]

        budget = max(_kGapFillMinTimeoutSecs, spanSecs * _kGapFillTimeoutFrac)
        t0 = time.time()
        timedOut = False
        try:
            rc = subprocess.run(cmd, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL,
                                timeout=budget,
                                creationflags=_kNoWindow).returncode
        except subprocess.TimeoutExpired:
            rc, timedOut = 1, True
        except Exception:
            rc = 1
        took = time.time() - t0
        if rc == 0 and os.path.isfile(tmp) and os.path.getsize(tmp) > 0:
            try:
                os.replace(tmp, path)
                self._fill_slow_runs = 0
                self._log("remux: %s had %d outage(s) totalling %.1fs -> "
                          "filled black (live rate %.1f fps, took %.1fs of a "
                          "%.0fs budget)"
                          % (os.path.basename(path), len(gaps), gapSecs, rate,
                             took, budget))
                return _probe_duration_ms(path)
            except Exception:
                pass
        if timedOut:
            self._fill_slow_runs += 1
            self._log("remux: black fill for %s overran its %.0fs budget and "
                      "was abandoned; the outage will be SMOOTHED AWAY by the "
                      "re-stamp instead of shown black (no footage is lost -- "
                      "the missing time just stops being visible)"
                      % (os.path.basename(path), budget))
            if self._fill_slow_runs >= _kGapFillMaxSlowRuns:
                self._fill_disabled = True
                self._log("remux: gap fills are too slow for this stream "
                          "(%d in a row overran) -- disabling them here so "
                          "finalizing keeps up with recording"
                          % self._fill_slow_runs)
        try:
            if os.path.isfile(tmp):
                os.remove(tmp)
        except Exception:
            pass
        return None

    def _ptsSpanIsPlausible(self, stamps, wall_span_ms):
        """Can this segment's packet timeline have really happened?

        `wall_span_ms` is how long the file was open on OUR clock.  If the
        camera's packet timestamps claim a longer span than that, the camera's
        timeline jumped -- it did not stall -- and treating the jump as an
        outage fabricates black footage and inflates the registered span.
        Unknown (None) means we have no ceiling and must trust the stamps.
        """
        if not wall_span_ms or not stamps or len(stamps) < 2:
            return True
        ptsSpanMs = stamps[-1] - stamps[0]
        return ptsSpanMs <= wall_span_ms + _kSegSpanSlackSecs * 1000.0

    def _reTimestampSegment(self, path, real_ms, allowFill=True,
                            wall_span_ms=None):
        """Rewrite a finished segment's video to a CONSTANT frame rate equal to
        its TRUE average rate (frame_count / real_elapsed).

        Cameras deliver bunched RTP timestamps (a burst of near-identical PTS
        then a periodic ~0.5s jump), which stream-copy preserves verbatim -> a
        visible "pause every second".  A fixed guessed fps can't fix it without
        desyncing audio -- the camera's real rate varies segment to segment (a
        struggling 4K stream drops to ~17fps, recovers to ~24) -- but the true
        rate of a FINISHED segment is exactly frame_count/real_elapsed, so
        re-stamping to that gives even spacing (smooth) AND keeps video length ==
        real time == audio (in sync).  Copy only (no re-encode), on the local
        temp segment before it's moved to the archive; best-effort, and only for
        I/P streams (setts stamps in decode order, unsafe with B-frames).

        @return the rewritten segment's duration in ms -- the CALLER must
                register that, not real_ms, because the rewrite re-stamps to
                the audio clock and a stalled video PTS span can overstate it
                by tens of seconds.  None when the file was left untouched, in
                which case real_ms still describes it.
        """
        if real_ms is None or real_ms < 1000:
            return None
        if self._rt_safe is None:
            has_b = self._segmentHasBframes(path)
            if has_b is None:
                return None  # probe failed -- undetermined, retry on a later one
            self._rt_safe = not has_b
            if not self._rt_safe:
                self._log("remux: stream has B-frames -> archiving without "
                          "re-timestamp")
        if not self._rt_safe:
            return None

        # Did the camera actually stop sending inside this segment?  The probe
        # is demux-only, so asking is nearly free; only the answer "yes" costs
        # anything.  Must come BEFORE the constant-rate re-stamp below, which
        # would erase the evidence by spreading the survivors evenly.
        stamps = self._probePacketTimestampsMs(path)
        if stamps and len(stamps) > 1 and allowFill:
            gaps = self._findGaps(stamps)
            if gaps and not self._ptsSpanIsPlausible(stamps, wall_span_ms):
                self._log("remux: %s claims %.1fs of packet timeline in a file "
                          "written over %.1fs -- camera timestamps jumped, not "
                          "an outage; not filling"
                          % (os.path.basename(path),
                             (stamps[-1] - stamps[0]) / 1000.0,
                             wall_span_ms / 1000.0))
                gaps = []
            if gaps:
                filled = self._fillGapsWithBlack(path, gaps, stamps)
                if filled:
                    return filled
                # Fall through: a failed fill must still get the old treatment
                # rather than leaving the segment unsmoothed.

        n = self._probeVideoFrameCount(path)
        if not n:
            return None
        # Spread the frames over the AUDIO's elapsed time, not the container
        # duration.  The container duration comes from the video PTS span, and
        # when the camera stalls mid-segment that span keeps running while no
        # frames arrive -- re-stamping to it spreads the outage evenly across
        # the whole clip, so the video plays slow and walks away from the audio
        # (measured on a stalled x_Inside segment: 691 frames stretched over
        # 50.6s of container time that held 46.5s of audio -> 4.2s adrift by
        # the end).  Anchoring on the audio clock makes video length == audio
        # length by construction.  On healthy segments the two agree to within
        # 0.06s, so this changes nothing there.
        span_ms = real_ms
        if self._audio_ok:
            audio_ms = self._probeAudioContentMs(path)
            # Both failures push the same way (a stall stretches the video span,
            # loss shortens the audio), so the gap alone can't say which stream
            # is at fault -- the band only has to reject an audio clock that is
            # nonsense, e.g. a stream that died after a few seconds.  A stall
            # long enough to stretch a 60s segment past 2x its audio is past
            # the point where either clock means anything.
            if audio_ms and abs(audio_ms - real_ms) < real_ms * 0.5:
                span_ms = audio_ms
        fps = n / (span_ms / 1000.0)
        if not (1.0 <= fps <= 120.0):
            return None
        # Video: even out the bunched timestamps to the true constant rate
        # (bitstream copy, no re-encode).  Audio: the SAME burst delivery leaves
        # the audio timestamps bunched with ~0.5s holes, and the samples are all
        # there -- so the timeline has to be rebuilt, not obeyed.
        #
        # It must NOT be rebuilt with aresample=async: that filter's job is
        # "filling and trimming" AGAINST those timestamps, so it discarded the
        # bunched samples and wrote silence across every jump.  Measured on real
        # archived clips: 38-96% of the recorded sound replaced by digital
        # silence, in ~0.4s holes about a second apart -- audible as the sound
        # cutting out and resuming.  asetpts re-derives the timeline from the
        # SAMPLE COUNT instead (the camera paces those on its own audio clock),
        # which lays every sample down contiguously, keeps the audio the same
        # length as real time, and stays aligned with the re-stamped video.
        tmp = path + '.rt.mp4'
        cmd = [_get_ffmpeg(), '-y', '-loglevel', 'error', '-i', path,
               '-map', '0:v', '-c:v', 'copy',
               '-bsf:v', 'setts=ts=N/%.6g/TB' % fps]
        if self._audio_ok:
            cmd += ['-map', '0:a?', '-af', 'asetpts=N/SR/TB',
                    '-c:a', 'aac', '-b:a', '96k']
        cmd += ['-movflags', '+faststart', tmp]
        try:
            rc = subprocess.run(
                cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                timeout=120,
                creationflags=_kNoWindow).returncode
        except Exception:
            rc = 1
        if rc == 0 and os.path.isfile(tmp) and os.path.getsize(tmp) > 0:
            try:
                os.replace(tmp, path)
                # Measure what actually landed rather than assuming span_ms:
                # the file is as long as its LONGEST stream, so when the audio
                # clock was rejected above the audio can still outrun the
                # re-stamped video.
                return _probe_duration_ms(path)
            except Exception:
                pass
        try:
            if os.path.isfile(tmp):
                os.remove(tmp)
        except Exception:
            pass
        return None

    def analysisSize(self):
        """The exact (w, h) of the analysis frames this recorder emits.

        THE single source of truth for that geometry: a raw stream carries no
        framing, so if the scale filter and the reader disagree the frames
        don't error, they shear.  Both sides call this.
        """
        if self._analysis_wh is None:
            sw, sh = self._src_size
            if sw > 0 and sh > 0:
                w = min(_kAnalysisMaxWidth, int(sw))
                h = max(2, int(round(int(sh) * (float(w) / int(sw)) / 2.0)) * 2)
            else:
                # Nothing local to derive it from -- the first ever run for
                # this camera.  Assume the fleet's usual 16:9 at the cap; the
                # next open probes a real segment and corrects it.
                w = _kAnalysisMaxWidth
                h = int(round(w * 9 / 16.0))
                h -= h % 2
            self._analysis_wh = (w - w % 2, h)
        return self._analysis_wh

    def analysisRung(self):
        return self._rungs[self._rung]

    def _spawn(self):
        ff = _get_ffmpeg()
        # Before choosing a decoder: if open() had no recording to probe, a
        # previous run has since made one.  See _learnSourceRung().
        self._learnSourceRung()
        cmd = [ff, '-y', '-loglevel', 'error']
        single = self._analysis_port is not None
        gpu = single and self._rungs[self._rung] != 'sw'
        if single:
            # Full-rate decode, feeding BOTH the analysis stream and the
            # snapshot ring from this one connection.  Frames are kept in GPU
            # memory and each output scales itself, because the decoder's own
            # `-resize` would shrink EVERY decoded output -- including the
            # full-resolution snapshots the face path depends on (its gate
            # requires the snapshot to be bigger than the analysis frame).
            if gpu:
                cmd += ['-hwaccel', 'cuda', '-hwaccel_output_format', 'cuda']
                if self._rungs[self._rung] == 'cuvid':
                    # Named decoder: same CUDA frames, far fewer of them.
                    # -surfaces 6 is the floor -- 4 kills the session outright
                    # (measured; it is also what _NvdecCapture settled on).
                    cmd += ['-c:v', self._cuvid, '-surfaces', '6']
            # 'sw' adds nothing here: plain software decode, no GPU memory.
        elif self._snapshot:
            # Two-session shape: decode KEYFRAMES ONLY for the snapshot output
            # (one I-frame per GOP ≈ trivial CPU, vs full-stream decode).
            # Video recording is stream-copy so this doesn't affect it.
            cmd += ['-skip_frame', 'nokey']
        if self._uri.lower().startswith(('rtsp://', 'rtp://', 'udp://')):
            if self._force_tcp:
                cmd += ['-rtsp_transport', 'tcp']
            cmd += ['-timeout',
                    str(int(_kRemuxSocketTimeoutSecs * 1000000))]
        cmd += ['-i', self._uri]
        pattern = os.path.join(self._seg_dir, '%Y-%m-%d-%H%M%S.mp4')
        seg = ['-map', '0:v:0', '-c:v', 'copy']
        if self._audio_ok:
            seg += ['-map', '0:a?', '-c:a', 'aac', '-b:a', '96k']
        seg += ['-f', 'segment',
                '-segment_time', str(_kRemuxShortSegmentSecs
                                     if self._unstable
                                     else self._segment_secs),
                '-reset_timestamps', '1',
                '-segment_format', 'mp4',
                # FRAGMENTED, not faststart.  A plain mp4 only becomes readable
                # when the muxer writes its moov at close, so every segment
                # ffmpeg was mid-way through when it got killed was a total
                # loss -- 638 "dropping unusable segment" events across the
                # fleet in 14h on 2026-08-10, up to 184 on one camera, and 23
                # of those stubs were still REGISTERED in clipdb as footage
                # that would not play.  empty_moov puts a valid moov at byte 0
                # and every moof/mdat pair after it stands on its own, so a
                # killed segment stays playable up to its last whole fragment.
                # frag_duration bounds what "last whole fragment" costs:
                # frag_keyframe alone would bound it by the GOP, and these
                # cameras use smart encoding with ~12s GOPs (4 keyframes in a
                # measured 47s segment), so a kill could still lose 12s.  At 1s
                # the loss is a second.  This is the IN-FLIGHT format only --
                # _reTimestampSegment rewrites with +faststart before
                # _finalize archives it, so the archive is unchanged.
                '-segment_format_options',
                'movflags=+frag_keyframe+empty_moov+default_base_moof'
                ':frag_duration=%d' % int(_kFragmentSecs * 1000000),
                '-strftime', '1', pattern]
        cmd += seg
        if single:
            # Analysis frames: scaled ON THE GPU so only the small frame
            # crosses to the host, then out over the loopback socket the
            # _AnalysisFrameSource is listening on.  ffmpeg connects OUT to
            # us because stdout is taken by the audio tee and Windows has no
            # usable pipe:3.
            aw, ah = self.analysisSize()
            vf = ('scale_cuda=%d:%d,hwdownload,format=nv12' % (aw, ah)
                  if gpu else 'scale=%d:%d' % (aw, ah))
            cmd += ['-map', '0:v:0', '-vf', vf, '-pix_fmt', 'bgr24']
            if self._analysis_passthrough:
                # PASS EVERY DEMUXED FRAME THROUGH, ONCE.  Without this ffmpeg
                # defaults the rawvideo output to CFR at the input's guessed
                # frame rate, and these cameras advertise a rate they do not
                # deliver: measured on a real 20s 07_Back_Yard sub segment,
                # 300 demuxed frames came out as 403 with `dup=180 drop=77`.
                # So the detector was being fed 60% duplicated pixels while
                # 26% of the REAL frames were thrown away before it ever saw
                # them -- and each duplicate arrives with a fresh timestamp on
                # stale content, the same forgery the snapshot output below
                # avoids with -fps_mode vfr.  It also inflates
                # _AnalysisFrameSource's measured rate (15 fps read as 20),
                # which sizes the release pacing and the queue cap, so the
                # surplus fabricated frames were also pushing real ones off
                # the back of the queue.
                cmd += ['-fps_mode', 'passthrough']
            if self._analysis_pts:
                # Same pixels, in a container that carries each frame's PTS.
                # The reader listens; this connects out, exactly as before.
                cmd += ['-c:v', 'rawvideo', '-f', 'nut',
                        'tcp://127.0.0.1:%d' % self._analysis_port]
            else:
                cmd += ['-f', 'rawvideo',
                        'tcp://127.0.0.1:%d' % self._analysis_port]
        if self._snapshot:
            # Rolling RING of full-resolution JPEGs, matched by the detector
            # to the ANALYZED frame's timestamp — detection can run many
            # seconds behind capture, so "the latest snapshot" is usually the
            # wrong moment entirely, and a file's mtime must equal its
            # content's capture moment.  Old entries are pruned by _sweep.
            # (%H%M%S: Windows strftime has no %s epoch specifier.)
            if single:
                # Fixed cadence.  Safe here, and NOT safe in the branch below:
                # there the decoder runs `-skip_frame nokey`, so a fixed rate
                # duplicates the last I-frame into files with fresh mtimes —
                # forging timestamps onto stale content, which is exactly the
                # bug that broke the full-res face path in July.  Under
                # single-stream every frame is genuinely decoded, so each
                # emitted file holds real, current pixels.  It is also
                # steadier than keyframes: these cameras use smart encoding
                # with GOPs up to ~10s on a static scene.
                vf = 'fps=%g' % _kSnapshotFps
                if _kSnapshotMaxWidth:
                    vf += (',scale_cuda=%d:-2' % _kSnapshotMaxWidth if gpu
                           else ',scale=%d:-2' % _kSnapshotMaxWidth)
                if gpu:
                    vf += ',hwdownload,format=nv12'
                cmd += ['-map', '0:v:0', '-vf', vf]
            else:
                # One file PER KEYFRAME (vfr, no fps filter) — see above.
                cmd += ['-map', '0:v:0']
            cmd += ['-fps_mode', 'vfr', '-q:v', '4',
                    '-strftime', '1', '-f', 'image2',
                    os.path.join(self._snapshot, '%H%M%S.jpg')]
        if self._audio_ok:
            # Tee decoded audio to stdout for the live-view ring buffer
            # (same format _AudioCapture produced: s16le stereo 44.1k).
            cmd += ['-map', '0:a?', '-vn', '-acodec', 'pcm_s16le',
                    '-ac', '2', '-ar', '44100', '-f', 's16le', 'pipe:1']
        if single:
            aw, ah = self.analysisSize()
            self._log("remux: starting (%s)%s [single stream: analysis "
                      "%dx%d via %s]" % (
                          self._uri,
                          '' if self._audio_ok else ' video-only',
                          aw, ah, self._rungs[self._rung]))
        else:
            self._log("remux: starting (%s)%s" % (
                self._uri, '' if self._audio_ok else ' [video-only]'))
        return subprocess.Popen(
            cmd, stdin=subprocess.PIPE,
            stdout=subprocess.PIPE if self._audio_ok else subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            creationflags=_kNoWindow)

    def _drainStderr(self, proc):
        try:
            for line in iter(proc.stderr.readline, b''):
                if not line:
                    break
                s = line.decode('utf-8', 'replace').rstrip()
                if not s:
                    continue
                # A camera with no audio track makes the audio-only outputs
                # invalid and kills ffmpeg with this message; that's the ONLY
                # failure that justifies retrying video-only (a plain connect
                # failure must NOT permanently disable audio).  Check this
                # BEFORE the noise filter so the hint is never masked.  Must
                # still drain the pipe so ffmpeg doesn't block on stderr.
                if 'does not contain any stream' in s:
                    self._audio_bad_hint = True
                # Likewise for the DECODE path: these mean the analysis
                # decoder or its filter graph could not be built for THIS
                # camera's bitstream, which is a reason to drop a rung -- not
                # a reason to keep reconnecting to a healthy camera.  Checked
                # before the noise filter so a hint is never masked.
                if any(h in s for h in _kDecodeFaultHints):
                    self._decode_bad_hint = True
                if any(h in s for h in _kConnectFailHints):
                    self._connect_failed = True
                # ffmpeg's repeat suppression stands in for whatever it last
                # printed, so this line is worth exactly as much as that one
                # was.  Following a message we just dropped it is pure spam --
                # 1-2 lines/sec per camera, enough on its own to roll every
                # 5MB camera log several times a day and erase the history
                # needed to debug anything else (measured 2026-08-29: 545 of
                # them on 05_Gate_lr).  Following a real error it says that
                # error recurred, which is worth keeping.
                if 'Last message repeated' in s:
                    if not self._lastStderrDropped:
                        self._log("remux ffmpeg: " + s)
                    continue

                # Drop non-actionable decoder/tee chatter (see _kBenignRemuxNoise);
                # keep genuine errors.
                if any(n in s for n in _kBenignRemuxNoise):
                    self._lastStderrDropped = True
                    continue
                self._lastStderrDropped = False
                self._log("remux ffmpeg: " + s)
        except Exception:
            pass

    def _learnSourceRung(self):
        """Re-pick the decode rung once this camera has recorded something.

        The ladder is chosen in __init__ from _probeStreamInfo, which reads a
        LOCAL segment -- so a camera that has never recorded (fresh install,
        or an archive that was just wiped) reports "source unknown" and keeps
        the memory-hungry generic nvdec rung for the life of the process.
        Only a camera that RECONNECTS is ever re-probed, which inverts the
        intent: on 2026-08-09 the eleven HEALTHY cameras were the ones stuck
        on GPU decode, holding the 4 GB card at 94%, while the four flaky ones
        had correctly dropped to software.

        Called at spawn, where a segment from the previous run is on disk and
        answers for free -- probing the URI instead would open the extra
        camera session single-stream exists to avoid.  (Reading it from
        ffmpeg's own stderr would be cheaper still, but the recorder runs at
        `-loglevel error`, which prints no stream banner at all.)

        Only ever narrows the ladder, and only while the head rung is still
        the one __init__ picked -- a rung that _run() demoted failed for a
        reason no probe result undoes.  Never forces a restart of its own: it
        lands on the next spawn the recorder was going to do anyway.
        """
        if self._src_known or self._rung != 0:
            return
        segs = self._listSegments()
        if not segs:
            return
        # Newest first, but never the one ffmpeg may still be writing: it has
        # no moov atom yet and cannot be probed at all, so trying costs a
        # guaranteed-to-fail launch on the reconnect path.  With nothing else
        # on disk, wait for the next spawn.
        line = ''
        for path in reversed(segs[:-1][-2:]):
            try:
                res = subprocess.run(
                    [_get_ffmpeg(), '-hide_banner', '-nostdin', '-i', path],
                    stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                    timeout=20,
                    creationflags=_kNoWindow)
            except Exception:
                continue
            text = res.stderr.decode('utf-8', 'replace')
            line = next((l for l in text.splitlines() if 'Video:' in l), '')
            if line:
                break
        if not line:
            return
        m = re.search(r'Video:\s*([A-Za-z0-9]+).*?(\d{2,5})x(\d{2,5})', line)
        if not m:
            return
        self._src_known = True
        codec, w = m.group(1).lower(), int(m.group(2))
        cuvid = _kCuvidByCodec.get(codec)
        rungs = ['cuvid', 'nvdec', 'sw']
        if not cuvid:
            rungs.remove('cuvid')
        if w and w <= _kSoftwareDecodeMaxWidth:
            rungs = ['sw']
        if tuple(rungs) == self._rungs:
            return
        self._log("remux: camera records %s %dpx wide -- analysis decode "
                  "%s -> %s" % (codec, w, self._rungs[0], rungs[0]))
        # Deliberately NOT touching _src_size: analysisSize() has already
        # committed to a value, and the recorder's scale filter and the frame
        # source's size must stay in lockstep or every frame shears.
        self._cuvid = cuvid
        self._rungs = tuple(rungs)

    def _analysisFrames(self):
        """Frames the reader has taken delivery of, for the rung check."""
        if self._analysis_frames_fn is None:
            return 0
        try:
            return int(self._analysis_frames_fn())
        except Exception:
            return 0

    def _drainPcm(self, proc):
        try:
            for chunk in iter(lambda: proc.stdout.read(16384), b''):
                if not chunk:
                    break
                if self._writer is not None:
                    try:
                        self._writer.write(chunk)
                    except Exception:
                        pass
        except Exception:
            pass

    @staticmethod
    def _parseSegMs(name):
        """Segment filename (local time, second precision) -> epoch ms."""
        try:
            t = time.strptime(os.path.basename(name)[:17], '%Y-%m-%d-%H%M%S')
            return int(time.mktime(t) * 1000)
        except Exception:
            return None

    def _listSegments(self):
        # `.gap.mp4` / `.rt.mp4` are OUR OWN in-flight rewrites, written beside
        # the segment they came from -- and they end in '.mp4', so without this
        # a sweep landing mid-rewrite treats one as a segment and archives it.
        # The gap fill re-encodes, which can take seconds on 4K, so its window
        # is wide enough that 13 reached the archive on the first day.
        try:
            names = [f for f in os.listdir(self._seg_dir)
                     if f.endswith('.mp4')
                     and not f.endswith(('.gap.mp4', '.rt.mp4', '.df.mp4'))]
            names.sort()
            return [os.path.join(self._seg_dir, f) for f in names]
        except Exception:
            return []

    def _finalize(self, path, first_ms, last_ms):
        if first_ms is None or last_ms is None or last_ms <= first_ms:
            try:
                os.remove(path)
            except Exception:
                pass
            return
        if not os.path.isfile(path):
            # Somebody already archived it.  start()'s rescue and a previous
            # run's _run thread can both be looking at this directory -- stop()
            # only joins that thread for 45s and gives up -- so the same
            # segment gets finalized twice and the loser retries a file that is
            # no longer there.  670 FileNotFoundError lines in one morning.
            self._final_fails.pop(path, None)
            return
        self._deFragment(path)
        try:
            self._finalize_cb(path, first_ms, last_ms)
            self._final_fails.pop(path, None)
            self._lastArchivedMs = int(time.time() * 1000)
        except Exception as e:
            # Transient locks (e.g. the faststart rewrite as a segment
            # closes) resolve on a later sweep — never delete a locked file,
            # but don't log every retry either.
            n = self._final_fails.get(path, 0) + 1
            self._final_fails[path] = n
            if n == 1 or n % 30 == 0:
                self._log("remux: finalize failed for %s (attempt %d): %r" %
                          (os.path.basename(path), n, e))

    @staticmethod
    def _isFragmented(path):
        """Does this file carry moof boxes (i.e. is it still the in-flight
        container)?  Top-level box walk, no ffmpeg."""
        try:
            size = os.path.getsize(path)
            with open(path, 'rb') as f:
                off = 0
                while off < size:
                    f.seek(off)
                    hdr = f.read(16)
                    if len(hdr) < 8:
                        return False
                    bsz = struct.unpack('>I', hdr[0:4])[0]
                    typ = hdr[4:8]
                    if typ == b'moof':
                        return True
                    if typ == b'mdat' and bsz == 0:
                        return False          # unfinished, runs to EOF
                    if bsz == 1:
                        bsz = struct.unpack('>Q', hdr[8:16])[0]
                    elif bsz == 0:
                        return False
                    if bsz < 8:
                        return False
                    off += bsz
        except Exception:
            return False
        return False

    def _deFragment(self, path):
        """Rewrite a fragmented segment as an ordinary faststart mp4.

        Keeps the ARCHIVE format exactly what it has always been.  Normally
        _reTimestampSegment has already produced a faststart file and this is a
        no-op, but it bails out for B-frame streams (`_rt_safe`) and whenever
        retimestamping is off -- and on those paths the raw fragmented segment
        would otherwise be what lands in the archive.  Stream copy, so it costs
        one cheap ffmpeg and changes no pixels.
        """
        if not self._isFragmented(path):
            return
        tmp = path + '.df.mp4'
        cmd = [_get_ffmpeg(), '-y', '-hide_banner', '-loglevel', 'error',
               '-i', path, '-map', '0', '-c', 'copy',
               '-movflags', '+faststart', tmp]
        try:
            rc = subprocess.run(cmd, capture_output=True, timeout=120,
            creationflags=_kNoWindow)
            if rc.returncode == 0 and os.path.getsize(tmp) > 0:
                os.replace(tmp, path)
                return
            self._log("remux: de-fragment failed for %s: %s"
                      % (os.path.basename(path),
                         rc.stderr.decode('utf-8', 'replace')[:160]))
        except Exception as e:
            self._log("remux: de-fragment error for %s: %r"
                      % (os.path.basename(path), e))
        try:
            os.remove(tmp)
        except Exception:
            pass
        # Archive it fragmented rather than lose it -- ffmpeg reads either.

    def _brokenDir(self):
        return os.path.join(self._seg_dir, _kBrokenSubdir)

    def _quarantineSegment(self, path):
        """Move an unplayable segment out of the way, keeping the bytes.

        Deleting was the old behaviour and it was wrong twice over.  It threw
        away real footage -- a stub still holds every 256 KiB the muxer managed
        to flush, and 21 of the 23 found on 2026-08-10 held between 256 KB and
        5 MB of video.  And it did not even work: os.remove loses to the lock
        the exiting ffmpeg still has, the file stayed in the segment dir, and a
        later sweep registered it as footage.

        Moving to a subdirectory settles both.  _listSegments never looks in
        subdirectories, so a stub cannot come back as a clip however many
        sweeps run, and the bytes survive for tools/recover_stub.py.
        """
        name = os.path.basename(path)
        try:
            d = self._brokenDir()
            os.makedirs(d, exist_ok=True)
            shutil.move(path, os.path.join(d, name))
            self._log("remux: quarantined unplayable segment %s "
                      "(%d bytes kept)"
                      % (name, self._sizeOf(os.path.join(d, name))))
        except Exception as e:
            # Still locked by the dying ffmpeg.  Leave it: the next sweep will
            # try again, and it can no longer be registered in the meantime.
            self._log("remux: cannot quarantine %s yet (%r)" % (name, e))

    @staticmethod
    def _sizeOf(path):
        try:
            return os.path.getsize(path)
        except Exception:
            return 0

    def _pruneBroken(self):
        """Age quarantined stubs out so the directory cannot grow forever."""
        d = self._brokenDir()
        cutoff = time.time() - _kBrokenKeepSecs
        try:
            for f in os.listdir(d):
                p = os.path.join(d, f)
                try:
                    if os.path.getmtime(p) < cutoff:
                        os.remove(p)
                except Exception:
                    continue
        except Exception:
            pass

    def _pruneSnapshots(self):
        """Drop ring snapshots older than the retention window."""
        self._pruneBroken()
        if not self._snapshot:
            return
        cutoff = time.time() - _kSnapshotRingSecs
        try:
            for f in os.listdir(self._snapshot):
                p = os.path.join(self._snapshot, f)
                try:
                    if os.path.getmtime(p) < cutoff:
                        os.remove(p)
                except Exception:
                    continue
        except Exception:
            pass

    def _sampleAnalysisLag(self):
        """Append one (wall_ms, frames delivered) sample to the probe's ring.

        Called once per sweep tick AFTER the segment listing, so every segment
        this sweep can see opened before the newest sample and _deliveredAt can
        always bracket it rather than extrapolate.
        """
        self._lagSamples.append(
            (int(time.time() * 1000), self._analysisFrames()))
        if len(self._lagSamples) > _kLagSampleRing:
            del self._lagSamples[:-_kLagSampleRing]

    def _deliveredAt(self, ms):
        """Frames delivered as of wall time `ms`, interpolated between samples.

        None when `ms` predates the ring.  Guessing there would put a
        fabricated number into a measurement whose only value is being
        trustworthy -- better to skip the run than to report a wrong lag.
        """
        s = self._lagSamples
        if ms is None or not s or ms < s[0][0]:
            return None
        if ms >= s[-1][0]:
            return float(s[-1][1])
        lo, hi = 0, len(s) - 1
        while hi - lo > 1:
            mid = (lo + hi) // 2
            if s[mid][0] <= ms:
                lo = mid
            else:
                hi = mid
        t0, f0 = s[lo]
        t1, f1 = s[hi]
        if t1 <= t0:
            return float(f1)
        return f0 + (f1 - f0) * (ms - t0) / float(t1 - t0)

    def analysisLagMs(self):
        """Measured demux->delivery backlog for THIS camera, in ms.

        0 when the correction is off, when nothing has been measured yet, or
        when this recorder owns no analysis socket -- in every one of those
        cases the honest answer is "apply nothing", never a guess.  The median
        of the recent window rejects a single stepped segment; the clamp keeps
        a pathological reading from throwing timestamps across the archive.
        """
        if not self._lag_correct or not self._lagRecent:
            return 0
        vals = sorted(self._lagRecent)
        med = vals[len(vals) // 2]
        return max(0, min(_kMaxLagCorrectionMs, int(med * 1000)))

    def _reportAnalysisLag(self, path, nxt, run):
        """LOG ONLY: how far analysis frame delivery trails the demux.

        Applies no correction.  `path` is a COMPLETE segment and `nxt` is its
        successor, so `nxt`'s -strftime name is the instant ffmpeg had demuxed
        exactly the frames of `path` and everything before it in this run.  Two
        counts are compared at that instant: `_lagCum`, every frame demuxed
        since the run's first segment opened, and the socket counter
        interpolated to the same two instants.  Both come from the SAME ffmpeg,
        so the difference is the backlog sitting in decode/scale/hwdownload/
        socket -- no clock, no vision, no calibration.

        Anchoring on the RUN's first segment is what makes the difference a
        backlog rather than a rate: a fresh ffmpeg has demuxed nothing and
        delivered nothing, so the backlog there is genuinely zero.

        -strftime names are second-precision, so each endpoint's open instant is
        up to 1s early.  That cancels between the two endpoints when segments
        roll on a steady phase and otherwise shows up as sub-second noise on a
        single reading, which is why the numbers are worth averaging over a few
        log lines rather than read off one.
        """
        if run != self._lagRun or self._lagBase is None:
            return
        try:
            # A SEGMENT THAT CANNOT BE COUNTED POISONS THE RUN.  `delivered` is
            # cumulative from the run's start, so skipping one segment's frames
            # in `_lagCum` while the socket counter runs on would subtract that
            # whole segment from the lag and keep reporting confidently.  Drop
            # the run instead; the next respawn re-anchors within minutes.
            d = self._deliveredAt(self._parseSegMs(nxt))
            n = self._probeVideoFrameCount(path)
            if d is None or not n:
                self._lagBase = None
                return
            self._lagCum += n
            delivered = d - self._lagBase
            dur = _probe_duration_ms(path)
            fps = (n / (dur / 1000.0)) if dur else 0.0
            behind = self._lagCum - delivered
            secs = (behind / fps) if fps else 0.0
            # Record BEFORE the log throttle: the correction wants every
            # segment's estimate, while the log only wants one a minute.
            # `fps` must be real -- without a duration `secs` collapses to a
            # tidy-looking 0.0 that is not a measurement of anything, and
            # feeding those into the median quietly drags the correction down.
            # A negative reading is likewise not a negative lag; it means the
            # two counts disagree (the state this probe spent three defects
            # escaping), so drop it rather than push a stamp forward.
            # `fps` comes from n/duration on ONE segment, and on the lossy
            # cameras the duration probe is unreliable: 11_Fire Pit produced
            # 3.8, 54.5, 77.0 and 121.4 fps on a ~14 fps stream, which swung
            # the SAME ~58-frame backlog between +0.45s and +9.13s.  A rate
            # nowhere near the camera's own is a broken duration, not a fast
            # camera, and the seconds derived from it are meaningless.
            plausible = _kMinPlausibleFps <= fps <= _kMaxPlausibleFps
            if plausible and 0.0 <= secs <= (_kMaxLagCorrectionMs / 1000.0):
                self._lagRecent.append(secs)
            now = time.monotonic()
            if now - self._lagLoggedAt < _kAnalysisLagLogSecs:
                return
            self._lagLoggedAt = now
            drops = ""
            if self._analysis_dropped_fn is not None:
                try:
                    drops = ", queue-dropped %d" % int(
                        self._analysis_dropped_fn())
                except Exception:
                    pass
            self._log(
                "analysis lag probe: demuxed %d, delivered %.0f, behind %.0f "
                "frame(s) at %.1f fps = %+.2fs%s"
                % (self._lagCum, delivered, behind, fps,
                   (behind / fps) if fps else 0.0, drops))
        except Exception as e:
            self._log("analysis lag probe failed: %r" % e)

    def _sweep(self, closing):
        """Finalize completed segments.  While ffmpeg runs, everything except
        the newest file (still being written) is complete.  When closing a
        run, the newest is complete too — its length comes from a probe."""
        self._pruneSnapshots()
        segs = self._listSegments()
        if self._lag_probe:
            # AFTER the listing, so every segment named above opened before
            # this sample and _deliveredAt never has to extrapolate forward.
            self._sampleAnalysisLag()
        if not segs:
            return
        # Tag each segment with the run that wrote it.  Only the active (or
        # just-ended) run can create files, so first sighting decides.
        for path in segs:
            name = os.path.basename(path)
            if name not in self._seg_run:
                self._seg_run[name] = self._run_id
                # segs is name-sorted, so the first one we meet for a run is
                # that run's opening segment (needed after its file is gone).
                self._run_first_seg.setdefault(self._run_id, name)
                if self._lag_probe and self._run_id != self._lagRun:
                    # First segment of a new run: the zero-backlog anchor.
                    # Totals are per RUN because the analysis source
                    # deliberately survives recorder respawns -- its counter
                    # carries on while the segments start over -- so the base
                    # has to be re-read at each new ffmpeg.
                    self._lagRun = self._run_id
                    self._lagBase = self._deliveredAt(self._parseSegMs(name))
                    self._lagCum = 0
                    # A respawn empties the pipeline, so the previous run's
                    # backlog says nothing about this one.  Keep correcting
                    # with the old median until the new run measures its own,
                    # rather than dropping to zero for a segment -- a stamp
                    # that jumps 1.8s and back is worse than a slightly stale
                    # one.  _lagRecent ages out within _kLagCorrectionWindow.
        runOf = lambda p: self._seg_run.get(os.path.basename(p))

        pending = segs if closing else segs[:-1]
        # A closing sweep runs inside stop(), on the path that must release the
        # camera session promptly.  Finalizing is not cheap -- a 4K segment can
        # need a re-timestamp copy and even a gap-fill re-encode -- so it gets
        # a budget.  Segments left behind are not lost: start() rescues every
        # playable one ("recovered N unswept segment(s) from the previous run").
        deadline = (time.time() + _kClosingSweepBudgetSecs) if closing else None
        for i, path in enumerate(pending):
            if deadline is not None and time.time() > deadline:
                self._log("remux: closing sweep out of time; leaving %d "
                          "segment(s) for the next start to recover"
                          % (len(pending) - i))
                break
            name_ms = self._parseSegMs(path)
            nxt = segs[i + 1] if i + 1 < len(segs) else None
            if nxt is not None and runOf(nxt) == runOf(path):
                # Same ffmpeg: it opened that segment the instant this one
                # ended, so its name IS this segment's end.
                last_ms = self._parseSegMs(nxt)
            else:
                # Last segment of its run.  Any file after it was opened by a
                # DIFFERENT ffmpeg, whose name is its own first-write wall
                # time and so trails its content by that run's RTSP setup
                # (measured: 6.4s on 06_Garage, 6.9s on 07_Back_Yard).  Dating
                # this segment from that name pushed it seconds late, which
                # invented a recording gap in front of it, overlapped the file
                # behind it, and made playback jump the timeline mid-clip
                # while the camera's burned-in clock ran on smoothly.  The
                # run's own end is when THIS content stopped.
                last_ms = self._run_end_ms.get(runOf(path))
                if last_ms is None:
                    last_ms = int(time.time() * 1000)  # still running
            dur = _probe_duration_ms(path)
            if dur is None or dur < 1000:
                # UNPROBEABLE MEANS UNREGISTERABLE, wherever it sits in the
                # run.  This used to drop the segment only when it was the
                # NEWEST file and otherwise fall through to `first_ms =
                # name_ms` ("chained but unprobeable"), and that branch is
                # where every bad row in the archive came from: on 2026-08-10
                # there were 23 clipdb rows across 10 cameras pointing at mp4s
                # with no moov, one of them registering 20.6s of footage that
                # could not be opened at all.  The path is reachable because
                # os.remove FAILS while the dying ffmpeg still holds the file
                # (see the lock note in _finalize) -- 08_FrontStep logged
                # "dropping unusable segment 2026-08-10-034244.mp4" three times
                # in six seconds, a later run created a newer file, and the
                # stub was no longer newest.  start()'s own recovery already
                # applies the strict rule (dur and dur >= 1000); this makes the
                # sweep agree with it.
                self._quarantineSegment(path)
                continue
            else:
                runStart = self._run_start_ms.get(runOf(path))
                isRunFirst = (self._run_first_seg.get(runOf(path)) ==
                              os.path.basename(path))
                if isRunFirst and runStart is not None:
                    # A run's OPENING segment is dated forward from the spawn,
                    # not backward from its successor.  Backward assumes the
                    # segment holds a full window of content, and after a
                    # reconnect it often doesn't (the stream stalls, frames
                    # drop) — that dated 09_Jungle's post-reconnect segment
                    # 18.8s late.  Measured against the burned-in clock, real
                    # content starts 1-2s BEFORE the spawn on both cameras
                    # tested (the camera replays its last keyframe), so the
                    # spawn is the closest anchor we have and errs by ~1-2s
                    # instead of tens of seconds.
                    first_ms = runStart
                    last_ms  = first_ms + dur
                    anchorIsStart = True
                else:
                    # Anchor the START at (end - duration): on a fresh RTSP
                    # connect cameras REPLAY from their previous keyframe, so
                    # a segment can hold content from BEFORE its filename's
                    # wall time — anchoring by name made the program timeline
                    # run ahead of the camera's burned-in clock.
                    first_ms = (last_ms or 0) - dur
                    anchorIsStart = False
                if name_ms is not None and first_ms > name_ms:
                    # A segment cannot hold content newer than the moment its
                    # file was created, so the name is a hard UPPER bound on
                    # the start.  The backward anchor breaks that whenever
                    # content is missing inside the segment -- on a lossy
                    # long-range wifi camera it dated segments up to 32s late
                    # (measured against the burned-in clock; the clamp cuts
                    # that to ~11s, the camera's own delivery lag, which no
                    # wall-clock anchor can see).
                    first_ms = name_ms
                    last_ms  = first_ms + dur
                    anchorIsStart = True
                if name_ms is not None and abs(first_ms - name_ms) > 30000:
                    # Sanity: don't trust wild anchors.  Fall back to the name
                    # AND rebuild the end from it -- keeping the bad last_ms
                    # would register a span hours long, which then swallows
                    # unrelated searches.
                    first_ms = name_ms
                    last_ms  = first_ms + dur
                    anchorIsStart = True
                # Smooth the bunched-timestamp stutter by re-stamping to the
                # segment's true constant rate; runs only when we have a real
                # duration.
                # Never re-encode on the teardown path.  stop() joins this
                # thread for 45s and then start() runs; a gap fill on a badly
                # gapped 4K segment can outlast that, so the join timed out,
                # the segments never got swept, and the next start() deleted
                # them.  Closing sweeps take the cheap copy-only path; the
                # footage keeps its gaps rather than being lost.
                # The substream BUFFER skips all of this.  Its segments are a
                # rolling 600s window that is mostly pruned unread, they arrive
                # 3x as often as archive segments (20s vs 60s), and the few
                # that ever reach the archive are re-cut by remuxSubClip on
                # promotion anyway -- so smoothing and black-filling them buys
                # nothing and costs ~6 ffmpeg launches each.  Measured
                # 2026-08-07: clip registration was running 2-14 minutes behind
                # the recording, and the five worst cameras were all substream
                # ones while the four best had no substream at all.
                # The next same-run segment's filename is the moment ffmpeg
                # wrote its first packet, so this file was open for exactly
                # that long -- a hard ceiling on how much time it can hold.
                nxt_ms = self._parseSegMs(nxt) if (
                    nxt is not None and runOf(nxt) == runOf(path)) else None
                wallSpan = (nxt_ms - name_ms) if (
                    nxt_ms is not None and name_ms is not None) else None

                # No same-run successor -- this is the LAST segment of a run.
                # Both sanity checks downstream (_ptsSpanIsPlausible, and the
                # span cap further below) are keyed on wallSpan and fail OPEN
                # when it is None, so without a fallback the one segment most
                # likely to carry a jumped camera clock -- the one where the
                # stream just reconnected -- is the only one nothing guards.
                #
                # Measured 2026-08-18 on 12_Ravine_lr: its 06:15:01 segment sat
                # at a run boundary, its camera timeline jumped ~197s, and with
                # wallSpan=None the jump was read as an outage and painted
                # black.  The file registered 228.6s, overlapped the next FOUR
                # clips, and every wall-clock seek into that window landed in
                # fabricated black -- five snapshots came out pure black and
                # playback of the window was black too.
                #
                # mtime is when the last packet was written, so mtime - name is
                # a real ceiling on how much time the file can hold.  It is
                # LOOSER than the same-run successor (a re-encode extends
                # mtime, and a dead recorder leaves it late), which is why it is
                # only a fallback -- but loose beats absent, and on that segment
                # it reads 50.2s against a 228.6s claim.  Verified on the
                # healthy neighbours too: 66.7s and 63.6s for ~60s segments, so
                # it does not false-trigger.
                if wallSpan is None and name_ms is not None:
                    try:
                        mtime_ms = int(os.path.getmtime(path) * 1000)
                        if mtime_ms > name_ms:
                            wallSpan = mtime_ms - name_ms
                            self._log("remux: %s has no same-run successor; "
                                      "using mtime for the %.1fs wall ceiling"
                                      % (os.path.basename(path),
                                         wallSpan / 1000.0))
                    except Exception:
                        pass
                actual = (self._reTimestampSegment(path, dur,
                                                   allowFill=not closing,
                                                   wall_span_ms=wallSpan)
                          if self._retimestamp else None)
                if actual and abs(actual - dur) > 100:
                    # The rewrite settled on a different length than the raw
                    # container claimed (it re-stamps to the audio clock, which
                    # a stalled video PTS span can overstate by tens of
                    # seconds).  The span registered here MUST match what the
                    # file holds: playback seeks to (wanted - firstMs), so a
                    # span longer than the media lands past the end -- an event
                    # whose thumbnail shows a person plays as footage without
                    # one.  Keep whichever end was anchored above and move the
                    # other one.
                    self._log("remux: %s holds %.2fs but its container claimed "
                              "%.2fs; registering the real length"
                              % (os.path.basename(path), actual / 1000.0,
                                 dur / 1000.0))
                    if anchorIsStart:
                        last_ms = first_ms + actual
                    else:
                        first_ms = last_ms - actual
                # A file cannot hold more time than it was OPEN for.  The fill
                # and the audio-clock re-stamp can each overshoot that -- on
                # 2026-08-09 09_Jungle's 124054.mp4 was written over a 35s
                # window (its successor's filename) from a container claiming
                # 29.6s, and came back 45.7s long.  Registering that pushed the
                # run's end 10.7s past the moment the NEXT file was created, and
                # the overlap slide below then carried the error into every
                # remaining segment of the run: the whole 12:41-13:20 timeline
                # ran ~16s late, so a person detected at 13:17:22 played as
                # empty patio from 13:17:04.
                #
                # Trim the fabricated tail rather than the content: the excess
                # is black fill and stalled-clock padding, and keeping the
                # anchored end fixed keeps (wanted - firstMs) landing where
                # playback expects for every real frame.
                if (wallSpan is not None and first_ms is not None
                        and last_ms is not None):
                    cap = wallSpan + int(_kSegSpanSlackSecs * 1000)
                    if (last_ms - first_ms) > cap:
                        self._log("remux: %s registered %.2fs but its file was "
                                  "only open %.2fs; capping the span"
                                  % (os.path.basename(path),
                                     (last_ms - first_ms) / 1000.0,
                                     wallSpan / 1000.0))
                        if anchorIsStart:
                            last_ms = first_ms + cap
                        else:
                            first_ms = last_ms - cap
            # A segment cannot start after the moment its own file was created.
            # The clamp further up enforces that when the anchors are chosen,
            # but THREE later steps can each break it again: the `actual`
            # length correction above (measured 2026-08-09 -- 08_FrontStep's
            # 162450.mp4 held 27.5s of content in a 40s window after a stall,
            # and the backward correction dated it 12.0s late), the span cap,
            # and the overlap slide below.  Enforce it once here, after the
            # adjustments and BEFORE the slide, so the slide still gets to do
            # its job within the bound.  Keep the span: playback seeks to
            # (wanted - firstMs), so it must go on matching the media.
            if (name_ms is not None and first_ms is not None
                    and last_ms is not None and first_ms > name_ms):
                span = last_ms - first_ms
                first_ms = name_ms
                last_ms = first_ms + span
            # Consecutive segments of one run share no packet, so their rows
            # cannot legitimately overlap.  Each is dated independently above,
            # though, and the backward anchor (first = end - duration) can
            # reach back past where the previous one was registered to end.
            # Slide this one forward rather than shortening it: playback seeks
            # to (wanted - firstMs), so the span MUST keep matching the media.
            if (first_ms is not None and last_ms is not None
                    and self._last_reg_run == runOf(path)
                    and self._last_reg_end_ms is not None
                    and first_ms < self._last_reg_end_ms):
                shift = self._last_reg_end_ms - first_ms
                if name_ms is not None:
                    # Never slide past the moment THIS file was created.  That
                    # is the same hard bound the anchor block above enforces,
                    # but this runs after it, so without re-applying it here a
                    # single over-long neighbour ratchets the segment forward
                    # -- and because the shifted end becomes the next
                    # segment's floor, the error is inherited by the whole
                    # rest of the run and never recovers (it only clears when
                    # a recorder restart resets _last_reg_run).  A small
                    # residual overlap is the honest signal that the PREVIOUS
                    # row was registered too long; a drifting timeline is not.
                    shift = min(shift, max(0, name_ms - first_ms))
                first_ms += shift
                last_ms += shift
            if last_ms is not None:
                if self._last_reg_run != runOf(path):
                    # A new run: its own timeline starts here.  Carrying the
                    # previous run's end forward would leave this run
                    # unclamped until it passed that end.
                    self._last_reg_end_ms = last_ms
                    self._last_reg_run = runOf(path)
                else:
                    self._last_reg_end_ms = max(self._last_reg_end_ms or 0,
                                                last_ms)
            if self._lag_probe and nxt is not None:
                self._reportAnalysisLag(path, nxt, runOf(path))
            self._finalize(path, first_ms, last_ms)

        # Forget segments that have been finalized out of the dir, and runs no
        # surviving segment refers to (the active run always stays).
        alive = set(os.path.basename(p) for p in self._listSegments())
        for name in list(self._seg_run):
            if name not in alive:
                del self._seg_run[name]
        keep = set(self._seg_run.values()) | {self._run_id}
        for d in (self._run_end_ms, self._run_start_ms, self._run_first_seg):
            for rid in list(d):
                if rid not in keep:
                    del d[rid]

    def _run(self):
        if AudioRingWriter is not None and self._ring_path:
            try:
                self._writer = AudioRingWriter(self._ring_path, 44100, 2)
            except Exception as e:
                self._log("remux: ring writer create failed: %r" % e)
                self._writer = None
        backoff = 0.5
        while not self._stop.is_set():
            self._flush_req.clear()
            try:
                proc = self._spawn()
            except Exception as e:
                self._log("remux: spawn failed: %r" % e)
                if self._stop.wait(backoff):
                    break
                backoff = min(backoff * 2, 15.0)
                continue
            # A spawn started before stop() was called can finish after it:
            # subprocess.Popen alone takes ~3s on this machine (every ffmpeg
            # launch gets scanned), which is long enough for stop() to kill
            # the old process, sweep orphans and return -- leaving this brand
            # new one holding the camera session behind its back.  Kill it
            # here, where we still have the handle.
            if self._stop.is_set():
                try:
                    proc.kill()
                    proc.wait(timeout=5)
                except Exception:
                    pass
                break
            with self._lock:
                self._proc = proc
            spawn_t = time.time()
            frames_at_spawn = self._analysisFrames()
            if self._lag_probe:
                # Seed the ring at the spawn instant.  This run's first segment
                # opens once RTSP is up, seconds from now, and the probe has to
                # interpolate the delivered count back to THAT moment -- which
                # needs a sample from before it.  On the very first run of a
                # process there is no earlier sweep to provide one.
                self._lagSamples.append(
                    (int(spawn_t * 1000), frames_at_spawn))
            lastAnalysisFrames = frames_at_spawn
            lastAnalysisAt = spawn_t
            self._decode_bad_hint = False
            self._connect_failed = False
            # New ffmpeg = new run.  Segments are dated per run (see _sweep):
            # only a segment's OWN run can say where its content starts/ends.
            self._run_id += 1
            self._run_start_ms[self._run_id] = int(spawn_t * 1000)
            threading.Thread(target=self._drainStderr, args=(proc,),
                             daemon=True).start()
            if self._audio_ok:
                threading.Thread(target=self._drainPcm, args=(proc,),
                                 daemon=True).start()

            # Supervise: finalize segments as they complete, until ffmpeg
            # exits (error), we're asked to stop/flush (graceful 'q'), or it
            # goes quiet without exiting (the watchdog below).
            while proc.poll() is None:
                self._lastProcAliveAt = time.time()
                if self._stop.is_set() or self._flush_req.is_set():
                    self._signalQuit()
                    try:
                        # Shorter than stop()'s join, deliberately: a graceful
                        # 'q' only has to write the moov atom, so anything
                        # longer means ffmpeg is wedged.  Waiting 10s here (the
                        # old value) outlived that join, so stop() had to kill
                        # the process itself on the way out -- measured 8 times
                        # in 17 minutes on 2026-08-03.
                        proc.wait(timeout=_kRecorderQuitWaitSecs)
                    except Exception:
                        proc.kill()
                    break
                self._sweep(closing=False)

                # Watchdog.  What matters is footage REACHING THE ARCHIVE, not
                # merely ffmpeg rolling files: on 2026-08-02 08_FrontStep rolled
                # segments normally for 5m40s while every finalize failed, so a
                # watchdog that only watched the recorder saw nothing wrong and
                # 5m40s of footage -- including a person detection the user went
                # looking for -- never landed.  Either symptom means the same
                # thing to the user: no video.  Segments rolling still counts as
                # progress, so a camera mid-segment is never restarted early.
                # Rolling a file is NOT progress -- that was the trap: segments
                # rolled on time for 5m40s while every finalize failed, so a
                # roll-based check stayed quiet through the whole hole.  Only
                # an archived segment counts.  The spawn time seeds the grace
                # period so a freshly started recorder gets its full window.
                # Analysis frames have stopped while ffmpeg is ALIVE and
                # segments are still reaching the archive.  The recording half
                # is demonstrably fine, so only the analysis output needs
                # re-establishing: respawn ffmpeg (~1s).  The alternative is
                # letting CameraCapture's 15s stream timeout fire, which tears
                # down the entire camera -- and that teardown is what orphaned
                # this ffmpeg onto the camera's session and span the 45-100s
                # loss loop measured on 2026-08-03.  Gated on a RECENT archived
                # segment so a camera that is actually down falls through to
                # the normal reconnect/backoff instead of being respawned every
                # few seconds.
                if (self._analysis_port is not None
                        and self._analysis_frames_fn is not None):
                    nowT = time.time()
                    got = self._analysisFrames()
                    if got != lastAnalysisFrames:
                        lastAnalysisFrames = got
                        lastAnalysisAt = nowT
                    recordingOk = (
                        self._lastArchivedMs > 0 and
                        (nowT * 1000 - self._lastArchivedMs)
                        < _kAnalysisStallNeedsArchiveSecs * 1000)
                    quiet = nowT - lastAnalysisAt
                    if (quiet > _kAnalysisStallSecs
                            and nowT - spawn_t > _kAnalysisStallSecs
                            and recordingOk):
                        self._log("remux: no analysis frames for %.0fs while "
                                  "recording continues -- respawning ffmpeg "
                                  "to restore detection without restarting "
                                  "the camera" % quiet)
                        try:
                            proc.kill()
                        except Exception:
                            pass
                        break

                lastProgress = max(self._lastArchivedMs / 1000.0, spawn_t)
                stalled = time.time() - lastProgress
                if stalled > _kRecorderStallSecs:
                    self._log("remux: nothing reached the archive in %.0fs "
                              "(ffmpeg still running) -- restarting the "
                              "recorder; that time is NOT recorded" % stalled)
                    try:
                        proc.kill()
                    except Exception:
                        pass
                    break

                # Hand SHORT segments back once this run has proven healthy.
                # The verdict is otherwise only revisited when a run ENDS, so a
                # recorder that recovers and then stays up keeps recording 15s
                # segments for its whole life -- days, off one bad startup run.
                # Cycle through the flush path: it closes the current segment
                # gracefully, and _flush_req makes the run-end check above skip
                # this deliberate restart rather than reading it as instability.
                if (self._unstable
                        and not self._flush_req.is_set()
                        and not self._stop.is_set()
                        and time.time() - spawn_t >= _kRemuxPromoteAfterSecs
                        and stalled < self._segment_secs):
                    self._log("remux: %.0fs healthy on short segments -- "
                              "returning to normal (%ds) segments"
                              % (time.time() - spawn_t, self._segment_secs))
                    self._unstable = False
                    self._last_flush = time.time()
                    self._flush_req.set()
                    self._signalQuit()
                    continue

                if self._stop.wait(1.0):
                    continue  # loop once more to take the stop branch
            with self._lock:
                self._proc = None
            # This run has stopped writing: its last segment's content ends
            # here.  Recorded BEFORE any _sweep below, which needs it.
            self._run_end_ms[self._run_id] = int(time.time() * 1000)

            ran_secs = time.time() - spawn_t

            # A run that delivered NO analysis frames while stderr blamed the
            # decoder indicts the RUNG, not the camera: 08_FrontStep records
            # perfectly by stream-copy yet kills the hardware decoder's h264
            # parser.  Drop a rung instead of reconnecting forever into the
            # same failure.  Permanent for this process -- see _rungs.
            #
            # Evaluated BEFORE the segment-length verdict below, which needs to
            # know whether this is what ended the run.
            rungDemoted = False
            if (self._analysis_port is not None
                    and self._analysis_frames_fn is not None
                    and self._rung < len(self._rungs) - 1
                    and self._decode_bad_hint
                    and not self._connect_failed
                    and not self._stop.is_set()
                    and not self._flush_req.is_set()
                    and self._analysisFrames() == frames_at_spawn):
                self._log("remux: %s decode delivered no analysis frames in "
                          "%.0fs and stderr blamed the decoder -- this camera "
                          "falls back to %s"
                          % (self._rungs[self._rung], ran_secs,
                             self._rungs[self._rung + 1]))
                self._rung += 1
                rungDemoted = True

            # Did this run live long enough to complete a segment?  If not, the
            # in-progress one was discarded and that footage is gone -- so the
            # next run uses short segments to cap what a repeat costs.  A flush
            # is a deliberate cycle, not instability, so it does not count.
            #
            # Neither is a rung demotion.  It is a one-time, self-correcting
            # probe of a decoder we were never sure of, and the run it ends is
            # short BY CONSTRUCTION -- the whole point is to find out fast and
            # move down.  Counting it condemned every GPU-less machine to 15s
            # segments permanently: nvdec cannot init, the run dies in ~3s, and
            # the software run that follows and works perfectly inherits the
            # verdict (Hyper-V VM, 2026-08-14).  The camera said nothing about
            # itself here; only our decoder choice did.
            if (not self._flush_req.is_set() and not self._stop.is_set()
                    and not rungDemoted):
                wasUnstable = self._unstable
                self._unstable = ran_secs < self._segment_secs
                if self._unstable != wasUnstable:
                    self._log("remux: run lasted %.0fs -> %s segments"
                              % (ran_secs, "SHORT (%ds, unstable camera)"
                                 % _kRemuxShortSegmentSecs if self._unstable
                                 else "normal (%ds)" % self._segment_secs))
            elif rungDemoted and self._unstable:
                # Demoting also clears a verdict an EARLIER decode rung caused:
                # the next run uses a different decoder, so the old evidence no
                # longer describes it.
                self._unstable = False
                self._log("remux: decode rung changed -- back to normal (%ds) "
                          "segments" % self._segment_secs)

            if self._stop.is_set():
                self._sweep(closing=True)   # shutdown: finalize the last segment
                break
            if self._flush_req.is_set():
                self._flush_req.clear()
                # A flush cycles ffmpeg ~once per segment.  Finalizing the just-
                # closed segment here -- above all the 4K re-timestamp copy --
                # would block the respawn by seconds and widen the very gap the
                # flush creates.  closing=False leaves that newest file alone;
                # the next run's _sweep(closing=False) finalizes it ~1s after
                # respawn, off the reconnect-critical path.  (On shutdown/real
                # drop below, closing=True still catches it, and start() wipes
                # the temp dir, so it can never be orphaned across a restart.)
                self._sweep(closing=False)
                continue  # immediate restart after an on-demand flush

            # ffmpeg exited on its own -- there's already a real gap, so finalize
            # the just-closed segment before we back off and reconnect.
            self._sweep(closing=True)
            if ran_secs < 10:
                self._fast_fails += 1
                # Only fall back to video-only when stderr blamed the audio
                # outputs ("Output file ... does not contain any stream" =
                # camera has no audio track).  Fast connect failures from an
                # offline/flaky camera must NOT permanently disable audio for
                # when the camera comes back.
                if (self._fast_fails >= 2 and self._audio_ok
                        and self._audio_bad_hint):
                    self._audio_ok = False
                    self._log("remux: audio outputs rejected - retrying "
                              "video-only (camera reports no audio track)")
                    self._fast_fails = 0
            else:
                self._fast_fails = 0
                backoff = 0.5
            self._audio_bad_hint = False
            self._log("remux: ffmpeg exited (ran %.1fs); reconnecting" %
                      ran_secs)
            if self._stop.wait(backoff):
                break
            backoff = min(backoff * 2, 15.0)

        if self._writer is not None:
            try:
                self._writer.close()
            except Exception:
                pass
            self._writer = None


class _AnalysisFrameSource:
    """Analysis frames read from the RECORDER's ffmpeg -- no second session.

    Under single-stream, the one ffmpeg that stream-copies the archive also
    emits a GPU-scaled 640-wide bgr24 stream.  That stream has to leave the
    process somehow, and on Windows there are only two usable channels: the
    recorder already owns stdout (the live-audio PCM tee), stderr carries the
    log, and `pipe:3` does not work -- Python cannot hand a CRT fd to a child.
    So the frames come out over a loopback TCP socket: this object listens,
    ffmpeg connects OUT to it (`-f rawvideo tcp://127.0.0.1:<port>`).

    It exposes the slice of cv2.VideoCapture that _capture_loop uses, so the
    loop, _stampFrameMs, the mmap live-view publish and getNewFrame are all
    unchanged.

    TWO WIRE FORMATS, chosen by SV_ANALYSIS_PTS (see _kDefaultAnalysisPts):

      nut  frames arrive muxed, each carrying its own PTS, which
           CAP_PROP_POS_MSEC hands to _stampFrameMs.  Its anchor then removes
           the socket backlog from the timestamp entirely, which is the whole
           point: a frame delayed by decode/scale/socket still reports when it
           was CAPTURED, so detections line up with the recorded video.
           libavformat does the demuxing (through cv2), so there is no
           hand-rolled parser on this path.

      raw  headerless rawvideo, fixed bytes per frame, no timestamps.
           CAP_PROP_POS_MSEC returns 0.0 and _stampFrameMs falls back to
           stamping on arrival -- the historical behaviour, and the reason
           detection boxes trailed their subjects by 0.7-2.3s.

    IT SURVIVES RECORDER RESPAWNS, and that is the point.  The recorder cycles
    its ffmpeg deliberately (flush ~1x/segment, the stall watchdog, any stream
    drop).  If that ended the capture, every flush would tear down the whole
    camera -- tens of seconds -- instead of the ~1s the respawn actually
    costs.  So EOF is not failure: the reader waits for the next ffmpeg to
    connect and keeps going.  Fate-sharing is preserved either way, because no
    frames flow while no recording ffmpeg exists.

    A DEDICATED THREAD drains the socket, because this socket is an output of
    the RECORDING ffmpeg: if it ever filled, ffmpeg would block on it and the
    archive would stall too.  Under the old two-session shape a slow analysis
    loop could only starve itself.

    FRAMES ARE RELEASED AT THE CAMERA'S MEASURED RATE, not as fast as they
    arrive, and that pacing is load-bearing -- getting it wrong cost real
    detections on 2026-08-03.  These cameras deliver in ~500 ms bursts.  The
    old path software-decoded inside the capture loop at ~9.6 ms/frame, which
    spread each burst over ~100 ms, so CameraCapture's ~33/s poll of the
    ONE-DEEP StreamReader._latest_frame slot collected most of the frames.
    Move that decode into ffmpeg and the loop costs 0.6 ms/frame: a burst of
    ten frames then publishes in ~6 ms and nine are overwritten before the
    poller looks.  Peak analysed fps fell ~20 -> ~11 on every camera and two
    people were logged as brief generic "object" tracks instead of "person".
    So: queue a burst and hand it out evenly, which is what the decode cost
    used to do by accident.  Frames carry their ARRIVAL time so the pacing
    delay never lands in an event's timestamp.
    """

    _kAcceptPollSecs = 0.5
    # Hold at most this much camera time; a burst is ~0.5 s, and anything
    # older is worth dropping rather than analysing late.
    _kMaxQueueSecs = 0.6
    # Until the rate has been measured, assume a middling camera.
    _kDefaultFps = 15.0

    def __init__(self, location='', logFn=None, usePts=False):
        self._location = location or ''
        self._logFn = logFn
        self._usePts = bool(usePts)
        self._w = self._h = 0
        self._frameBytes = 0
        self._conn = None              # socket, or cv2 demuxer on the PTS path
        self._closed = False
        self._connections = 0
        self._frames = 0
        self._dropped = 0
        self._q = deque()              # (arrivalMs, ptsMs, frame), oldest first
        self._arrivals = deque(maxlen=64)   # monotonic times, for the rate
        self._nextRelease = 0.0
        self._lastArrivalMs = 0
        self._lastPtsMs = 0.0
        self._gaveUp = False
        self._cond = threading.Condition()
        self._pump = None
        self._lastDataAt = time.monotonic()
        self._srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        # Loopback only: nothing outside this machine may attach to a camera's
        # video, and binding to 0 lets the OS pick a free port per camera.
        self._srv.bind(('127.0.0.1', 0))
        self._srv.listen(2)
        self._srv.settimeout(self._kAcceptPollSecs)
        self._port = self._srv.getsockname()[1]
        if self._usePts:
            # libavformat has to bind this port itself, so hand it over now --
            # while still holding the reservation, so nothing can take it in
            # between.  Only the listen is released; the port stays ours.
            self._srv.close()
            self._srv = None

    # -- wiring ---------------------------------------------------------
    @property
    def port(self):
        return self._port

    @property
    def frames(self):
        """Frames delivered since construction.  The recorder samples this
        across a run to tell a bad decode rung from a bad camera."""
        return self._frames

    def setFrameSize(self, w, h):
        """Must match the recorder's scale filter EXACTLY -- a raw stream has
        no framing, so a wrong size doesn't error, it silently shears every
        frame.  Both sides take this from _RemuxRecorder.analysisSize().

        Starts the drain thread: nothing may read this socket before the
        frame size is known, and nothing may leave it unread afterwards.
        """
        self._w, self._h = int(w), int(h)
        self._frameBytes = self._w * self._h * 3
        # On the PTS transport the container states the size and libavformat
        # frames every packet, so a mismatch here cannot shear anything -- but
        # the size is still what the queue cap and the live view are sized from.
        if self._pump is None and self._frameBytes > 0 and not self._closed:
            self._pump = threading.Thread(target=self._pumpLoop, daemon=True)
            self._pump.start()

    def _log(self, msg):
        if self._logFn:
            try:
                self._logFn(msg)
            except Exception:
                pass

    # -- internals ------------------------------------------------------
    def _accept(self, deadline):
        """Wait for the (next) ffmpeg to connect.  None if it never does.

        On the PTS transport this opens a cv2 demuxer in listen mode instead of
        accepting a bare socket.  The open is bounded by CAP_PROP_OPEN_TIMEOUT_MSEC
        and retried, for the same reason the raw path polls accept() on a short
        timeout: release() has to be able to interrupt a camera that is waiting
        for a recorder that will never come.
        """
        if self._usePts:
            url = 'tcp://127.0.0.1:%d?listen=1' % self._port
            while not self._closed and time.monotonic() < deadline:
                # ONE long open, not a poll: libavformat binds the port only
                # while this call is waiting, so a short timeout would drop the
                # listener and rebind it over and over -- and a recorder that
                # connected into one of those gaps got no analysis stream at
                # all (measured: 6 frames, then nothing).  Waiting out the whole
                # budget in a single call keeps the port continuously bound, the
                # way the raw path's always-listening socket does.  release()
                # unblocks it; see the poke there.
                remaining = max(0.5, deadline - time.monotonic())
                cap = cv2.VideoCapture(
                    url, cv2.CAP_FFMPEG,
                    [cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, int(remaining * 1000),
                     cv2.CAP_PROP_READ_TIMEOUT_MSEC,
                     int(_kAnalysisReadTimeoutSecs * 1000)])
                if cap.isOpened():
                    self._connections += 1
                    if self._connections > 1:
                        self._log('analysis: recorder reconnected (stream %d)'
                                  % self._connections)
                    return cap
                cap.release()
            return None

        while not self._closed and time.monotonic() < deadline:
            try:
                conn, _ = self._srv.accept()
            except socket.timeout:
                continue
            except OSError:
                return None
            conn.settimeout(_kAnalysisConnectWaitSecs)
            self._connections += 1
            if self._connections > 1:
                self._log('analysis: recorder reconnected (stream %d)'
                          % self._connections)
            return conn
        return None

    def _readFrame(self):
        """One (ptsMs, payload), or None if this connection ended.

        payload is a decoded BGR ndarray on the PTS transport and a raw byte
        buffer on the raw one; read() turns either into the same ndarray.
        ptsMs is 0.0 when the transport carries no timestamps.

        Reads straight into a fresh buffer rather than accumulating chunks:
        at 14 cameras this path carries ~170 MB/s, so a join here and a
        further copy in read() would be two extra full-frame copies per
        frame for nothing.  A FRESH buffer each time (not a reused one) is
        what lets read() hand out a numpy view with no copy at all -- the
        consumer may still hold the previous frame.
        """
        if self._usePts:
            ok, frame = self._conn.read()
            if not ok or frame is None:
                return None
            self._lastDataAt = time.monotonic()
            # PTS in ms on the stream's own timeline.  It restarts at 0 whenever
            # the recorder respawns its ffmpeg; _stampFrameMs notices the jump
            # back and re-anchors (_kPtsBackJumpMs).
            return (self._conn.get(cv2.CAP_PROP_POS_MSEC), frame)

        need = self._frameBytes
        buf = bytearray(need)
        view = memoryview(buf)
        got = 0
        while got < need:
            try:
                n = self._conn.recv_into(view[got:], need - got)
            except socket.timeout:
                return None
            except OSError:
                return None
            if not n:
                # Partial frame at EOF is discarded, never emitted: half a
                # frame joined to the next connection's first bytes would
                # shear every frame after it.
                return None
            got += n
        self._lastDataAt = time.monotonic()
        return (0.0, buf)

    def _pumpLoop(self):
        """Keep the socket drained no matter how slow the consumer is."""
        try:
            self._pumpForever()
        finally:
            # This thread owns the connection; nothing else may close it while
            # a read is in flight.  See release().
            conn, self._conn = self._conn, None
            if conn is not None:
                try:
                    if self._usePts:
                        conn.release()
                    else:
                        conn.close()
                except Exception:
                    pass

    def _pumpForever(self):
        while not self._closed:
            if self._conn is None:
                self._conn = self._accept(
                    time.monotonic() + _kAnalysisConnectWaitSecs)
                if self._conn is None:
                    if self._closed:
                        break
                    self._log('analysis: no recorder connected for %.0fs'
                              % _kAnalysisConnectWaitSecs)
                    with self._cond:
                        self._gaveUp = True
                        self._cond.notify_all()
                    break
            got = self._readFrame()
            if got is None:
                # This ffmpeg is gone.  Wait for its replacement rather than
                # ending the capture (see the class docstring).
                try:
                    if self._usePts:
                        self._conn.release()
                    else:
                        self._conn.close()
                except Exception:
                    pass
                self._conn = None
                continue
            ptsMs, raw = got
            with self._cond:
                self._frames += 1
                self._arrivals.append(time.monotonic())
                # Stamp on ARRIVAL: the release below deliberately holds
                # frames back, and that delay must not end up in the event
                # timestamps the way a read-time stamp would.  On the PTS
                # transport the arrival time is only used to ANCHOR the frame's
                # own PTS, so even this residual delay drops out.
                self._q.append((int(time.time() * 1000), ptsMs, raw))
                cap = self._queueCap()
                while len(self._q) > cap:
                    self._q.popleft()
                    self._dropped += 1
                self._cond.notify()

    def _measuredFps(self):
        """Delivery rate over the recent window (bursts included)."""
        a = self._arrivals
        if len(a) >= 8:
            span = a[-1] - a[0]
            if span > 0.2:
                return max(1.0, (len(a) - 1) / span)
        return self._kDefaultFps

    def _queueCap(self):
        return max(3, int(self._kMaxQueueSecs * self._measuredFps()))

    # -- cv2.VideoCapture-compatible surface -----------------------------
    def isOpened(self):
        return not self._closed and self._frameBytes > 0

    def read(self):
        """@return (ret, bgrFrame) -- the OLDEST queued frame, released no
        faster than the camera's measured rate (see the class docstring).

        ret is False only when the recorder has produced nothing for
        _kAnalysisConnectWaitSecs; a respawn in between is absorbed.
        """
        if self._closed or not self._frameBytes:
            return (False, None)
        while True:
            with self._cond:
                while not self._q and not self._closed and not self._gaveUp:
                    if not self._cond.wait(
                            timeout=_kAnalysisConnectWaitSecs + 5):
                        return (False, None)
                if self._closed or (self._gaveUp and not self._q):
                    return (False, None)
                interval = 1.0 / self._measuredFps()
                # Falling behind: release a little faster so the queue drains
                # instead of dropping frames off the back.
                if len(self._q) > 0.75 * self._queueCap():
                    interval *= 0.7
                now = time.monotonic()
                delay = self._nextRelease - now
                if delay <= 0:
                    arrivalMs, ptsMs, raw = self._q.popleft()
                    self._lastArrivalMs = arrivalMs
                    self._lastPtsMs = ptsMs
                    # max(now, ...) so an idle stretch cannot bank up credit
                    # and then dump a whole burst at once.
                    self._nextRelease = max(now, self._nextRelease) + interval
                    break
                wait = min(delay, interval)
            time.sleep(wait)      # outside the lock: never block the pump
        if self._usePts:
            # Already a decoded ndarray, allocated per frame by libavformat.
            return (True, raw)
        # No .copy(): the buffer was allocated for this frame alone and is
        # never reused, so the array can own it outright.
        return (True, np.frombuffer(raw, np.uint8).reshape(
            self._h, self._w, 3))

    @property
    def frameArrivalMs(self):
        """Epoch ms at which the frame just returned by read() ARRIVED, so
        _stampFrameMs can time the event by capture rather than by release."""
        return self._lastArrivalMs

    @property
    def dropped(self):
        """Frames dropped off the back of the queue because the consumer
        could not keep up -- NOT frames the camera failed to send."""
        return self._dropped

    @property
    def connections(self):
        """How many ffmpegs have attached so far.  The analysis lag probe
        counts frames against the recorder's segments, and those two totals
        only line up within ONE connection -- a respawn restarts the segment
        side while this object's frame counter carries on."""
        return self._connections

    def get(self, prop):
        if prop == cv2.CAP_PROP_FRAME_WIDTH:
            return float(self._w)
        if prop == cv2.CAP_PROP_FRAME_HEIGHT:
            return float(self._h)
        if prop == cv2.CAP_PROP_POS_MSEC:
            # The PTS of the frame read() just handed out, which is what lets
            # _stampFrameMs time an event by when it was captured rather than
            # when it reached us.  0.0 on the raw transport, which is exactly
            # the "no PTS" signal _stampFrameMs already handles.
            return float(self._lastPtsMs)
        # No meaningful CAP_PROP_FPS -- the capture loop measures the true rate
        # from frame timestamps anyway.
        return 0.0

    def set(self, prop, value):
        return False

    def release(self):
        self._closed = True
        if self._usePts:
            # Deliberately NOT releasing the capture here.  The pump thread is
            # normally sitting inside its read(), and releasing a VideoCapture
            # under a concurrent read throws out of OpenCV's C++ (seen in
            # testing).  A socket tolerates being closed from another thread;
            # this does not.  So the pump owns the capture's whole life and
            # tears it down itself once it sees _closed -- all we do is make
            # sure it gets there promptly.
            #
            # A listen-mode open blocks inside libavformat where _closed cannot
            # reach it, and holds the port for as long as it waits.  Connecting
            # to ourselves lets that accept return, so the pump can see _closed
            # and let the port go -- otherwise restarting this camera would find
            # its own port still taken.
            try:
                socket.create_connection(('127.0.0.1', self._port),
                                         timeout=0.5).close()
            except Exception:
                pass
        for sock in (self._conn, self._srv):
            try:
                if sock is not None:
                    sock.close()
            except Exception:
                pass
        self._conn = None
        with self._cond:
            self._cond.notify_all()     # wake a parked read()
        pump, self._pump = self._pump, None
        if pump is not None and pump is not threading.current_thread():
            pump.join(timeout=5)


class _NvdecCapture:
    """NVDEC (GPU) camera capture that downscales BEFORE the host download.

    Measured motivation: decode is ~80% of a camera process's CPU, split
    between the Python capture thread (GPU->host download + BGR pack + resize)
    and ffmpeg's internal C decoder threads.  Both scale with pixel count, and
    the analysis pipeline caps frames to ~640 wide anyway -- so today a 1280
    substream is decoded, downloaded and then thrown away by a CPU resize.
    `-c:v <codec>_cuvid -resize WxH` decodes AND downscales on the GPU, so only
    the small frame crosses to the host: it removes both halves of that cost.

    Exposes the small slice of the cv2.VideoCapture API the capture loop uses
    (isOpened/read/get/set/release) so it can be swapped in without touching
    the loop.  CAP_PROP_POS_MSEC returns None -- a raw pipe carries no PTS --
    which makes _stampFrameMs fall back to its existing wall-clock path (the
    same one used for cameras whose PTS is dead).
    """

    # Cap decode surfaces: 15 cameras share one GPU with ~2.5 GB free.
    _kSurfaces = 6
    _kSpawnTimeoutSecs = 20

    def __init__(self, uri, requestedW=0, requestedH=0, capWidth=0,
                 forceTcp=True, logFn=None, minWidth=0):
        """Open an NVDEC capture, downscaling on the GPU.

        The output size is decided HERE (from the probed source size) because
        the caller can't know it yet -- it normally learns the frame size from
        the capture itself.  Applying the analysis cap up front is the whole
        point: those pixels must never be decoded to host memory.

        @param  requestedW/H  Explicit size (camera recordSize), 0 = native.
        @param  capWidth      Cap the width to this (0 = no cap), matching the
                              analysis cap so the GPU does that resize.
        """
        self._uri = uri
        self._logFn = logFn
        self._proc = None
        self._opened = False
        self._pending = None
        self._fps = 0.0

        decoder, self._srcW, self._srcH, self._fps = self._probe(uri, forceTcp)
        if decoder is None or not (self._srcW and self._srcH):
            return

        if requestedW and requestedH:
            self._w, self._h = int(requestedW), int(requestedH)
        else:
            self._w, self._h = self._srcW, self._srcH
        # Never decode SMALLER than the live view needs: the live-view frame is
        # resized from this one, so decoding below it would visibly soften the
        # enlarged camera.  minWidth is the live-view width when a camera is
        # enlarged (0 for previews, which are tiny).
        if minWidth and capWidth and minWidth > capWidth:
            capWidth = min(minWidth, self._srcW)
        if capWidth and self._w > capWidth:
            scale = float(capWidth) / self._w
            self._w = capWidth
            self._h = max(2, int(round(self._h * scale / 2.0)) * 2)
        # cuvid needs even dimensions.
        self._w -= self._w % 2
        self._h -= self._h % 2
        self._frameBytes = self._w * self._h * 3
        try:
            self._proc = subprocess.Popen(
                self._buildCmd(uri, decoder, forceTcp),
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                stdin=subprocess.DEVNULL,
                creationflags=_kNoWindow)
        except Exception as e:
            self._log('nvdec: spawn failed: %r' % e)
            return
        # Only call it open once a real frame has arrived: cuvid can start and
        # then fail (unsupported profile, GPU busy), and we must fall back
        # BEFORE the camera is considered live.
        self._pending = self._readExact()
        if self._pending is None:
            self._log('nvdec: no first frame; falling back to cv2')
            self.release()
            return
        self._opened = True

    # ------------------------------------------------------------------
    def _log(self, msg):
        if self._logFn:
            try:
                self._logFn(msg)
            except Exception:
                pass

    def _buildCmd(self, uri, decoder, forceTcp):
        cmd = [_get_ffmpeg(), '-hide_banner', '-loglevel', 'error', '-nostdin']
        if uri.lower().startswith(('rtsp://', 'rtp://', 'udp://')):
            if forceTcp:
                cmd += ['-rtsp_transport', 'tcp']
            cmd += ['-timeout', '5000000']
        # Low latency: don't let ffmpeg build a buffer ahead of us.
        cmd += ['-fflags', 'nobuffer', '-flags', 'low_delay',
                '-c:v', decoder, '-surfaces', str(self._kSurfaces),
                '-resize', '%dx%d' % (self._w, self._h),
                '-i', uri,
                '-an', '-f', 'rawvideo', '-pix_fmt', 'bgr24', 'pipe:1']
        return cmd

    def _probe(self, uri, forceTcp):
        """Return (cuvidDecoder, srcW, srcH, fps) or (None, 0, 0, 0)."""
        cmd = [_get_ffmpeg(), '-hide_banner', '-nostdin']
        if uri.lower().startswith(('rtsp://', 'rtp://', 'udp://')):
            if forceTcp:
                cmd += ['-rtsp_transport', 'tcp']
            cmd += ['-timeout', '5000000']
        cmd += ['-i', uri]
        try:
            res = subprocess.run(cmd, stdout=subprocess.DEVNULL,
                                 stderr=subprocess.PIPE,
                                 timeout=self._kSpawnTimeoutSecs,
                                 creationflags=_kNoWindow)
            text = res.stderr.decode('utf-8', 'replace')
        except Exception as e:
            self._log('nvdec: probe failed: %r' % e)
            return (None, 0, 0, 0.0)
        line = next((l for l in text.splitlines() if 'Video:' in l), '')
        low = line.lower()
        decoder = None
        for key, dec in (('hevc', 'hevc_cuvid'), ('h265', 'hevc_cuvid'),
                         ('h264', 'h264_cuvid')):
            if key in low:
                decoder = dec
                break
        if decoder is None:
            self._log('nvdec: codec not NVDEC-decodable (%s)' % line.strip()[:80])
            return (None, 0, 0, 0.0)
        w = h = 0
        m = re.search(r'(\d{2,5})x(\d{2,5})', line)
        if m:
            w, h = int(m.group(1)), int(m.group(2))
        fps = 0.0
        m = re.search(r'([\d.]+) fps', low)
        if m:
            try:
                fps = float(m.group(1))
            except ValueError:
                pass
        return (decoder, w, h, fps)

    def _readExact(self):
        """Read exactly one frame's bytes; None on EOF/short read."""
        if self._proc is None or self._proc.stdout is None:
            return None
        need = self._frameBytes
        chunks = []
        got = 0
        try:
            read = self._proc.stdout.read
            while got < need:
                chunk = read(need - got)
                if not chunk:
                    return None
                chunks.append(chunk)
                got += len(chunk)
        except Exception:
            return None
        return b''.join(chunks)

    # -- cv2.VideoCapture-compatible surface --------------------------------
    def isOpened(self):
        return self._opened

    def read(self):
        """@return (ret, bgrFrame) -- ret False once the stream ends."""
        if not self._opened:
            return (False, None)
        if self._pending is not None:
            raw, self._pending = self._pending, None
        else:
            raw = self._readExact()
        if raw is None:
            self._opened = False
            return (False, None)
        return (True, np.frombuffer(raw, np.uint8).reshape(
            self._h, self._w, 3).copy())

    def get(self, prop):
        if prop == cv2.CAP_PROP_FRAME_WIDTH:
            return float(self._w)
        if prop == cv2.CAP_PROP_FRAME_HEIGHT:
            return float(self._h)
        if prop == cv2.CAP_PROP_FPS:
            return float(self._fps)
        # No PTS on a raw pipe -> _stampFrameMs uses its wall-clock fallback.
        return 0.0

    def set(self, prop, value):
        return False        # nothing to tune on a pipe

    def release(self):
        self._opened = False
        self._pending = None
        if self._proc is not None:
            for fn in (lambda: self._proc.stdout.close(),
                       self._proc.kill,
                       lambda: self._proc.wait(timeout=2)):
                try:
                    fn()
                except Exception:
                    pass
            self._proc = None

    # Native size of the SOURCE (before the GPU downscale), for logging.
    @property
    def sourceSize(self):
        return (self._srcW, self._srcH)


class _CaptureProfiler:
    """Where a camera's capture thread spends TIME vs CPU, once per interval.

    Cameras are ~90% of Sighthound's CPU, so the capture path needs real
    numbers before it's optimized.  Both clocks are tracked per stage and the
    distinction is the whole point: `cap.read()` BLOCKS waiting for the next
    network frame, so its wall time is ~the frame interval on every camera
    while its actual CPU cost is much smaller.  Measuring only wall time makes
    an idle, network-bound thread look 100% busy (it did), which would send
    optimization work at the wrong target.

    time.thread_time() is per-thread system+user CPU, so it excludes the wait.

    Stages: decode (cap.read = demux+decode+GPU->host download+BGR pack),
    analysis (resize to the analysis frame + BGR->RGB copy), publish
    (live-view mmap resize + tobytes + write + flush).
    """

    _kStages = ('decode', 'analysis', 'publish')

    def __init__(self, logFn, intervalSecs=60.0):
        self._log = logFn
        self._interval = intervalSecs
        self._t0 = time.time()
        self._n = 0
        self._wall = dict.fromkeys(self._kStages, 0.0)
        self._cpu = dict.fromkeys(self._kStages, 0.0)

    def now(self):
        """A (wall, threadCpu) mark to hand to mark()."""
        return (time.perf_counter(), time.thread_time())

    def mark(self, stage, since):
        """Accumulate wall + CPU for a stage; returns the new mark."""
        cur = (time.perf_counter(), time.thread_time())
        self._wall[stage] += cur[0] - since[0]
        self._cpu[stage] += cur[1] - since[1]
        return cur

    def frameDone(self, mw, mh, nw, nh):
        """Count a frame and emit the summary once per interval."""
        self._n += 1
        elapsed = time.time() - self._t0
        if elapsed < self._interval or not self._n:
            return
        n = self._n
        cpuTotal = sum(self._cpu.values())
        try:
            self._log(
                'profile: %.1f fps | cpu/frame decode %.1f  analysis %.1f  '
                'publish %.1f ms | decode wait %.1f ms | thread %.0f%% of a '
                'core | native %dx%d live %dx%d' %
                (n / elapsed,
                 self._cpu['decode'] * 1000.0 / n,
                 self._cpu['analysis'] * 1000.0 / n,
                 self._cpu['publish'] * 1000.0 / n,
                 (self._wall['decode'] - self._cpu['decode']) * 1000.0 / n,
                 cpuTotal * 100.0 / elapsed, nw, nh, mw, mh))
        except Exception:
            pass
        self._t0 = time.time()
        self._n = 0
        for k in self._kStages:
            self._wall[k] = 0.0
            self._cpu[k] = 0.0


class _Frame:
    """Frame object returned by StreamReader.getNewFrame()."""
    def __init__(self, img_rgb, width, height, ms=None, filename='', wasResized=False):
        self.dummy = False
        self.width = width
        self.height = height
        self.ms = ms if ms is not None else int(time.time() * 1000)
        self.filename = filename
        self.wasResized = wasResized
        # Keep numpy array alive for the lifetime of this frame
        self._data = np.ascontiguousarray(img_rgb)
        self.buffer = self._data.ctypes.data   # plain int — from_address() requires it

    def getLargeFrame(self):
        return None


class StreamReader:
    """OpenCV-backed StreamReader compatible with SighthoundVideo's API."""

    def __init__(self, locationName, clipMgr=None, clipMgrLock=None,
                 tmpPath=None, archivePath=None, userDir=None,
                 logFn=None, *a, **kw):
        self.locationName = locationName or ''
        self._clipMgr      = clipMgr
        self._clipMgrLock  = clipMgrLock
        self._tmpPath      = tmpPath
        self._archivePath  = archivePath
        self._userDir      = userDir
        self._logFn        = logFn

        self._cap = None
        self._running = False
        # Bumped on every open()/close().  The capture thread carries the
        # generation it was started with and stops the moment it no longer
        # matches, so a thread still blocked in read() from a previous
        # connection can never touch the state of the next one.
        self._capGen = 0
        # generation -> monotonic time the capture was handed to its thread.
        # An entry survives until that thread releases the capture, so the size
        # of this is the number of RTSP sessions we are actually still holding
        # on the analysis endpoint -- close() cannot force a release while a
        # read() is in flight, so they can stack across reconnects.  Measured
        # before deciding whether that stacking is starving the recorder's
        # main-stream session; see the plan for this work.
        self._capsHeld = {}
        self._lock = threading.Lock()
        self._latest_frame = None
        self._wsgi_frame = None
        self._thread = None
        self._capture_width = 320
        self._capture_height = 240
        self._mmap_width = 320
        self._mmap_height = 240
        self._mmap_fps = 25.0
        self._record_fps = 25.0   # actual camera FPS — measured in capture loop, never overwritten by live-view
        self._reported_fps = 0.0  # what CAP_PROP_FPS claimed (for logging)
        # Rolling publish rate: how fast frames are ACTUALLY reaching the live
        # mmap (and, from the same loop iteration, _latest_frame -> detection).
        # This is what the monitor view's low-frame-rate warning judges; it used
        # to be handed _mmap_fps, which is only what the UI asked for and so
        # could never be anything but a false positive on a 2fps preview tile.
        # Distinct from _record_fps, which is a one-shot connect-time estimate
        # for the recording timebase and never updates after that.
        self._publish_fps = 25.0
        self._publish_count = 0
        self._publish_window_start = None
        # FPS auto-measurement state (network cameras)
        self._fps_measured = False
        self._fps_first_ms = None
        self._fps_window_start_ms = None
        self._fps_window_count = 0
        # PTS-anchored timestamping state (see _stampFrameMs); reset per open()
        self._pts_offsets = deque(maxlen=_kPtsWindowFrames)
        self._pts_anchor = None          # settled offset, analysis path only
        self._pts_anchor_seen = deque(maxlen=_kPtsAnchorSettleFrames)
        self._pts_anchor_start = None    # wall ms of this connection's first frame
        self._pts_anchor_count = 0
        self._pts_last = None
        self._pts_stale_count = 0
        self._pts_dead = False
        self._pts_mode_logged = False
        self._last_stamp_ms = 0
        # Fixed video pipeline latency bias (ms); env-tunable, see constants
        try:
            self._video_latency_ms = int(os.environ.get(
                'SV_VIDEO_LATENCY_MS', str(_kDefaultVideoLatencyMs)))
        except Exception:
            self._video_latency_ms = _kDefaultVideoLatencyMs
        # GPU (D3D11VA) decode for the analysis stream; env-tunable kill switch
        try:
            self._hw_decode = bool(int(os.environ.get(
                'SV_HW_DECODE', str(_kDefaultHwDecode))))
        except Exception:
            self._hw_decode = bool(_kDefaultHwDecode)
        # NVDEC pipe decode (GPU decode + GPU downscale).  Kill switch:
        # SV_NVDEC=0 falls back to the cv2/D3D11VA path.
        try:
            self._nvdec = bool(int(os.environ.get(
                'SV_NVDEC', str(_kDefaultNvdec))))
        except Exception:
            self._nvdec = bool(_kDefaultNvdec)
        # The recorder's ffmpeg also produces the analysis frames, so this is
        # the analysis capture for every network camera.  None for webcams and
        # local files, which decode their own capture.
        self._analysisSource = None
        self._mmap_handle = None
        self._mmap_mem = None
        self._mmap_id = 0

        # Video writer state (FFmpeg subprocess)
        self._ffmpeg_proc   = None   # subprocess.Popen writing to current clip
        self._writer_path   = None   # full path to the current tmp file
        self._clip_rel_path = None   # path relative to archivePath (used in DB)
        self._clip_first_ms = None
        self._clip_last_ms  = None
        self._prev_clip_rel = ''
        self._frames_written = 0       # frames written to current clip (time anchor)
        self._last_frame_bytes = None  # raw bytes of last real frame (freeze fill)
        self._black_frame_bytes = None # cached black raw frame for current clip dims
        self._audio_capture = None     # persistent _AudioCapture (live + recording)

        self._is_file_source = False   # set True in open() for local MP4/AVI files
        self._is_network = False       # set True in open() for rtsp/http/etc sources
        self._uri = ''                 # camera/source URI — reused as FFmpeg audio input
        self._force_tcp = False        # honour forceTCP for the audio RTSP connection

        # Stream-copy recorder (network cameras with recording configured).
        # When active it fully replaces the legacy decode->libx264 recording
        # path AND _AudioCapture (it feeds the live-audio ring itself).
        self._remux = None

        # Decoupled recording queue — capture loop never blocks on FFmpeg I/O
        self._record_queue  = queue.Queue(maxsize=120)
        self._record_writer = None   # writer thread
        self._lastFlushTime = 0.0    # wall time of last on-demand finalize (writer thread only)

    # ------------------------------------------------------------------
    # Core API used by TestStream and CameraCapture
    # ------------------------------------------------------------------

    _kSegNameRe = re.compile(r'^\d{4}-\d{2}-\d{2}-\d{6}\.mp4$')

    def getHealth(self):
        """Return a small, XML-RPC-safe snapshot of this stream's health.

        This is deliberately read-only and does not probe the camera or spawn
        any process.  It is intended for the existing SystemHealthView.
        """
        now_ms = int(time.time() * 1000)
        result = {
            'state': 'running' if self._running else 'stopped',
            'width': int(self._capture_width or 0),
            'height': int(self._capture_height or 0),
            'fps': float(self._record_fps or 0.0),
            'reportedFps': float(self._reported_fps or 0.0),
            'lastFrameAgeSecs': None,
            'analysisFrames': 0,
            'analysisDropped': 0,
            'analysisFps': None,
            'decoder': 'unknown',
            # recorderAlive alone cannot say whether a recorder is DEAD or
            # simply not configured -- both read False, because the flag is
            # only ever overwritten inside the `rec is not None` block below.
            # The health view needs to tell those apart before it flags a
            # camera, or every camera without a remux recorder is reported as
            # a broken one.
            'recorderPresent': False,
            'recorderAlive': False,
            'recorderRung': None,
            'audioOk': None,
            'gapFillDisabled': False,
            'fillSlowRuns': 0,
            'lastArchivedAgeSecs': None,
        }

        # Capture timing.  _last_stamp_ms is the best available timestamp
        # from the actual frame pipeline; avoid reporting negative ages.
        last_ms = self._last_stamp_ms or 0
        if last_ms:
            result['lastFrameAgeSecs'] = max(0.0, (now_ms - last_ms) / 1000.0)

        src = self._analysisSource
        if src is not None:
            try:
                result['analysisFrames'] = int(src.frames)
            except Exception:
                pass
            try:
                result['analysisDropped'] = int(src.dropped)
            except Exception:
                pass
            try:
                result['analysisFps'] = round(float(src._measuredFps()), 2)
            except Exception:
                pass

        rec = self._remux
        if rec is not None:
            result['recorderPresent'] = True
            try:
                result['recorderAlive'] = bool(rec.isAlive())
            except Exception:
                try:
                    result['recorderAlive'] = rec._proc is not None and rec._proc.poll() is None
                except Exception:
                    pass
            try:
                result['recorderRung'] = str(rec._rungs[rec._rung])
                result['decoder'] = result['recorderRung']
            except Exception:
                pass
            try:
                result['audioOk'] = bool(rec._audio_ok)
            except Exception:
                pass
            try:
                result['gapFillDisabled'] = bool(rec._fill_disabled)
                result['fillSlowRuns'] = int(rec._fill_slow_runs)
            except Exception:
                pass
            try:
                archived = int(rec._lastArchivedMs or 0)
                if archived:
                    result['lastArchivedAgeSecs'] = max(0.0, (now_ms - archived) / 1000.0)
            except Exception:
                pass

        return result

    def _probeStreamInfo(self, seg_dir, useArchive=True):
        """(w, h, codec) of this camera's stream, from a LOCAL recorded file.

        The analysis scale has to be sized before a single frame is decoded,
        and the obvious way -- probing the RTSP URI -- opens an EXTRA camera
        session.  That is the very thing single-stream exists to remove, and
        it is what got the old NVDEC capture disabled in July: its probe took
        a second session and timed out on session-limited cameras.  A
        finished segment is a byte-for-byte copy of the live stream, so a
        local file answers the question for free.

        Returns None if this camera has never recorded (first ever run), in
        which case the recorder falls back to a 16:9 default and the next
        open corrects it.
        """
        cands = []

        def _addDir(d, n):
            """Append this directory's n newest real segments, newest first."""
            try:
                names = [f for f in sorted(os.listdir(d))
                         if self._kSegNameRe.match(f)]
            except Exception:
                return
            cands.extend(os.path.join(d, f) for f in reversed(names[-n:]))

        # Archive first: those files are closed and complete.  Segment-dir
        # files are newer but the one ffmpeg was writing has no moov atom and
        # cannot be probed at all, so it would just cost a failed attempt.
        # Only real segment names -- the re-timestamp and gap-fill
        # temporaries live in the same directory (the trap that once let a
        # .gap.mp4 be archived as a clip).
        # useArchive=False for the SUBSTREAM buffer: the archive holds
        # main-stream segments, whose size would mis-scale substream analysis.
        cam = (self.locationName or 'camera').lower()
        base = os.path.join(self._archivePath or '', cam)
        if useArchive and os.path.isdir(base):
            try:
                days = sorted(d for d in os.listdir(base)
                              if os.path.isdir(os.path.join(base, d)))
            except Exception:
                days = []
            for day in reversed(days[-2:]):
                _addDir(os.path.join(base, day), 2)
        if seg_dir and os.path.isdir(seg_dir):
            _addDir(seg_dir, 2)

        for path in cands:
            try:
                res = subprocess.run(
                    [_get_ffmpeg(), '-hide_banner', '-nostdin', '-i', path],
                    stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                    timeout=20,
                    creationflags=_kNoWindow)
                text = res.stderr.decode('utf-8', 'replace')
            except Exception:
                continue
            line = next((l for l in text.splitlines() if 'Video:' in l), '')
            m = re.search(r'(\d{2,5})x(\d{2,5})', line)
            if m:
                w, h = int(m.group(1)), int(m.group(2))
                if w > 0 and h > 0:
                    c = re.search(r'Video:\s*([A-Za-z0-9]+)', line)
                    return (w, h, (c.group(1).lower() if c else ''))
        return None

    def _openCapture(self, src):
        """Open a capture for src, preferring GPU (D3D11VA) decode.

        Hardware decode can open successfully yet fail at the first read, so
        a probe frame validates it before we commit; any trouble falls back
        to software decode.  Only attempted for network streams (file sources
        keep the plain software path).  Returns an opened VideoCapture or
        None.
        """
        # NVDEC pipe first: it decodes AND downscales on the GPU, so the host
        # never sees the full-size frame.  Measured to be ~80% of a camera's
        # CPU when done the cv2 way (capture thread + ffmpeg's C decoder
        # threads).  Falls through to the cv2 paths below on any failure.
        if self._nvdec and self._is_network:
            try:
                # No width cap: this path is only reached by cameras with no
                # recorder (webcams, local files), whose capture IS the
                # recording, so shrinking it would lose the footage.  Network
                # cameras get their analysis frames from the recorder instead.
                cap = _NvdecCapture(src, self._req_w, self._req_h, 0,
                                    self._force_tcp, self._log,
                                    minWidth=self._mmap_width)
                if cap.isOpened():
                    sw, sh = cap.sourceSize
                    outW = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
                    outH = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
                    # Only worth a separate process + pipe when the GPU is
                    # actually shrinking the frame; otherwise cv2 in-process
                    # decode is cheaper (measured: no downscale = no win).
                    if sw and outW * outH > 0.75 * sw * sh:
                        self._log('nvdec: no downscale benefit (%dx%d -> '
                                  '%dx%d); using cv2' % (sw, sh, outW, outH))
                        cap.release()
                    else:
                        self._log('nvdec decode engaged (%dx%d -> %dx%d '
                                  'on GPU)' % (sw, sh, outW, outH))
                        self._nvdecOutW = outW
                        self._nvdecOpenedAt = time.time()
                        return cap
            except Exception as e:
                self._log('nvdec error (%s); using cv2 decode' % e)

        if self._hw_decode and self._is_network:
            try:
                cap = cv2.VideoCapture(src, cv2.CAP_FFMPEG,
                                       [cv2.CAP_PROP_HW_ACCELERATION,
                                        cv2.VIDEO_ACCELERATION_D3D11])
                if cap.isOpened():
                    ret, frame = cap.read()
                    if ret and frame is not None:
                        self._log("hw decode engaged (d3d11va)")
                        return cap
                cap.release()
                self._log("hw decode unavailable; using software decode")
            except Exception as e:
                self._log("hw decode error (%s); using software decode" % e)
        cap = cv2.VideoCapture(src, cv2.CAP_FFMPEG)
        return cap if cap.isOpened() else None

    def open(self, uri, extras=None):
        """Open a camera/RTSP URI.  Returns True on success."""
        if not _cv2_available:
            return False

        requested_w, requested_h = 0, 0
        if extras and 'recordSize' in extras:
            requested_w, requested_h = extras['recordSize']
            if requested_w and requested_h:
                self._capture_width = int(requested_w)
                self._capture_height = int(requested_h)

        # Low-latency capture options, set before VideoCapture() is created.
        # Without these, OpenCV's FFmpeg backend buffers incoming RTSP frames;
        # when the capture loop drains slower than the camera delivers (e.g. CPU
        # contention from analytics), the buffer fills and every read() returns a
        # frame stale by the buffer depth — the recorded VIDEO lags real time, and
        # the separately-captured nobuffer audio, by seconds to minutes.  We mirror
        # the audio capture's flags so the video stream stays equally fresh.
        _is_rtsp = any(uri.lower().startswith(p)
                       for p in ('rtsp://', 'rtmp://', 'rtp://', 'udp://'))
        if _is_rtsp:
            _force_tcp = bool(extras and extras.get('forceTCP'))
            # TCP delivers packets in order, so the demuxer needs almost no
            # reorder headroom — a small max_delay measurably reduces the
            # fixed capture latency (frames reach us sooner, shrinking both
            # the timeline-vs-camera-clock offset and the audio lead).  UDP
            # keeps the larger value since it genuinely reorders.
            _opts = ['fflags;nobuffer', 'flags;low_delay',
                     'max_delay;100000' if _force_tcp else 'max_delay;500000',
                     'reorder_queue_size;0']
            if _force_tcp:
                _opts.insert(0, 'rtsp_transport;tcp')
            os.environ['OPENCV_FFMPEG_CAPTURE_OPTIONS'] = '|'.join(_opts)
        else:
            os.environ.pop('OPENCV_FFMPEG_CAPTURE_OPTIONS', None)

        # Detect local file sources (no rate limiting from camera, need to pace manually)
        _is_network = any(uri.lower().startswith(p)
                          for p in ('rtsp://', 'rtmp://', 'http://', 'https://',
                                    'udp://', 'rtp://'))
        self._is_file_source = not _is_network and os.path.isfile(uri)
        self._is_network = _is_network
        # Remember the source so the recording FFmpeg can pull the camera's audio
        # track directly (OpenCV's capture discards audio entirely).
        self._uri = uri
        self._force_tcp = bool(extras and extras.get('forceTCP'))

        # ONE connection per network camera: the recorder's ffmpeg stream-copies
        # the archive AND emits the analysis frames, so recording and detection
        # share fate -- a detection with no footage is impossible by
        # construction -- at one RTSP session per camera.
        #
        # `single` is therefore just "can this camera be recorded at all".  It
        # is false for webcams and local files, which have no recorder and
        # decode their own capture below; that is the only reason the other
        # branch still exists.
        use_remux = bool(self._is_network and self._tmpPath and
                         self._archivePath and self._clipMgr is not None)
        single = use_remux

        # Live-audio ring + snapshot ring paths.  Computed here rather than
        # further down because the recorder has to exist before anything can
        # read analysis frames from it.
        ring_path = None
        snapshot_path = None
        if self._userDir:
            live_dir = os.path.join(self._userDir, 'live')
            try:
                os.makedirs(live_dir, exist_ok=True)
            except Exception:
                pass
            ring_path = os.path.join(self._userDir, 'live',
                                     self.locationName + '.audio')
            # Directory holding the rolling ring of full-res snapshots.
            snapshot_path = os.path.join(self._userDir, 'live',
                                         self.locationName + '.snaps')

        # open() can be called again on the same instance (reconnect paths in
        # CameraCapture).  A previous recorder/audio session MUST be stopped
        # first: a leaked recorder means TWO ffmpegs writing the same segment
        # dir (file-lock fights, endless finalize retries) and an extra
        # camera session that can trip the camera's session limit.
        #
        # A recorder left running by close(keepRecorder=True) cannot survive
        # this: it OWNS the analysis socket, so a rebuilt _AnalysisFrameSource
        # would be listening on a port the running ffmpeg knows nothing about.
        # It kept recording for the duration of the reconnect, which is the
        # point of keeping it (see CameraCapture's close call) -- but the
        # handover ends here.
        if self.recorderAlive():
            self._log("restarting the recorder: it owns the analysis socket, "
                      "so it cannot outlive a rebuilt analysis source")
        self._stopRecorderAndAudio()

        cam = self.locationName.lower() or 'camera'
        seg_dir = os.path.join(self._tmpPath, cam, '_seg') \
            if self._tmpPath else None

        if single:
            info = self._probeStreamInfo(seg_dir)
            src_size = info[:2] if info else None
            src_codec = info[2] if info else ''
            self._analysisSource = _AnalysisFrameSource(self.locationName,
                                                        self._log,
                                                        _analysisPtsEnabled())
            if not self._startRemux(seg_dir, ring_path, snapshot_path,
                                    analysis_port=self._analysisSource.port,
                                    source_size=src_size,
                                    source_codec=src_codec):
                # There is no second shape to fall back to: the recorder IS the
                # analysis source.  Fail the open loudly and let the caller's
                # normal reconnect/retry path have another go, rather than
                # quietly running a camera in a configuration nobody chose.
                self._log("recorder failed to start; cannot open this camera "
                          "(the recorder is also the analysis source)")
                try:
                    self._analysisSource.release()
                except Exception:
                    pass
                self._analysisSource = None
                return False

            aw, ah = self._remux.analysisSize()
            self._analysisSource.setFrameSize(aw, ah)
            self._cap = self._analysisSource
            # These dims ARE the analysis space: they become the frame
            # size, the mmap source and the clip's procW/H, so detection
            # coordinates line up with what was actually analysed.
            self._capture_width, self._capture_height = aw, ah
            self._log("single stream: analysis %dx%d from the recorder "
                      "(source %s), 1 session for this camera"
                      % (aw, ah,
                         ('%dx%d' % src_size) if src_size else 'unknown'))
        else:
            # Webcams and local files: no recorder, so this decodes its own
            # capture.  Network cameras never reach here -- see `single`.
            self._req_w, self._req_h = requested_w, requested_h
            self._cap = self._openCapture(uri)
            if self._cap is None:
                return False

        # Keep OpenCV's frame buffer shallow so read() returns the freshest frame
        # instead of draining a backlog (reinforces the nobuffer capture options).
        try:
            self._cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        except Exception:
            pass

        # Fresh PTS anchor per connection: POS_MSEC restarts near 0 on a new
        # stream, so a stale anchor from a previous connection is meaningless.
        self._pts_offsets.clear()
        self._resetPtsAnchor()
        self._pts_last = None
        self._pts_stale_count = 0
        self._pts_dead = False
        self._pts_mode_logged = False
        if single and not _analysisPtsEnabled():
            # A raw frame stream carries no PTS.  Say so up front: otherwise
            # the first frame's 0.0 reads as valid PTS and _logPtsMode -- which
            # only ever fires once -- records the wrong mode for the life of
            # the connection, while the code silently falls back to the wall
            # clock 50 frames later anyway.  RECORDING timestamps are
            # unaffected; those are the camera's own RTP timestamps inside the
            # segment, which stream-copy preserves exactly.
            #
            # Conditional since SV_ANALYSIS_PTS: on that transport the frames DO
            # carry their own timestamps, and declaring PTS dead here would have
            # left _stampFrameMs on the wall clock and made the whole change
            # inert -- while every other sign of it working looked right.
            self._pts_dead = True
            self._logPtsMode('wall clock (analysis stream carries no PTS)')
        self._log("video latency bias %d ms (SV_VIDEO_LATENCY_MS)" %
                  self._video_latency_ms)

        # Single-stream already fixed these to the recorder's analysis size --
        # the frames arriving on the socket ARE that size, and re-deriving
        # them from the capture (which reports the same numbers) or capping
        # them again could only introduce a disagreement.
        if not single:
            # If recordSize was (0,0) or absent, use the camera's native res.
            if not (requested_w and requested_h):
                native_w = int(self._cap.get(cv2.CAP_PROP_FRAME_WIDTH))
                native_h = int(self._cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
                if native_w > 0 and native_h > 0:
                    self._capture_width = native_w
                    self._capture_height = native_h

            # Under stream-copy recording these dims drive ONLY analysis/live
            # preview (recording is camera-native in the recorder ffmpeg).
            # Some cameras serve 720p+ "substreams"; motion+YOLO gain nothing
            # from that many pixels, and per-frame copies/MOG2 at 720p burn
            # ~4x the CPU of 360p.  Cap analysis at 640 wide — same class of
            # reduction the Py2 pipeline used for analytics.
            if use_remux and self._capture_width > 704:
                native_w, native_h = self._capture_width, self._capture_height
                scale = 640.0 / native_w
                self._capture_width = 640
                self._capture_height = max(
                    2, int(round(native_h * scale / 2.0)) * 2)
                self._log("analysis capped to %dx%d (stream native %dx%d)" %
                          (self._capture_width, self._capture_height,
                           native_w, native_h))

        # Seed the recording fps from CAP_PROP_FPS, but for network cameras this
        # is often wrong — _capture_loop measures the true rate before recording.
        # File sources have a reliable CAP_PROP_FPS and are paced to it, so we
        # trust it and skip live measurement.
        native_fps = self._cap.get(cv2.CAP_PROP_FPS)
        self._reported_fps = native_fps if native_fps else 0.0
        if native_fps and native_fps > 0:
            self._record_fps = native_fps
        self._fps_measured = self._is_file_source
        self._fps_first_ms = None
        self._fps_window_start_ms = None
        self._fps_window_count = 0

        self._running = True
        self._record_writer = threading.Thread(target=self._record_writer_loop, daemon=True)
        self._record_writer.start()
        # The capture thread OWNS this capture: it is the only thread that
        # touches it and the only one that releases it (see _capture_loop /
        # close()).  Hand it over explicitly rather than letting the thread
        # read self._cap, so a reconnect can install a new capture while an
        # old thread is still unwinding.
        self._capGen += 1
        with self._lock:
            self._capsHeld[self._capGen] = time.monotonic()
            held = len(self._capsHeld)
            oldest = min(self._capsHeld.values())
        if held > 1:
            # We are opening a new session while previous ones are still up.
            # On a camera with a small session budget this is the thing most
            # likely to squeeze out the recorder's main-stream connection.
            self._log("analysis sessions held: %d (oldest orphaned %.0fs ago) "
                      "-- opening another" %
                      (held, time.monotonic() - oldest))
        self._thread = threading.Thread(target=self._capture_loop, daemon=True,
                                        args=(self._cap, self._capGen))
        self._thread.start()

        # Stream-copy recorder for the two-session shape.  Under single-stream
        # it is already running -- it had to be, since it is what produces the
        # analysis frames the capture thread above is reading.
        if use_remux and self._remux is None:
            self._startRemux(seg_dir, ring_path, snapshot_path)

        # Persistent single-session audio capture: only needed when the remux
        # recorder isn't running (it feeds the live ring itself).
        if self._remux is None and self._is_network and self._uri:
            try:
                self._audio_capture = _AudioCapture(
                    self._uri, self._force_tcp, self._is_network,
                    ring_path, self.locationName)
                self._audio_capture.start()
            except Exception:
                self._audio_capture = None

        return True

    def recorderAlive(self):
        """Is the stream-copy recorder still running under its own steam?"""
        try:
            return self._remux is not None and self._remux.isRunning()
        except Exception:
            return False

    def _mayKeepRecorder(self):
        """Is there a recorder worth leaving running across a close()?

        Leaving it up keeps the archive being written while CameraCapture
        re-establishes the analysis stream; stopping it there cost 1113
        recorder restarts and ~24% of all footage in one measured 8-hour
        window.  open() takes it down again when it rebuilds the analysis
        source, since the recorder owns that socket.
        """
        return self.recorderAlive()

    def _stopRecorderAndAudio(self, keepRecorder=False):
        """Tear down a previous recorder / audio session / analysis source.

        open() is called again on the same instance by CameraCapture's
        reconnect paths.  A leaked recorder means TWO ffmpegs writing the same
        segment dir (file-lock fights, endless finalize retries) and an extra
        camera session that can trip the camera's session limit.

        @param  keepRecorder  Leave a HEALTHY recorder running.  Used when the
                              reopen is only about the analysis stream -- see
                              close(keepRecorder=True).
        """
        if keepRecorder and self.recorderAlive():
            # Audio belongs to the recorder (it feeds the ring itself), so it
            # stays too.  Only the analysis source is rebuilt below.
            pass
        elif self._remux is not None:
            try:
                self._remux.stop()
            except Exception:
                pass
            self._remux = None
        if self._audio_capture is not None:
            try:
                self._audio_capture.stop()
            except Exception:
                pass
            self._audio_capture = None
        # Safe to close from here, unlike a cv2 capture: this is a socket, so
        # closing it under an in-flight recv() raises in that thread instead
        # of aborting the process the way freeing an ffmpeg context does.
        if self._analysisSource is not None:
            try:
                self._analysisSource.release()
            except Exception:
                pass
            self._analysisSource = None

    def _startRemux(self, seg_dir, ring_path, snapshot_path,
                    analysis_port=None, source_size=None, source_codec=''):
        """Create and start the stream-copy recorder.  True on success."""
        if not seg_dir:
            return False
        try:
            self._remux = _RemuxRecorder(
                self._uri, self._force_tcp, seg_dir, ring_path,
                self.locationName, _kRemuxSegmentSecs,
                self._registerRemuxSegment, self._log,
                snapshot_path=snapshot_path,
                analysis_port=analysis_port,
                source_size=source_size,
                source_codec=source_codec,
                analysis_frames_fn=(
                    (lambda: self._analysisSource.frames)
                    if analysis_port is not None else None))
            if not self._remux.start():
                self._remux = None
        except Exception as e:
            self._log("remux recorder failed to start: %r" % e)
            self._remux = None
        return self._remux is not None

    # -- substream gap-fill archive -------------------------------------




    # -- cross-stream delta ---------------------------------------------













    def _registerRemuxSegment(self, tmp_file, first_ms, last_ms):
        """Move a finished stream-copy segment into the archive and register
        it in the ClipManager (the remux recorder's finalize callback)."""
        name = os.path.basename(tmp_file)
        cam = self.locationName.lower() or 'camera'
        date_dir = name[:10]                       # YYYY-MM-DD from the name
        rel_path = '/'.join((cam, date_dir, name))

        dst = os.path.join(self._archivePath, cam, date_dir, name)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.move(tmp_file, dst)

        # No timeline correction is applied here any more.  It existed because
        # the archive (main stream) and the detections (substream) were two
        # different clocks, up to 7s apart on 11_Fire Pit.  With one connection
        # there is one clock: these segments and the frames that were analysed
        # come out of the same ffmpeg, so there is nothing to reconcile.
        if self._clipMgr is None:
            return
        prev_rel = self._prev_clip_rel
        self._prev_clip_rel = rel_path
        lock = self._clipMgrLock
        try:
            if lock:
                lock.acquire()
            self._clipMgr.addClip(
                rel_path, self.locationName,
                first_ms, last_ms,
                prev_rel, '',
                _kCacheStatus_Cache,
                self._capture_width, self._capture_height
            )
            self._clipMgr.save()
        except Exception:
            pass
        finally:
            if lock:
                lock.release()

    def _capture_loop(self, cap, gen):
        """Own `cap` for the life of this thread and release it on the way out.

        RELEASING A cv2.VideoCapture WHILE THIS THREAD IS INSIDE read() ABORTS
        THE PROCESS inside opencv_videoio_ffmpeg (0x40000015), which is exactly
        the state a stream timeout leaves us in: no frames for 15s means this
        thread is parked in read() when CameraCapture tears the stream down.
        So close() never releases -- it asks us to stop and we release here,
        after read() has returned, however long that takes.
        """
        try:
            self._capture_loop_inner(cap, gen)
        except Exception:
            self._log('capture loop exited: %s' % traceback.format_exc())
        finally:
            try:
                cap.release()
            except Exception:
                pass
            with self._lock:
                started = self._capsHeld.pop(gen, None)
                held = len(self._capsHeld)
                if self._capGen == gen:
                    self._cap = None
            if started is not None and held:
                # Released late, and others are still out there: this is the
                # stacking the measurement is looking for.
                self._log("analysis session released after %.0fs; %d still "
                          "held" % (time.monotonic() - started, held))

    def _capture_loop_inner(self, cap, gen):
        frame_interval = 1.0 / max(1.0, self._record_fps)  # seconds between frames
        prof = _CaptureProfiler(self._log)
        # Start this connection's publish-rate window fresh, and seed the rate
        # from the connect-time measurement so the front end never sees a bogus
        # 0.0 during the first window.  Clamped: _record_fps can still be a raw
        # CAP_PROP_FPS value at this point, and some RTSP cameras report the
        # 90000 timebase there -- which would overflow the header's %07.2f and
        # push it past the fixed kLiveHeaderSize the front end parses by offset.
        self._publish_fps = max(_kFpsMin, min(_kFpsMax, self._record_fps))
        self._publish_count = 0
        self._publish_window_start = None
        while self._running:
            if self._capGen != gen:
                # A newer connection has taken over (or close() ran while we
                # were blocked in read()).  Everything below writes shared
                # state that now belongs to that connection -- stop here.
                break
            if getattr(self, '_reopen_requested', False):
                # Live-view size changed enough that the GPU decode size should
                # follow it (see setMmapParams).  Drop the capture; the normal
                # reconnect path re-opens it at the new size.
                self._reopen_requested = False
                self._running = False
                break

            t0 = time.time()
            _p = prof.now()
            try:
                ret, bgr = cap.read()
            except Exception:
                # OpenCV can THROW (e.g. "Unknown C++ exception from OpenCV
                # code") instead of returning ret=False when a flaky camera
                # drops mid-read.  Treat it like a failed read so we fall into
                # the normal stop/reconnect path below rather than letting the
                # exception kill this capture thread.
                ret, bgr = False, None
            if not ret:
                if self._is_file_source:
                    # Loop: seek back to first frame and continue
                    cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
                    continue
                self._running = False
                break
            if self._capGen != gen:
                break
            _p = prof.mark('decode', _p)
            native_h, native_w = bgr.shape[:2]
            ms = self._stampFrameMs(cap)

            # Resize to recording dimensions for the Frame object
            if native_w != self._capture_width or native_h != self._capture_height:
                bgr_rec = cv2.resize(bgr, (self._capture_width, self._capture_height))
            else:
                bgr_rec = bgr
            rgb_rec = bgr_rec[:, :, ::-1].copy()
            _p = prof.mark('analysis', _p)

            # Determine current clip filename (for frame.filename)
            cur_filename = ''
            if self._archivePath and self._clip_rel_path:
                cur_filename = os.path.join(self._archivePath, self._clip_rel_path)

            frame = _Frame(rgb_rec, self._capture_width, self._capture_height,
                           ms=ms, filename=cur_filename)

            # Rolling publish rate.  Counted here, on wall clock (t0), rather
            # than from `ms`: that is a stream PTS and need not track real time
            # on a camera that is stalling -- which is exactly the case the
            # warning exists to catch.  Kept outside the lock below so the
            # critical section stays as short as it was.
            if self._publish_window_start is None:
                # The frame that opens a window is its left edge, not a tick
                # inside it -- counting it too would inflate every window by one
                # frame.
                self._publish_window_start = t0
                self._publish_count = 0
            else:
                self._publish_count += 1
                elapsed = t0 - self._publish_window_start
                if elapsed >= _kPublishFpsWindowSecs:
                    self._publish_fps = max(_kFpsMin, min(
                        _kFpsMax, self._publish_count / elapsed))
                    self._publish_window_start = t0
                    self._publish_count = 0

            with self._lock:
                self._latest_frame = frame
                self._wsgi_frame = frame  # kept for WSGI; not consumed by getNewFrame
                if self._mmap_mem is not None:
                    mw = self._mmap_width
                    mh = self._mmap_height
                    try:
                        # Resize from native resolution to mmap dims for best quality
                        if native_w != mw or native_h != mh:
                            rgb_native = bgr[:, :, ::-1]
                            out = cv2.resize(rgb_native, (mw, mh))
                        else:
                            out = bgr[:, :, ::-1].copy()
                        self._mmap_id = (self._mmap_id + 1) % 1000000000
                        # Field 1 is what the front end asked for, field 2 is
                        # what it is actually getting.  These used to both be
                        # _mmap_fps, which made the monitor view's
                        # low-frame-rate warning compare the requested 2fps
                        # preview rate against its own 7.5fps threshold and
                        # triangle every tile.  Width is unchanged (%07.2f), so
                        # an un-restarted front end still parses this fine.
                        header = ('%09d%04d%04d%07.2f %07.2f' % (
                            self._mmap_id, mw, mh,
                            self._mmap_fps, self._publish_fps)).encode('ascii')
                        footer = ('%04d%04d' % (mw, mh)).encode('ascii')
                        self._mmap_mem.seek(0)
                        self._mmap_mem.write(header)
                        self._mmap_mem.write(out.tobytes())
                        self._mmap_mem.write(footer)
                        self._mmap_mem.flush()
                    except Exception:
                        pass
            _p = prof.mark('publish', _p)
            prof.frameDone(self._mmap_width, self._mmap_height,
                           native_w, native_h)

            # Measure the camera's true fps from the first frames before we start
            # recording (CAP_PROP_FPS is often wrong for network cameras).
            if not self._fps_measured:
                self._measureFps(ms)

            # Queue frame for recording once fps is known — never block capture.
            # Not needed when the stream-copy recorder is active: recording
            # happens camera-native in its own ffmpeg, untouched by Python.
            if self._fps_measured and self._remux is None:
                try:
                    self._record_queue.put_nowait((bgr_rec, ms))
                except queue.Full:
                    pass  # drop frame; FFmpeg writer is falling behind

            # Rate-limit file sources to real-time playback
            if self._is_file_source:
                elapsed = time.time() - t0
                sleep_s = frame_interval - elapsed
                if sleep_s > 0:
                    time.sleep(sleep_s)

    def _log(self, msg):
        """Log to the per-camera Python logger (same route _AudioCapture._log
        uses).  NOTE: self._logFn is getCLogFn()'s ctypes (int, c_char_p)
        callback — calling it with a plain Python string silently fails, which
        is why StreamReader's measured-fps lines never appeared in the camera
        logs.  Route informational lines here instead."""
        try:
            logging.getLogger(self.locationName + '.log').info(
                "StreamReader[%s]: %s" % (self.locationName, msg))
        except Exception:
            pass

    def _analysisLagMs(self):
        """This camera's measured analysis backlog, or 0 if there is none.

        Read from the recorder because that is the ffmpeg that both writes the
        segments and feeds the analysis socket -- the two counts whose
        difference is the backlog.  A camera with no recorder (a webcam, or one
        whose open failed) gets no correction, which is right: with no socket
        hop there is nothing to correct.
        """
        rec = self._remux
        if rec is None:
            return 0
        try:
            return int(rec.analysisLagMs())
        except Exception:
            return 0

    def _stampFrameMs(self, cap):
        """Return the capture timestamp (ms since epoch) for the frame that
        cv2.read() just returned.

        Uses the camera's own PTS (CAP_PROP_POS_MSEC) anchored to the wall
        clock so downstream buffering can't distort timestamps — see the
        constants block up top for the full rationale.  Falls back to plain
        wall-clock time for file sources, cameras with dead/stuck PTS, or any
        pathological reading.  Output is guaranteed monotonically increasing.
        """
        wall = int(time.time() * 1000)

        # The single-stream analysis source deliberately HOLDS frames back to
        # pace out the camera's bursts, so "now" is the release moment, not
        # the capture moment.  It hands us the arrival time instead; without
        # this every event would be stamped with the pacing delay included.
        arrival = getattr(cap, 'frameArrivalMs', 0)
        if arrival:
            wall = int(arrival)

        # File sources are paced by the capture loop itself; wall time is the
        # correct stamp there (and no camera latency exists to bias out).
        if self._is_file_source:
            return self._monotonicStamp(wall)

        # Fixed-latency bias applies to every network stamp regardless of
        # mode: the frame was captured this long before we could ever see it.
        bias = self._video_latency_ms

        # MEASURED analysis backlog, on top of the fixed bias.  `arrival` is
        # stamped in _AnalysisFrameSource._pumpLoop the instant a frame comes
        # off the loopback socket -- AFTER nvdec decode, scale, hwdownload and
        # the socket itself.  The archive is dated from the same ffmpeg's
        # segment filenames, written at DEMUX, so the two timelines disagree by
        # exactly the backlog held in between: measured 0.9-1.9s depending on
        # the camera, which is enough to walk a subject clean out of their own
        # detection box.  Gated on `arrival` because only frames that actually
        # came through that socket carry the delay -- a camera falling back to
        # cv2 capture has no such hop, and neither does one with no substream.
        #
        # It applies ONLY where we stamp by arrival.  When the frames carry
        # their own PTS the anchor below removes that same backlog exactly, so
        # subtracting an estimate of it as well would correct twice -- which is
        # why lagBias is kept separate from bias rather than folded into it.
        lagBias = self._analysisLagMs() if arrival else 0

        if self._pts_dead:
            return self._monotonicStamp(wall - bias - lagBias)

        try:
            pts = cap.get(cv2.CAP_PROP_POS_MSEC)
        except Exception:
            pts = None

        # A dead PTS source repeats the same value forever (commonly 0.0 or
        # -1).  Distinguish that from legitimately-valid PTS, which may be
        # negative for the first few frames (decoder reordering) but advances.
        if pts is None or not np.isfinite(pts):
            return self._monotonicStamp(wall - bias - lagBias)
        if self._pts_last is not None and pts == self._pts_last:
            self._pts_stale_count += 1
            if self._pts_stale_count >= _kPtsStaleLimit:
                self._pts_dead = True
                self._logPtsMode("wall clock (PTS not advancing)")
            return self._monotonicStamp(wall - bias - lagBias)
        # PTS jumped backward: the source restarted its timeline mid-stream.
        # Old offsets are meaningless against the new timeline; start over.
        if self._pts_last is not None and pts < self._pts_last - _kPtsBackJumpMs:
            self._pts_offsets.clear()
            self._resetPtsAnchor()
        self._pts_stale_count = 0
        self._pts_last = pts

        # Anchor = the most caught-up moment.  Frames read with backlog get
        # correspondingly older (correct!) stamps.
        offset = wall - pts
        if arrival:
            # Analysis path: settle once per connection and hold.  See
            # _kPtsAnchorSettleFrames.
            anchor = self._settledPtsAnchor(offset, wall)
        else:
            self._pts_offsets.append(offset)
            anchor = min(self._pts_offsets)
        stamped = int(pts + anchor)

        # Sanity: never stamp into the future, and a stamp absurdly far in
        # the past means our anchor state is broken — reset and use wall.
        if stamped > wall:
            stamped = wall
        elif wall - stamped > _kPtsMaxLagMs:
            self._pts_offsets.clear()
            self._resetPtsAnchor()
            self._pts_last = None
            return self._monotonicStamp(wall - bias - lagBias)

        self._logPtsMode("camera PTS (CAP_PROP_POS_MSEC)")
        return self._monotonicStamp(stamped - bias)

    def _resetPtsAnchor(self):
        """Forget a settled anchor, so the next connection measures its own."""
        self._pts_anchor = None
        self._pts_anchor_seen.clear()
        self._pts_anchor_start = None
        self._pts_anchor_count = 0

    def _settledPtsAnchor(self, offset, wall):
        """The PTS-to-wall offset for this connection, settled once and held.

        Returns the best offset seen so far while settling, so stamps are
        usable immediately, and freezes it once enough frames have been seen --
        which is what keeps a segment's timestamps from wandering as a sliding
        window would make them.

        @param  offset  wall - pts for this frame.
        @param  wall    Arrival time of this frame, in epoch ms.
        @return anchor  Offset to add to a PTS to get an epoch stamp.
        """
        if self._pts_anchor is not None:
            return self._pts_anchor

        if self._pts_anchor_start is None:
            self._pts_anchor_start = wall
        # Frames buffered before we attached arrive too promptly to be
        # representative; anchoring on them would time the whole connection
        # early.
        if (wall - self._pts_anchor_start) < _kPtsAnchorWarmupMs:
            return offset

        self._pts_anchor_seen.append(offset)
        self._pts_anchor_count += 1
        seen = sorted(self._pts_anchor_seen)
        estimate = seen[int(_kPtsAnchorPercentile * (len(seen) - 1))]

        if len(seen) >= _kPtsAnchorSettleFrames:
            # Judge steadiness on the same robust span the measurement tool
            # reports, so a single stray frame cannot decide either question.
            spread = (seen[int(0.9 * (len(seen) - 1))] -
                      seen[int(0.1 * (len(seen) - 1))])
            giveUp = self._pts_anchor_count >= _kPtsAnchorMaxFrames
            if spread <= _kPtsAnchorCalmMs or giveUp:
                self._pts_anchor = estimate
                self._log("PTS anchor settled after %d frames; backlog spread "
                          "%d ms%s" % (self._pts_anchor_count, spread,
                                       " (never steadied; settled anyway)"
                                       if giveUp and spread > _kPtsAnchorCalmMs
                                       else ""))
                return self._pts_anchor
            # Still turbulent: keep sliding.  The deque already dropped the
            # oldest offset, so the next frame judges a fresher window.
        return estimate

    def _monotonicStamp(self, ms):
        """Clamp a stamp so timestamps never move backward (the recorder and
        fps measurement assume forward time; the anchor can drop when a
        backlog drains, which would otherwise step time back briefly)."""
        if ms <= self._last_stamp_ms:
            ms = self._last_stamp_ms + 1
        self._last_stamp_ms = ms
        return ms

    def _logPtsMode(self, mode):
        """Log which timestamping mode this stream settled on, once."""
        if self._pts_mode_logged:
            return
        self._pts_mode_logged = True
        self._log("frame timestamps from %s" % mode)

    def _measureFps(self, ms):
        """Estimate the camera's true delivery fps from the first frames.

        Skips an initial connect burst, then averages frame arrivals over a
        short window and locks that in as the recording fps.  Until measurement
        completes, frames are shown live but not recorded (the first ~2.5s after
        connect is not archived — an acceptable startup cost for correct timing).
        """
        if self._fps_first_ms is None:
            self._fps_first_ms = ms
            return
        # Ignore the initial connect burst (buffered frames arrive too fast).
        if (ms - self._fps_first_ms) < _kFpsWarmupMs:
            return
        if self._fps_window_start_ms is None:
            self._fps_window_start_ms = ms
            self._fps_window_count = 0
            return
        self._fps_window_count += 1
        elapsed = ms - self._fps_window_start_ms
        if (elapsed >= _kFpsMeasureWindowMs and
                self._fps_window_count >= _kFpsMeasureMinFrames):
            measured = self._fps_window_count / (elapsed / 1000.0)
            measured = max(_kFpsMin, min(_kFpsMax, measured))
            self._record_fps = measured
            self._fps_measured = True
            self._log("measured %.1f fps (CAP_PROP_FPS reported %.1f)" %
                      (measured, self._reported_fps))

    def _record_writer_loop(self):
        """Drain the recording queue and write frames to FFmpeg in a dedicated thread."""
        while True:
            item = self._record_queue.get()
            if item is None:
                break
            if item is _kFlushSentinel:
                # On-demand finalize: close the current clip so it lands in the
                # archive/DB right away.  Recording resumes on the next frame.
                nowt = time.time()
                if self._ffmpeg_proc is not None and \
                        (nowt - self._lastFlushTime) >= _kFlushDebounceSecs:
                    self._finalize_clip()
                    self._lastFlushTime = nowt
                continue
            bgr_frame, ms = item
            self._record_frame(bgr_frame, ms)
        # Flush the current clip on exit
        if self._ffmpeg_proc is not None:
            self._finalize_clip()

    def _record_frame(self, bgr_frame, ms):
        """Write a frame to the current clip, anchored to real wall-clock time.

        The FFmpeg pipe is constant-rate (-r), but cameras seldom deliver an
        exact, steady fps and frames can be dropped at the record queue under
        load.  Both make naive 1-frame-per-received-frame writing desync the
        clip from real time (slow motion if the camera runs fast, compressed if
        frames drop).  To keep the archived clip real-time-accurate we place
        each frame at the output index implied by its real timestamp:
          * camera ahead of nominal fps  -> drop the frame
          * camera behind / frames dropped -> pad (freeze short gaps, black long)
        This keeps video duration == (last_ms - first_ms), so the burned-in
        camera clock and the program's index-based overlay stay in sync.
        """
        if not self._tmpPath or not self._archivePath:
            return

        try:
            fps = self._record_fps if self._record_fps else 25.0
            frame_interval = 1000.0 / fps

            # Roll a new clip if we have none, the 2-min limit is reached, or a
            # stall longer than the fill cap occurred (don't pad huge black —
            # let it show as a between-clip jump instead).
            need_roll = self._ffmpeg_proc is None or (
                self._clip_first_ms is not None and
                ms - self._clip_first_ms >= _kClipDurationMs)
            if (not need_roll and self._frames_written > 0 and
                    self._clip_first_ms is not None):
                deficit = int(round((ms - self._clip_first_ms) / frame_interval)) \
                          + 1 - self._frames_written
                if (deficit - 1) * frame_interval > _kGapFillMaxMs:
                    need_roll = True
            if need_roll:
                self._roll_clip(ms)

            if self._ffmpeg_proc is None:
                return

            data = bgr_frame.tobytes()

            # First frame of the clip anchors the timeline.
            if self._frames_written == 0:
                self._ffmpeg_proc.stdin.write(data)
                self._frames_written = 1
                self._last_frame_bytes = data
                self._clip_last_ms = ms
                return

            # Output index this frame belongs at, from its real timestamp.
            idx = int(round((ms - self._clip_first_ms) / frame_interval))
            deficit = (idx + 1) - self._frames_written
            if deficit <= 0:
                # Camera running ahead of nominal fps — drop to stay real-time.
                return

            pad = deficit - 1
            if pad > 0:
                gap_ms = pad * frame_interval
                # Short gap: freeze last frame (brief stutter).  Long: black.
                if gap_ms <= _kGapFreezeMaxMs and self._last_frame_bytes is not None:
                    fill = self._last_frame_bytes
                else:
                    if self._black_frame_bytes is None:
                        black = np.zeros(
                            (self._capture_height, self._capture_width, 3),
                            dtype=np.uint8)
                        self._black_frame_bytes = black.tobytes()
                    fill = self._black_frame_bytes
                for _ in range(pad):
                    self._ffmpeg_proc.stdin.write(fill)
                self._frames_written += pad

            self._ffmpeg_proc.stdin.write(data)
            self._frames_written += 1
            self._last_frame_bytes = data
            self._clip_last_ms = ms
        except Exception:
            pass

    def _roll_clip(self, ms):
        """Close current clip (if any) and open a new one."""
        if self._ffmpeg_proc is not None:
            self._finalize_clip()

        cam_name = self.locationName.lower()
        time_folder = datetime.utcfromtimestamp(ms / 1000.0).strftime('%Y-%m-%d')
        clip_name   = '%s-%s.mp4' % (time_folder, time.strftime('%H%M%S', time.localtime(ms / 1000.0)))
        rel_path    = os.path.join(cam_name, time_folder, clip_name)

        tmp_dir = os.path.join(self._tmpPath, cam_name, time_folder)
        try:
            os.makedirs(tmp_dir, exist_ok=True)
        except Exception:
            return

        tmp_file = os.path.join(tmp_dir, clip_name)
        fps  = self._record_fps if self._record_fps else 25.0
        w, h = self._capture_width, self._capture_height

        # Video-only encoder: our time-anchored raw video pipe -> H.264 mp4.
        # Audio is captured separately (_AudioCapture, persistent session) and
        # muxed in at finalize, so the video pipeline and its timing are
        # completely unaffected.
        cmd = [
            _get_ffmpeg(), '-y',
            '-f', 'rawvideo', '-vcodec', 'rawvideo',
            '-s', f'{w}x{h}', '-r', str(fps), '-pix_fmt', 'bgr24',
            '-i', 'pipe:0',
            '-vcodec', 'libx264',
            '-preset', 'veryfast',
            '-b:v', _kTargetBitrate, '-maxrate', _kTargetBitrate,
            '-bufsize', '2048k',
            '-pix_fmt', 'yuv420p',
            '-movflags', '+faststart',
            tmp_file,
        ]
        try:
            proc = subprocess.Popen(cmd, stdin=subprocess.PIPE,
                                    stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL,
                                    creationflags=_kNoWindow)
        except Exception:
            return

        self._ffmpeg_proc   = proc
        self._writer_path   = tmp_file
        self._clip_rel_path = rel_path.replace(os.sep, '/')
        self._clip_first_ms = ms
        self._clip_last_ms  = ms
        # Start gap tracking fresh — never pad across a clip boundary.
        self._frames_written = 0
        self._last_frame_bytes = None
        self._black_frame_bytes = None

        # Tap the persistent audio capture for this clip's audio (muxed at
        # finalize).  No new camera session — the capture is already running.
        if self._audio_capture is not None:
            try:
                self._audio_capture.begin_clip(tmp_file + '.pcm', ms)
            except Exception:
                pass

    def _mux_clip_audio(self, video_path, pcm_path, latency, out_path):
        """Mux captured PCM audio onto a finished video clip, offset by latency.

        Video is stream-copied; audio is re-encoded to AAC (giving clean
        timestamps).  The leading `latency` seconds are padded with silence so
        the audio lines up with video frame 0.  Returns True on success.
        """
        cmd = [
            _get_ffmpeg(), '-y',
            '-i', video_path,
            '-itsoffset', '%.3f' % max(0.0, latency),
            '-f', 's16le', '-ar', '44100', '-ac', '2', '-i', pcm_path,
            '-map', '0:v:0', '-map', '1:a:0',
            '-c:v', 'copy', '-c:a', 'aac', '-b:a', '128k',
            '-movflags', '+faststart', '-shortest', out_path,
        ]
        try:
            r = subprocess.run(cmd, stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL, timeout=120,
                               creationflags=_kNoWindow)
        except Exception:
            return False
        return (r.returncode == 0 and os.path.isfile(out_path)
                and os.path.getsize(out_path) > 0)

    def _finalize_clip(self):
        """Flush current clip, mux audio, move to archive, register in ClipManager."""
        proc, self._ffmpeg_proc = self._ffmpeg_proc, None
        if proc is not None:
            try:
                proc.stdin.close()
                proc.wait(timeout=30)
            except Exception:
                proc.kill()

        tmp_path = self._writer_path
        rel_path = self._clip_rel_path
        first_ms = self._clip_first_ms
        last_ms  = self._clip_last_ms

        self._writer_path   = None
        self._clip_rel_path = None
        self._clip_first_ms = None
        self._clip_last_ms  = None

        # Collect this clip's audio PCM (+ small alignment offset) from the
        # persistent capture.
        pcm_path, latency = (None, 0.0)
        if self._audio_capture is not None:
            try:
                pcm_path, latency = self._audio_capture.end_clip()
            except Exception:
                pcm_path = None

        def _rm(p):
            try:
                if p and os.path.isfile(p):
                    os.remove(p)
            except Exception:
                pass

        if tmp_path is None or not os.path.isfile(tmp_path):
            _rm(pcm_path)
            return

        # Mux audio in if we captured any; otherwise keep the video-only clip.
        # `latency` is the measured audio start offset from end_clip() and
        # nothing else: there used to be a fixed +150ms fudge on top of it
        # (SV_AV_SYNC_MS), which never applied to any real camera -- this path
        # only runs when the stream-copy recorder is absent -- and was removed.
        src_path = tmp_path
        if pcm_path and os.path.isfile(pcm_path) and os.path.getsize(pcm_path) > 0:
            muxed = tmp_path + '.av.mp4'
            if self._mux_clip_audio(tmp_path, pcm_path, latency, muxed):
                _rm(tmp_path)
                src_path = muxed
            else:
                _rm(muxed)
        _rm(pcm_path)

        # Move to archive
        dst = os.path.join(self._archivePath, rel_path)
        try:
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.move(src_path, dst)
        except Exception:
            return

        # Register in ClipManager
        if self._clipMgr is None or first_ms is None or last_ms is None:
            return

        prev_rel = self._prev_clip_rel
        self._prev_clip_rel = rel_path

        lock = self._clipMgrLock
        try:
            if lock:
                lock.acquire()
            self._clipMgr.addClip(
                rel_path, self.locationName,
                first_ms, last_ms,
                prev_rel, '',
                _kCacheStatus_Cache,
                self._capture_width, self._capture_height
            )
            self._clipMgr.save()
        except Exception:
            pass
        finally:
            if lock:
                lock.release()

    def getNewFrame(self, liveViewEnabled=False):
        """Return the latest frame or None if no new frame is available."""
        with self._lock:
            f = self._latest_frame
            self._latest_frame = None
            return f

    @property
    def isRunning(self):
        return self._running

    def close(self, termFunc=None, keepRecorder=False):
        """Close the analysis capture, and normally the recorder with it.

        @param  keepRecorder  Leave a HEALTHY stream-copy recorder running.

        Set by CameraCapture's in-process reconnect, which fires when the
        ANALYSIS stream stalls.  Until 2026-08-04 that path stopped the
        recorder too, so every analysis timeout also threw away the
        main-stream connection and its in-progress segment: 785 timeouts in
        one 8-hour window produced 1113 recorder starts and 713 audio-ring
        recreate failures, and roughly a quarter of all footage was missing.
        The two halves are on separate RTSP connections and the recorder has
        its own supervise loop with backoff, so a stalled analysis stream is
        no reason to disturb it.  (The Py2 original never had this problem: it
        ran ONE session per camera and its watchdog was fed by recording
        activity, not by the analysis frames.)
        """
        self._running = False
        # Hand the capture to the capture thread to release (see
        # _capture_loop): releasing it here would free the FFmpeg context out
        # from under a read() still in flight and abort the whole camera
        # process.  We wait a short while for the thread to notice -- if it is
        # parked on a dead stream it releases whenever read() finally returns,
        # and the generation bump keeps it away from the next connection.
        self._capGen += 1
        thread, self._thread = self._thread, None
        cap, self._cap = self._cap, None
        # The analysis source is the ONE capture kind it is safe to release
        # from another thread: it is a socket, so closing it makes an
        # in-flight recv() raise in the capture thread rather than aborting
        # the process the way freeing an ffmpeg context does.  Do it before
        # the join -- otherwise the reader sits in its reconnect wait for up
        # to _kAnalysisConnectWaitSecs while we block here.
        if self._analysisSource is not None:
            try:
                self._analysisSource.release()
            except Exception:
                pass
        if thread is threading.current_thread():
            pass                # we ARE the owner; our finally releases it
        elif thread is not None and thread.is_alive():
            thread.join(timeout=_kCaptureJoinSecs)
            if thread.is_alive():
                self._log("capture thread still in read(); it will release "
                          "the stream when the read returns")
        elif cap is not None:
            # No thread ever owned it (open() failed part-way) -- safe here.
            try:
                cap.release()
            except Exception:
                pass
        self._analysisSource = None
        # Stop the stream-copy recorder first: it closes the in-progress
        # segment gracefully and registers it before we tear anything down.
        # Unless we were asked to keep it -- see the docstring.
        if keepRecorder and self._mayKeepRecorder():
            self._log("analysis capture closed; recorder left running "
                      "(reconnect does not interrupt recording)")
        elif self._remux is not None:
            try:
                self._remux.stop()
            except Exception:
                pass
            self._remux = None
        # Signal the writer thread to flush and finalize the current clip
        if self._record_writer is not None:
            self._record_queue.put(None)
            self._record_writer.join(timeout=60)
            self._record_writer = None
        # Fallback: finalize directly if writer thread never started
        if self._ffmpeg_proc is not None:
            self._finalize_clip()
        # Stop the persistent audio capture (after the final clip is muxed so it
        # still had access to the sink); this also closes the live ring buffer.
        if self._audio_capture is not None:
            try:
                self._audio_capture.stop()
            except Exception:
                pass
            self._audio_capture = None
        if termFunc:
            try:
                termFunc()
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Stubs for methods used only by CameraCapture (live monitoring path)
    # ------------------------------------------------------------------

    def getProcSize(self):
        return (self._capture_width, self._capture_height)

    def getInitialFrameBufferSize(self):
        return self._capture_width * self._capture_height * 3

    def setLiveStreamLimits(self, maxRes, maxBitrate):
        return 0

    def flush(self, *a):
        """Finalize the current clip on demand so recorded video becomes available
        without waiting for the 2-minute clip boundary.  Safe to call from any
        thread — the actual finalize runs in the writer thread via a sentinel."""
        if self._remux is not None:
            self._remux.flush()
            return
        if self._record_writer is None:
            return
        try:
            self._record_queue.put_nowait(_kFlushSentinel)
        except queue.Full:
            # Frames are backed up; give the sentinel a brief chance to enqueue
            # rather than dropping the flush outright.
            try:
                self._record_queue.put(_kFlushSentinel, timeout=0.5)
            except Exception:
                pass

    def setMmapParams(self, enable, width, height, fps):
        _MAX_W, _MAX_H = 2560, 1440
        self._mmap_width = min(width if width > 0 else self._capture_width, _MAX_W)
        self._mmap_height = min(height if height > 0 else self._capture_height, _MAX_H)
        self._mmap_fps = float(fps) if fps else 25.0

        # An NVDEC capture decodes at a fixed GPU-downscaled size, so enlarging
        # this camera in the monitor view would just upscale a small frame.
        # Ask the capture loop to re-open at the new (larger) size; shrinking
        # back to a preview likewise reclaims the CPU.  Only a meaningful
        # change is worth the reconnect.
        outW = getattr(self, '_nvdecOutW', 0)
        if outW:
            want = self._mmap_width
            # Rate-limited AND only once the stream has been up a while: a
            # re-open restarts the camera, which resets the live-view size,
            # which would request another re-open -- an endless loop (observed
            # 2026-07-26).  Never re-open more than once every 60s.
            now = time.time()
            settled = (now - getattr(self, '_nvdecOpenedAt', 0)) > 30.0
            cooled = (now - getattr(self, '_lastReopenAt', 0)) > 60.0
            if want > outW * 1.2 and settled and cooled:
                self._lastReopenAt = now
                self._log('live view now %dpx wide (decoding %dpx) -- '
                          're-opening capture at the new size' % (want, outW))
                self._reopen_requested = True

    def setAudioVolume(self, volume):
        pass

    def open_mmap(self, filename):
        try:
            import mmap as _mmap
            # Size the file for the maximum display resolution so that any
            # subsequent setMmapParams call (e.g. switching to large view) can
            # write without overflowing the file.
            _MAX_W, _MAX_H = 2560, 1440
            kLiveHeaderSize = 32
            file_size = kLiveHeaderSize + _MAX_W * _MAX_H * 3 + 8
            # Only recreate the file if it doesn't exist or has wrong size.
            # Unconditional 'w+b' truncation fails on Windows when another
            # process still has the file mapped (PermissionError / ERROR_USER_MAPPED_FILE).
            try:
                existing_size = os.path.getsize(filename)
            except OSError:
                existing_size = 0
            if existing_size != file_size:
                with open(filename, 'w+b') as f:
                    f.write(b'\x00' * file_size)
            handle = open(filename, 'r+b')
            mem = _mmap.mmap(handle.fileno(), file_size)
            with self._lock:
                self._mmap_handle = handle
                self._mmap_mem = mem
                self._mmap_id = 0
            return True
        except Exception:
            return False

    def close_mmap(self):
        with self._lock:
            mem, self._mmap_mem = self._mmap_mem, None
            handle, self._mmap_handle = self._mmap_handle, None
        if mem is not None:
            try:
                # Zero the header so the frontend immediately detects "camera off"
                # and releases its mmap handle before the next open_mmap call.
                mem.seek(0)
                mem.write(b'\x00' * 32)  # kLiveHeaderSize
                mem.flush()
            except Exception:
                pass
            try:
                mem.close()
            except Exception:
                pass
        if handle is not None:
            try:
                handle.close()
            except Exception:
                pass

    def disableLiveStream(self, profileId):
        pass

    def enableLiveStream(self, profileId, fileName, tsOption, maxFileIndex):
        return 0

    def getNewestFrameAsJpeg(self, width, height):
        """Return the latest captured frame as JPEG bytes at the requested size."""
        with self._lock:
            f = self._wsgi_frame
        if f is None:
            return None
        try:
            from PIL import Image
            import io as _io
            img = Image.fromarray(f._data, 'RGB')
            if img.size != (width, height):
                img = img.resize((width, height), Image.Resampling.BILINEAR)
            buf = _io.BytesIO()
            img.save(buf, 'JPEG', quality=75)
            return buf.getvalue()
        except Exception:
            return None
