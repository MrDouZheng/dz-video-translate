$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$BundledPython = Join-Path $env:USERPROFILE ".cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe"
$Candidates = @(
    (Join-Path $Root ".venv\Scripts\python.exe"),
    $BundledPython
)
$Python = $null
$CheckArgs = @("-c", "import faster_whisper")
foreach ($Candidate in $Candidates) {
    if (Test-Path -LiteralPath $Candidate) {
        & $Candidate @CheckArgs 2>$null
        if ($LASTEXITCODE -eq 0) {
            $Python = $Candidate
            break
        }
    }
}
if (-not $Python) {
    foreach ($Candidate in $Candidates) {
        if (Test-Path -LiteralPath $Candidate) {
            $Python = $Candidate
            break
        }
    }
}
if (-not $Python) {
    $Command = Get-Command python -ErrorAction SilentlyContinue
    if ($Command) { $Python = $Command.Source }
}
if (-not $Python) {
    throw "Python 3.12 was not found. Install Python or edit start_webui.ps1."
}
$PythonScripts = Split-Path -Parent $Python
$env:Path = $PythonScripts + ";" + $env:Path

& $Python @CheckArgs 2>$null
if ($LASTEXITCODE -ne 0) {
    Write-Host "Installing faster-whisper..." -ForegroundColor Yellow
    & $Python -m pip install -r (Join-Path $Root "requirements.txt")
}

& $Python (Join-Path $Root "app.py") --config (Join-Path $Root "config.json") @args
