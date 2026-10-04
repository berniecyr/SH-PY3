# Building the Sighthound Video Py3 installer

One command, from the repo root, on a machine with **Python 3.12.10** and
**Inno Setup 6/7** installed:

```bash
powershell -ExecutionPolicy Bypass -File build\build_installer.ps1
```

Output: `build\out\SighthoundVideoPy3-Setup-<version>.exe` (plus `.bin` slices
when the compressed payload exceeds the ~2.1 GB a single setup.exe can hold —
they must be distributed together). The version is the build date, e.g.
`SighthoundVideoPy3-Setup-2026.08.28.exe`.

For what the installer *produces* — what ships inside it, the SHLaunchPY3
service, the data directory, and how to update an installed copy — see
[INSTALL.md](INSTALL.md).

| Switch | Effect |
|---|---|
| `-AppOnly` | Re-stage only the app sources and launcher, keeping the staged Python runtime. The loop to use after editing code. |
| `-SkipPayload` | Compile the installer from whatever is already staged. |
| `-Fast` | `lzma2/fast` instead of `lzma2/max` — much quicker, bigger output. Testing only. |
| `-BasePython <dir>` | Python 3.12.10 home to copy the private runtime from. |
| `-Iscc <path>` | Explicit path to `ISCC.exe`. |

## The pieces

| File | Role |
|---|---|
| `make_payload.py` | Stages `build\stage\payload`, which *is* the install directory: private CPython 3.12.10 + the pinned stack + AI models + renamed/stamped executables + app sources. |
| `SighthoundVideoPy3.iss` | The Inno Setup script: install, shortcuts, service registration, uninstall. |
| `build_installer.ps1` | Runs the two steps above and reports what came out. |
| `launcher.py` / `setup_launcher.py` | The ~100 KB `SighthoundVideoPy3.exe` shim the shortcuts point at, and its cx_Freeze build. |

## Things worth knowing before changing any of this

**The private Python is a copy, not a venv.** A venv still needs the base
interpreter installed on the target machine; a copied CPython tree finds its
stdlib relative to its own executable, needs no registry entries, and cannot be
disturbed by another Python on the box. `make_payload.py` copies a stock
3.12.10 install (stripped of `site-packages`, `Scripts`, `Doc`, `Tools`,
`Lib\test`), then installs the pinned stack into it — including the same three
post-`requirements.txt` corrections `Start.bat` makes (headless-only OpenCV,
`onnxruntime-gpu` 1.22, torch cu126). If no matching Python is installed it
falls back to running the bundled `python-3.12.10-amd64.exe` into a scratch
directory.

**Process names come from two places.** Task Manager's Details tab shows the
image name, and the Processes tab shows the version resource's
`FileDescription`. So every executable is *copied* to a Sighthound name (the
originals stay, because pip and cx_Freeze still want them) and re-stamped via
pywin32's `win32verstamp`: `SighthoundPy3.exe`, `SighthoundPy3c.exe`,
`SighthoundPy3Service.exe`, `SighthoundPy3-ffmpeg.exe`. The stamping runs in the
*staged* interpreter through `python.exe` — `BeginUpdateResource` cannot write
to a running executable.

**pywin32's DLLs are placed app-locally**, next to the service host, instead of
running `pywin32_postinstall.py -install` (which copies them into System32).
The service otherwise fails to start with a bare "Error 1053" — but an
installer that writes to System32 breaks any other pywin32 on the machine.

**Models are bundled** from this build machine: `~\.insightface\models\buffalo_l`
and `%LOCALAPPDATA%\Sighthound Video Py3\TtsModels`. If they are missing the
build still succeeds, with a warning, and the installed app downloads them on
first use — which is exactly what we are trying to avoid on a machine that may
have no internet, and under a service account whose home is not the user's.

**`--stage runtime` needs internet and ~7 GB of disk**, and takes 10–20 minutes
mostly waiting on the 2.6 GB torch wheel. `--stage app` needs neither.
