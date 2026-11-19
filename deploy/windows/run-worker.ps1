# 启动 worker（Windows）。由计划任务或 NSSM 调用，也可以手跑。
#
#   run-worker.ps1 -Root C:\handoff\store -Worker backend-01
#
# 三件事：UTF-8、PYTHONPATH、把输出落到日志文件（计划任务没有控制台）。

param(
    [Parameter(Mandatory = $true)][string]$Root,
    [string]$Worker = "backend-01",
    [string]$Role = "backend",
    [int]$PollInterval = -1,
    [string]$Listen = "",
    [string]$Python = ""
)

$ErrorActionPreference = "Stop"

$App = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"
$env:PYTHONPATH = Join-Path $App "src"
$env:HOME = $env:USERPROFILE

if (-not $Python) {
    $win = Join-Path $App ".venv-win\Scripts\python.exe"
    if (Test-Path $win) {
        $Python = $win
    } elseif (Get-Command python -ErrorAction SilentlyContinue) {
        $Python = (Get-Command python).Source
    } else {
        throw "找不到 Python。建 .venv-win，或用 -Python 指定解释器。"
    }
}

$logDir = Join-Path $Root ".handoff-run\logs\worker"
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$log = Join-Path $logDir ("worker-{0}.log" -f (Get-Date -Format "yyyyMMdd"))

# 每天一个文件，不无限增长
Get-ChildItem $logDir -Filter "worker-*.log" |
    Where-Object { $_.LastWriteTime -lt (Get-Date).AddDays(-14) } |
    Remove-Item -Force -ErrorAction SilentlyContinue

$args = @(
    "-m", "handoff.cli",
    "--root", $Root,
    "worker",
    "--worker", $Worker,
    "--role", $Role
)

if ($Listen) {
    $args += @("--listen", $Listen)
}
if ($PollInterval -ge 0) {
    $args += @("--poll-interval", $PollInterval)
} elseif (-not $Listen) {
    $args += @("--poll-interval", 15)
}

"[{0}] start worker={1} root={2} python={3}" -f (Get-Date -Format s), $Worker, $Root, $Python |
    Tee-Object -FilePath $log -Append

& $Python @args *>&1 | Tee-Object -FilePath $log -Append
exit $LASTEXITCODE
