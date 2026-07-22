param(
    [Parameter(Mandatory = $true)]
    [string]$Python,
    [Parameter(Mandatory = $true)]
    [string]$WorkRoot
)

$ErrorActionPreference = "Stop"
$Manager = Join-Path $PSScriptRoot "..\skills\codex-image-bridge\scripts\bridge_manager.py"
$CodexHome = Join-Path $WorkRoot ".codex"
$Config = Join-Path $CodexHome "config.toml"
$State = Join-Path $CodexHome "image-bridge\state.json"
$TaskName = "Codex Image Bridge"
$OriginalUrl = "https://gateway.example/openai/"

New-Item -ItemType Directory -Force -Path $CodexHome | Out-Null
$ConfigText = @"
model = "gpt-test"
model_provider = "Test Gateway"

[model_providers."Test Gateway"]
name = "Test Gateway"
base_url = "$OriginalUrl"
experimental_bearer_token = "ci-placeholder"
"@
[System.IO.File]::WriteAllText($Config, $ConfigText, [System.Text.UTF8Encoding]::new($false))

try {
    & $Python $Manager --codex-home $CodexHome preflight
    if ($LASTEXITCODE -ne 0) { throw "preflight failed" }

    & $Python $Manager --codex-home $CodexHome install
    if ($LASTEXITCODE -ne 0) { throw "install failed" }

    schtasks.exe /Query /TN $TaskName | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "Scheduled Task was not registered" }

    $Health = Invoke-RestMethod -Uri "http://127.0.0.1:8787/__codex_image_bridge__/health"
    if ($Health.status -ne "ok") { throw "bridge health endpoint failed" }

    $InstalledConfig = [System.IO.File]::ReadAllText($Config)
    if (-not $InstalledConfig.Contains('base_url = "http://127.0.0.1:8787/openai/"')) {
        throw "install did not update the provider URL"
    }

    & $Python (Join-Path $CodexHome "image-bridge\bridge_manager.py") --codex-home $CodexHome uninstall
    if ($LASTEXITCODE -ne 0) { throw "uninstall failed" }

    $RestoredConfig = [System.IO.File]::ReadAllText($Config)
    if (-not $RestoredConfig.Contains("base_url = `"$OriginalUrl`"")) {
        throw "uninstall did not restore the provider URL"
    }
    schtasks.exe /Query /TN $TaskName 2>$null | Out-Null
    if ($LASTEXITCODE -eq 0) { throw "uninstall did not delete the Scheduled Task" }
}
finally {
    if (Test-Path $State) {
        & $Python (Join-Path $CodexHome "image-bridge\bridge_manager.py") --codex-home $CodexHome uninstall
    }
    schtasks.exe /End /TN $TaskName 2>$null | Out-Null
    schtasks.exe /Delete /TN $TaskName /F 2>$null | Out-Null
}

$global:LASTEXITCODE = 0
