[CmdletBinding(SupportsShouldProcess=$true)]
param([switch]$FunctionsOnly)
Set-StrictMode -Version Latest
$ErrorActionPreference='Stop'

function Get-DerivativeNightlyTaskSpec {
    $root=Join-Path $env:USERPROFILE '.derivative-lab\daily-collection'
    $wrapper=Join-Path $root 'nightly_tws_wake.ps1'
    $log=Join-Path $root 'nightly-wake-launcher.log'
    $command="& '"+$wrapper.Replace("'","''")+"' *>> '"+$log.Replace("'","''")+"'; exit `$LASTEXITCODE"
    [pscustomobject]@{
        Name='DerivativeLab-TWS-Restart-Wake'; Path='\'
        Marker='DerivativeLab nightly TWS restart wake window v1'
        User=[Security.Principal.WindowsIdentity]::GetCurrent().Name
        Root=$root; Wrapper=$wrapper; SharedWrapper=(Join-Path $root 'start_daily_collection.ps1')
        Execute=(Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe')
        Arguments='-NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -WindowStyle Hidden -Command "'+$command+'"'
        WorkingDirectory=$root
        Repository='\\wsl.localhost\Ubuntu\home\michperez\projects\Derivative_Trading_Strategy'
    }
}

function Test-DerivativeNightlyTaskOwnership {
    param([Parameter(Mandatory)]$Task,[Parameter(Mandatory)]$Spec)
    $actions=@($Task.Actions)
    if ($Task.TaskName -ne $Spec.Name -or $Task.TaskPath -ne $Spec.Path -or
        $Task.Description -ne $Spec.Marker -or $actions.Count -ne 1) { return $false }
    $action=$actions[0]
    if ($null -eq $action -or $action.Execute -ine $Spec.Execute -or
        $action.Arguments -cne $Spec.Arguments -or $action.WorkingDirectory -ine $Spec.WorkingDirectory) { return $false }
    try {
        $identity=[string]$Task.Principal.UserId
        $sid=if($identity.StartsWith('S-1-')) {$identity} else {
            ([Security.Principal.NTAccount]::new($identity)).Translate([Security.Principal.SecurityIdentifier]).Value
        }
        return $sid -eq [Security.Principal.WindowsIdentity]::GetCurrent().User.Value -and
            [string]$Task.Principal.LogonType -in @('Interactive','InteractiveToken','3') -and
            [string]$Task.Principal.RunLevel -in @('Limited','LeastPrivilege','0')
    } catch { return $false }
}

function New-DerivativeNightlyTrigger {
    $trigger=New-ScheduledTaskTrigger -Daily -At '23:40'
    # A boundary without a UTC offset follows Windows local DST transitions.
    $trigger.StartBoundary=[DateTime]::Today.AddHours(23).AddMinutes(40).ToString('yyyy-MM-ddTHH:mm:ss')
    return $trigger
}

function Install-DerivativeNightlyWakeTask {
    [CmdletBinding(SupportsShouldProcess=$true)]
    param()
    if ((Get-TimeZone).Id -ne 'Eastern Standard Time') { throw 'The nightly wake task requires the Windows Eastern time zone.' }
    $spec=Get-DerivativeNightlyTaskSpec
    $expectedTools=Join-Path $spec.Repository 'tools'
    if ([IO.Path]::GetFullPath($PSScriptRoot).TrimEnd('\') -ine [IO.Path]::GetFullPath($expectedTools).TrimEnd('\')) {
        throw 'Run this installer from the existing Ubuntu project checkout.'
    }
    $source=Join-Path $PSScriptRoot 'nightly_tws_wake.ps1'
    foreach($path in @($source,$spec.SharedWrapper)) {
        $item=Get-Item -LiteralPath $path -Force -ErrorAction Stop
        if($item.PSIsContainer -or ($item.Attributes -band [IO.FileAttributes]::ReparsePoint)) { throw 'A regular existing local wrapper is required.' }
    }
    $existing=Get-ScheduledTask -TaskName $spec.Name -TaskPath $spec.Path -ErrorAction SilentlyContinue
    if($null -ne $existing -and -not(Test-DerivativeNightlyTaskOwnership $existing $spec)) {
        throw 'A task with this name has unrelated ownership or actions; it was not changed.'
    }
    $trigger=New-DerivativeNightlyTrigger
    $action=New-ScheduledTaskAction -Execute $spec.Execute -Argument $spec.Arguments -WorkingDirectory $spec.WorkingDirectory
    $principal=New-ScheduledTaskPrincipal -UserId $spec.User -LogonType Interactive -RunLevel Limited
    $settings=New-ScheduledTaskSettingsSet -WakeToRun -StartWhenAvailable -MultipleInstances IgnoreNew `
        -ExecutionTimeLimit (New-TimeSpan -Minutes 30) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
    $task=New-ScheduledTask -Action $action -Trigger $trigger -Principal $principal -Settings $settings -Description $spec.Marker
    if(-not $PSCmdlet.ShouldProcess($spec.Name,'Install/update current-user wake window, daily 23:40–23:55 Eastern')) { return }
    . $spec.SharedWrapper -FunctionsOnly
    Initialize-DerivativePrivateDirectory -Path $spec.Root
    $stamp=[DateTime]::UtcNow.ToString('yyyyMMddTHHmmssfffffffZ')+'-'+[guid]::NewGuid().ToString('N')
    if($null -ne $existing) {
        Export-ScheduledTask -TaskName $spec.Name -TaskPath $spec.Path |
            Set-Content -LiteralPath (Join-Path $spec.Root ('nightly-task-before-'+$stamp+'.xml')) -Encoding Unicode
    }
    if(Test-Path -LiteralPath $spec.Wrapper) {
        $old=Get-Item -LiteralPath $spec.Wrapper -Force
        if($old.PSIsContainer -or ($old.Attributes -band [IO.FileAttributes]::ReparsePoint)) { throw 'Existing nightly wrapper is not a regular file; it was not changed.' }
        if((Get-FileHash -LiteralPath $spec.Wrapper -Algorithm SHA256).Hash -ne (Get-FileHash -LiteralPath $source -Algorithm SHA256).Hash) {
            Copy-Item -LiteralPath $spec.Wrapper -Destination (Join-Path $spec.Root ('nightly-wrapper-before-'+$stamp+'.ps1'))
        }
    }
    Copy-Item -LiteralPath $source -Destination $spec.Wrapper -Force
    [void](Register-ScheduledTask -TaskName $spec.Name -TaskPath $spec.Path -InputObject $task -Force)
    $installed=Get-ScheduledTask -TaskName $spec.Name -TaskPath $spec.Path
    if(-not(Test-DerivativeNightlyTaskOwnership $installed $spec)) { throw 'Registered nightly wake task differs from the intended current-user action.' }
    [pscustomobject]@{ task=$spec.Name; timezone='America/New_York'; start='23:40'; stop='23:55';
        wake_to_run=$true; execution_limit_minutes=30; requires_windows_sign_in=$true }
}

if(-not $FunctionsOnly) {
    try { Install-DerivativeNightlyWakeTask -WhatIf:$WhatIfPreference } catch {
        Write-Error $_.Exception.Message -ErrorAction Continue
        exit 2
    }
}
