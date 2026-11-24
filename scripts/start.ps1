# Запуск signal-broadcast: контейнер Signal + веб-панель
# Использование: .\scripts\start.ps1 [-SkipDocker]
param(
    [switch]$SkipDocker,
    [switch]$Reload
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

if (-not $SkipDocker) {
    Write-Host "[1/4] Поднимаю контейнер signal-cli-rest-api..." -ForegroundColor Cyan
    docker compose up -d
    if ($LASTEXITCODE -ne 0) { throw "docker compose up завершился с ошибкой ($LASTEXITCODE). Docker Desktop запущен?" }
} else {
    Write-Host "[1/4] Пропускаю Docker (-SkipDocker)" -ForegroundColor DarkGray
}

Write-Host "[2/4] Жду готовности Signal API..." -ForegroundColor Cyan
$deadline = (Get-Date).AddSeconds(90)
do {
    try {
        $r = Invoke-WebRequest -Uri "http://127.0.0.1:8080/v1/health" -UseBasicParsing -TimeoutSec 3
        if ($r.StatusCode -in 200, 204) { Write-Host "      Signal API готов." -ForegroundColor Green; break }
    } catch { Start-Sleep -Seconds 2 }
} while ((Get-Date) -lt $deadline)

Write-Host "[3/4] Проверяю виртуальное окружение..." -ForegroundColor Cyan
if (-not (Test-Path "$root\.venv\Scripts\python.exe")) {
    py -3.13 -m venv .venv
    & "$root\.venv\Scripts\python.exe" -m pip install --upgrade pip
    & "$root\.venv\Scripts\python.exe" -m pip install -r requirements.txt
}

Write-Host "[4/4] Запускаю панель: http://127.0.0.1:8000" -ForegroundColor Cyan
$reloadFlag = if ($Reload) { @("--reload") } else { @() }
& "$root\.venv\Scripts\python.exe" -m uvicorn app.main:app --host 127.0.0.1 --port 8000 @reloadFlag
