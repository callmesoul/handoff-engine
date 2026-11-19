# 注册 Windows 计划任务：登录后自动跑 worker，崩了自动重启。
#
#   powershell -ExecutionPolicy Bypass -File deploy\windows\install-task.ps1 `
#       -StoreRoot C:\handoff\store -Worker backend-01
#
# 为什么用「登录时」而不是「开机时」：worker 要用你的 gh / codex 凭据（在
# %USERPROFILE% 下）。以 SYSTEM 身份跑，那些凭据不存在，PR 和 codex 都会失败。
# 要真正常驻不依赖登录，用 NSSM 把服务账号设成同一个用户（见文档）。

param(
    [Parameter(Mandatory = $true)][string]$StoreRoot,
    [string]$Worker = "backend-01",
    [string]$TaskName = "HandoffWorker",
    [string]$Python = "",
    [string]$Listen = "",
    [switch]$Uninstall
)

$ErrorActionPreference = "Stop"

if ($Uninstall) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
    Write-Host "已移除计划任务 $TaskName"
    exit 0
}

$StoreRoot = (Resolve-Path $StoreRoot).Path
$script = Join-Path $PSScriptRoot "run-worker.ps1"
if (-not (Test-Path $script)) { throw "找不到 $script" }

$ps = Join-Path $env:SystemRoot "System32\WindowsPowerShell\v1.0\powershell.exe"
$argList = "-NoProfile -ExecutionPolicy Bypass -File `"$script`" -Root `"$StoreRoot`" -Worker $Worker"
if ($Python) { $argList += " -Python `"$Python`"" }
if ($Listen) { $argList += " -Listen `"$Listen`"" }

$action = New-ScheduledTaskAction -Execute $ps -Argument $argList -WorkingDirectory $StoreRoot
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -MultipleInstances IgnoreNew `
    -RestartCount 999 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit ([TimeSpan]::Zero)

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -Description "Codex handoff worker（认领任务、跑 codex、开 PR）" `
    -Force | Out-Null

Start-ScheduledTask -TaskName $TaskName
Write-Host "已注册并启动 $TaskName"
Write-Host "看日志： Get-Content `"$StoreRoot\.handoff-run\logs\worker\worker-$(Get-Date -Format yyyyMMdd).log`" -Wait"
