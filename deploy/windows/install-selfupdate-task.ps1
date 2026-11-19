# 注册 Windows 计划任务：每 10 分钟检查交接系统的核心代码有没有更新。
#
#   powershell -ExecutionPolicy Bypass -File deploy\windows\install-selfupdate-task.ps1 `
#       -AppRoot C:\handoff\app -StoreRoot C:\handoff\store `
#       -Services "HandoffWorker"
#
# 为什么 10 分钟：等的是人写代码，不是等任务事件。核心代码一天改几次。
#
# 为什么 MultipleInstances=IgnoreNew：合并 + pip install + 重启可能超过 10 分钟。
# 上一次没跑完时，新的触发直接丢弃，不会叠成两个进程抢锁。
#
# 注意：-Services 填的是**计划任务名**（或 NSSM 服务名，脚本用
# Stop-ScheduledTask/Start-ScheduledTask，NSSM 会失败，见文档）。

param(
    [Parameter(Mandatory = $true)][string]$AppRoot,
    [string]$StoreRoot = "",
    [string]$Script = "",
    [string]$Python = "",
    [string]$Services = "HandoffWorker",
    [int]$DeferMax = 6,
    [string]$TaskName = "HandoffSelfUpdate",
    [int]$IntervalMinutes = 10,
    [switch]$Uninstall
)

$ErrorActionPreference = "Stop"

if ($Uninstall) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
    Write-Host "已移除计划任务 $TaskName"
    exit 0
}

$AppRoot = (Resolve-Path $AppRoot).Path
if ($StoreRoot) { $StoreRoot = (Resolve-Path $StoreRoot).Path }

if (-not $Script) { $Script = Join-Path $PSScriptRoot "selfupdate.py" }
if (-not (Test-Path $Script)) { throw "找不到 selfupdate.py：$Script" }

$runner = Join-Path $PSScriptRoot "run-selfupdate.ps1"
if (-not (Test-Path $runner)) { throw "找不到 $runner" }

$argList = "-NoProfile -ExecutionPolicy Bypass -File `"$runner`" -AppRoot `"$AppRoot`" -DeferMax $DeferMax"
if ($StoreRoot) { $argList += " -StoreRoot `"$StoreRoot`"" }
if ($Services)  { $argList += " -Services `"$Services`"" }
if ($Python)    { $argList += " -Python `"$Python`"" }
if ($Script)    { $argList += " -Script `"$Script`"" }

$ps = Join-Path $env:SystemRoot "System32\WindowsPowerShell\v1.0\powershell.exe"
$action = New-ScheduledTaskAction -Execute $ps -Argument $argList -WorkingDirectory $AppRoot

$start = (Get-Date).AddMinutes(2)
$trigger = New-ScheduledTaskTrigger -Once -At $start `
    -RepetitionInterval (New-TimeSpan -Minutes $IntervalMinutes) `
    -RepetitionDuration (New-TimeSpan -Days 9999)

$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 30)

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -Description "Handoff 核心代码自动更新（只认 src/ bin/ schemas/ 等代码路径）" `
    -Force | Out-Null

Write-Host "已注册 $TaskName（每 ${IntervalMinutes} 分钟）"
Write-Host "试跑： Start-ScheduledTask -TaskName $TaskName"
Write-Host "结果： Get-ScheduledTaskInfo -TaskName $TaskName | Select-Object LastRunTime,LastTaskResult"
Write-Host "日志： Get-Content `"$AppRoot\.handoff-run\log\selfupdate.log`" -Tail 30"
Write-Host ""
Write-Host "提醒：交接仓的 tasks/ events/ 变更不会触发更新，这是有意的。"
