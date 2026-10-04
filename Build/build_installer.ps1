<#
.SYNOPSIS
    Build SighthoundVideoPy3-Setup.exe end to end.

.DESCRIPTION
    Two steps, either of which can be run on its own:

      1. make_payload.py stages the install image in build\stage\payload --
         a private CPython 3.12.10 with the whole pinned stack, the AI models,
         Sighthound-named executables and the app sources.
      2. Inno Setup compiles that image into build\out\.

    The first run downloads ~6 GB of wheels and takes a while; after that,
    -SkipPayload (or -AppOnly) turns a rebuild into a couple of minutes.

    Nothing here needs to run on the target machine: the output is a single
    installer that asks the user for nothing.

.PARAMETER SkipPayload
    Compile the installer from the payload already staged.

.PARAMETER AppOnly
    Re-stage only the app sources and launcher (keeps the Python runtime), then
    compile. This is the loop to use after editing code.

.PARAMETER Fast
    Compress with lzma2/fast instead of lzma2/max: a much quicker build, a
    noticeably bigger installer. For testing, not for shipping.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File build\build_installer.ps1
#>
[CmdletBinding()]
param(
    [switch] $SkipPayload,
    [switch] $AppOnly,
    [switch] $Fast,
    [string] $BasePython,
    [string] $Iscc
)

$ErrorActionPreference = 'Stop'

$buildDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$repoRoot = Split-Path -Parent $buildDir
$payloadDir = Join-Path $buildDir 'stage\payload'
$outDir = Join-Path $buildDir 'out'

function Write-Step([string] $message) {
    Write-Host ""
    Write-Host "=== $message" -ForegroundColor Cyan
}

# --- 1. the payload ---------------------------------------------------------

function Resolve-BuildPython {
    if ($BasePython) {
        $exe = Join-Path $BasePython 'python.exe'
        if (Test-Path $exe) { return $exe }
    }
    # Any 3.12 will do to RUN make_payload.py; the interpreter it copies into
    # the payload is resolved (and version-checked) inside that script.
    $candidates = @(
        'C:\Program Files\Python312\python.exe',
        "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe"
    )
    foreach ($candidate in $candidates) {
        if (Test-Path $candidate) { return $candidate }
    }
    $fromLauncher = (& py -3.12 -c "import sys; print(sys.executable)" 2>$null)
    if ($LASTEXITCODE -eq 0 -and $fromLauncher) { return $fromLauncher.Trim() }
    throw "No Python 3.12 found to build with. Pass -BasePython <dir>."
}

if (-not $SkipPayload) {
    $python = Resolve-BuildPython
    $stage = if ($AppOnly) { 'app' } else { 'all' }
    Write-Step "Staging payload ($stage) with $python"

    $payloadArgs = @((Join-Path $buildDir 'make_payload.py'), '--stage', $stage)
    if ($BasePython) { $payloadArgs += @('--base-python', $BasePython) }

    & $python @payloadArgs
    if ($LASTEXITCODE -ne 0) { throw "make_payload.py failed ($LASTEXITCODE)" }
}

if (-not (Test-Path (Join-Path $payloadDir 'FrontEndLaunchpad.py'))) {
    throw "No staged payload in $payloadDir -- run without -SkipPayload first."
}

# --- 2. the installer -------------------------------------------------------

function Resolve-Iscc {
    if ($Iscc) { return $Iscc }
    $candidates = @(
        'C:\Program Files\Inno Setup 7\ISCC.exe',
        'C:\Program Files (x86)\Inno Setup 7\ISCC.exe',
        'C:\Program Files (x86)\Inno Setup 6\ISCC.exe'
    )
    foreach ($candidate in $candidates) {
        if (Test-Path $candidate) { return $candidate }
    }
    $onPath = (Get-Command iscc -ErrorAction SilentlyContinue)
    if ($onPath) { return $onPath.Source }
    throw "Inno Setup's ISCC.exe not found. Install Inno Setup or pass -Iscc."
}

# Version comes from the app's own constant, so the installer, the file version
# resources and the About box cannot disagree.
$versionLine = Select-String -Path (Join-Path $repoRoot 'appCommon\CommonStrings.py') `
                             -Pattern '^kVersionString\s*=\s*"([^"]+)"' | Select-Object -First 1
$appVersion = if ($versionLine) { $versionLine.Matches[0].Groups[1].Value } else { '0.0.0' }

# Inno needs a four-part version for the resource. Keep the constant's zero
# padding (2026.08.01 -> 2026.08.01.0) so the string Windows shows matches the
# version used everywhere else; the numeric fields drop it either way. Same rule
# as make_payload.appVersion().
$verParts = @([regex]::Matches($appVersion, '\d+') | ForEach-Object { $_.Value })
$verParts = @($verParts | Select-Object -First 4)
while ($verParts.Count -lt 4) { $verParts += '0' }
$appVersionFour = $verParts -join '.'

$payloadMb = [math]::Round((Get-ChildItem $payloadDir -Recurse -File |
                            Measure-Object -Property Length -Sum).Sum / 1MB, 0)
Write-Step "Compiling installer for $appVersion (payload $payloadMb MB)"

$iscc = Resolve-Iscc
New-Item -ItemType Directory -Force -Path $outDir | Out-Null

$isccArgs = @("/DAppVersion=$appVersion", "/DAppVersionFour=$appVersionFour")
if ($Fast) { $isccArgs += '/DFastCompress' }

# A single setup.exe cannot exceed ~2.1 GB. This payload compresses to roughly
# half its staged size, so anything past ~4 GB staged is going to need disk
# spanning -- ask for it up front rather than discovering it at the END of a
# 20-minute compression pass. The retry below stays as the backstop.
$spanned = $payloadMb -gt 4096
if ($spanned) {
    Write-Host "Payload is large; compiling with disk spanning." -ForegroundColor Yellow
    $isccArgs += '/DSpanned'
}
$isccArgs += (Join-Path $buildDir 'SighthoundVideoPy3.iss')

& $iscc @isccArgs
if ($LASTEXITCODE -ne 0 -and -not $spanned) {
    # The usual cause is that 2.1 GB ceiling. Re-run with disk spanning, which
    # puts the data in .bin slices beside the installer.
    Write-Step "Single-file compile failed; retrying with disk spanning"
    & $iscc @($isccArgs[0..($isccArgs.Length - 2)] + '/DSpanned' + $isccArgs[-1])
    if ($LASTEXITCODE -ne 0) { throw "ISCC failed ($LASTEXITCODE)" }
} elseif ($LASTEXITCODE -ne 0) {
    throw "ISCC failed ($LASTEXITCODE)"
}

Write-Step "Done"
Get-ChildItem $outDir | Sort-Object Name |
    Select-Object Name, @{n = 'MB'; e = { [math]::Round($_.Length / 1MB, 1) } } |
    Format-Table -AutoSize
