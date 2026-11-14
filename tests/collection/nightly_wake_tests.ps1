# Broker-free timing, cleanup, ownership and task-metadata checks.
[CmdletBinding()]
param()
Set-StrictMode -Version Latest
$ErrorActionPreference='Stop'
$repo=[IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\..'))
. (Join-Path $repo 'tools\nightly_tws_wake.ps1') -FunctionsOnly
. (Join-Path $repo 'tools\install_nightly_tws_wake_task.ps1') -FunctionsOnly
$script:checks=0
function Assert-True([bool]$Condition,[string]$Message) {
    if(-not $Condition){throw $Message};$script:checks++
}
function Assert-Throws([scriptblock]$Action,[string]$Message) {
    $failed=$false;try{& $Action | Out-Null}catch{$failed=$true}
    Assert-True $failed $Message
}
Add-Type -TypeDefinition @'
namespace DerivativeLab {
    public static class NightlyPower {
        public static uint Last;
        public static int Requests;
        public static bool Fail;
        public static uint SetThreadExecutionState(uint value) {
            Last=value; Requests++; return Fail ? 0u : 1u;
        }
    }
}
'@
$script:zone='Eastern Standard Time'
$script:at=[DateTime]::Parse('2026-10-14T23:40:00')
$script:elapsed=0.0
$script:initializations=0
$script:sleeps=@()
$script:failSleep=$false
$script:frozenClock=$false
function Get-TimeZone { [pscustomobject]@{Id=$script:zone} }
function Get-DerivativeNightlyTime { $script:at }
function Get-DerivativeNightlyElapsedSeconds { param($Timer) $script:elapsed }
function Initialize-DerivativeNightlyDirectory { param($Runtime) $script:initializations++ }
function Start-Sleep {
    param([int]$Milliseconds)
    Assert-True ($Milliseconds -gt 0 -and $Milliseconds -le 15000) 'A sleep exceeded the bounded nightly interval.'
    $script:sleeps += $Milliseconds
    if($script:failSleep){throw 'Synthetic sleep failure.'}
    if($script:frozenClock){$script:elapsed=900.0}else{
        $script:elapsed += $Milliseconds/1000.0
        $script:at=$script:at.AddMilliseconds($Milliseconds)
    }
}

foreach($value in @('2026-10-14T23:39:59','2026-10-14T23:55:00','2026-10-15T00:00:00','2026-10-14T09:30:00')) {
    $script:at=[DateTime]::Parse($value)
    Assert-True ((Invoke-DerivativeNightlyWake) -eq 0) 'An outside-window invocation failed.'
}
Assert-True ([DerivativeLab.NightlyPower]::Requests -eq 0 -and $script:initializations -eq 0) 'An outside-window invocation requested power or touched runtime files.'
$script:at=[DateTime]::Parse('2026-10-14T23:54:59');$script:elapsed=0
Assert-True ((Invoke-DerivativeNightlyWake) -eq 0) 'A late wake-window invocation failed.'
Assert-True ($script:sleeps.Count -eq 1 -and $script:sleeps[0] -eq 1000) 'A late wake-window invocation ran beyond 23:55.'
Assert-True ([DerivativeLab.NightlyPower]::Requests -eq 2 -and [DerivativeLab.NightlyPower]::Last -eq [uint32]2147483648) 'The power request was not released at the close.'
$script:at=[DateTime]::Parse('2026-10-14T23:40:00');$script:elapsed=0;$script:sleeps=@()
Assert-True ((Invoke-DerivativeNightlyWake) -eq 0) 'A full wake window failed.'
Assert-True ($script:at -eq [DateTime]::Parse('2026-10-14T23:55:00') -and $script:elapsed -eq 900) 'The full wake window did not stop after 15 minutes.'
$script:at=[DateTime]::Parse('2026-10-14T23:40:00');$script:elapsed=0;$script:frozenClock=$true
$before=[DerivativeLab.NightlyPower]::Requests
Assert-True ((Invoke-DerivativeNightlyWake) -eq 0) 'A frozen wall clock prevented bounded exit.'
Assert-True ([DerivativeLab.NightlyPower]::Requests -eq $before+2) 'The monotonic cap did not release the power request.'
$script:frozenClock=$false;$script:elapsed=0;$script:failSleep=$true
$before=[DerivativeLab.NightlyPower]::Requests
Assert-Throws {Invoke-DerivativeNightlyWake} 'A sleep failure was hidden.'
Assert-True ([DerivativeLab.NightlyPower]::Requests -eq $before+2 -and [DerivativeLab.NightlyPower]::Last -eq [uint32]2147483648) 'A failure leaked the power request.'
$script:failSleep=$false;[DerivativeLab.NightlyPower]::Fail=$true
$before=[DerivativeLab.NightlyPower]::Requests
Assert-Throws {Invoke-DerivativeNightlyWake} 'A failed native power request was accepted.'
Assert-True ([DerivativeLab.NightlyPower]::Requests -eq $before+1) 'An unsuccessful power acquisition was treated as held.'
[DerivativeLab.NightlyPower]::Fail=$false;$script:zone='UTC'
$before=[DerivativeLab.NightlyPower]::Requests
Assert-Throws {Invoke-DerivativeNightlyWake} 'An incorrect Windows timezone was accepted.'
Assert-True ([DerivativeLab.NightlyPower]::Requests -eq $before) 'An incorrect timezone requested power.'
$script:zone='Eastern Standard Time'

$spec=Get-DerivativeNightlyTaskSpec
$owned=[pscustomobject]@{TaskName=$spec.Name;TaskPath=$spec.Path;Description=$spec.Marker;
    Actions=@([pscustomobject]@{Execute=$spec.Execute;Arguments=$spec.Arguments;WorkingDirectory=$spec.WorkingDirectory});
    Principal=[pscustomobject]@{UserId=[Security.Principal.WindowsIdentity]::GetCurrent().User.Value;LogonType='Interactive';RunLevel='Limited'}}
Assert-True (Test-DerivativeNightlyTaskOwnership $owned $spec) 'The intended limited current-user task was rejected.'
$owned.Principal.RunLevel='Highest'
Assert-True (-not(Test-DerivativeNightlyTaskOwnership $owned $spec)) 'An elevated task was accepted.'
$owned.Principal.RunLevel='Limited';$owned.Principal.UserId='S-1-5-18'
Assert-True (-not(Test-DerivativeNightlyTaskOwnership $owned $spec)) 'Another task principal was accepted.'
$owned.Principal.UserId=[Security.Principal.WindowsIdentity]::GetCurrent().User.Value
$owned.Description='Unrelated task'
Assert-True (-not(Test-DerivativeNightlyTaskOwnership $owned $spec)) 'An unrelated task marker was accepted.'
$owned.Description=$spec.Marker;$owned.Actions[0].WorkingDirectory='C:\Windows'
Assert-True (-not(Test-DerivativeNightlyTaskOwnership $owned $spec)) 'Another working directory was accepted.'
Assert-True ($spec.Arguments.Contains('-WindowStyle Hidden') -and $spec.Arguments.Contains('*>>') -and
    -not $spec.Arguments.Contains('-File ')) 'The task did not use the fixed hidden local command action.'

# Construct metadata only: these cmdlets do not register or execute a task.
$trigger=New-DerivativeNightlyTrigger
$settings=New-ScheduledTaskSettingsSet -WakeToRun -StartWhenAvailable -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 30)
$principal=New-ScheduledTaskPrincipal -UserId $spec.User -LogonType Interactive -RunLevel Limited
Assert-True ($trigger.StartBoundary.EndsWith('T23:40:00') -and $trigger.DaysInterval -eq 1) 'The trigger was not daily at 23:40.'
Assert-True ($trigger.StartBoundary -match '^\d{4}-\d{2}-\d{2}T23:40:00$') 'A fixed UTC offset prevented the wake trigger from following local DST.'
Assert-True ($settings.WakeToRun -and $settings.StartWhenAvailable -and $settings.ExecutionTimeLimit -eq 'PT30M') 'The wake task lacked its bounded timer or wake settings.'
Assert-True ([string]$principal.RunLevel -eq 'Limited' -and [string]$principal.LogonType -eq 'Interactive') 'The task metadata elevated privileges or required credentials.'
Write-Output ('Nightly wake broker-free checks passed: '+$script:checks)
