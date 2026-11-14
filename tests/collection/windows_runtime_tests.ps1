# Run in Windows PowerShell. These checks never register a task or contact TWS.
[CmdletBinding()]
param()
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$repo = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\..'))
. (Join-Path $repo 'tools\start_daily_collection.ps1') -FunctionsOnly
. (Join-Path $repo 'tools\install_daily_collection_task.ps1') -FunctionsOnly
$script:checks = 0
function Assert-True([bool]$Condition, [string]$Message) {
    if (-not $Condition) { throw $Message }
    $script:checks++
}
function Assert-Throws([scriptblock]$Action, [string]$Message) {
    $threw = $false
    try { & $Action | Out-Null } catch { $threw = $true }
    Assert-True $threw $Message
}

$valid = [pscustomobject]@{in_runtime_window=$true; market_open_ms=1791984600000L; market_close_ms=1792008000000L;
    collection_open_ms=1791982800000L; collection_close_ms=1792009800000L; wsl_source_ip='172.26.122.234'}
Assert-True (Test-DerivativeWindow $valid) 'A valid active exchange window was rejected.'
Assert-True (-not (Test-DerivativeWindow ([pscustomobject]@{in_runtime_window=$false}))) 'A closed exchange window was accepted.'
Assert-Throws { Test-DerivativeWindow ([pscustomobject]@{in_runtime_window='true'}) } 'A string was accepted as the active-window flag.'
$invalid = $valid.PSObject.Copy(); $invalid.collection_open_ms = $invalid.collection_close_ms + 1
Assert-Throws { Test-DerivativeWindow $invalid } 'An inverted collection window was accepted.'
$invalid = $valid.PSObject.Copy(); $invalid.collection_open_ms += 0.5
Assert-Throws { Test-DerivativeWindow $invalid } 'A fractional collection timestamp was accepted.'
$invalid = $valid.PSObject.Copy(); $invalid.collection_open_ms = $true
Assert-Throws { Test-DerivativeWindow $invalid } 'A boolean collection timestamp was accepted.'
Assert-Throws { Test-DerivativeWindow ([pscustomobject]@{in_runtime_window=$true}) } 'Missing session times were accepted.'

$spec = Get-DerivativeDailyTaskSpec
Assert-True ($spec.Root -ceq (Join-Path $env:USERPROFILE '.derivative-lab\daily-collection')) 'The task runtime is in virtualized application data.'
$weeklyTrigger = New-DerivativeDailyWeeklyTrigger
Assert-True ($weeklyTrigger.StartBoundary -match '^\d{4}-\d{2}-\d{2}T08:50:00$') 'The weekly trigger freezes a UTC offset instead of following Eastern DST.'
Assert-True ($spec.RestartCount -eq 999 -and $spec.RestartIntervalMinutes -eq 5) 'An unattended preflight failure cannot retry after TWS login.'
Assert-True (Test-DerivativeSourceAddress '172.26.122.234') 'A private WSL source address was rejected.'
Assert-True (-not (Test-DerivativeSourceAddress '8.8.8.8')) 'A public address was accepted for TWS trust.'
Assert-True (-not (Test-DerivativeSourceAddress '127.0.0.1')) 'Loopback was accepted as the WSL source.'
Assert-True (-not (Test-DerivativeSourceAddress '172.26.122.234 --extra')) 'Unvalidated data was accepted as a process argument.'
$owned = [pscustomobject]@{TaskName=$spec.Name; TaskPath=$spec.Path; Description=$spec.Marker;
    Principal=[pscustomobject]@{UserId=$spec.User;LogonType=3;RunLevel=0};
    Actions=@([pscustomobject]@{Execute=$spec.Execute; Arguments=$spec.Arguments;WorkingDirectory=$spec.WorkingDirectory})}
