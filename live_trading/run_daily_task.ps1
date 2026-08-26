# Windows Task Scheduler用のラッパースクリプト。live_trading.run_dailyを1回実行する。
#
# 安全のため、デフォルトは検証用環境(-Production指定なし)。本番切り替えは、
# プチ株のAPI対応確認・入金が済み、実際に本番で動かす準備ができてから、
# タスクスケジューラのアクション引数に -Production を明示的に追加すること。
#
# 使い方(タスクスケジューラのアクション設定例):
#   プログラム: powershell.exe
#   引数: -ExecutionPolicy Bypass -File "...\run_daily_task.ps1" -SessionLabel "前場"
#   (本番で動かす場合のみ) -Production を追加

param(
    [string]$SessionLabel = "手動",
    [switch]$Production
)

$ErrorActionPreference = "Stop"
$ProjectDir = "C:\Users\takuy\OneDrive\デスクトップ\claude code作業フォルダ\daytrade-auto-execution"
$VenvPython = "C:\Users\takuy\OneDrive\デスクトップ\claude code作業フォルダ\.venv\Scripts\python.exe"
$LogFile = Join-Path $ProjectDir "data\live_trading_task.log"

Set-Location $ProjectDir
$env:PYTHONPATH = $ProjectDir
$env:PYTHONIOENCODING = "utf-8"

$timestamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
Add-Content -Path $LogFile -Value "`n=== $timestamp (session=$SessionLabel, production=$($Production.IsPresent)) ===" -Encoding utf8

try {
    # 1. その日の指標(daily_metrics)を最新化する。GitHub Actions側の実行・pull待ちに
    #    依存させず、live_trading実行の直前にローカルで確定させておく。
    $pipelineOutput = & $VenvPython -m pipeline.run_daily 2>&1 | Out-String
    Add-Content -Path $LogFile -Value $pipelineOutput -Encoding utf8

    # 2. live_trading本体を実行(エントリー/決済の確認、Discordへの運用成果通知を含む)。
    $pyBool = if ($Production.IsPresent) { "True" } else { "False" }
    $liveOutput = & $VenvPython -c @"
import sys
sys.stdout.reconfigure(encoding='utf-8')
from live_trading.run_daily import run_safely
run_safely(session_label='$SessionLabel', production=$pyBool)
"@ 2>&1 | Out-String
    Add-Content -Path $LogFile -Value $liveOutput -Encoding utf8
} catch {
    Add-Content -Path $LogFile -Value "ERROR: $_" -Encoding utf8
}
