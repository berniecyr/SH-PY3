# Sighthound Video — Python 3 Port

A Python 2.7 → Python 3 migration of the open-source
[Sighthound Video](https://github.com/sighthoundinc/SighthoundVideo) VMS, with a
custom AI detection backend (YOLO + InsightFace + NudeNet) replacing the original
C-extension analytics engine.

This file is the **operator and user guide**: what the app does and how to run it.

- **Building the installer:** [build/README.md](build/README.md)
- **Installing it as a program, and the Windows service:** [build/INSTALL.md](build/INSTALL.md)
- **Engineering handoff — architecture, design decisions, traps, open items:** [HANDOFF.md](HANDOFF.md)

---

## Requirements

- **Python 3.12.x** — install from **python.org** ("Add to PATH"), **NOT the
  Microsoft Store build** (the Store sandbox redirects `%LOCALAPPDATA%` writes and
  breaks databases and logs). Verify with
  `py -3.12 -c "import sys; print(sys.executable)"` → must NOT be under
  `WindowsApps`. *(Not needed if you install the packaged program — it carries its
  own interpreter.)*
- **Windows** (developed and run on Windows 11)
- **RTSP IP cameras**, or local video files
- *(Recommended)* **NVIDIA GPU** + current driver. The whole media/AI stack uses
  the GPU when present and **falls back to CPU automatically**:
  - YOLO, InsightFace and NudeNet inference on **CUDA**
  - camera **decode** via **NVDEC/CUVID** in the recorder's ffmpeg
    (`-hwaccel cuda`), and only for cameras above `_kSoftwareDecodeMaxWidth` — see
    [Recording](#recording). **D3D11VA** is a separate cv2 path, used by webcams
    and local files, which decode their own capture.
  - clip **playback** decode via **NVDEC** (`cuvid`) for large sources
  - clip markup / export / daily-summary **encode** via **NVENC**

  4 GB VRAM is enough — inference is one shared process, not one per camera.

**Without a GPU the app runs, but the tuning changes.** Everything above falls back
to CPU, YOLO inference serializes on a single lock, and model choice starts to
matter a great deal: NudeNet `320n` costs ~47 ms per call on CPU while `640m` costs
**1000–1516 ms** and holds the shared inference lock for the whole of it. Prefer
`320n` on a CPU-only machine; `640m` is a GPU-box option.

> **The development machine is a GPU box** (verified 2026-09-02, and it is now the
> only machine used for development): GTX 1650 Ti, 4.3 GB VRAM, torch
> `2.12.1+cu126`, CUDA 12.6, 34.1 GB RAM. `DetectionService.log` reports
> `cuda available: True` on every start, and NudeNet runs `640m` on
> `CUDAExecutionProvider`. The two per-call figures above are **CPU-era
> measurements that have never been re-taken on the GPU** — read them as the
> CPU-fallback comparison they were, not as current numbers. `HANDOFF.md` §3.

---

## Quick Start (from a source checkout)

The development repository is `G:\Documents\Programming\Sighthound PY3 Dev\`, Run the launch scripts from that checkout.

Double-click **`Start.bat`**, or run it from a terminal. On first run (or after
deleting `venv\`) it will:

1. Create a virtual environment on **Python 3.12** (`py -3.12`)
2. Install dependencies from `requirements.txt`
3. Normalize OpenCV to a single **headless** build — ultralytics transitively
   installs the full build, and having **both** corrupts the heap
4. Install the **CUDA builds**: PyTorch `cu126` and `onnxruntime-gpu==1.22.0`
   (1.27+ targets CUDA 13 and will not load against torch cu126)
5. Verify cv2 + ffmpeg + CUDA, then launch `python -m frontEnd.FrontEndApp`

To rebuild cleanly on a new machine, delete `venv\` and run `Start.bat` again.

`StartFrontend.bat` runs only the **front end** and attaches to an already-running
back end — the fast way to test a front-end change without restarting cameras.

**First-minute caveat:** detection goes live before the first 60-second recording
segment closes, so events in the first minute after startup have no playable clip
yet. Wait ~90 s after launch before testing playback.

### What you see while it starts

A branded startup window appears within about a second of launch and stays up,
narrating each phase, until the main window replaces it. Launching a second copy
raises the first instead of silently exiting.

If startup stalls, the log records timed `startup:` marks for every phase
(interpreter entry, `import wx`, startup window shown, back end spawned/connected/
ready, frame constructed, main loop). Reading those marks is how you find which
phase is slow rather than guessing.

---

## Key Dependencies

| Package | Purpose |
|---------|---------|
| `wxPython 4.2.5` | GUI framework |
| `opencv-python 4.13` | Motion detection, clip decode (D3D11VA hw decode) |
| `ultralytics 8.4.14` | YOLO object detection (people / animals / vehicles) |
| `insightface 0.7.3` | Face recognition (ArcFace `buffalo_l`) |
| `nudenet 3.4.2` | Nudity detection (off by default) |
| `sounddevice 0.5.5` | In-app audio playback (bundles PortAudio) |
| `kokoro-onnx` | Text-to-speech response (local or cast to a network speaker) |
| `Pillow 12.2` | Thumbnails, bounding-box overlays |
| `numpy 2.4` | Frame buffers |
| `imageio-ffmpeg` | Bundled FFmpeg 7.1 (stream-copy recording, NVENC encode, snapshots, audio) |
| `torch 2.12.1+cu126` | GPU inference (`Start.bat` swaps in the CUDA build) |
| `onnxruntime-gpu 1.22.0` | Face/nudity inference (CUDA-12 build ONLY — pairs with torch cu126's DLLs) |
| `psutil` | Orphan-process/ffmpeg cleanup at recorder start |
| `pytapo 3.4.18` | Tapo siren / spotlight control (port 443) |
| `PyAudio 0.2.14` | Legacy requirement; playback actually uses `sounddevice` |

> `Start.bat` only runs `pip install -r requirements.txt` when it **creates** the
> venv. An existing venv needs `pytapo` installed once by hand:
> `venv\Scripts\python.exe -m pip install pytapo==3.4.18`

---

## Architecture

The app runs as **multiple processes**. The front end talks to the back end over
localhost XML-RPC through `BackEndClient` to a separate `NetworkMessageServer`
process; the back end spawns and supervises the workers. Commands and worker
messages use multiprocessing queues. Live video/audio use memory-mapped files,
and desktop search opens the shared SQLite databases directly.

| Process | Role |
|---------|------|
| **FrontEndApp** | wxPython GUI; spawns the back end when no service owns it |
| **BackEndApp** | Orchestrates cameras, detection, rules, responses; supervises services |
| **NetworkMessageServer** | Desktop XML-RPC control/status API; queues commands to the backend |
| **CameraCapture** ×N | One per camera: `StreamReader` (recorder + analysis frames) + `VideoPipeline` |
| **DetectionService** | Shared YOLO + InsightFace + NudeNet on one CUDA context |
| **ResponseRunner** | Executes alert responses (email, FTP, TTS, snapshot, sound, iHost, Tapo, webhook) |
| **DiskCleaner** | Storage quota, low-disk safety net, daily-summary video generation |
| **WebServer** | Optional LAN record viewer |

This runtime shape differs from stock Sighthound in four ways that matter:

- **Recording is camera-native stream-copy** — one ffmpeg per camera, `-c:v copy`,
  60 s segments. No decode/re-encode cost; A/V sync exact by construction from the
  camera's own timestamps.
- **One RTSP session per camera.** That same recorder ffmpeg also produces the
  analysis frames (a 640-wide bgr24 stream over loopback TCP), the full-res
  snapshot ring and the live-audio tee. Recording and detection share fate, so a
  detection with no footage behind it is impossible by construction, and the
  camera's session budget is halved.
- **One shared DetectionService process** owns YOLO + InsightFace + NudeNet on a
  single CUDA context. Camera processes are ML-free (~150–250 MB each).
- **Full-res snapshot rings** (`live/<cam>.snaps/`, 1 fps, 40 s retention) feed
  face/nudity with main-stream pixels, time-matched to the analyzed frame.

### Detection pipeline

```
StreamReader — ONE main-stream ffmpeg per camera:
    -c:v copy → 60 s archive segments
    scaled 640-wide bgr24 → loopback TCP → _AnalysisFrameSource → numpy BGR
    full-res JPEG snapshot ring + s16le PCM audio tee
  → VideoPipeline.processClipFrame  (MOG2 background subtraction)
        Global-illumination guard: a frame that is ≥35 % foreground is a lighting
          step (IR kicking in, exposure hunting), not motion — suppressed while
          MOG2 re-converges
        Candidate gate: blobs tracked for ≥3 frames before being promoted
  → objectCollector.addObject / addFrame   (type = "unknown" initially)
  → _requestDetections (throttled to once per 500 ms) → DetectionService RPC
        YOLO:
          person  → "person"  [+ InsightFace recognition + NudeNet if conf ≥ PERSON_CONF_FOR_ATTRS]
          animal  → "animal"  (bird/cat/dog/horse/cow)
          vehicle → "vehicle" (car/motorcycle/truck)
          else    → "unknown"
        Face/nudity use a full-res snapshot crop (person re-detected in the snapshot)
  → readyToReport() → _generateFrameReport
  → DataManager (objdb2, WAL)
  → real-time rule match → responses fire; clip appears in the Search window
```

---

## Project Layout

```
SH_ImageCheck\                   # F:\Git\SH_ImageCheck (Git checkout)
├── Start.bat                    # Create venv, install deps, launch the app
├── StartFrontend.bat            # Front end only; attaches to a running back end
├── requirements.txt
├── backEnd\                     # Back-end processes
│   ├── BackEndApp.py            # Orchestrator (spawns cameras, services, cleaner)
│   ├── BackEndPrefs.py          # Back-end preference store (pickle)
│   ├── NetworkMessageServer.py  # XML-RPC control surface (front end ↔ back end)
│   ├── VideoPipeline.py         # MOG2 motion detector (replaces the C-extension stub)
│   ├── DetectionService.py      # Shared YOLO + InsightFace + NudeNet server
│   ├── DetectionServiceClient.py# Localhost length-prefixed-pickle RPC to the service
│   ├── DetectionReplay.py       # Offline replay of archived clips through the pipeline
│   ├── ObjectDetectorClientImageCheck.py  # Per-camera detection client (full-res path)
│   ├── ImageCheckConfig.py      # AI thresholds/gates — single source of truth
│   ├── FaceEnrollment.py        # Shared harvest/save core for face enrollment
│   ├── DataManager.py           # Object/event SQLite DB (objdb2, WAL)
│   ├── ClipManager.py           # Clip index SQLite DB (clipdb, WAL)
│   ├── SavedQueryDataModel.py   # Rule / saved-query data models
│   ├── triggers\                # Search/rule triggers (MinSizeTrigger, MinTravelTrigger, …)
│   ├── ResponseRunner.py        # Executes rule responses
│   ├── ResponseDbManager.py     # Response-state SQLite (WAL) + corrupt-file self-heal
│   ├── IHostController.py       # "Send iHost command" action + auto-off state machine
│   ├── DiskCleaner.py           # Storage quota, low-disk net, daily-summary generation
│   ├── WebServer.py             # LAN record viewer (replaces nginx/XNAT)
│   ├── WebRuleSearch.py         # LAN viewer search using the desktop Search rules
│   ├── webroot\                 # Static UI for the LAN viewer
│   └── responses\               # Response handlers incl. TtsManager.py
├── frontEnd\                    # wxPython GUI
│   ├── FrontEndApp.py           # Entry point (also spawns the back end)
│   ├── StartupWindow.py         # The window shown while the app starts
│   ├── FrontEndFrame.py         # Main window (menus, message handling, alerts)
│   ├── BackEndClient.py         # XML-RPC client wrappers
│   ├── MonitorView.py           # Live camera view with audio
│   ├── SearchView.py            # Search screen (incl. the live movement filter)
│   ├── SearchResultsPlaybackPanel.py  # Clip playback + Detections panel
│   ├── DetectionTestDialog.py   # Developer → Detection Test Suite
│   ├── CameraSetupWizard.py     # Add/edit camera, motion settings
│   └── OptionsDialog.py         # Settings tabs
├── videoLib2\python\            # Video I/O (replaces the C-backed videoLib2)
│   ├── StreamReader.py          # Recorder, analysis frames, snapshot ring, audio tee
│   ├── ClipReader.py            # Clip playback, live audio player
│   ├── ClipUtils.py             # Export/remux, NVENC markup, daily summary
│   └── AudioRelay.py            # Shared-memory audio ring (back end → front end)
├── appCommon\
│   ├── InstallPaths.py          # getUserDataDir() — the single answer for every process
│   ├── SearchUtils.py           # The shared rule-search engine (wx-free)
│   ├── DbRecovery.py            # Corruption flags and repair
│   └── LegacyMigration.py       # One-time Py2→Py3 camera/rules import on first launch
├── vitaToolbox\                 # Shared utility library (wx widgets, logging, networking)
├── launch\                      # SHLaunchPY3 service + Launch.py
├── build\                       # Installer: see build/README.md and build/INSTALL.md
├── models\                      # YOLO weights, InsightFace, TTS voices
└── venv\                        # Auto-created virtual environment
```

---

## User Data Directory

```
C:\Users\<you>\AppData\Local\Sighthound Video Py3\
├── camdb                        # Camera database (pickle)
├── backEndPrefs                 # Back-end preferences (pickle): storage quota, webAuth,
│                                #   summaryEnabled/summaryDir, …
├── imagecheck_config.json       # AI detection thresholds/gates (auto-created first run)
├── ihost_config.json            # iHost hub config
├── tapo_config.json             # TP-Link account for siren/spotlight
├── known_faces.dat              # Enrolled face embeddings (hot-reloaded by cameras ~5 s)
├── rules\                       # Alert rules and saved queries
├── videos\
│   ├── tmp\<cam>\_seg\          # In-progress 60 s segments
│   ├── archive\<cam>\<date>\    # Finalized clips + thumbs\
│   ├── clipdb           # Clip index (SQLite, WAL)
│   └── objdb2                   # Detection event database (SQLite, WAL)
├── live\
│   ├── <cam>.audio              # Audio ring files (mmap, live view)
│   └── <cam>.snaps\             # Full-res snapshot ring (1 fps JPEG, 40 s)
└── logs\                        # BackEndApp/DetectionService/DiskCleaner/… + cameras\<cam>.log
```

The daily-summary path is **not** here — it is wherever you point it in
**Options → Saved Events**, stored in `backEndPrefs` (`summaryDir`).

**Where that path comes from.** `appCommon/InstallPaths.py:getUserDataDir()` is the
single answer for every process: `%SV_DATA_DIR%` → `datadir.txt` in the install
directory → `%LOCALAPPDATA%\Sighthound Video Py3` → `~\AppData\Local\…`. Back-end
modules must not build this path from `expanduser("~")` — under the service that is
the service account's profile, not yours. See [build/INSTALL.md](build/INSTALL.md).

---

## Recording

- **Stream-copy, 60 s segments.** One ffmpeg per camera on the **main** stream,
  `-c:v copy` plus AAC audio, strftime-named, `+faststart`. No re-encode, so
  recording resolution is the camera's own and costs almost no CPU.
- **The recorder is also the analysis source.** 
- **Webcams and local files** have no recorder and decode their own capture. That
  path is untouched.
- **Decode policy.** A hardware decode session costs ≈300 MiB of VRAM, so software
  decode is cheaper below ~1080p: `_kSoftwareDecodeMaxWidth = 1920` means only
  4K / 2688×1520 / 2560×1440 cameras use the GPU. Ladder `cuvid → nvdec → sw`,
  demoted permanently per process on failure.
- **Analysis frames are paced** at the camera's *measured* rate rather than as fast
  as they arrive (queue capped at ~0.6 s). This is load-bearing: ffmpeg decodes far
  faster than the old in-loop cv2 decode, so an unpaced burst publishes in a few
  milliseconds and most frames are overwritten before the analytics thread polls —
  which measurably cost detections. Frames carry their arrival time, so pacing never
  enters an event's timestamp.
- **An analysis stall does not restart the recorder.** `close(keepRecorder=True)`
  leaves a healthy recorder running for the duration of the reconnect (backoff
  `[0, 10, 20, 30] s`), so the archive keeps being written while the analysis stream
  comes back.
- **Smoothing.** Cameras burst-deliver frames, which stream-copy would preserve as a
  per-second stutter plus audio dropouts. Each closed segment is re-stamped to a
  constant frame rate (`-bsf:v setts`, no re-encode) and its audio re-laid on a
  continuous clock. Skipped for B-frame streams (the original segment is kept).
- **Camera outages are recorded as BLACK.** The constant-rate re-stamp above would
  otherwise *delete an outage from the timeline* — playback stays smooth while the
  camera's burned-in clock jumps forward. Every closed segment is probed for holes
  in delivery; a hole of **≥ 2 s** makes the segment re-encode with the real timing
  preserved and those frames painted black, so clip length stays true to wall time
  and missing footage is visible. The camera log says
  `remux: <segment> had N outage(s) totalling Xs -> filled black`.
- **Segment dating** is anchored per ffmpeg **run**, not by chaining durations.
  Cameras replay from their previous keyframe on RTSP connect, so a segment's
  content usually precedes its filename. Mid-run a segment starts at
  `(next segment's filename) − (probed duration)`; the last segment of a run is
  anchored to when ffmpeg exited; the first is anchored forward from the spawn; and
  a computed start later than the segment's own file-creation time is clamped back.
  Healthy cameras land within ~1–2 s of their burned-in clock. **Do not date
  segments by chaining durations** — on a lossy camera the probed duration wobbles
  ±5 s against real elapsed time and the error accumulates.
- **Flush on demand.** A snapshot/export response, or a near-now search, closes the
  current segment early, **debounced 60 s** so repeated searches don't fragment the
  archive.
- **Orphan cleanup.** A hard-killed camera leaves its ffmpeg holding the RTSP
  session; the recorder kills orphans (matched by segment directory) at start.

### Audio

Audio is muxed into each segment by the same ffmpeg session. The live view reads a
shared PCM ring (`live/<cam>.audio` → `AudioRingReader` → `sounddevice`) with no
extra camera connection.

Cameras deliver audio in bursts, so its timestamps arrive bunched with ~0.4–0.5 s
holes even though every sample is present. That timeline is **rebuilt from the
sample count** (`asetpts=N/SR/TB`), never synced *to* the bunched timestamps —
`aresample=async` does "filling and trimming" against them, which discarded real
samples and wrote digital silence over the jumps. The video is then re-stamped to
the **audio sample clock** rather than the container duration, so video length ==
audio length by construction.

To check a clip for that class of damage — invisible in durations and packet
timelines — look for digital silence, which an outdoor camera never records:
`silencedetect=noise=-90dB`.

---

## AI Detection Configuration

Thresholds live in `imagecheck_config.json` (auto-created on first run) and are
edited via **Options → AI Detection**. `backEnd/ImageCheckConfig.py` is the single
source of truth for defaults.

| Key | Default | Description |
|-----|---------|-------------|
| `YOLO_CONF_THRESHOLD` | 0.25 | Minimum YOLO confidence to report any object |
| `PERSON_CONF_FOR_ATTRS` | 0.50 | Gate: minimum **person** confidence before face/nudity run at all |
| `FACE_DET_CONF` | 0.60 | InsightFace floor for "is this actually a face?" |
| `FACEMATCH_CONF` | 0.32 | **Recognition** floor: minimum cosine similarity against the enrolled library to attach a name. Distinct from `PERSON_CONF_FOR_ATTRS`. Measured: true matches 0.33–0.44 at camera distances, strangers ~0.1–0.25; camera-view enrollments push real matches to 0.6+ |
| `FULLRES_ATTRS` | true | Face/nudity analyse a time-matched full-res snapshot crop rather than the small analysis-stream crop |
| `NUDE_ENABLED` | `[]` | NudeNet classes that are active (empty = nudity off) |

Changes take effect after restarting cameras, not the whole app. Per-event
diagnostics land in `logs\cameras\<cam>.log` as
`[ImageCheck] face: <crop source+size> -> det=<score> name=<match>` and
`fullres skip: <reason>` — read these before adjusting thresholds.

### Face enrollment & management

Enrollment harvests real camera-view samples, which beat portrait photos
(similarity ~0.6 same-camera vs ~0.3–0.5 photo-to-camera). Three entry points share
one harvest/save core (`FaceEnrollment.py`), all routed through the
DetectionService:

- **LAN viewer:** open a person detection → *"Add this face to the baseline"*
- **Desktop playback right-click** → *"Add face to baseline…"*
- **Click a face in the Detections panel** (keyed by object id)

Enrollment is **harvest → preview → commit** with quality floors (`ENROLL_MIN_DET`,
minimum pixel size) so blurry or tiny crops are rejected. New embeddings are
appended to `known_faces.dat` (atomic write + one-time `.bak`); cameras hot-reload
it within ~5 s, so faces start matching **without a restart**.
**Options → AI Detection → "Manage enrollments…"** lists enrolled people and lets
you delete / move / rename samples, then triggers a service-routed rebuild.

---

## Motion Sensitivity (per camera)

Set in the Camera Setup Wizard → Advanced.

| Level | Description | Recommended for |
|-------|-------------|-----------------|
| 1 – Very Low | Suppresses most motion; large area threshold | Busy outdoor scene with foliage / shadows |
| 2 – Low | Reduced sensitivity | Open driveway with trees |
| 3 – Medium (default) | Balanced | Covered porch / entrance |
| 4 – High | Catches subtle movement | Interior room |
| 5 – Very High | Maximum sensitivity | Low-traffic indoor area |

**Ignore Shadows** enables MOG2 shadow detection plus a pixel-mask threshold,
filtering grey shadow regions before contour extraction. Recommended for cameras
with heavy cast shadows.

**Minimum travel** (spin control, default 0 = off) suppresses a track at *record
time* until it has moved that far. Quoted in 1280×720 reference pixels and scaled
linearly per camera. Most users should leave this at 0 and use the **movement
filter** below instead, which keeps the detection and lets you retune later.

Settings are stored in `camdb` per camera and apply when the camera process starts.
Editing a camera through the UPnP or ONVIF wizard screens carries the motion keys
through — those screens once dropped them, silently resetting sensitivity to
default.

### Global-illumination guard (automatic)

A step change in scene lighting — IR kicking in, auto-exposure hunting, garden
lights, lightning — makes MOG2 report most of the **frame** as foreground, which the
blob path reads as several huge simultaneous objects. Measured on one camera over
30 minutes of empty IR-lit patio: ~227 tracked objects, essentially all lighting.

Neither sensitivity nor shadow filtering helps: the blobs are the whole frame, so no
size threshold excludes them, and on a flash the pixels are *brighter*, not shadow.
So the pipeline gates on foreground fraction instead — at or above **0.35 of the
frame** (`_kIllumFgFraction`) it treats the frame as a lighting step and suppresses
motion for a few frames while MOG2 re-converges at a forced learning rate. The
threshold sits ~3× clear of genuine motion: real subjects walking through three
cameras peaked at 0.115, while lighting bursts ran 0.41–0.85.

It **fails open** after 60 consecutive suppressed frames (`_kIllumMaxRun`) and says
so in the log — a noisy camera is recoverable, a silently blind one is not.

Effect: deep-night grouped events fell ~17 % fleet-wide, and one camera's false
person detections went 10 → 0. Reported per camera on the `[motion]` log line.

### Things on the lens — spider webs, insects, rain

A spider web strung in front of a camera produces a **detection storm**: one camera
went 721 → 3159 objects overnight. It does **not** produce false *person* alerts and
costs no measurable CPU — what it does is flood the timeline with unnamed `object`
tracks and churn the database.

A sharpness test was built to reject it automatically and **measured against a
matched night-time corpus, it does not work**: the best threshold that loses no
people catches only about a quarter of the web. Masking the region is worse — half
the genuine people tracks on that camera fell inside the web region, which is
exactly where visitors approach. The instrumentation remains but is **measure-only**
and changes no detection behaviour (per-camera `measureFocus` opt-out).

**The physical fix is the one that works.** The IR illuminator attracts insects,
insects attract spiders. Mount the illuminator away from the lens, or clear the web.

The **movement filter** below is the practical answer to the timeline flood: web
tracks barely move, so a low threshold hides them without losing anything.

---

## The Movement Filter

Every detection records **how far it moved** — `travel`, defined as the extent of
its bounding-box centre over its life, `(maxCx−minCx) + (maxCy−minCy)`, stored in
1280×720 reference pixels so one value means the same thing on every camera.

Travel is **recorded, not used to suppress**. Nothing is discarded at record time,
so the threshold stays a query-time dial you can retune forever — including over
footage already captured. The upgrade backfills the whole archive, so the filter
works on history as well as new events.

It appears in four places, all in the same units:

- **Search screen — a live slider** under the criteria, left-aligned with
  `Camera:`/`When:`. Reads `Movement filter: off` or `Movement filter: 227px+`.
  - Selecting a rule **loads that rule's setting onto the slider**, and dragging it
    **overrides** the rule for these results only. The rule's own filter is stripped
    from a copy of the query before the search runs, so dragging *below* the rule's
    value genuinely widens the results. **The saved rule on disk is never touched.**
  - Custom searches zero and **disable** the slider — they don't run through the
    trigger tree, so there is nothing for it to wrap.
- **Rule editor — "Ignore detections that barely move"** on the *Look for* page,
  beside minimum size: a checkbox, a 0–500 slider, and a live
  **"showing N of M detections"** readout **scoped to the rule's camera**. The scope
  matters: at a threshold of 40 one camera keeps 64 % while another keeps 74 %, so a
  fleet-wide number would mislead for both.
- **LAN record viewer** — the same filter, server-side.
- **Detection Test Suite** — shown as a preview column on replay results.

Calibration measured over 16,215 recorded detections:

| threshold | detections shown | person/animal kept |
|---|---|---|
| 0 | 100 % | 100 % |
| 20 | 75 % | 96 % |
| **40** | **66 %** | **92 %** |
| 80 | 57 % | 87 % |
| 240 | 39 % | 69 % |
| 640 | 11 % | 31 % |

The useful action is all in 20–80; 80 still shows well over half of everything, and
reaching "almost nothing" needs ~1200. This is why the readout exists — the raw
number means nothing on its own.

Filters **compose**: nesting two travel filters applies the stricter of the two,
in either order.

---

## Developer Menu — Detection Test Suite

**Developer → Detection Test Suite…** replays **real archived clips** back through
the live motion and detection pipeline, so a sensitivity change can be measured
against footage you already have instead of waiting a night to find out.

Controls: camera; date and start/end time; **Load camera's current settings**;
sensitivity slider 1–5; **Ignore shadows**; **Classify with YOLO**; **Run**
and **Sweep all levels**.

- **Run** reports, per object: time, frames, duration, span, travel, area, area as
  a % of frame, whether the movement filter would hide it, and type when YOLO is
  on. The header line gives objects reported, illumination-guard events, frames
  scored and wall time.
- **Sweep all levels** reruns the cached decode across levels 1–5 × shadows off/on
  and shows a grid of object counts — the direct "does this calm the false ones
  without losing the real ones" comparison. With YOLO ticked each configuration is
  classified too, and the **Classified** column counts the labels it produced
  (`person 1, unknown 12`).

  An object whose nearest retained frame sits more than two seconds from its
  midpoint is reported as **`no frame`**, never as `unknown`. The distinction is the
  point: `unknown` means the detector looked and could not name the track, while
  `no frame` means it was never handed footage close enough to judge. If the frame
  budget runs out part-way through, the result line says so outright — *"only
  footage up to HH:MM:SS was kept for classification, so later objects show no
  frame"* — instead of leaving a column that reads like a detector verdict.

  A sweep replays its cached frames once per configuration, so **it covers the
  first minute of the selected range only** — ten replays of an hour would cost a
  gigabyte of frames and most of an afternoon. The button says so; the result line
  names the window actually swept against the one requested, and reports how many
  frames landed *inside* it — zero being an error rather than a quiet camera. If
  the decode cache fills before the window ends, the counts are flagged as **a
  floor** instead of being presented as totals. The progress dialog names the
  configuration currently running.

  The "roughly eight minutes for that minute" figure previously quoted here was
  measured on a CPU-only machine and **has not been re-taken** on the current GPU
  development box, so it is not a current expectation.

Replays always record everything (the record-time gate is left at 0); the only
threshold in the dialog is the movement-filter preview. Nothing is persisted
between sessions, and no back-end restart is needed — the harness runs in-process
in the front end.

The dialog only sees footage still in the archive; it reads the configured
retention rather than quoting a fixed figure.

Both Run and Sweep prime the background model with the two minutes of footage
before the window, and report only objects that *start* inside it. If a run
cannot reach the window at all it says so and stops, rather than reporting zero
— zero objects and "the window was never decoded" look identical in a count, and
telling them apart is the whole point of the frames-in-window figure on the
result line.

---

## Rules, Targets & Alert Responses

### The Rule Editor flow chart

One block per configuration page, top to bottom:

**Schedule** → **Video source** → **Look for** → **That are** → **Save clip** →
**Take action**

The Schedule block summarises itself — `Every day / All day`, `Weekdays /
08:00-18:30`, `Mon Wed`, `Set-Rise`, or **`Never`** when no day is ticked and the
rule's actions can therefore never fire. Rules saved before Schedule became its own
block open on **Take action**.

### Look-for targets

Beyond object types (person / animal / vehicle), a rule can target:

- **Faces** — a specific enrolled name, or **Unknown** (a detected but unrecognized
  face)
- **Nudity** — when NudeNet classes are enabled

Face results can arrive slightly after the initial report; **post-report face
updates** patch the event so name-based rules still fire.

### Substitution variables

Response text (webhook body, TTS message, email) supports:

- `{SvRuleLookFor}` — the rule's "look for" target
- `{SvRuleFace}` — face name(s) recognized, or Unknown
- `{SvRuleName}` — the rule that fired
- `{SvCameraName}` — the camera location
- `{SvEventTime}` — local `%Y-%m-%d %H:%M:%S`

### Responses

| Response | Notes |
|----------|-------|
| Email | Image attachment via SMTP |
| FTP upload | Uploads a snapshot |
| Local export | Saves a clip to a local folder |
| Snapshot | Saves a JPEG to a configurable folder |
| Play sound | WAV **locally** or **cast to a network speaker** |
| Text-to-speech | `kokoro-onnx`, local or cast to a network speaker |
| Send iHost command | Native call to a SONOFF/eWeLink **iHost** hub |
| Tapo camera | Siren and/or spotlight on the rule's own Tapo camera |
| Webhook | HTTP POST with substitution variables |
| Remote notification | Push via the Sighthound web service |

**Speaker casting:** both *Play sound* and *Text-to-speech* can cast to a discovered
Chromecast-compatible speaker; the target is stored per rule (`soundOutput`,
`ttsChromecastName`). A per-rule cooldown (`ttsCooldownMins`) throttles the whole
sound/TTS response.

**iHost:** drives the hub over its LAN API (config in `ihost_config.json`), with an
auto-off state machine so a device turned on by an event turns itself back off.

**Tapo:** sounds the siren and/or switches on the white spotlight of **the camera
the rule watches** — there is no camera picker. Fire-and-forget: the camera stops
the siren after its own alarm duration and the spotlight on its own timer (~300 s).
The same two controls are on the Monitor screen as **Siren** and **Light** buttons.
Requires a TP-Link account under *Options → Tapo*.

**Exported-file metadata:** files a rule writes carry capture-time metadata matching
their `yyyy-mm-dd-hhmmss` filename — snapshots get EXIF **DateTimeOriginal**;
exported clips get QuickTime **MediaCreateDate** + **TrackCreateDate**.

**Snapshot reliability.** Rule snapshots are cut from recorded video, which can lag
the event while the current segment is still being written, so the response waits
and retries. Several defects in that path were fixed: the "is the video there yet?"
check asked whether *any* video had arrived rather than whether the requested
**moment** was covered; the retry ladder advertised five attempts but could only
reach four; a saturated worker pool could spend the whole retry budget in seconds
without attempting a save; and two saves in the same second could lose **every**
copy. Snapshot filenames now include the camera. Measured over a full day,
snapshots saved rose from **84.2 % to 98.7 %** of events.

**A black snapshot is not always a bug** — the camera may genuinely have been dark.
But two real causes were found and fixed: the filename collision above, and a camera
whose own clock jumped at the moment its recorder reconnected, which could get up to
~200 s of video *painted* black and registered over the clips that followed (so
playback of that window was black too, not just the JPEG). Check the **Camera
Health** report for a clock jump on that camera at that time before concluding the
picture was lost.

---

## Live View

The Monitor window's large view zooms by a **ladder of discrete stops**, which the
**mouse wheel** (one notch per stop, about the pointer) and the **Zoom slider** under
the view both walk, so the two always agree (hardware-accelerated mode only):

```
Live → 1.0x → 1.2x → 1.5x → 2.0x → 2.5x → 3.0x → 4.0x → 5.0x → 6.0x → 8.0x
```

**The first step off Live is a source swap, not a magnification.** `Live` is the video
stream; every step above it shows the recorder's **full-resolution keyframe still**
instead — `1.0x` is that still at the same size as Live, which is simply the sharpest
the picture gets without magnifying anything. The readout says `still <n>s` and a
`STILL hh:mm:ss` badge is burned into the frame, so you can always tell which source
you are looking at. Stills refresh about every 2s on healthy cameras; if none newer
than 6s exists (common on the long-range wifi cameras) the view silently stays on live
video rather than present a frozen frame as if it were now. **Click-drag** pans once
actually magnified. Changing camera returns to `Live`.

Live view itself is fed by a shared memory frame the back end writes per camera. The
front end asks for frames at the on-screen widget's own pixel size, and the capture
loop resizes into that buffer from the decoded analysis frame. From 1.2× up the view
re-requests a proportionally larger frame, capped at **1280×720**. When a camera stops
delivering a larger frame than it already does, that size is remembered as its ceiling
(`live view: <cam> tops out at WxH` in the log).

Live video is built from the **640-wide analysis frame**, which is why the stills exist
— they come from the main stream and carry roughly 2x the linear detail. **Recording is
unaffected** by any of this — it is a stream copy of the full main stream. See
*Known Limitations*.

### The low-frame-rate warning

A yellow triangle on a preview tile means that camera's **measured publish rate** — the
rate frames are actually reaching the live view and the detection pipeline — has stayed
below **7.5 fps** for ~17.5s. That is a real signal: it is the same loop iteration that
feeds detection, so a sustained drop does reduce how many chances the detector gets.

It does **not** indicate a recording problem. Recording is a separate stream copy of the
camera's main stream and is unaffected by this rate.

> Before 2026-09-11 this warning was meaningless: the back end wrote the fps the *UI had
> asked for* into both header fields, and the monitor view requests 2 fps for preview
> tiles, so every unselected tile triangled permanently against the 7.5 threshold
> regardless of the camera. The header's second field now carries a measured rate.

---

## Search & Playback

### Clip entry and audio

- **A clip starts at its beginning**, not at the moment of detection, so you see the
  run-up to the event. Clicking the timeline or entering at a time still goes
  exactly where you ask; the detection moment is kept for the timebar marker.
- **Audio stops when you move on.** Switching segments or leaving the Search view
  silences it, and the stop discards the device's buffered tail (`abort()`, not
  `stop()`) so it ends at once.

### Detection overlays

In the **View** menu, overlays render on the **playing video**, not just thumbnails:

- **Show Boxes Around Objects** / **Show Different Color Boxes** — bounding boxes,
  time-synced, coloured per type (person = yellow, vehicle = orange, animal = pink;
  blue when the colour option is off)
- **Show Region Zones** — the rule's trigger regions, outlined in red

Overlays are drawn on the decoded frame in memory only — the stored or exported clip
is never modified — and a toggle applies immediately, even while paused.

### Zoom, pan and playback performance

- **Controls → Play from detection** is checked by default. Selecting a Search
  result starts playback one second before its first detection, or at the first
  available frame if there is less lead-in footage. Uncheck it to start at the
  beginning of the clip. This preference is remembered and applies to the next
  clip loaded; explicit timeline seeks retain their selected position.

- **Mouse wheel** zooms centred on the pointer; **click-drag** pans. A **Zoom
  slider** sits next to the audio button. Zoom resets when you load a different clip,
  and boxes and zones stay aligned at every level.
- **While paused**, zooming re-decodes that single frame at the clip's **full source
  resolution**, so magnifying a 4K clip reveals real detail. There is a brief pause
  the first time you zoom in on a frame.
- High-resolution clips (4K HEVC) decode on the **GPU (NVDEC)** when the codec and
  resolution warrant it, downscaling on the GPU to playback size, and fall back to
  CPU automatically. The chosen path is logged once per clip
  (`Playback decode: NVDEC … / cv2 …`).

### Events at the edge of a recording gap

Detection runs on the analysis stream and recovers **seconds before** the recorder
does after a reconnect, so an object can legitimately start before the first
archived frame. Playback clamps the entry time into the clip's real bounds, and a
frame lookup landing in a hole between two files of the same clip moves to the
nearest frame **inside that clip**.

An event whose footage genuinely was never recorded still shows no video — that is a
camera or network outage, not a playback fault.

### Detection record

The detections panel has a **👓 View record** link that opens a read-only dump of
everything the pipeline stored about that clip's objects: type, camera, absolute and
clip-relative times, duration, all attributes (face name/confidence, gender, age,
nudity …) and a track summary (frame count, first/last box, entry/exit edge,
bounding envelope). **Export JSON…** and **Copy** are provided. Useful for answering
"why did this fire?" without opening the databases.

### Exporting frames & clips

- **Export Frame** writes the current frame as a **full-resolution** JPEG.
- **Export Clip** writes an MP4. With no burned-in overlays it is a fast
  stream-copy; with **Bounding Boxes** or **Timestamp** overlays it re-encodes at a
  sane bitrate (≈6 Mbps at 4K, scaled by resolution). The temporary `*.video.mp4`
  next to the output during a re-encode is normal.

### Date selection

The date button shows the full weekday and date. Its width is measured from the
longest label it can display, with a margin — an earlier version measured on an
unrealized button and under-read, truncating mid-week dates to `Wed 2026...` while
"Today" and "Yesterday" looked fine.

### Rule schedules

A rule can carry a **schedule** (days of week plus a window that may be fixed clock
times or **sunrise/sunset ± offset**). In the Search window:

- **Schedule-aware results:** searching a scheduled rule returns only results whose
  event falls within that rule's active window(s) for the searched day — matching how
  the rule fires live. Rules with the default schedule are unaffected.
- **Clock indicator:** rules with a non-default schedule show a clock icon in the
  rule list — enabled when the schedule can run, disabled when it never can (no days
  selected, or a zero-length window).
- **Duplicating a rule** carries over the original's schedule.

---

## Saved Events — Daily Summary Video

**Options → Saved Events** enables a per-camera **daily summary video**: a day's
activity thumbnails stitched into a small **10 fps** montage — a fast scrub through
everything that moved.

- Output: `<summaryDir>\yyyy\mm\yyyy-mm-dd\video summary\video-yyyy-mm-dd-<camera>.mp4`
- Config is two `backEndPrefs` keys (`summaryEnabled`, `summaryDir`) — no new JSON
- Generation runs in the **DiskCleaner** (low priority, storage-aware) roughly every
  30 minutes, at most one (camera, date) per pass, skipping summaries that exist.
  Days are bucketed by **local** calendar date. NVENC encode with x264 fallback.
- Each summary is stamped with **MediaCreateDate** + **TrackCreateDate** at that
  day's **23:59** — a summary spans the whole day, so there is no single capture time.

---

## Reports — what has happened since

A top-level **Reports** menu writes a plain-text report to a file you choose. The
System view answers *"what is happening now"*; Reports answer *"what has happened
since"*.

Each asks for a **start date and time** (it runs to now, across all cameras) and
then where to save. You are asked for the destination **before** the scan runs, and
offered the file when it finishes.

| Report | Answers | Typical time |
|---|---|---|
| **Camera Health** | clock jumps, outages, black-filled and abandoned gaps, and **coverage** per camera | ~30–80 s |
| **Log Errors & Warnings** | every ERROR/WARNING, grouped by message shape, then listed in full | ~3 s |
| **Detections** | detections per camera per type, plus sub-types | under a second |

**Camera Health** is the one to run when footage looks wrong. Its `cover%` column —
clip seconds ÷ wall-clock seconds — is the only honest measure of whether video is
missing; clip *counts* and outage totals both hide loss. It also reports
`jumps`/`worstS` (a camera whose own clock ran away), `blackS` (outage recorded as
black — missing time you can see) and `aband` (fill gave up — missing time you
**cannot** see).

**Log Errors & Warnings** groups before it lists, which is what makes it usable: on
one system 246,283 matching lines collapsed to **301 distinct message shapes**, one
of which was 71 % of the volume.

**Detections** keeps `object` in its own column and never folds it into a total. It
is ~93 % of all rows and does **not** mean "something was seen" — it means the
classifier never named the track. It is the number to watch after any change to
motion sensitivity.

Camera Health and Log Errors run on a background thread with a cancellable progress
bar; Detections runs inline.

---

## System View — health dashboard

**View → System View / Ctrl-4**, or the toolbar button. The desktop polls every
two seconds while this view is visible; camera telemetry continues in the backend.

- **Cameras table** — status, dimensions, measured analysis FPS, decoder, frame
  age, recent dropped-frame percentage, reconnects and failure details. Frames
  older than 30 seconds trigger **No frames**. If frames arrive but analysis is
  more than 120 seconds behind, the status is **Analysis lag**. A stopped recorder
  can show **Recorder** separately.
- **System lines** — disk space, system/app memory, NVIDIA GPU compute and decode
  utilization, VRAM, temperature and power, PC clock offset and total app CPU.
  GPU sampling runs in the background. Database damage and more than ten minutes
  without clip registration are flagged; the dashboard does not repair databases.
- **Processes table** — tracked backend/service processes with memory and CPU use.
- **Footer** — backend uptime and the detection service's loaded model names.

The implementation is `frontEnd/SystemHealthView.py` →
`frontEnd/BackEndClient.py` → `backEnd/NetworkMessageServer.py`. Camera metrics
originate in `StreamReader.getHealth()` and are forwarded by `CameraCapture` and
`BackEndApp`. Historical reports use the separate `SystemHealth*Report.py` modules.

---

## Image View — your own photos and videos

Every other screen looks at footage **we** recorded. The **Image** tab (Ctrl-5) is
the exception: it browses your own filesystem and runs your photos and videos
through the **same models, with the same settings**, that the cameras use.

- **Folder tree** on the left, **thumbnail grid** in the middle, **details** on the
  right. The Show-only filters are the same targets the rule editor offers —
  People, Animals, Vehicles, Nudity, Faces.
- **Detection is on demand.** Click a file and press **Analyze this file**, or use
  **Add folder to database** to import supported media recursively. Choose
  **with analysis** (default) or **filenames only**, which needs no detection
  service. Nothing is analysed just by browsing. Both modes verify file contents
  and link true duplicates to their existing descriptions, tags, detections and
  **Duplicates found in folders** list. Filenames-only preserves existing results.
  Dot-prefixed files/folders and folders containing `RAW` (case-insensitive) are
  skipped, including their subfolders. Analysis warns when face or nudity models
  are unavailable or disabled and offers **Continue** or **Abort**. Existing
  successful results for the same content/settings are reused. **Stop** keeps
  completed registrations; cancelled partial video analysis is not saved.
- **Results are stored separately**, in `<dataDir>\usermedia\usermedia.db`. They are *not*
  in `objdb2`, they do not appear in the Search window, and no cleanup process can
  reach them or your files.
- **Videos are sampled, not tracked** — one frame every 2 s through YOLO / face /
  nudity. That answers "a person is visible around 00:42"; it is not object
  tracking and carries no duration or travel.
- **Add faces to the baseline.** After analysis, choose a face in the Details
  pane and click **Add face to baseline…**. Named and unknown faces are both
  available; video entries show their sample time. Review the crops, untick any
  wrong or poor faces, choose an existing person or enter a new name, then confirm.
  This uses the same baseline and preview dialog as Search. Cameras reload it
  automatically; click **Analyze this file** again to refresh stored Image results.
- **Metadata** for the selected file is listed at the bottom of the details pane,
  and is editable when ExifTool is installed (see *Metadata editing*).

### Import descriptions and duplicate locations

The thumbnail **Sort by** control applies to both folder browsing and search
results. The default is **Name A-Z** (filename, then folder for ties). Other
options are **Name Z-A**, **Modified date: newest first**, and **Modified date:
oldest first**. Date means the filesystem's last-modified time, not EXIF capture
time. The chosen order stays in effect as you browse and search during the session.

Right-click a thumbnail for **Show in Explorer** (opens the folder and selects
the file) or **Rename file...**. Keep the existing extension. For indexed
duplicates, choose **This copy only** or **All copies**. Renaming updates the
stored locations and the duplicates section while keeping descriptions and
detections on the shared content record. Existing destination files are never
overwritten. If a copy is missing, changed, or cannot be renamed, the operation
reports the problem and rolls back moves already made. Finish Image view
analysis before renaming files.

The Image details accordion allows multiple sections to stay expanded while
browsing. **Duplicates found in folders** lists other indexed folders containing
the same verified content.

Use the search box above the thumbnail grid to search saved file fields,
descriptions, tags, duplicate paths and detection fields. Press Enter (including
the numeric keypad Enter) or Search. Spaces and arrow keys work normally in the
search field; video playback shortcuts are suspended while Image view is active.

**Advanced search**, beside Search, opens a condition builder. Select **All fields**
or a column from `files`, `detections`, or `file_locations` (including IDs and
duplicate-location metadata). Add conditions and choose **All** (AND) or **Any**
(OR); **Exclude** negates a condition. Available comparisons include contains,
whole word, exact field value, blank/not blank, exact semicolon-separated tag,
and numeric or date ranges. Exact text matching ignores case; exact person names
match the full name. Date fields accept local `YYYY-MM-DD` or
`YYYY-MM-DD HH:MM:SS`; date-only end bounds include the whole day. Size is in
bytes, duration/sample offsets in milliseconds, and confidence scores in 0–1.
Both Between bounds apply to one value; separate conditions can match different
detection rows for the same media record.

**Apply search to** defaults to **New search** every time the dialog opens.
Choose **Current selection** to narrow only the thumbnails currently displayed
by the previous search. Here, “selection” means the displayed result list, not
the single highlighted thumbnail. It cannot add other files or other duplicate
locations. Reopen Advanced search to refine the resulting list again.

**Saved selections** stores named snapshots of the current displayed file paths,
separately from saved search expressions. Use **Save current selection...**,
**Load selection**, and **Delete** in Advanced search. Loading closes the dialog,
restores available files, and clears text/Show only/person filters; it does not
rerun the original query. Missing or unavailable files are skipped with a count.
An empty selection stays empty. Reopen Advanced search and choose Current
selection to search within a loaded snapshot. Sorting still applies. New search,
the regular Search button/Enter, or choosing another folder returns to normal
folder/all-indexed-folder scope. Renames made inside Image view update stored
selection paths; external moves/renames cannot be followed automatically.
Deleting a saved selection removes only the saved list, never the files.

The existing text expression can be combined with builder conditions using AND.
The generated expression is previewed, validated, and placed in the normal
search bar when Search is pressed. **Save as**, **Load**, and **Delete** manage
named search expressions/conditions; the current folder and Show only filters
still apply and are not part of the saved search. **Show matches** opens a
scrollable display with matching positive field values highlighted for the
selected thumbnail. NOT and blank checks do not have text to highlight.

Equivalent typed examples: `person:exact:Bernie`, `tags:tag:beach`,
`detections.conf:ge:0.8`, `files.durationMs:between:"1000,60000"`, and
`empty:files.description_ai`. Supported numeric modes: `eq`, `ge`, `gt`, `le`,
`lt`, `between`. Date conditions generated by the dialog use stored timestamp
units; use the dialog to avoid entering epoch timestamps manually.
Terms use case-insensitive contains matching; adjacent terms imply AND. Supported
operators are AND, OR, NOT (precedence: NOT, then AND, then OR), with parentheses
for grouping and double quotes for phrases. Examples:

For whole-word matching, use **`"door"`** (or **`word:door`**). It finds `door` without matching
`outdoor`, `doorbell`, `indoor` or `doors`, and still includes a record containing
both `door` and `outdoor`. Use **`ai:word:door`**, **`tags:word:door`** or
**`filename:word:door`** to restrict the field. **`ai:word:"front door"`** finds
that literal phrase with word boundaries. Matching ignores case; spaces,
punctuation and underscores separate words. Combine these terms with AND/OR/NOT
as usual. **`ai:"door"`** also works for a field-specific whole word, and
**`"front door"`** matches that phrase with word boundaries. Plain terms retain
substring matching. For quoted substrings use **`CONTAINS "door"`** or
**`ai CONTAINS "door"`**. Advanced search's Contains condition generates this
explicit syntax, so its behavior is unchanged.

- `person:Bernie AND (tags:sailing OR ai:"blue sail")`
- `sailing NOT filename:crop`
- `tags CONTAINS family`
- `has:tags AND NOT has:ai` (nonblank tags, blank AI description)
- `has:description_tags AND empty:description_ai` (same check using full names)

`has:field` means the field contains non-whitespace text; `empty:field` is its
opposite. NULL, empty strings, spaces, tabs and line breaks count as blank.

Field shortcuts include `filename:`, `path:`, `tags:`, `ai:`, `person:` and `type:`;
other stored file/detection column names also work. With no field, all stored
columns are searched. EXIF read directly from media files is not indexed.
Search covers the selected folder and subfolders, or **All indexed folders**.
Show-only filters combine with the text search. Syntax errors display a message
and leave the previous results visible.

`scripts/import.py` reads JSON arrays from `IMPORT/Files.txt` and `IMPORT/Ai.txt`.
It combines distinct tags with `; ` and maps Gemini text by filename only when
that name identifies one verified file content. Distinct Gemini descriptions for
verified duplicates are combined with blank lines. Existing AI text is preserved,
and repeating an import does not append the same descriptions again. Missing files
and same-name files with different bytes are reported for review; their ambiguous
filename-only AI descriptions are not imported.

The default is a **three-content preview**, with a separate `import-preview.db`.
Without `--apply`, only a plan/report is written. Run from the checkout:

```powershell
.\venv\Scripts\python.exe scripts\import.py --apply
```

Use `--filename "photo.jpg"` (repeatable), `--tag "family"`, or `--limit 5` to
select a sample. `--database` and `--report` choose output paths. A full run
requires `--all --apply`; explicitly select the live `<dataDir>\usermedia\usermedia.db` only after
reviewing the pilot. An existing target database is backed up before changes.
Keep the application/detection service running for analysis.

For an interrupted import, add `--resume` to reuse successful analysis only when
the file hash, file state and model settings still match. Descriptions and tags
are still merged. Progress reports are checkpointed every 25 records; temporary
Windows report-file locks are retried without discarding database work.

Very large photos are reduced to at most 16 million pixels for detector requests
to fit the service's 64 MiB message limit. Source files and their recorded
dimensions stay unchanged; detection boxes remain normalized to the source.

SHA-256 identifies exact byte duplicates, which share descriptions and detections
while `file_locations` keeps each path. Photos with different bytes, including
metadata-only differences, remain separate. Normal Image-view analysis performs
the same identity check. Existing records are consolidated as they are encountered;
this is not an automatic full-library rescan.

### What it costs

Nothing in this screen loads a model. Inference is one RPC to the
`DetectionService` process that is already running, so there is no second model
load and no second CUDA context — the same seam `DetectionReplay` uses.

What it *does* cost is time on that service's single inference lock, shared with
every camera. So: one worker, one file at a time, with a deliberate ~100 ms gap
between files during a folder scan. On a GPU box a photo is quick; on a CPU-only
machine a single YOLO pass is measured in **seconds**, which makes a
several-thousand-photo folder an hours-long job. The progress line says where it
is up to, and **Stop** ends it.

### Metadata editing

Reading works out of the box (Pillow, common image formats). **Editing needs
ExifTool**, staged at `tools\exiftool\exiftool.exe`; without it the pane is
read-only and says so.

ExifTool rather than Pillow for one specific reason: `img.save(..., exif=...)`
**re-encodes the image**, losing quality and silently dropping XMP, IPTC and
MakerNotes. That is fine for a snapshot we generated (which is what
`DataManager.saveEventSnapshot` does) and not fine for someone's original
photograph. ExifTool rewrites only the metadata segment.

### Known limitations

- **HEIC is not readable.** Nothing in the tree can decode it — no `pillow-heif`,
  and this Pillow build has no HEIC support. iPhone libraries in HEIC will list
  nothing.
- **The classifier vocabulary is narrow for a photo library.** Only
  `bird, cat, dog, horse, cow` map to *animal* and `car, motorcycle, truck` to
  *vehicle* (`_kAnimalClasses` / `_kVehicleClasses`). A bicycle, bus, boat, sheep
  or bear classifies as nothing.
- **Faces are only named if `RUN_FACE` is on** in Options → AI Detection. With it
  off, a face is not even detected — which is correct "same settings as the
  cameras" behaviour, but surprises people.
- The tab button is unlabelled artwork until `frontEnd/bmps/View_Image_*.png` are
  replaced.

---

## Storage & Reliability

- **Storage quota** (Options → Storage) trims the oldest clips to stay under the cap.
- **Low-disk safety net:** below **10 %** free on the video drive the DiskCleaner
  raises an alert and **stops recording** to prevent disk-full corruption; recording
  resumes above **15 %** (hysteresis).
- **WAL journal mode** on all three SQLite databases lets readers and the writer run
  concurrently — this removed the Search-window freeze caused by lock contention.
- **Response-DB self-heal:** on open, a corrupt response DB is detected (probe write)
  and moved aside (`.corrupt-<ts>`) so a fresh one is created instead of crashing the
  ResponseRunner.
- **Corruption-tolerant LAN viewer:** WebServer clip/object lookups catch SQLite
  `DatabaseError`, so an unreadable page degrades that one clip to "unavailable"
  instead of failing the whole search.
- **A damaged-database report is made from evidence, and only once.** There used to be
  two parallel mechanisms for one condition — two marker files, two dialogs, and a
  message poll that short-circuited the whole front-end queue while a marker existed.
  Now: when clip registration stalls, an integrity check runs **first**. If a database
  really is damaged you are told, once, with a **Repair now** button (which closes the
  app and runs the repair). If they all pass, the orphan sweep is suspended silently
  so unregistered footage is still protected, and nothing claims damage. The stall
  baseline is clamped to the back end's own start time, so downtime — an install, an
  overnight shutdown — is no longer counted as silence.
- **Clock-sync watchdog:** SV timestamps from the PC clock while cameras burn their
  own NTP-synced clock into the picture. If the PC clock drifts more than a few
  seconds, recorded times won't match what the cameras show — so SV checks against
  public NTP (daily and at startup) and warns you, with the fix
  (`w32tm /resync /force`). It never changes your clock. If you see the warning, make
  sure the Windows Time service is running and Automatic.
- **Camera clock jumps:** separately from the PC clock, individual *cameras* can have
  their own clock run away — on one fleet 10 of them did, in three distinct patterns
  (escalating, intermittent, and rare but severe at ~230 s). Sanity guards cap what a
  segment can claim even at a recorder reconnect, which was previously the one moment
  nothing guarded. The **Camera Health** report has `jumps`/`worstS` for this.
- **Saved-clip trimming:** two long-standing DiskCleaner warnings — `Couldn't create
  clip` and `Correcting last timestamp` — were investigated and found to be **noise,
  not lost footage**. Both are fixed, and only genuine shortfalls (over a second)
  still appear as warnings.
- **Duplicate clip records:** a segment registered twice in `clipdb` used to stop the
  disk cleaner outright, and it failed again on every restart. The cleaner now reports
  the duplicate and carries on, which also clears it.

---

## Adding Cameras — ONVIF / UPnP Discovery

Camera **discovery is off by default** and costs nothing at rest. It only auto-lists
network cameras in the **Add-Camera wizard** — it is *not* needed for recording. The
app starts a scan automatically when you open the wizard and stops it ~20 s after you
close it, so `Onvif.log` / `Upnp.log` stay quiet during normal operation.

- **Just open Add Camera** — ONVIF/UPnP cameras appear on their own.
- **ONVIF credentials:** for TP-Link **Tapo** cameras this is the per-camera
  **"Camera Account"** you create in the Tapo app (Advanced Settings → Camera Account
  / Third-Party access), **not** your TP-Link cloud login.
  ⚠ The **siren / spotlight** controls want the *opposite* account — your TP-Link
  sign-in — because the cameras' control API on port 443 refuses the camera account.
  Two different accounts, two different purposes.
- **To keep discovery always on**, create an empty file named `enableOnvif` (and/or
  `enableUpnp`) in the user data directory and restart. You can always add a camera
  manually by RTSP URL.

A camera entry with **no credentials in its URI** — a placeholder you created and
never configured — is recognised as unconfigured: it is never probed for Tapo
control, and its siren/spotlight buttons read as unavailable. Previously the Camera
screen fired a speculative control query at whichever camera sorted first, which on a
placeholder meant a 2-second timeout and a WARNING per visit.

---

## Web / Remote Access — LAN Record Viewer

A self-contained **LAN record viewer** (`backEnd/WebServer.py`) — a pure-Python
`http.server` in its own child process serving the static UI in `backEnd/webroot/`.
It replaces the original nginx + XNAT (NAT-traversal) stack: no nginx, no openssl,
no cloud, no router port opening.

**Enable it:** Options → Remote Access → check *Enable remote access*, set a
username/password and a port > 1024, Apply → reach it from any LAN device at
`http://<this-PC-LAN-IP>:<port>/`.

- Requires a login (HttpOnly session cookie; credentials stored hashed in `webAuth`;
  per-IP rate limit).
- Reads the object and clip databases **read-only** (`?mode=ro`), so it never
  interferes with the running back end.
- **Filter search:** any field or combination — camera, object type, sub-type, face
  name, gender, age range, nudity, minimum confidence, **movement filter**, time
  range — then play the matching clip (streamed with HTTP Range). Per-detection
  thumbnails are extracted on demand and cached.
- **Search by rule:** the viewer can also search with the same **rules as the desktop
  Search screen** — the seven built-ins (*All objects*, *People*, *Vehicles*,
  *Animals*, *Unknown objects*, *Nudity*, *Faces*) plus every saved rule, regions,
  boundaries, durations and all. Pick one from the **Search rule** dropdown and the
  detection filters stand down; camera and date range still apply. A rule's
  **schedule is honoured**, so a sunset→sunrise rule returns only night events, and
  scheduled rules are marked with a clock in the dropdown. It runs the same engine
  (`appCommon/SearchUtils.getSearchResults`) the desktop uses, so the two agree. One
  pass per day, so a single rule search covers at most 31 days.
- **Search runs itself.** There is no Search button: selects, checkboxes and dates
  search immediately; text and sliders are debounced 400 ms. A **Clear filters** link
  in the panel header resets everything (including the rule) and searches once. A
  stale-response guard drops any reply that isn't the newest request — a rule search
  takes ~1–2 s against a filter search's ~50 ms, so without it a slow reply could
  repaint over a newer one.
- **Face enrollment from footage** — open a person detection → *"Add this face to the
  baseline"*.
- **Mobile layout.** At ≤640 px the results grid is one column, From/To stack, inputs
  are 16 px (which stops iOS zoom-on-focus), and buttons are full-width tap targets.
  Verified at 320/375/412 px with zero horizontal overflow; desktop is unchanged.

**Access is LAN-only in practice, not in code.** The server binds `0.0.0.0` and does
**no source-IP filtering** — "LAN-only" means nobody has forwarded a router port to
it. There is no allowlist and no WAN switch. Two things do vary per client machine:
the per-IP login lockout (the failure count clears only on a *successful* login, so a
machine that has failed six times stays on a hair trigger until it gets one right),
and the Options tab advertising a `https://` URL for a plaintext server — a browser
with HTTPS-Only mode or an HSTS entry will silently upgrade and fail. If a machine
cannot connect, check `WebServer.log` for its IP: if it appears with `failed login`
it is the lockout; if it never appears, the request is not reaching the server at all
(firewall profile, routing, or the `https://` upgrade).

---

## Options tabs

`General` · `Storage` · `Remote Access` · `Grid` · `AI Detection` · `iHost` ·
`Tapo` · `Saved Events` · `Colors`.

**`General`** also holds the two service controls — "Run the back end as a Windows
service" and "Run Sighthound Video at system startup" — both disabled until
SHLaunchPY3 is installed. See [build/INSTALL.md](build/INSTALL.md).

**`Colors`** sets the application background colour: a picker plus **Use default**.
Monitor, Search, Grid and System Health recolour **immediately** on OK — no restart —
and the choice persists (front-end pref `uiBackgroundColor`). Text contrast is
**repaired, not overwritten**: a label is recoloured only when its current colour
would fall below a 4.5:1 contrast ratio against the new background, so deliberate
accents (blue face names, orange warnings, System Health's green/red) survive.
Deliberately not recoloured, as the tab says: the view tabs across the top (opaque
grey is baked into the PNGs), pop-up dialogs, the rule-editor canvas, and video areas.

**`iHost`.** Both fields stay hand-editable; the buttons are shortcuts.
- **Search…** finds the hub on the LAN: it tries `ihost.local` first, then sweeps
  this machine's own subnets, asking each address to identify itself via the hub's
  unauthenticated `/bridge` endpoint — so a printer answering HTTP is rejected rather
  than accepted. Runs off the UI thread; the button becomes **Stop**.
- **Get token…** performs the hub's confirmation handshake: it answers `401 link
  button not pressed` until somebody presses **Done** on the hub's own console. Polls
  for up to five minutes with a countdown.
- **Location** (used by the night-only rule gate) is detected from the public IP when
  both fields are blank, with a **Pick city…** list for no-internet cases. A position
  you typed is never overwritten.

**`Tapo`.** Credentials for siren/spotlight, stored globally in `tapo_config.json`
(**not** per rule and **not** in front-end preferences — the rule action fires from
the ResponseRunner process, which cannot read those).
- **These are your TP-LINK ACCOUNT credentials** — the email and password you sign in
  to the Tapo app with. The *camera account* in your stream URI streams RTSP fine but
  is **refused** by the control API on port 443. This trips everyone once.
- **Test** tries the entered credentials against a chosen camera and reports what came
  back — success with model and firmware, a wrong-password message naming which
  password is wanted, or the actual network error.
- Repeated failed logins earn a **30-minute lockout** from the camera. Nothing retries
  in a loop.

---

## Legacy Migration (first launch)

On first launch, if a Py2 `Sighthound Video` data folder is found and the Py3 folder
has no existing config, a dialog offers to **Import Cameras & Rules** or **Start
Fresh**. The import converts protocol-0 pickles (which Py2 wrote with Windows `\r\n`
line endings) by stripping `\r` before unpickling. Storage paths (`dataDir`,
`videoDir`) are intentionally dropped so the Py3 app uses its own default video
directory. The legacy folder is **never modified**.

---

## Known Limitations

- Audio playback is **1× speed only** (no time-stretching at other speeds).
- Remote access is **LAN-only by convention, not by enforcement** — see the LAN
  viewer section above.
- The web viewer's rule search covers built-in and saved rules, but not the Search
  screen's front-end-only *Custom searches* — those live in the front-end prefs
  pickle, which no back-end process can read.
- `PyAudio` is listed in `requirements.txt` but is not used for playback.
- ONVIF / UPnP discovery is **off by default** and runs only while the Add-Camera
  wizard is open.
- A camera with a flaky RTSP feed produces real recording gaps; export/snapshot
  responses for events inside a gap report "no video" and are skipped, with no retry
  loop.
- **A persistently low-coverage camera is usually the camera, not the software —
  check before changing code.** The tell is that the recorder's own counters are
  *clean* (few restarts, no unusable segments, no finalize failures) while the log
  shows the stream arriving half-empty: measured delivery far below the declared rate
  (one 4K HEVC camera ran **3.1–5.9 fps against a declared 25**), `remux: <seg> holds
  Xs but its container claimed Ys`, and repeated `had N outage(s) … filled black`
  *inside* single segments. That camera sat at 33 % coverage over 37 hours while the
  rest of the fleet averaged 79 %. Check its link and bitrate, or drop it to 1080p.
- **Live view is built from the 640-wide analysis frame**, scaled from the main
  stream. **Recording is unaffected**; only the monitor image is soft, and live zoom
  sharpens only up to 640 wide. This is a deliberate trade and a known regression for
  the handful of cameras whose substream was 720p/960p. Raising a camera's substream
  settings now does nothing — nothing reads it. The real fix would be a third scaled
  output at live-view size; do **not** raise the analysis cap to follow the live view.
- The `liveMaxResolution` / `liveMaxBitrate` preferences do nothing — they reach
  `StreamReader.setLiveStreamLimits`, a stub in the Py3 port. Don't debug live
  resolution by changing them.
- **Segment times still lag on long-range/wifi cameras.** Segment dating is anchored
  to the PC clock at ffmpeg spawn/exit, which cannot see a camera's own delivery lag.
  Healthy cameras land within ~1–2 s; a camera delivering ~11 s late keeps that error.
  **Before suspecting the recorder, check the PC clock** — a stopped `w32time` makes
  *every* camera read late.
- **Event timestamps can be tens of seconds late on a badly backlogged camera.** When
  a stream runs far behind real time, `_stampFrameMs` discards its PTS anchor once a
  stamp would be >30 s behind the wall clock and falls back to arrival time — turning
  "this frame is old" into a confidently wrong stamp. Seen once at 33.7 s on a camera
  running 3.9–6.3 fps; not reproducible on healthy cameras.
- **A short track can be classified without ever reaching the detector.** Detection
  requests are throttled to one per 500 ms globally, so a track shorter than the gap
  to the next analysed frame is never sampled and falls back to the raw motion type
  (`no frames! … frameCount=0` in the camera log). It shows as `object`/unknown.
- **`subType` can disagree with `type`.** The best-confidence sub-type is tracked
  globally rather than per category, so a strong person hit can leave a stale animal
  sub-type attached. Rules fire on `type`, which is not affected — the wrong value
  only shows in search results and the record viewer.

---

## Development Notes

- **Qt dialog pilots:** `frontEnd/qt/` contains Designer `.ui` layouts and Python
  behavior for FTP setup and camera removal. `Start.bat` enables both dialog flags;
  the main application remains wxPython. See [the Qt guide](frontEnd/qt/README.md).

- **Check which copy is running before concluding anything.** Several near-identical
  trees can exist (a checkout, `C:\Program Files\Sighthound Video Py3`, and
  `build\stage\payload`). `DetectionService.log` prints an absolute path on every
  start (`loading YOLO weights: <tree>\models\yolo\...`), and `SHLaunchPY3.log` prints
  `installDir=`. Read those, not the folder name. 
- **Start the app:** `Start.bat`. Front end only: `StartFrontend.bat`.
- **Logs:** `%LOCALAPPDATA%\Sighthound Video Py3\logs\` (per service) and
  `…\logs\cameras\<cam>.log` (per camera: `[ImageCheck]` lines, remux lifecycle,
  measured fps, `hw decode engaged (d3d11va)` / `… unavailable`, `[motion]` settings).
- **Per-camera CPU profiling** is always on — one line per camera per minute in the
  camera log, prefix `profile:`: capture-thread stages (decode / analysis / live-view
  publish) with decode *wait* separated from decode *CPU*, MOG2 cost, the main-thread
  breakdown, and a named per-thread CPU table. Two cautions if you extend it: measure
  CPU with `time.thread_time()` (wall-clock makes a network-blocked thread look 100 %
  busy), and note that `thread_time` has ~15.6 ms granularity on Windows — trust the
  per-thread 60 s totals over per-frame stage numbers.
- **Env knobs:** `SV_HW_DECODE` (1 = try D3D11VA, default on), `SV_NVDEC`
  (experimental NVDEC pipe decode for analysis, **default off** — its codec probe
  opens an extra RTSP session and it re-opens the capture when the live-view size
  changes; both destabilised session-limited cameras), `SV_VIDEO_LATENCY_MS`
  (frame-timestamp bias), `IMAGECHECK_CONFIG` (override the AI-config JSON path).
  Documented in `Start.bat`.
- **Record constants:** top of `videoLib2/python/StreamReader.py` —
  `_kRemuxSegmentSecs`, `_kGapFreezeMaxMs`, `_kGapFillMaxMs`, `_kGapBlackMinSecs`.
- **Motion tuning:** top of `backEnd/VideoPipeline.py` — `_kSensitivityLevels`,
  `_kPromoteHits`, `_kCandidateTimeout`, `_kIllumFgFraction`, `_kIllumMaxRun`.
- **UI background colour** (`vitaToolbox/wx/AppColors.py`): new windows inherit the
  chosen colour automatically. If a control must keep its own background, set
  `svKeepOwnBackground = True` on it — video surfaces are skipped already.
  `AppColors` must never import from `frontEnd`.
- **YOLO weights:** `models/yolo/`.
