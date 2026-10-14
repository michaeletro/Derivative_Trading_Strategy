[CmdletBinding(SupportsShouldProcess = $true)]
param([switch]$FunctionsOnly)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Get-DerivativeDailyTaskSpec {
    $runtimeRoot = Join-Path $env:LOCALAPPDATA 'DerivativeLab\daily-collection'
    $wrapper = Join-Path $runtimeRoot 'start_daily_collection.ps1'
    [pscustomobject]@{
        Name = 'DerivativeLab-SPY-BATS-Collection'
        Path = '\'
        Marker = 'DerivativeLab SPY/BATS local paper read-only collection v1'
        User = [Security.Principal.WindowsIdentity]::GetCurrent().Name
        Root = $runtimeRoot
        Wrapper = $wrapper
        Execute = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
        Arguments = '-NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -WindowStyle Hidden -File "' + $wrapper + '"'
        Repository = '\\wsl.localhost\Ubuntu\home\michperez\projects\Derivative_Trading_Strategy'
        RestartCount = 999
        RestartIntervalMinutes = 5
    }
}

function Test-DerivativeDailyTaskOwnership {
    param([Parameter(Mandatory)]$Task, [Parameter(Mandatory)]$Spec)
    $actions = @($Task.Actions)
    if ($Task.TaskName -ne $Spec.Name -or $Task.TaskPath -ne $Spec.Path -or
        $Task.Description -ne $Spec.Marker -or $actions.Count -ne 1) { return $false }
    $action = $actions[0]
    if ($null -eq $action -or $action.Execute -ine $Spec.Execute -or
        $action.Arguments -cne $Spec.Arguments) { return $false }
    return $true
}

function Install-DerivativeDailyTask {
    [CmdletBinding(SupportsShouldProcess = $true)]
    param()
    if ((Get-TimeZone).Id -ne 'Eastern Standard Time') {
        throw 'The 08:50 task requires the Windows time zone to be Eastern Standard Time.'
    }
    $spec = Get-DerivativeDailyTaskSpec
    $expectedTools = Join-Path $spec.Repository 'tools'
    if ([IO.Path]::GetFullPath($PSScriptRoot).TrimEnd('\') -ine [IO.Path]::GetFullPath($expectedTools).TrimEnd('\')) {
        throw 'Run this installer from the existing Ubuntu Derivative_Trading_Strategy checkout.'
    }
    $source = Join-Path $PSScriptRoot 'start_daily_collection.ps1'
    foreach ($required in @($source, (Join-Path $PSScriptRoot 'daily_collection.py'),
                            (Join-Path $spec.Repository '.venv\bin\python'))) {
        if (-not (Test-Path -LiteralPath $required -PathType Leaf)) {
            throw 'The existing collection controller, wrapper, or Python environment is missing.'
        }
    }
    $existing = Get-ScheduledTask -TaskName $spec.Name -TaskPath $spec.Path -ErrorAction SilentlyContinue
    if ($null -ne $existing -and -not (Test-DerivativeDailyTaskOwnership $existing $spec)) {
        throw 'A task with this name has unrelated actions or ownership metadata; it was not changed.'
    }
    $weekly = New-ScheduledTaskTrigger -Weekly -WeeksInterval 1 `
        -DaysOfWeek Monday, Tuesday, Wednesday, Thursday, Friday -At '08:50'
    $logon = New-ScheduledTaskTrigger -AtLogOn -User $spec.User
    $action = New-ScheduledTaskAction -Execute $spec.Execute -Argument $spec.Arguments
    $principal = New-ScheduledTaskPrincipal -UserId $spec.User -LogonType Interactive -RunLevel Limited
    $settings = New-ScheduledTaskSettingsSet -WakeToRun -StartWhenAvailable -MultipleInstances IgnoreNew `
        -ExecutionTimeLimit (New-TimeSpan -Hours 10) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
        -RestartCount $spec.RestartCount -RestartInterval (New-TimeSpan -Minutes $spec.RestartIntervalMinutes)
    $task = New-ScheduledTask -Action $action -Trigger @($weekly, $logon) `
        -Principal $principal -Settings $settings -Description $spec.Marker
    if (-not $PSCmdlet.ShouldProcess($spec.Name,
        'Install/update the current-user local SPY/BATS collection task; weekday 08:50 Eastern and user logon')) {
        return
    }
    . $source -FunctionsOnly
    Initialize-DerivativePrivateDirectory -Path $spec.Root
    $stamp = [DateTime]::UtcNow.ToString('yyyyMMddTHHmmssfffffffZ')
    if ($null -ne $existing) {
        $backup = Join-Path $spec.Root ('task-before-' + $stamp + '.xml')
        Export-ScheduledTask -TaskName $spec.Name -TaskPath $spec.Path |
            Set-Content -LiteralPath $backup -Encoding Unicode
    }
    $newHash = (Get-FileHash -LiteralPath $source -Algorithm SHA256).Hash
    if (Test-Path -LiteralPath $spec.Wrapper) {
        $old = Get-Item -LiteralPath $spec.Wrapper -Force
        if ($old.PSIsContainer -or ($old.Attributes -band [IO.FileAttributes]::ReparsePoint)) {
            throw 'The existing local wrapper is not a regular file; it was not changed.'
        }
        if ((Get-FileHash -LiteralPath $spec.Wrapper -Algorithm SHA256).Hash -ne $newHash) {
            Copy-Item -LiteralPath $spec.Wrapper -Destination (Join-Path $spec.Root ('wrapper-before-' + $stamp + '.ps1'))
        }
    }
    Copy-Item -LiteralPath $source -Destination $spec.Wrapper -Force
    [void](Register-ScheduledTask -TaskName $spec.Name -TaskPath $spec.Path -InputObject $task -Force)
    $installed = Get-ScheduledTask -TaskName $spec.Name -TaskPath $spec.Path
    if (-not (Test-DerivativeDailyTaskOwnership $installed $spec)) {
        throw 'The registered task did not match the expected local collection action.'
    }
    $info = Get-ScheduledTaskInfo -TaskName $spec.Name -TaskPath $spec.Path
    [pscustomobject]@{ task = $spec.Name; user = $spec.User; state = [string]$installed.State;
        next_run = $info.NextRunTime.ToString('o'); time_zone = (Get-TimeZone).Id;
        requires_windows_sign_in = $true; requires_tws_paper_read_only_login = $true }
}

if (-not $FunctionsOnly) {
    try { Install-DerivativeDailyTask -WhatIf:$WhatIfPreference } catch {
        Write-Error $_.Exception.Message -ErrorAction Continue
        exit 2
    }
}
