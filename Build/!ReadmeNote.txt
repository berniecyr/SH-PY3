Rebuild any time with powershell -ExecutionPolicy Bypass -File build\build_installer.ps1 — -AppOnly after code edits takes ~3 min. This build used -Fast compression; dropping that shrinks it further at the cost of a much longer compile.

The end user gets: double-click, click through, done. Private CPython 3.12.10, every dependency including CUDA torch, the AI models, Start-menu + desktop shortcuts, the SHLaunchPY3 service registered and started, and a firewall rule for the LAN viewer. No Python, no pip, no internet, no console windows. /VERYSILENT for unattended rollout; /DATADIR= and /SERVICEUSER= where needed.


test install command line: (otherwise just click and install)
S:\GIT\2026-07-28-v3\build\out\SighthoundVideoPy3-Setup-8.0.01.exe /DIR=S:\GIT\_svinstalltest /DATADIR=S:\GIT\_svinstalltest-data /MERGETASKS=!desktopicon,!service /LOG=S:\GIT\install.log


