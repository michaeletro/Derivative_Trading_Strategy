[CmdletBinding()]
param(
    [switch]$ValidateOnly,
    [switch]$FunctionsOnly
)

# This Windows-side process keeps the WSL invocation alive. It never receives a
# dashboard token or an IBKR credential; the Linux controller loads its profile.
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Get-DerivativeDailyRuntime {
    # Codex's packaged children can virtualize LocalAppData. Task Scheduler runs
    # outside that package, so keep its private files in a real per-user directory.
    $root = Join-Path $env:USERPROFILE '.derivative-lab\daily-collection'
    [pscustomobject]@{
        Root = $root
        Wsl = Join-Path $env:SystemRoot 'System32\wsl.exe'
        Repository = '/home/michperez/projects/Derivative_Trading_Strategy'
        Python = '/home/michperez/projects/Derivative_Trading_Strategy/.venv/bin/python'
        Controller = '/home/michperez/projects/Derivative_Trading_Strategy/tools/daily_collection.py'
        Profile = 'paper-tws'
        WindowsPython = Join-Path $env:USERPROFILE '.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe'
        PreflightHelper = '\\wsl.localhost\Ubuntu\home\michperez\projects\Derivative_Trading_Strategy\tools\check_tws_daily.py'
        Sdk = '\\wsl.localhost\Ubuntu\home\michperez\.local\share\ibkr-api-10.45.01\IBJts\source\pythonclient'
        PreflightDependencies = Join-Path $env:USERPROFILE '.derivative-lab\ibkr-preflight-runtime'
    }
}

function Initialize-DerivativePrivateDirectory {
    param([Parameter(Mandatory)][string]$Path)
    if (Test-Path -LiteralPath $Path) {
        $item = Get-Item -LiteralPath $Path -Force
        if (-not $item.PSIsContainer -or ($item.Attributes -band [IO.FileAttributes]::ReparsePoint)) {
            throw 'The collection runtime directory must be a regular local directory.'
        }
    } else {
        [void][IO.Directory]::CreateDirectory($Path)
    }
    $sid = [Security.Principal.WindowsIdentity]::GetCurrent().User
    $directory = [IO.DirectoryInfo]::new($Path)
    $owner = $directory.GetAccessControl([Security.AccessControl.AccessControlSections]::Owner).GetOwner([Security.Principal.SecurityIdentifier])
    if ($owner.Value -ne $sid.Value) {
        throw 'The collection runtime directory must belong to the current Windows user.'
    }
    # Work with only the discretionary ACL. Reading/reapplying an audit ACL can
    # require SeSecurityPrivilege, which an ordinary interactive task does not have.
    $acl = $directory.GetAccessControl([Security.AccessControl.AccessControlSections]::Access)
    $acl.SetAccessRuleProtection($true, $false)
    foreach ($rule in @($acl.GetAccessRules($true, $false, [Security.Principal.SecurityIdentifier]))) {
        [void]$acl.RemoveAccessRuleAll($rule)
    }
    $inheritance = [Security.AccessControl.InheritanceFlags]'ContainerInherit, ObjectInherit'
    $propagation = [Security.AccessControl.PropagationFlags]::None
    foreach ($identity in @($sid, [Security.Principal.SecurityIdentifier]::new('S-1-5-18'),
                             [Security.Principal.SecurityIdentifier]::new('S-1-5-32-544'))) {
        $acl.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new(
            $identity, [Security.AccessControl.FileSystemRights]::FullControl,
            $inheritance, $propagation, [Security.AccessControl.AccessControlType]::Allow))
    }
    $directory.SetAccessControl($acl)
}

