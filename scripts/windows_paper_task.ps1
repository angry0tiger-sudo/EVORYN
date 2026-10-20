[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidateSet("register", "unregister", "status")]
    [string]$Action,
    [Parameter(Mandatory = $true)]
    [string]$PythonPath,
    [Parameter(Mandatory = $true)]
    [string]$RunnerScript,
    [Parameter(Mandatory = $true)]
    [string]$ConfigPath,
    [Parameter(Mandatory = $true)]
    [string]$TaskName
)

$ErrorActionPreference = "Stop"
$productionTaskName = "EVORYN-PaperOnly"
$ciTaskPattern = '^EVORYN-CI-PaperOnly-[A-Za-z0-9-]{1,48}$'
if (($TaskName -ne $productionTaskName) -and ($TaskName -notmatch $ciTaskPattern)) {
    throw "Refusing to manage a task outside the EVORYN Paper namespace."
}

function Get-EvorynTask {
    param([string]$Name)
    return Get-ScheduledTask -TaskName $Name -ErrorAction SilentlyContinue
}

switch ($Action) {
    "register" {
        if (-not (Test-Path -LiteralPath $PythonPath -PathType Leaf)) { throw "Configured Python executable does not exist." }
        if (-not (Test-Path -LiteralPath $RunnerScript -PathType Leaf)) { throw "EVORYN Paper runner script does not exist." }
        if (-not (Test-Path -LiteralPath $ConfigPath -PathType Leaf)) { throw "Paper-only configuration does not exist." }
        if ($PythonPath -match '["\r\n]' -or $RunnerScript -match '["\r\n]' -or $ConfigPath -match '["\r\n]') {
            throw "Task action paths contain unsupported quoting or newline characters."
        }
        if ($ConfigPath.StartsWith("\\") -or $ConfigPath.StartsWith("//")) {
            throw "Paper task configuration must be on a local filesystem, not a UNC path."
        }

        # This read-only preflight validates the offline CSV and freshness without opening/creating the journal.
        & $PythonPath $RunnerScript preflight --config $ConfigPath
        if ($LASTEXITCODE -ne 0) { throw "Paper-only preflight failed; Task Scheduler registration was not attempted." }
        if (Get-EvorynTask -Name $TaskName) { throw "Task already exists; refusing to overwrite an existing scheduled task." }

        $identity = [System.Security.Principal.WindowsIdentity]::GetCurrent()
        $projectRoot = Split-Path -Parent (Split-Path -Parent $RunnerScript)
        $arguments = '"{0}" run --config "{1}"' -f $RunnerScript, $ConfigPath
        $taskAction = New-ScheduledTaskAction -Execute $PythonPath -Argument $arguments -WorkingDirectory $projectRoot
        $trigger = New-ScheduledTaskTrigger -AtLogOn -User $identity.Name
        $settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -StartWhenAvailable -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 5) -ExecutionTimeLimit ([TimeSpan]::Zero) -DontStopIfGoingOnBatteries -AllowStartIfOnBatteries
        $principal = New-ScheduledTaskPrincipal -UserId $identity.Name -LogonType Interactive -RunLevel Limited

        Register-ScheduledTask -TaskName $TaskName -Action $taskAction -Trigger $trigger -Settings $settings -Principal $principal -Description "EVORYN offline CSV Paper-only runtime. No Champion, credentials, or exchange connection." | Out-Null
        $registered = Get-EvorynTask -Name $TaskName
        if (-not $registered) { throw "Task Scheduler did not return the registered task." }
        [PSCustomObject]@{
            status = "registered"
            task_name = $TaskName
            task_state = [string]$registered.State
            run_as = $identity.Name
            privilege = "limited"
            trigger = "at_logon"
            multiple_instances = "ignore_new"
            restart_budget = 3
            live_connection = "disabled"
            champion = "none"
        } | ConvertTo-Json -Compress
    }
    "unregister" {
        $registered = Get-EvorynTask -Name $TaskName
        if (-not $registered) {
            [PSCustomObject]@{ status = "not_registered"; task_name = $TaskName } | ConvertTo-Json -Compress
            break
        }
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        if (Get-EvorynTask -Name $TaskName) { throw "Task Scheduler still reports the task after unregistration." }
        [PSCustomObject]@{ status = "unregistered"; task_name = $TaskName } | ConvertTo-Json -Compress
    }
    "status" {
        $registered = Get-EvorynTask -Name $TaskName
        if (-not $registered) {
            [PSCustomObject]@{ status = "not_registered"; task_name = $TaskName } | ConvertTo-Json -Compress
            break
        }
        $info = Get-ScheduledTaskInfo -TaskName $TaskName
        [PSCustomObject]@{
            status = "registered"
            task_name = $TaskName
            task_state = [string]$registered.State
            last_run_time = $info.LastRunTime.ToUniversalTime().ToString("o")
            last_task_result = $info.LastTaskResult
            next_run_time = $info.NextRunTime.ToUniversalTime().ToString("o")
        } | ConvertTo-Json -Compress
    }
}
