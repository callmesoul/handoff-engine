# 计划任务的包装：计划任务本身没法传环境变量，先把变量设好再调脚本。
#
# 由 install-selfupdate-task.ps1 调用，一般不用手跑。
# 想手动跑一次： powershell -File run-selfupdate.ps1 -AppRoot C:\handoff\app

param(
    [Parameter(Mandatory = $true)][string]$AppRoot,
    [string]$StoreRoot = "",
    [string]$Script = "",
    [string]$Python = "",
    [string]$Services = "",
    [int]$DeferMax = 6,
    [switch]$Force
)

$ErrorActionPreference = "Stop"

if (-not $Script) {
    $Script = Join-Path $PSScriptRoot "selfupdate.py"
    if (-not (Test-Path $Script)) {
        # 装好之后脚本在 C:\handoff\selfupdate\
        $Script = Join-Path (Split-Path $PSScriptRoot -Parent) "selfupdate\selfupdate.py"
    }
}
if (-not (Test-Path $Script)) { throw "找不到 selfupdate.py：$Script" }

$env:ROLE = "backend"
$env:APP = (Resolve-Path $AppRoot).Path
if (-not $env:BRANCH) { $env:BRANCH = "main" }
$env:DEFER_MAX = "$DeferMax"
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"
if ($StoreRoot) { $env:STORE = (Resolve-Path $StoreRoot).Path }
if ($Services) { $env:SERVICES = $Services }
if ($Force) { $env:FORCE = "1" }

if (-not $Python) {
    $candidate = Join-Path $env:APP ".venv-win\Scripts\python.exe"
    if (Test-Path $candidate) {
        $Python = $candidate
    } else {
        $Python = "python"
    }
}

& $Python $Script
exit $LASTEXITCODE
