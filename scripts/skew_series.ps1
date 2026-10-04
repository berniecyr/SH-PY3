# skew_series.ps1 -- take the same skew measurement at several times of day.
#
# WHY THIS EXISTS
# The analysis-to-recording offset has to be shown to hold STILL before anyone
# calibrates SV_VIDEO_LATENCY_MS against it.  One morning's reading cannot show
# that: load, light and camera activity all change through a day, and those are
# exactly the things the offset was suspected of tracking.  So take the same
# measurement, the same way, at four points across a full cycle and compare.
#
# Each run appends to its own timestamped file plus a one-line-per-camera
# summary that makes the four directly comparable.
#
#   powershell -ExecutionPolicy Bypass -File scripts\skew_series.ps1
#
# Runs detached and survives this shell closing.  Stop it by killing the
# powershell process, or delete nothing -- it exits on its own after the last
# measurement.

param(
    [string]$OutDir = "$env:LOCALAPPDATA\Sighthound Video Py3\logs\skew",
    [int]$Samples = 12
)

$root = Split-Path -Parent $PSScriptRoot
$py = Join-Path $root 'venv\Scripts\python.exe'
$script = Join-Path $root 'scripts\measure_analysis_skew.py'
New-Item -ItemType Directory -Force -Path $OutDir | Out-Null

# When to measure.  The first is immediate; the rest are the next occurrence of
# each clock time, so this works whatever time of day it is started.
$targets = @(
    @{ Label = 'morning';   At = (Get-Date) },
    @{ Label = 'afternoon'; At = $null; Hour = 14; Minute = 30 },
    @{ Label = 'night';     At = $null; Hour = 21; Minute = 0 },
    @{ Label = 'nextmorn';  At = $null; Hour = 6;  Minute = 30 }
)

function Next-Occurrence([int]$h, [int]$m, [datetime]$after) {
    $t = (Get-Date -Hour $h -Minute $m -Second 0)
    while ($t -le $after) { $t = $t.AddDays(1) }
    return $t
}

$prev = Get-Date
foreach ($t in $targets) {
    if (-not $t.At) { $t.At = Next-Occurrence $t.Hour $t.Minute $prev }
    $prev = $t.At
}

$summary = Join-Path $OutDir 'series-summary.txt'
"=== skew series started $(Get-Date -Format 'yyyy-MM-dd HH:mm') ===" |
    Out-File -Append -Encoding utf8 $summary
foreach ($t in $targets) {
    ("  {0,-10} scheduled {1:yyyy-MM-dd HH:mm}" -f $t.Label, $t.At) |
        Out-File -Append -Encoding utf8 $summary
}

foreach ($t in $targets) {
    $wait = $t.At - (Get-Date)
    if ($wait.TotalSeconds -gt 0) { Start-Sleep -Seconds ([int]$wait.TotalSeconds) }

    $stamp = Get-Date -Format 'yyyy-MM-dd-HHmm'
    $day = Get-Date -Format 'yyyy-MM-dd'
    $out = Join-Path $OutDir "skew-$($t.Label)-$stamp.txt"

    & $py -u $script --day $day --samples $Samples 2>&1 |
        Out-File -Encoding utf8 $out

    # One comparable line per camera, so four runs can be read side by side.
    "" | Out-File -Append -Encoding utf8 $summary
    "--- $($t.Label)  $(Get-Date -Format 'yyyy-MM-dd HH:mm') ---" |
        Out-File -Append -Encoding utf8 $summary
    Select-String -Path $out -Pattern 'median|NO USABLE' |
        ForEach-Object { "  " + $_.Line.Trim() } |
        Out-File -Append -Encoding utf8 $summary
}

"" | Out-File -Append -Encoding utf8 $summary
"=== series complete $(Get-Date -Format 'yyyy-MM-dd HH:mm') ===" |
    Out-File -Append -Encoding utf8 $summary
