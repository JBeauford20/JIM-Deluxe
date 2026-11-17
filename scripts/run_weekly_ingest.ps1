# Weekly ingest wrapper for the "JIM Weekly Ingest" scheduled task.
# Runs the pipeline; on failure, launches Claude Code headless to triage and repair
# using the /jim-ingest-triage skill. ASCII-only file (PS 5.1 encoding rules).

$repo   = Split-Path $PSScriptRoot -Parent
$python = "C:\Python313\python.exe"
$logDir = Join-Path $repo "backend\ingest\logs"
New-Item -ItemType Directory -Force $logDir | Out-Null

Set-Location $repo
& $python "backend\ingest\weekly_ingest.py"
$code = $LASTEXITCODE

if ($code -ne 0) {
    $stamp = Get-Date -Format "yyyyMMdd_HHmm"
    $triageLog = Join-Path $logDir "triage_$stamp.log"
    "Ingest exited $code - launching Claude triage..." | Out-File $triageLog -Encoding utf8
    $claude = (Get-Command claude -ErrorAction SilentlyContinue).Source
    if ($claude) {
        & $claude -p "/jim-ingest-triage The scheduled JIM weekly ingest just failed with exit code $code. Read the newest log in backend/ingest/logs, diagnose, repair what the skill allows, rerun the failed stage, and append your report." `
            --permission-mode acceptEdits 2>&1 | Out-File $triageLog -Append -Encoding utf8
    } else {
        "claude CLI not found on PATH - manual triage needed." | Out-File $triageLog -Append -Encoding utf8
    }
    exit $code
}
exit 0