Assert-True (Test-DerivativeDailyTaskOwnership $owned $spec) 'The intended task was not recognized.'
$owned.Actions[0].WorkingDirectory = 'C:\Unrelated'
Assert-True (-not (Test-DerivativeDailyTaskOwnership $owned $spec)) 'An unrelated task working directory was accepted.'
$owned.Actions[0].WorkingDirectory = $spec.WorkingDirectory
$owned.Principal.UserId = 'S-1-5-18'
Assert-True (-not (Test-DerivativeDailyTaskOwnership $owned $spec)) 'A different task principal was accepted.'
$owned.Principal.UserId = $spec.User
$legacy = [pscustomobject]@{TaskName=$spec.Name;TaskPath=$spec.Path;Description=$spec.Marker;Principal=$owned.Principal;
    Actions=@([pscustomobject]@{Execute=$spec.Execute;Arguments=$spec.LegacyArguments;WorkingDirectory=$null})}
Assert-True (Test-DerivativeLegacyTaskOwnership $legacy $spec) 'The exact original owned task cannot be migrated.'
$legacy.Actions[0].Arguments += ' -Unrelated'
Assert-True (-not (Test-DerivativeLegacyTaskOwnership $legacy $spec)) 'A modified legacy action was accepted for migration.'
$legacy.Actions[0].Arguments = $spec.LegacyArguments
$legacy.Principal = [pscustomobject]@{UserId='S-1-5-18';LogonType=3;RunLevel=0}
Assert-True (-not (Test-DerivativeLegacyTaskOwnership $legacy $spec)) 'A different legacy task principal was accepted for migration.'
$virtualizedSpec = Get-DerivativeDailyTaskSpec -LegacyVirtualized
$old = [pscustomobject]@{TaskName=$spec.Name;TaskPath=$spec.Path;Description=$spec.Marker;Principal=$owned.Principal;
    Actions=@([pscustomobject]@{Execute=$spec.Execute;Arguments=$virtualizedSpec.Arguments;WorkingDirectory=$virtualizedSpec.WorkingDirectory})}
Assert-True (Test-DerivativeRegisteredTaskOwnership $old $spec $virtualizedSpec) 'The exact virtualized command action cannot be migrated.'
$old.Actions[0].Arguments = $virtualizedSpec.LegacyArguments; $old.Actions[0].WorkingDirectory = $null
Assert-True (Test-DerivativeRegisteredTaskOwnership $old $spec $virtualizedSpec) 'The exact virtualized file action cannot be migrated.'
$old.Actions[0].Arguments += ' -Unrelated'
Assert-True (-not (Test-DerivativeRegisteredTaskOwnership $old $spec $virtualizedSpec)) 'An unrelated virtualized action was accepted for migration.'
$owned.Description = 'Someone else''s task'
Assert-True (-not (Test-DerivativeDailyTaskOwnership $owned $spec)) 'An unrelated task description was accepted.'
$owned.Description = $spec.Marker
$owned.Actions[0].Arguments += ' -Unexpected'
Assert-True (-not (Test-DerivativeDailyTaskOwnership $owned $spec)) 'An unrelated task action was accepted.'
$owned.Actions[0].Arguments = $spec.Arguments
$owned.Actions += [pscustomobject]@{Execute='other.exe'; Arguments=''}
Assert-True (-not (Test-DerivativeDailyTaskOwnership $owned $spec)) 'A task with an extra action was accepted.'