function Test-DerivativeWindow {
    param([Parameter(Mandatory)]$Snapshot)
    if ($null -eq $Snapshot -or $null -eq $Snapshot.PSObject.Properties['in_runtime_window'] -or
        $Snapshot.in_runtime_window -isnot [bool]) {
        throw 'The collection controller returned an invalid calendar response.'
    }
    if ($Snapshot.in_runtime_window) {
        if ($null -eq $Snapshot.PSObject.Properties['wsl_source_ip'] -or
            -not (Test-DerivativeSourceAddress $Snapshot.wsl_source_ip)) {
            throw 'The calendar response did not identify a private WSL source address.'
        }
        foreach ($field in @('collection_open_ms', 'collection_close_ms', 'market_open_ms', 'market_close_ms')) {
            if ($null -eq $Snapshot.PSObject.Properties[$field] -or
                $Snapshot.$field -isnot [ValueType] -or $Snapshot.$field -is [bool] -or
                [double]$Snapshot.$field -le 0 -or
                [double]$Snapshot.$field -ne [math]::Truncate([double]$Snapshot.$field)) {
                throw 'The collection controller returned invalid session times.'
            }
        }
        if ($Snapshot.collection_close_ms -le $Snapshot.collection_open_ms -or
            $Snapshot.market_close_ms -le $Snapshot.market_open_ms -or
            $Snapshot.collection_open_ms -gt $Snapshot.market_open_ms -or
            $Snapshot.collection_close_ms -lt $Snapshot.market_close_ms) {
            throw 'The collection controller returned an invalid session interval.'
        }
    }
    return $Snapshot.in_runtime_window
}

function Get-DerivativeControllerArguments {
    param([Parameter(Mandatory)][ValidateSet('window', 'run', 'record-preflight')][string]$Command)
    $runtime = Get-DerivativeDailyRuntime
    # Every argument is fixed, whitespace-free and free of private profile data.
    return "--distribution Ubuntu --user michperez --exec $($runtime.Python) $($runtime.Controller) --profile $($runtime.Profile) $Command"
}

function Test-DerivativeSourceAddress {
    param($Value)
    if ($Value -isnot [string]) { return $false }
    $address = $null
    if (-not [Net.IPAddress]::TryParse($Value, [ref]$address) -or
        $address.AddressFamily -ne [Net.Sockets.AddressFamily]::InterNetwork -or
        $address.ToString() -cne $Value) { return $false }
    $bytes = $address.GetAddressBytes()
    return ($bytes[0] -eq 10 -or ($bytes[0] -eq 172 -and $bytes[1] -ge 16 -and $bytes[1] -le 31) -or
        ($bytes[0] -eq 192 -and $bytes[1] -eq 168))
}

function Get-DerivativePreflightArguments {
    param([Parameter(Mandatory)][string]$SourceAddress)
    if (-not (Test-DerivativeSourceAddress $SourceAddress)) { throw 'A private WSL source address is required.' }
    $runtime = Get-DerivativeDailyRuntime
    return '-X utf8 "' + $runtime.PreflightHelper + '" --source-ip ' + $SourceAddress +
        ' --sdk-path "' + $runtime.Sdk + '" --dependency-path "' + $runtime.PreflightDependencies + '"'
}

