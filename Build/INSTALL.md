# Installing and running Sighthound Video Py3 as a program

`Start.bat` runs the app from a source checkout and needs Python, a venv and an
internet connection. **Installing** it needs none of those: `build\` produces a
single Inno Setup installer that carries the entire program.

- To **build** the installer, see [README.md](README.md) in this directory.
- This file covers what the installer produces, how the service works, and how
  to update an installed copy.

---

## Installing

Run `SighthoundVideoPy3-Setup-<version>.exe` and click through. Keep any `.bin`
files that came with it in the same folder — the payload exceeds the ~2.1 GB a
single `setup.exe` can hold, so it ships disk-spanned and the slices must travel
together.

Setup installs to `C:\Program Files\Sighthound Video Py3`, creates Start-menu and
desktop shortcuts, registers and starts the **SHLaunchPY3** service, and offers
to open the app. **Nothing else is required on the machine** — no Python, no pip,
no CUDA toolkit, no internet connection, no console windows.

Unattended, for a scripted rollout:

```bash
SighthoundVideoPy3-Setup-2026.08.28.exe /VERYSILENT /SUPPRESSMSGBOXES /NORESTART
```

| Switch | Effect |
|---|---|
| `/DIR="…"` | Install location (default `C:\Program Files\Sighthound Video Py3`) |
| `/DATADIR="D:\SVData"` | Databases, logs, rules and face enrollments go here instead of `%LOCALAPPDATA%\Sighthound Video Py3` |
| `/SERVICEUSER=".\Name" /SERVICEPASSWORD="…"` | Run the service under a real account instead of LocalSystem. Needed **only** when video storage lives on a network share — LocalSystem cannot authenticate to SMB as you. |
| `/MERGETASKS=!desktopicon,!service` | Skip the desktop icon and/or the service registration — useful for a scratch test install |

Setup also adds one inbound firewall rule for the interpreter, on **private and
domain** networks only, so the LAN record viewer works. This matters because the
back end runs as a service and therefore never gets the interactive "allow
access?" prompt — without the rule it would silently fail to accept LAN
connections.

**Uninstalling** stops and unregisters the service, removes that firewall rule
and the program, and *asks* before deleting the data directory (default: keep).

`PrepareToInstall` stops the service and kills leftover `SighthoundPy3*`
processes before replacing files; without it the files being replaced are locked.

---

## What is inside it

| Installed as | What it is |
|---|---|
| `SighthoundVideoPy3.exe` + `lib\` | The launcher the shortcuts point at (cx_Freeze) |
| `python\` | A private CPython 3.12.10 with every pinned dependency, including the CUDA torch build |
| `python\SighthoundPy3.exe` | Windowless interpreter copy — **every app process runs as this** |
| `python\SighthoundPy3c.exe` | Console copy, for service administration and diagnostics |
| `models\` | InsightFace `buffalo_l` and the Kokoro TTS voices, so nothing is downloaded on first use |
| `datadir.txt` | The data directory the installer configured |
| everything else | The app sources, exactly as they are in the checkout |

**The private Python is a copy, not a venv.** A venv still needs the base
interpreter present on the target machine; a copied CPython tree finds its own
stdlib relative to its executable, needs no registry entries, and cannot be
disturbed by — or disturb — any other Python on the box.

**The app itself is not frozen.** The launcher (`build/launcher.py` →
`SighthoundVideoPy3.exe`, built by `build/setup_launcher.py`) is deliberately a
~100 KB shim that locates the private interpreter and starts
`frontEnd.FrontEndApp` with it. Freezing wx + torch + CUDA would be gigabytes,
slow and fragile, and this way what ships is exactly what was tested from source.

**Task Manager reads "Sighthound Py3"**, not "python" or "ffmpeg". Every
executable in the process list is a copy under a Sighthound name with a rewritten
version resource: the interpreter (`SighthoundPy3.exe`, inherited by every camera
and detection child through `sys.executable`), the service host
(`SighthoundPy3Service.exe`) and the recorders (`SighthoundPy3-ffmpeg.exe`, one
per camera).

**Models ship in the box.** InsightFace would otherwise download `buffalo_l`
(~280 MB) into `~\.insightface` and kokoro-onnx ~337 MB on first use — both need
internet, and under the service both would land in the *service account's* home.
They are bundled and passed explicitly, with the download left as a fallback.

Three details that are load-bearing and easy to undo by accident:

- **The service host lives in `python\`, not `site-packages\win32\`.** It imports
  `python312.dll`, and Windows searches the executable's own directory first. Left
  where pywin32 puts it, `SighthoundPy3Service.exe` starts only because some
  Python happens to be on `PATH` — true on a build machine, false on a clean
  target, where the service fails to start with nothing useful to go on.
- **pywin32 DLLs are placed app-locally**, next to the interpreter and the service
  host, rather than running `pywin32_postinstall.py -install` (which copies them
  into System32 and breaks any other pywin32 on the machine). Without them the
  service fails with a bare "Error 1053".
- **The VC++ runtime is app-local too** (`msvcp140*`, `vcomp140`, `vcruntime140*`
  beside the interpreter): the wheels need it and a bare Windows install may not
  have it.

---

## The SHLaunchPY3 service

The installer registers **SHLaunchPY3** and starts it, so out of the box the
*service* owns the back end: cameras keep recording with the app closed and with
nobody logged in, and the back end is restarted automatically if it dies. Without
it, the front end starts the back end and closing the app stops recording.

Turn it on or off in **Tools → Options → General → "Run the back end as a Windows
service"**. That checkbox and "Run Sighthound Video at system startup" are
disabled — with a hint saying why — when the service is not installed, which is
the case for a copy run from a source checkout. Changes take effect the next time
the back end starts.

**Manage it** from an elevated prompt, in the install directory:

```bash
python\SighthoundPy3c.exe -m launch.InstallService status
```

Verbs: `install` · `remove` · `start` · `stop` · `restart` · `status` ·
`restart-backend` · `stop-backend` · `start-backend`.

**How it works.** `launch/SHLaunchService.py` is a pywin32 service that supervises
the back end as a child process, restarting it with an escalating backoff
(5 s → 5 min) after an unexpected exit and stopping it cleanly (through the app's
own `--quit` path) when the service stops. `launch/Launch.py` keeps the API the
app already used but talks to the Service Control Manager and a JSON state file
(`shlaunch.state` in the data dir) instead of the original project's native
`launch.dll`. Settings live in `shlaunch.cfg` (`autostart`, `backend`), written by
the Options checkboxes.

The service is named **SHLaunchPY3** specifically so it cannot collide with a
Python 2 `shlaunch` install on the same machine. The original native service
(`launch/shlaunchWin`) is left in the tree for reference and is unused.

Notes:

- Installing, starting or stopping the service needs **administrator** rights. The
  installer grants your account start/stop/signal rights on the service so the app
  itself can restart the back end without elevation.
- Starting the service **restarts the back end**, which briefly drops every camera.
- The service writes `logs\SHLaunchPY3.log` in the data directory. Its
  `service starting (installDir=… pythonExe=… dataDir=…)` line is the fastest way
  to confirm which copy the service is actually running.

---

## The data directory, and the LocalSystem trap

The service runs as **LocalSystem**, because anything else means prompting for an
account password during setup. A LocalSystem service gets `%LOCALAPPDATA%` =
`C:\Windows\System32\config\systemprofile\…`, so a back end left to work out its
own paths would quietly build a *second*, empty set of databases, enrollments, AI
settings and live-view files while the Options dialog wrote to yours. **This is
the defect that would make the whole service feature look broken**, and it is
invisible until the service actually owns the back end.

So paths are not left to be worked out. `appCommon/InstallPaths.py:getUserDataDir()`
is the single answer for every process, resolved in this order:

1. `%SV_DATA_DIR%` — exported by the service and by the front end
2. `datadir.txt` in the install directory — written by the installer, which is how
   `/DATADIR=` takes effect
3. `%LOCALAPPDATA%\Sighthound Video Py3`
4. `~\AppData\Local\…`

**Back-end modules must not build this path from `expanduser("~")`.** Several once
did (`ImageCheckConfig`, `IHostConfig`, `FaceEnrollment`, `EnrollFaces`,
`TtsManager`, and the two crash-log paths) and were converted. They resolve their
paths at *import* time, which is why `FrontEndLaunchpad` exports `SV_DATA_DIR`
**before importing the back end** — doing it inside `BackEndApp.main` is already
too late.

`getUserLocalDataDir()` falls back to the directory recorded on the service key
(`Launch.getServiceDataDir()`) when the service is installed but stopped, so the
front end can still find its own databases.

---

## Updating an installed copy

**Whatever is installed is what runs.** When the service owns the back end, the
back end executes from `C:\Program Files\Sighthound Video Py3` — *not* from your
checkout. Editing source and restarting changes nothing until the files are copied
across. Check `logs\SHLaunchPY3.log` (`installDir=…`) before concluding a fix
didn't work.

Two ways to update:

- **Rebuild and reinstall.** `build_installer.ps1 -AppOnly` re-stages just the app
  sources and the launcher (a couple of minutes), then run the resulting setup.
  This is the supported path and the one to use for anything that ships.
- **Copy the changed files across** (needs elevation). Files under `backEnd/` take
  effect when that process respawns; files under `launch/` need the **service**
  restarted; static files under `backEnd/webroot/` need only a browser hard
  refresh (they are served with `max-age=60`).

`build\stage\payload` is installer staging — byte-identical to the checkout apart
from whatever was just edited. It is not what runs either, and editing it is
pointless: `make_payload.py` regenerates it.

---

## Verification status

**Verified on this machine:** the installer builds and installs; the service
registers, starts, supervises the back end and stops it cleanly (see
`SHLaunchPY3.log`); the private interpreter imports the whole
native stack under its renamed executable; version resources read "Sighthound
Py3"; uninstall removes the service and leaves the data directory when told to.
