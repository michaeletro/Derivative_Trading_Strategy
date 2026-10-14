[CmdletBinding()]
param([switch]$FunctionsOnly)

# A scoped wake window for TWS's existing restart timer. This script never
# launches or controls TWS, opens an API connection, or changes a power policy.
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Get-DerivativeNightlyRuntime {
    $root = Join-Path $env:USERPROFILE '.derivative-lab\daily-collection'
    [pscustomobject]@{ Root=$root; SharedWrapper=(Join-Path $root 'start_daily_collection.ps1') }
}

function Get-DerivativeNightlyTime { [DateTime]::Now }
function Get-DerivativeNightlyElapsedSeconds {
    param([Parameter(Mandatory)]$Timer)
    return $Timer.Elapsed.TotalSeconds
}

function Test-DerivativeNightlyWindow {
    param([Parameter(Mandatory)][DateTime]$At)
    return $At.TimeOfDay -ge [TimeSpan]::FromMinutes(23*60+40) -and
           $At.TimeOfDay -lt [TimeSpan]::FromMinutes(23*60+55)
}

function Initialize-DerivativeNightlyDirectory {
    param([Parameter(Mandatory)]$Runtime)
    $wrapper = Get-Item -LiteralPath $Runtime.SharedWrapper -Force -ErrorAction Stop
    if ($wrapper.PSIsContainer -or ($wrapper.Attributes -band [IO.FileAttributes]::ReparsePoint)) {
        throw 'The installed collection wrapper must be a regular local file.'
    }
    . $Runtime.SharedWrapper -FunctionsOnly
    Initialize-DerivativePrivateDirectory -Path $Runtime.Root
}

function Invoke-DerivativeNightlyWake {
    if ((Get-TimeZone).Id -ne 'Eastern Standard Time') {
        throw 'The nightly wake window requires the Windows Eastern time zone.'
    }
    $start = Get-DerivativeNightlyTime
    if (-not (Test-DerivativeNightlyWindow $start)) { return 0 }
    $runtime = Get-DerivativeNightlyRuntime
    Initialize-DerivativeNightlyDirectory $runtime
    if (-not ('DerivativeLab.NightlyPower' -as [type])) {
        Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
namespace DerivativeLab {
    public static class NightlyPower {
        [DllImport("kernel32.dll", SetLastError = true)]
        public static extern uint SetThreadExecutionState(uint state);
    }
}
'@
    }
    $held = $false
    $elapsed = [Diagnostics.Stopwatch]::StartNew()
    try {
        # ES_CONTINUOUS | ES_SYSTEM_REQUIRED. The display may still sleep.
        if ([DerivativeLab.NightlyPower]::SetThreadExecutionState([uint32]2147483649) -eq 0) {
            throw 'Windows could not hold the nightly restart wake window.'
        }
        $held = $true
        while ((Get-DerivativeNightlyElapsedSeconds $elapsed) -lt 900) {
            $now = Get-DerivativeNightlyTime
            if (-not (Test-DerivativeNightlyWindow $now)) { break }
            $untilClose = ($now.Date.AddHours(23).AddMinutes(55) - $now).TotalMilliseconds
            $remaining = [Math]::Min($untilClose, 900000-1000*(Get-DerivativeNightlyElapsedSeconds $elapsed))
            Start-Sleep -Milliseconds ([int][Math]::Max(1,[Math]::Min(15000,$remaining)))
        }
        return 0
    } finally {
        if ($held) {
            [void][DerivativeLab.NightlyPower]::SetThreadExecutionState([uint32]2147483648)
        }
        $elapsed.Stop()
    }
}

if (-not $FunctionsOnly) {
    try { exit (Invoke-DerivativeNightlyWake) } catch {
        Write-Error $_.Exception.Message -ErrorAction Continue
        exit 2
    }
}