function New-DerivativeFailedPreflight {
    param([string]$SourceAddress, [string]$Reason)
    return [pscustomobject]@{connected=$false; read_only_api=$false; paper_identity=$false;
        source_ip_trusted=$false; localhost_trusted=$false; auto_restart=$false;
        auto_restart_time=$null; auto_restart_period=$null; server_version=$null;
        timezone=$null; observed_at_ms=[DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds();
        ready=$false; reason=$Reason; source_ip=$SourceAddress}
}

function Test-DerivativePreflightShape {
    param($Proof, [string]$SourceAddress)
    if ($null -eq $Proof) { return $false }
    $fields = @('connected','read_only_api','paper_identity','source_ip_trusted','localhost_trusted',
        'auto_restart','auto_restart_time','auto_restart_period','server_version','timezone',
        'observed_at_ms','ready','reason','source_ip')
    $actual = @($Proof.PSObject.Properties.Name)
    if ($actual.Count -ne $fields.Count -or @($actual | Where-Object { $_ -notin $fields }).Count -ne 0) { return $false }
    foreach ($field in $fields) {
        $value = $Proof.$field
        if ($null -ne $value -and $value -isnot [bool] -and $value -isnot [int] -and
            $value -isnot [long] -and $value -isnot [string]) { return $false }
        if ($value -is [string] -and $value.Length -gt 200) { return $false }
    }
    return ($Proof.ready -is [bool] -and $Proof.source_ip -ceq $SourceAddress)
}

function Invoke-DerivativeTwsPreflight {
    param([Parameter(Mandatory)][string]$SourceAddress, [Parameter(Mandatory)][string]$RunId)
    $runtime = Get-DerivativeDailyRuntime
    $proofPath = Join-Path $runtime.Root ($RunId + '-tws-preflight.json')
    $proof = $null
    $helperOk = $false
    if ((Test-Path -LiteralPath $runtime.WindowsPython -PathType Leaf) -and
        (Test-Path -LiteralPath $runtime.PreflightHelper -PathType Leaf)) {
      try {
        $helper = Start-Process -FilePath $runtime.WindowsPython -ArgumentList (Get-DerivativePreflightArguments $SourceAddress) `
            -WindowStyle Hidden -PassThru -RedirectStandardOutput $proofPath `
            -RedirectStandardError (Join-Path $runtime.Root ($RunId + '-tws-preflight.stderr.log'))
        $null = $helper.Handle
        if (-not $helper.WaitForExit(90000)) {
            Stop-Process -Id $helper.Id -ErrorAction SilentlyContinue
            $proof = New-DerivativeFailedPreflight $SourceAddress 'tws_preflight_timeout'
        } else {
            $helper.Refresh()
            try {
                $proof = Get-Content -LiteralPath $proofPath -Raw | ConvertFrom-Json
                if (-not (Test-DerivativePreflightShape $proof $SourceAddress)) { $proof = $null }
            } catch { $proof = $null }
            if ($null -eq $proof) { $proof = New-DerivativeFailedPreflight $SourceAddress 'tws_preflight_invalid_response' }
            if ($helper.ExitCode -ne 0 -and $proof.ready) {
                $proof.ready = $false
                $proof.reason = 'tws_preflight_process_failed'
            }
            $helperOk = $helper.ExitCode -eq 0 -and $proof.ready
        }
      } catch { $proof = New-DerivativeFailedPreflight $SourceAddress 'tws_preflight_process_failed' }
    } else { $proof = New-DerivativeFailedPreflight $SourceAddress 'tws_preflight_runtime_missing' }
    # Keep failed observations too: the dashboard can explain why an unattended
    # run needs attention. Normalize only the bounded, query-only whitelist.
    [IO.File]::WriteAllText($proofPath, ($proof | ConvertTo-Json -Depth 4 -Compress), [Text.UTF8Encoding]::new($false))
    $record = Start-Process -FilePath $runtime.Wsl -ArgumentList (Get-DerivativeControllerArguments 'record-preflight') `
        -WindowStyle Hidden -PassThru -RedirectStandardInput $proofPath `
        -RedirectStandardOutput (Join-Path $runtime.Root ($RunId + '-record-preflight.stdout.log')) `
        -RedirectStandardError (Join-Path $runtime.Root ($RunId + '-record-preflight.stderr.log'))
    $null = $record.Handle
    if (-not $record.WaitForExit(120000)) {
        Stop-Process -Id $record.Id -ErrorAction SilentlyContinue
        throw 'Saving the local TWS preflight timed out; collection was not started.'
    }
    $record.Refresh()
    if (-not $helperOk -or $record.ExitCode -ne 0) {
        throw 'TWS Paper/Read-Only/trusted-IP preflight is not ready; collection was not started. See the dashboard daily-collection status and private Windows runtime logs.'
    }
}

function Get-DerivativeReportHoldTime { [DateTimeOffset]::UtcNow }
function Get-DerivativeReportHoldElapsedSeconds {
    param([Parameter(Mandatory)]$Timer)
    return $Timer.Elapsed.TotalSeconds
}
function Wait-DerivativeReportHold {
    param([Parameter(Mandatory)][int]$Milliseconds)
    Start-Sleep -Milliseconds $Milliseconds
}
function Set-DerivativeReportHoldPower {
    param([Parameter(Mandatory)][uint32]$State)
    return [DerivativeLab.NativePower]::SetThreadExecutionState($State)
}
function Get-DerivativeReportHoldContext {
    param([Parameter(Mandatory)]$Snapshot,[Parameter(Mandatory)][DateTimeOffset]$At)
    try {
        if ((Get-TimeZone).Id -cne 'Eastern Standard Time' -or $Snapshot.is_trading_day -isnot [bool] -or
            -not $Snapshot.is_trading_day -or $Snapshot.timezone -cne 'America/New_York' -or
            $Snapshot.session_date -isnot [string] -or $Snapshot.session_date -cnotmatch '^\d{4}-\d{2}-\d{2}$') { return $null }
        foreach ($name in @('collection_open_ms','collection_close_ms')) {
            $value=$Snapshot.$name
            if ($value -isnot [ValueType] -or $value -is [bool] -or [double]$value -le 0 -or
                [double]$value -ne [Math]::Truncate([double]$value)) { return $null }
        }
        $zone=[TimeZoneInfo]::FindSystemTimeZoneById('Eastern Standard Time')
        $close=[DateTimeOffset]::FromUnixTimeMilliseconds([long]$Snapshot.collection_close_ms)
        $open=[DateTimeOffset]::FromUnixTimeMilliseconds([long]$Snapshot.collection_open_ms)
        $localClose=[TimeZoneInfo]::ConvertTime($close,$zone)
        $localOpen=[TimeZoneInfo]::ConvertTime($open,$zone)
        $localNow=[TimeZoneInfo]::ConvertTime($At,$zone)
        if ($localClose.ToString('yyyy-MM-dd') -cne $Snapshot.session_date -or
            $localOpen.ToString('yyyy-MM-dd') -cne $Snapshot.session_date -or
            $localNow.ToString('yyyy-MM-dd') -cne $Snapshot.session_date -or
            $localClose.TimeOfDay -ne [TimeSpan]::FromMinutes(16*60+30) -or
            $localOpen.TimeOfDay -ne [TimeSpan]::FromHours(9) -or
            $At -lt $close -or $At -ge $close.AddHours(1)) { return $null }
        return [pscustomobject]@{SessionDate=$Snapshot.session_date;Close=$close;End=$close.AddHours(1)}
    } catch { return $null }
}
function Write-DerivativeReportHoldStatus {
    param([Parameter(Mandatory)]$Runtime,[Parameter(Mandatory)][string]$RunId,
          [Parameter(Mandatory)]$Context,[Parameter(Mandatory)][string]$State,
          [Parameter(Mandatory)][string]$Reason)
    # One bounded private record per existing wrapper launch; no child output,
    # broker identity, credentials, or exception details enter this record.
    if ($RunId -cnotmatch '^[A-Za-z0-9-]{1,80}$') { return }
    $value=[ordered]@{schema_version=1;session_date=$Context.SessionDate;phase=$State;reason=$Reason;
        updated_ms=(Get-DerivativeReportHoldTime).ToUnixTimeMilliseconds();
        stop_ms=$Context.End.ToUnixTimeMilliseconds();system_awake_required=($State -eq 'holding');
        display_awake_required=($State -eq 'holding')}
    [IO.File]::WriteAllText((Join-Path $Runtime.Root ($RunId+'-report-awake.json')),
        ($value|ConvertTo-Json -Compress),[Text.UTF8Encoding]::new($false))
}
function Invoke-DerivativeReportAwakeHold {
    param([Parameter(Mandatory)]$Snapshot,[Parameter(Mandatory)]$Runtime,
          [Parameter(Mandatory)][string]$RunId)
    $start=Get-DerivativeReportHoldTime
    $context=Get-DerivativeReportHoldContext $Snapshot $start
    if ($null -eq $context) { return }
    $budget=[Math]::Min(3600,($context.End-$start).TotalSeconds)
    $timer=[Diagnostics.Stopwatch]::StartNew()
    $held=$false;$phase='failed';$reason='postclose_power_request_failed'
    try {
        # The detached report can wait until 17:00 and analyze for 20 minutes.
        # On this Modern Standby host, screen-off can suspend WSL. Hold both
        # system and display only during the bounded post-close report window.
        if ((Set-DerivativeReportHoldPower ([uint32]2147483651)) -eq 0) { return }
        $held=$true
        try { Write-DerivativeReportHoldStatus $Runtime $RunId $context 'holding' 'postclose_report_window' } catch { }
        while ($true) {
            $now=Get-DerivativeReportHoldTime
            if ($null -eq (Get-DerivativeReportHoldContext $Snapshot $now)) {
                $reason='postclose_wall_boundary';break
            }
            $elapsed=Get-DerivativeReportHoldElapsedSeconds $timer
            if ($elapsed -ge $budget) { $reason='postclose_steady_deadline';break }
            $remaining=[Math]::Min($budget-$elapsed,($context.End-$now).TotalSeconds)
            Wait-DerivativeReportHold ([int][Math]::Max(1,[Math]::Min(15000,$remaining*1000)))
        }
        $phase='stopped'
    } catch { $reason='postclose_report_hold_failed' }
    finally {
        if ($held) { try { [void](Set-DerivativeReportHoldPower ([uint32]2147483648)) } catch { } }
        $timer.Stop()
        try { Write-DerivativeReportHoldStatus $Runtime $RunId $context $phase $reason } catch { }
    }
}

function Invoke-DerivativeDailyCollection {
    param([switch]$ValidateOnly)
    if ((Get-TimeZone).Id -ne 'Eastern Standard Time') {
        throw 'Daily collection requires the Windows time zone to be Eastern Standard Time.'
    }
    $runtime = Get-DerivativeDailyRuntime
    if (-not (Test-Path -LiteralPath $runtime.Wsl -PathType Leaf)) {
        throw 'The existing Windows WSL installation was not found.'
    }
    Initialize-DerivativePrivateDirectory -Path $runtime.Root
    $runId = [DateTime]::UtcNow.ToString('yyyyMMddTHHmmssfffffffZ') + '-' + $PID
    $calendarOut = Join-Path $runtime.Root ($runId + '-window.json')
    $calendarError = Join-Path $runtime.Root ($runId + '-window.stderr.log')
    $query = Start-Process -FilePath $runtime.Wsl -ArgumentList (Get-DerivativeControllerArguments 'window') `
        -WindowStyle Hidden -PassThru -RedirectStandardOutput $calendarOut -RedirectStandardError $calendarError
    # Windows PowerShell's returned Process can otherwise lose its exit status
    # when a fast child finishes before the first native-handle access.
    $null = $query.Handle
    if (-not $query.WaitForExit(120000)) {
        Stop-Process -Id $query.Id -ErrorAction SilentlyContinue
        throw 'The local collection calendar query timed out; collection was not started.'
    }
    $query.Refresh()
    if ($query.ExitCode -ne 0) {
        throw 'The local collection calendar query failed; collection was not started. See the private Windows runtime logs.'
    }
    try {
        $snapshot = Get-Content -LiteralPath $calendarOut -Raw | ConvertFrom-Json
        $inside = Test-DerivativeWindow $snapshot
    } catch {
        throw 'The local collection calendar response was invalid; collection was not started.'
    }
    if ($ValidateOnly) {
        $snapshot | ConvertTo-Json -Depth 8 -Compress
        return 0
    }
    if (-not $inside) {
        Write-Output 'Outside the exchange collection window; no connection or capture was started.'
        return 0
    }
    if (-not ('DerivativeLab.NativePower' -as [type])) {
        Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
namespace DerivativeLab {
    public static class NativePower {
        [DllImport("kernel32.dll", SetLastError = true)]
        public static extern uint SetThreadExecutionState(uint state);
    }
}
'@
    }
    $awake = $false
    try {
        # ES_CONTINUOUS | ES_SYSTEM_REQUIRED: keep the system awake, not its display.
        if ([DerivativeLab.NativePower]::SetThreadExecutionState([uint32]2147483649) -eq 0) {
            throw 'Windows could not keep this collection session awake; collection was not started.'
        }
        $awake = $true
        Invoke-DerivativeTwsPreflight -SourceAddress $snapshot.wsl_source_ip -RunId $runId
        $controller = Start-Process -FilePath $runtime.Wsl -ArgumentList (Get-DerivativeControllerArguments 'run') `
            -WindowStyle Hidden -PassThru `
            -RedirectStandardOutput (Join-Path $runtime.Root ($runId + '-controller.stdout.log')) `
            -RedirectStandardError (Join-Path $runtime.Root ($runId + '-controller.stderr.log'))
        $null = $controller.Handle
        # Keep this Windows process and the foreground WSL invocation present until
        # the controller finishes its configured 16:30 Eastern close and drain.
        # The controller owns the
        # capture stop and its private lock, never this wrapper.
        $controller.WaitForExit()
        $controller.Refresh()
        $controllerCode=[int]$controller.ExitCode
        if ($controllerCode -eq 0) {
            Invoke-DerivativeReportAwakeHold -Snapshot $snapshot -Runtime $runtime -RunId $runId
        }
        return $controllerCode
    } finally {
        if ($awake) {
            [void][DerivativeLab.NativePower]::SetThreadExecutionState([uint32]2147483648)
        }
    }
}

if (-not $FunctionsOnly) {
    try {
        $result = Invoke-DerivativeDailyCollection -ValidateOnly:$ValidateOnly
        # ValidateOnly writes a JSON response as well as the numeric return value.
        $code = 0
        foreach ($line in @($result)) {
            if ($line -is [int]) { $code = $line } else { Write-Output $line }
        }
        exit $code
    } catch {
        Write-Error $_.Exception.Message -ErrorAction Continue
        exit 2
    }
}
