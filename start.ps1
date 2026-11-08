$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$python = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python)) {
    throw 'Missing .venv. Run: py -3 -m venv .venv; .\.venv\Scripts\python.exe -m pip install -r requirements.txt'
}
& $python (Join-Path $PSScriptRoot 'doctor.py')
if ($LASTEXITCODE -ne 0) {
    exit $LASTEXITCODE
}
& $python -u (Join-Path $PSScriptRoot '_colab_local_gateway.py')
exit $LASTEXITCODE