$script:runtimeTestRoot = Join-Path ([IO.Path]::GetTempPath()) ('dts-windows-runtime-' + [guid]::NewGuid().ToString('N'))
$script:mockInside = $false
$script:mockCollectorFailure = $false
$script:mockQueryExit = 0
$script:mockQueryTimeout = $false
$script:mockInvalidJson = $false
$script:mockZone = 'Eastern Standard Time'
$script:mockHelperExit = 0
$script:mockHelperReady = $true
$script:mockHelperTimeout = $false
$script:mockHelperMalformed = $false
$script:mockHelperStartFailure = $false
$script:mockRecordTimeout = $false
$script:recordedProofs = @()
$script:started = @()
$script:stopped = @()
Add-Type -TypeDefinition @'
namespace DerivativeLab {
    public static class NativePower {
        public static uint Last;
        public static int Requests;
        public static uint SetThreadExecutionState(uint value) { Last = value; Requests++; return 1; }
    }
}
'@
# The production wrapper uses fixed arguments. Only process execution and the
# runtime directory are replaced here, so timeout/schema/power cleanup stay real.
function Get-DerivativeDailyRuntime {
    [pscustomobject]@{Root=$script:runtimeTestRoot;
        Wsl=(Join-Path $env:SystemRoot 'System32\cmd.exe');
        Python='/home/michperez/projects/Derivative_Trading_Strategy/.venv/bin/python';
        Controller='/home/michperez/projects/Derivative_Trading_Strategy/tools/daily_collection.py'; Profile='paper-tws';
        WindowsPython=(Join-Path $env:SystemRoot 'System32\cmd.exe');
        PreflightHelper=(Join-Path $repo 'tools\check_tws_daily.py'); Sdk='fixed-test-sdk'; PreflightDependencies='fixed-test-dependencies'}
}
function Get-TimeZone { [pscustomobject]@{Id=$script:mockZone} }
function Start-Process {
    param($FilePath, $ArgumentList, $WindowStyle, [switch]$PassThru, $RedirectStandardOutput, $RedirectStandardError, $RedirectStandardInput)
    if ($WindowStyle -ne 'Hidden') { throw 'The wrapper attempted to show an interactive window.' }
    $script:started += $ArgumentList
    if ($ArgumentList.EndsWith(' window')) {
        if ($script:mockInvalidJson) {
            '{broken' | Set-Content -LiteralPath $RedirectStandardOutput
        } else {
            [pscustomobject]@{in_runtime_window=$script:mockInside; market_open_ms=1791984600000L; market_close_ms=1792008000000L;
                collection_open_ms=1791982800000L; collection_close_ms=1792009800000L; wsl_source_ip='172.26.122.234'} |
                ConvertTo-Json -Compress | Set-Content -LiteralPath $RedirectStandardOutput
        }
    } elseif ($ArgumentList.StartsWith('-X utf8')) {
        if ([DerivativeLab.NativePower]::Last -ne [uint32]2147483649) { throw 'Preflight began before sleep prevention.' }
        if ($script:mockHelperStartFailure) { throw 'Synthetic preflight-process start failure.' }
        if ($script:mockHelperMalformed) { '{broken' | Set-Content -LiteralPath $RedirectStandardOutput } else {
            [pscustomobject]@{connected=$true; read_only_api=$true; paper_identity=$true; source_ip_trusted=$true;
                localhost_trusted=$true; auto_restart=$true; auto_restart_time='11:45'; auto_restart_period='PM';
                server_version=223; timezone='America/New_York'; observed_at_ms=[DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds();
                ready=$script:mockHelperReady; reason='test_only'; source_ip='172.26.122.234'} |
                ConvertTo-Json -Compress | Set-Content -LiteralPath $RedirectStandardOutput
        }
    } elseif ($ArgumentList.EndsWith(' record-preflight')) {
        $script:recordedProofs += (Get-Content -LiteralPath $RedirectStandardInput -Raw | ConvertFrom-Json)
    } elseif ($ArgumentList.EndsWith(' run') -and $script:mockCollectorFailure) { throw 'Synthetic process-start failure.' }
    $exitCode = 0; $timeoutFails = $false
    if ($ArgumentList.EndsWith(' window')) { $exitCode = $script:mockQueryExit; $timeoutFails = $script:mockQueryTimeout }
    elseif ($ArgumentList.StartsWith('-X utf8')) { $exitCode = $script:mockHelperExit; $timeoutFails = $script:mockHelperTimeout }
    elseif ($ArgumentList.EndsWith(' record-preflight')) {
        if (-not $script:recordedProofs[-1].ready) { $exitCode = 2 }
        $timeoutFails = $script:mockRecordTimeout
    }
    $process = [pscustomobject]@{ExitCode=$exitCode; Id=1234567; TimeoutFails=$timeoutFails; Handle=1}
    $process | Add-Member -MemberType ScriptMethod -Name WaitForExit -Value { param($timeout) if ($null -ne $timeout) { return -not $this.TimeoutFails } }
    $process | Add-Member -MemberType ScriptMethod -Name Refresh -Value { }
    return $process
}
function Stop-Process { param($Id, $ErrorAction) $script:stopped += $Id }
try {
    # An elevated CI runner can default a newly created directory's owner to the
    # Administrators group. The real task runs as an ordinary interactive user.
    # Give only this new, GUID-named test fixture that user's ownership so the
    # production ownership check and private-ACL behavior remain fully exercised.
    [void][IO.Directory]::CreateDirectory($script:runtimeTestRoot)
    $testDirectory = [IO.DirectoryInfo]::new($script:runtimeTestRoot)
    $testOwner = $testDirectory.GetAccessControl([Security.AccessControl.AccessControlSections]::Owner)
    $testOwner.SetOwner([Security.Principal.WindowsIdentity]::GetCurrent().User)
    $testDirectory.SetAccessControl($testOwner)
    Assert-True ((Get-Acl -LiteralPath $script:runtimeTestRoot).GetOwner([Security.Principal.SecurityIdentifier]).Value -eq
        [Security.Principal.WindowsIdentity]::GetCurrent().User.Value) 'The runtime test fixture is not owned by its current user.'
    $closedResult = @(Invoke-DerivativeDailyCollection)
    Assert-True ($closedResult[-1] -eq 0) 'A closed session did not exit successfully.'
    Assert-True ($script:started.Count -eq 1 -and $script:started[0].EndsWith(' window')) 'A closed session launched collection.'
    Assert-True ([DerivativeLab.NativePower]::Requests -eq 0) 'A closed session requested that Windows stay awake.'
    $script:mockInside = $true
    $script:started = @()
    Assert-True ((Invoke-DerivativeDailyCollection) -eq 0) 'The active collection wrapper failed.'
    Assert-True ($script:started.Count -eq 4 -and $script:started[1].StartsWith('-X utf8') -and
        $script:started[2].EndsWith(' record-preflight') -and $script:started[3].EndsWith(' run')) 'An active session did not verify and save preflight before collection.'
    Assert-True ($script:recordedProofs.Count -eq 1 -and $script:recordedProofs[0].ready) 'A successful bounded TWS proof was not saved.'
    Assert-True ([DerivativeLab.NativePower]::Requests -eq 2 -and [DerivativeLab.NativePower]::Last -eq [uint32]2147483648) 'The power request was not released after collection.'
    $script:started = @()
    [void](Invoke-DerivativeDailyCollection -ValidateOnly)
    Assert-True ($script:started.Count -eq 1) 'Validation launched a collector.'
    Assert-True ([DerivativeLab.NativePower]::Requests -eq 2) 'Validation changed the power request.'
    $script:mockCollectorFailure = $true
    Assert-Throws { Invoke-DerivativeDailyCollection } 'The synthetic process-start failure was hidden.'
    Assert-True ([DerivativeLab.NativePower]::Requests -eq 4 -and [DerivativeLab.NativePower]::Last -eq [uint32]2147483648) 'The power request leaked after a process-start failure.'
    $script:mockCollectorFailure = $false
    foreach ($case in @('not_ready', 'process_failed', 'timeout', 'malformed', 'start_failed')) {
        $script:mockHelperReady = $case -ne 'not_ready'
        $script:mockHelperExit = if ($case -eq 'process_failed' -or $case -eq 'not_ready') { 2 } else { 0 }
        $script:mockHelperTimeout = $case -eq 'timeout'
        $script:mockHelperMalformed = $case -eq 'malformed'
        $script:mockHelperStartFailure = $case -eq 'start_failed'
        $script:started = @()
        $requests = [DerivativeLab.NativePower]::Requests
        Assert-Throws { Invoke-DerivativeDailyCollection } ('A failed preflight was accepted: ' + $case)
        Assert-True (-not @($script:started | Where-Object { $_.EndsWith(' run') }).Count) ('A failed preflight launched the collector: ' + $case)
        Assert-True (-not $script:recordedProofs[-1].ready) ('A failed preflight was not saved as failed: ' + $case)
        Assert-True ([DerivativeLab.NativePower]::Requests -eq ($requests + 2) -and
            [DerivativeLab.NativePower]::Last -eq [uint32]2147483648) ('A failed preflight leaked the power request: ' + $case)
    }
    $script:mockHelperReady = $true; $script:mockHelperExit = 0
    $script:mockHelperTimeout = $false; $script:mockHelperMalformed = $false
    $script:mockHelperStartFailure = $false
    $requestsAfterPreflightFailures = [DerivativeLab.NativePower]::Requests
    $script:mockInvalidJson = $true
    $script:started = @()
    Assert-Throws { Invoke-DerivativeDailyCollection } 'An invalid calendar JSON response was accepted.'
    Assert-True ($script:started.Count -eq 1 -and [DerivativeLab.NativePower]::Requests -eq $requestsAfterPreflightFailures) 'An invalid calendar response launched capture or requested power.'
    $script:mockInvalidJson = $false
    $script:mockQueryExit = 2
    $script:started = @()
    Assert-Throws { Invoke-DerivativeDailyCollection } 'A failed calendar process was accepted.'
    Assert-True ($script:started.Count -eq 1 -and [DerivativeLab.NativePower]::Requests -eq $requestsAfterPreflightFailures) 'A failed calendar query launched capture or requested power.'
    $script:mockQueryExit = 0
    $script:mockQueryTimeout = $true
    Assert-Throws { Invoke-DerivativeDailyCollection } 'A calendar query timeout was accepted.'
    Assert-True ($script:stopped.Count -eq 2 -and $script:stopped[-1] -eq 1234567) 'A timed-out calendar query did not stop its own process.'
    Assert-True ([DerivativeLab.NativePower]::Requests -eq $requestsAfterPreflightFailures) 'A calendar query timeout requested power.'
    $script:mockQueryTimeout = $false
    $script:mockZone = 'UTC'
    $script:started = @()
    Assert-Throws { Invoke-DerivativeDailyCollection } 'A Windows time zone mismatch was accepted.'
    Assert-True ($script:started.Count -eq 0) 'A Windows time zone mismatch started WSL.'
    $script:mockZone = 'Eastern Standard Time'
    $acl = Get-Acl -LiteralPath $script:runtimeTestRoot
    Assert-True $acl.AreAccessRulesProtected 'Private runtime logs inherited broad parent permissions.'
    $text = (Get-DerivativeControllerArguments 'run')
    Assert-True (-not ($text -match 'token|password|secret')) 'Sensitive data appeared in process arguments.'
    # Verify the native-handle pattern against an actual short-lived Windows child.
    # This child has no network, profile, credentials, or project side effects.
    $native = Microsoft.PowerShell.Management\Start-Process -FilePath (Join-Path $env:SystemRoot 'System32\cmd.exe') `
        -ArgumentList '/d /c exit 7' -WindowStyle Hidden -PassThru `
        -RedirectStandardOutput (Join-Path $script:runtimeTestRoot 'native-smoke.stdout.log') `
        -RedirectStandardError (Join-Path $script:runtimeTestRoot 'native-smoke.stderr.log')
    $null = $native.Handle
    Assert-True ($native.WaitForExit(10000)) 'The native child process did not finish.'
    $native.Refresh()
    Assert-True ($native.ExitCode -eq 7) 'A short-lived native child lost its exit status.'
    Write-Output ("Windows runtime checks passed: " + $script:checks)
} finally {
    $resolved = [IO.Path]::GetFullPath($script:runtimeTestRoot)
    $temp = [IO.Path]::GetFullPath([IO.Path]::GetTempPath()).TrimEnd('\') + '\'
    if (-not $resolved.StartsWith($temp, [StringComparison]::OrdinalIgnoreCase) -or
        -not ([IO.Path]::GetFileName($resolved)).StartsWith('dts-windows-runtime-')) {
        throw 'Refusing to remove an unexpected runtime test directory.'
    }
    if (Test-Path -LiteralPath $resolved) { Remove-Item -LiteralPath $resolved -Recurse -Force }
}
