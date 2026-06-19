# TRADER 001 - Professional Launcher v2.0
$BOT_DIR  = Split-Path -Parent $MyInvocation.MyCommand.Path
$VENV_PY  = Join-Path $BOT_DIR "venv\Scripts\python.exe"
$MAIN_PY  = Join-Path $BOT_DIR "main.py"
$PORT     = 8000
$DASH_URL = "http://localhost:$PORT"
$ANA_URL  = "http://localhost:$PORT/analytics"

# Janela
$Host.UI.RawUI.WindowTitle = "Trader 001"
$Host.UI.RawUI.BackgroundColor = "Black"
$Host.UI.RawUI.ForegroundColor = "White"
try {
    $Host.UI.RawUI.BufferSize = New-Object System.Management.Automation.Host.Size(84, 3000)
    $Host.UI.RawUI.WindowSize = New-Object System.Management.Automation.Host.Size(84, 36)
} catch {}
Clear-Host

function Write-Check {
    param([string]$Label, [string]$Value, [bool]$Ok)
    $icon  = if ($Ok) { "[ OK ]" } else { "[ERRO]" }
    $color = if ($Ok) { "Green" } else { "Red" }
    $padLabel = $Label.PadRight(24)
    Write-Host "    $padLabel" -NoNewline -ForegroundColor DarkGray
    Write-Host $icon -NoNewline -ForegroundColor $color
    Write-Host "  $Value" -ForegroundColor White
}

function Write-Line {
    Write-Host ("    " + ("-" * 72)) -ForegroundColor DarkGray
}

Clear-Host
Write-Host ""
Write-Host "    +======================================================================+" -ForegroundColor DarkGreen
Write-Host "    |                                                                      |" -ForegroundColor DarkGreen
Write-Host "    |   >> TRADER 001  -  Binance Futures Bot                             |" -ForegroundColor Green
Write-Host "    |      ML | DCA | Monte Carlo | Walk-Forward | V6 Engine              |" -ForegroundColor DarkGray
Write-Host "    |                                                                      |" -ForegroundColor DarkGreen
Write-Host "    +======================================================================+" -ForegroundColor DarkGreen
Write-Host ""
Write-Host "    PRE-FLIGHT CHECKS" -ForegroundColor DarkGray
Write-Line
Write-Host ""

# 1. Porta 8000
$portProc = $null
try {
    $conn = Get-NetTCPConnection -LocalPort $PORT -State Listen -ErrorAction SilentlyContinue
    if ($conn) { $portProc = $conn | Select-Object -First 1 -ExpandProperty OwningProcess }
} catch {}

if ($portProc) {
    $procName = (Get-Process -Id $portProc -ErrorAction SilentlyContinue).Name
    Write-Check "Porta $PORT" "Ocupada por '$procName' (PID $portProc) - encerrando..." $false
    try {
        Stop-Process -Id $portProc -Force -ErrorAction Stop
        Start-Sleep -Milliseconds 900
        Write-Check "Porta $PORT" "Liberada com sucesso." $true
    } catch {
        Write-Check "Porta $PORT" "Nao foi possivel liberar." $false
    }
} else {
    Write-Check "Porta $PORT" "Disponivel" $true
}

# 2. Venv
$venvOk = Test-Path $VENV_PY
$venvMsg = if ($venvOk) { $VENV_PY } else { "NAO encontrado - execute: python -m venv venv" }
Write-Check "Ambiente virtual" $venvMsg $venvOk

# 3. Python version
if ($venvOk) {
    $pyVer = (& $VENV_PY --version 2>&1).ToString().Trim()
    Write-Check "Python" $pyVer $true
}

# 4. main.py
$mainOk = Test-Path $MAIN_PY
Write-Check "main.py" $(if ($mainOk) { "Encontrado" } else { "NAO encontrado em $BOT_DIR" }) $mainOk

# 5. Modulos criticos
if ($venvOk) {
    $modScript = @"
import sys
mods = ['fastapi','uvicorn','binance','apscheduler','joblib','sklearn','xgboost','websockets']
missing = []
for m in mods:
    try: __import__(m)
    except: missing.append(m)
print('OK' if not missing else 'FALTANDO: ' + ', '.join(missing))
"@
    $modCheck = (& $VENV_PY -c $modScript 2>&1).ToString().Trim()
    $modsOk = $modCheck -eq "OK"
    Write-Check "Dependencias" $modCheck $modsOk
    if (-not $modsOk) {
        Write-Host "    Instalando dependencias faltando..." -ForegroundColor Yellow
        & $VENV_PY -m pip install joblib scikit-learn xgboost websockets -q
        Write-Check "Dependencias" "Instaladas. Reinicie o launcher." $true
    }
}

# 6. Ultimo modo
$modeFile = Join-Path $BOT_DIR ".last_mode"
$lastMode = if (Test-Path $modeFile) { (Get-Content $modeFile -Raw).Trim() } else { "SINAIS (padrao)" }
Write-Check "Ultimo modo" $lastMode $true

# 7. Timestamp
Write-Check "Iniciando em" (Get-Date -Format "dd/MM/yyyy  HH:mm:ss") $true

Write-Host ""
Write-Line
Write-Host ""

# Aborta se falha critica
if (-not $venvOk -or -not $mainOk) {
    Write-Host "    [ERRO CRITICO] Ambiente incompleto. Bot nao pode iniciar." -ForegroundColor Red
    Write-Host ""
    Read-Host "    Pressione ENTER para sair"
    exit 1
}

# Countdown
Write-Host "    Dashboard : $DASH_URL" -ForegroundColor Cyan
Write-Host "    Analytics : $ANA_URL" -ForegroundColor Cyan
Write-Host ""

for ($i = 3; $i -ge 1; $i--) {
    Write-Host "`r    Iniciando em $i s... (Ctrl+C para cancelar)   " -NoNewline -ForegroundColor Yellow
    Start-Sleep -Seconds 1
}
Write-Host "`r    Abrindo dashboard...                             " -ForegroundColor Green
Start-Sleep -Milliseconds 400
Start-Process $DASH_URL

# Tela de execucao
Clear-Host
Write-Host ""
Write-Host "    +======================================================================+" -ForegroundColor DarkGreen
Write-Host "    |   TRADER 001  -  ONLINE                                             |" -ForegroundColor Green
Write-Host "    +----------------------------------------------------------------------+" -ForegroundColor DarkGreen
Write-Host "    |                                                                      |" -ForegroundColor DarkGreen
Write-Host "    |   Dashboard  ->  http://localhost:8000                               |" -ForegroundColor Cyan
Write-Host "    |   Analytics  ->  http://localhost:8000/analytics                    |" -ForegroundColor Cyan
Write-Host "    |   Sinais     ->  http://localhost:8000/signals/latest                |" -ForegroundColor Cyan
Write-Host "    |   Risco      ->  http://localhost:8000/risk/metrics                  |" -ForegroundColor Cyan
Write-Host "    |                                                                      |" -ForegroundColor DarkGreen
Write-Host "    |   Pressione Ctrl+C para encerrar                                     |" -ForegroundColor DarkGray
Write-Host "    +======================================================================+" -ForegroundColor DarkGreen
Write-Host ""

Set-Location $BOT_DIR
& $VENV_PY $MAIN_PY

# Encerrado
Write-Host ""
Write-Host "    +======================================================================+" -ForegroundColor DarkYellow
Write-Host "    |   Bot encerrado.                                                     |" -ForegroundColor Yellow
Write-Host "    +======================================================================+" -ForegroundColor DarkYellow
Write-Host ""
Read-Host "    Pressione ENTER para fechar"