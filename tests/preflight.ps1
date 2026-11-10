# Pre-recording preflight: every automated check in one run (Windows).
#   powershell -ExecutionPolicy Bypass -File tests\preflight.ps1
# Uses the Hermes install's own Python and Node; prints PASS/FAIL per step.

$ErrorActionPreference = 'Continue'
$root  = Split-Path -Parent $PSScriptRoot
$hh    = if ($env:HERMES_AGENT_DIR) { $env:HERMES_AGENT_DIR } else { Join-Path $env:LOCALAPPDATA 'hermes\hermes-agent' }
$py    = Join-Path $hh 'venv\Scripts\python.exe'
$node  = Join-Path $env:LOCALAPPDATA 'hermes\node\node.exe'
$herm  = Join-Path $env:LOCALAPPDATA 'hermes\bin\hermes.exe'
$state = Join-Path $env:TEMP 'mc_preflight_state.json'
$fails = 0

function Step($name, [scriptblock]$run, $passPattern) {
    $out = & $run 2>&1 | Out-String
    if ($out -match $passPattern) { Write-Host "PASS  $name" -ForegroundColor Green }
    else { Write-Host "FAIL  $name" -ForegroundColor Red; Write-Host $out; $script:fails++ }
}

Push-Location $root
try {
    Step 'unit tests (store, rollup, loops, pricing, routes)' { & $py tests\test_store.py } 'tests passed'
    Step 'desktop push bridge'                                 { & $py tests\test_push.py } 'PASS'
    Step 'HTTP routes on the real Hermes web server'           { & $py tests\e2e_http.py } 'E2E PASS'
    Step 'desktop UI smoke (React 19, every surface)'          { & $py tests\desktop_state.py $state; & $node tests\desktop_smoke.mjs $state (Join-Path $hh 'apps\desktop') } 'DESKTOP SMOKE PASS'
    Step 'hermes plugins validate'                             { & $herm plugins validate . } 'Validation passed'
    Step 'hermes plugins doctor --ci'                          { & $herm plugins doctor . --ci } 'registration passed'

    # Installed copy and git state: what the app and the catalog will actually see.
    $link = Get-Item (Join-Path $env:LOCALAPPDATA 'hermes\plugins\mission-control') -ErrorAction SilentlyContinue
    if ($link) { Write-Host "PASS  installed at plugins\mission-control ($($link.LinkType))" -ForegroundColor Green }
    else { Write-Host 'FAIL  not installed in %LOCALAPPDATA%\hermes\plugins\mission-control' -ForegroundColor Red; $fails++ }

    $copy = Join-Path $env:LOCALAPPDATA 'hermes\desktop-plugins\mission-control\plugin.js'
    if ((Test-Path $copy) -and ((Get-FileHash $copy).Hash -eq (Get-FileHash 'desktop\plugin.js').Hash)) {
        Write-Host 'PASS  Desktop runs the current desktop/plugin.js' -ForegroundColor Green
    } else { Write-Host 'FAIL  Desktop copy is stale: Capabilities -> Plugins -> Rescan' -ForegroundColor Red; $fails++ }

    $dirty = git status --porcelain
    if (-not $dirty) { Write-Host "PASS  git clean at $(git rev-parse --short HEAD)" -ForegroundColor Green }
    else { Write-Host "WARN  uncommitted changes (the pinned sha won't include them):" -ForegroundColor Yellow; $dirty }
    if (git ls-files | Select-String '\.py[co]$') { Write-Host 'FAIL  .pyc committed (catalog rule 17)' -ForegroundColor Red; $fails++ }
} finally {
    Get-ChildItem -Recurse -Directory -Filter __pycache__ | Remove-Item -Recurse -Force -ErrorAction SilentlyContinue
    Pop-Location
}

if ($fails) { Write-Host "`n$fails check(s) failed" -ForegroundColor Red; exit 1 }
Write-Host "`nAll automated checks passed." -ForegroundColor Green
