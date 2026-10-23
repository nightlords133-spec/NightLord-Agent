# Установка Python 3.12 для Nightlord агента (Windows 10/11, без прав администратора).
# Порядок: 1) уже стоит? 2) winget  3) официальный установщик с python.org (проверяется хэш и подпись).
# Запуск: двойной щелчок по install_python.bat
param([switch]$NoPrompt)     # -NoPrompt: без вопросов и пауз (так его запускает установщик с окном)
$ErrorActionPreference = "Stop"
try { [Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false) } catch {}
function Pause-Exit([int]$code) {
    if (-not $NoPrompt) { Read-Host "Нажмите Enter, чтобы закрыть" | Out-Null }
    exit $code
}
$Url  = "https://www.python.org/ftp/python/3.12.10/python-3.12.10-amd64.exe"
$Md5  = "5eddb0b6f12c852725de071ae681dde4"      # с официальной страницы python.org/downloads/release/python-31210/
$Here = Split-Path -Parent $MyInvocation.MyCommand.Path

function Find-Python312 {
    # 1) лаунчер py
    try {
        $p = & py -3.12 -c "import sys;print(sys.executable)" 2>$null
        if ($LASTEXITCODE -eq 0 -and $p) { return ($p | Select-Object -First 1).Trim() }
    } catch {}
    # 2) обычные места установки
    foreach ($c in @("$env:LOCALAPPDATA\Programs\Python\Python312\python.exe", "$env:ProgramFiles\Python312\python.exe")) {
        if (Test-Path $c) { return $c }
    }
    return $null
}

function Refresh-Path {
    $env:Path = [Environment]::GetEnvironmentVariable("Path", "Machine") + ";" + [Environment]::GetEnvironmentVariable("Path", "User")
}

Write-Host ""
Write-Host "=== Установка Python 3.12 ===" -ForegroundColor Cyan
$py = Find-Python312
if ($py) {
    Write-Host "Python 3.12 уже установлен: $py" -ForegroundColor Green
} else {
    $done = $false
    # --- winget
    if (Get-Command winget -ErrorAction SilentlyContinue) {
        Write-Host "Ставлю через winget (может появиться окно подтверждения)..."
        try {
            & winget install -e --id Python.Python.3.12 --scope user --silent --accept-package-agreements --accept-source-agreements
        } catch { Write-Host "winget не сработал: $($_.Exception.Message)" -ForegroundColor Yellow }
        Refresh-Path
        $py = Find-Python312
        if ($py) { $done = $true }
    }
    # --- прямая загрузка
    if (-not $done) {
        Write-Host "Скачиваю установщик с python.org (около 26 МБ)..."
        $exe = Join-Path $env:TEMP "python-3.12.10-amd64.exe"
        try {
            [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
            Invoke-WebRequest -Uri $Url -OutFile $exe -UseBasicParsing
        } catch {
            Write-Host "Не удалось скачать: $($_.Exception.Message)" -ForegroundColor Red
            Write-Host "Скачайте вручную: $Url  (или включите VPN) и запустите скрипт ещё раз."
            Pause-Exit 1
        }
        $hash = (Get-FileHash -Algorithm MD5 -Path $exe).Hash.ToLower()
        $sig = Get-AuthenticodeSignature -FilePath $exe
        $sigOk = ($sig.Status -eq "Valid") -and ($sig.SignerCertificate.Subject -like "*Python Software Foundation*")
        if ($hash -ne $Md5 -or -not $sigOk) {
            Write-Host "Файл не прошёл проверку (хэш: $hash, подпись: $($sig.Status)). Установка отменена." -ForegroundColor Red
            Remove-Item $exe -ErrorAction SilentlyContinue
            Pause-Exit 1
        }
        Write-Host "Хэш и подпись Python Software Foundation в порядке. Устанавливаю..."
        $p = Start-Process -FilePath $exe -ArgumentList "/quiet", "InstallAllUsers=0", "PrependPath=1", "Include_launcher=1",
             "Include_pip=1", "Include_tcltk=1", "Include_test=0", "Include_doc=0" -Wait -PassThru
        Remove-Item $exe -ErrorAction SilentlyContinue
        if ($p.ExitCode -ne 0) {
            Write-Host "Установщик завершился с кодом $($p.ExitCode)." -ForegroundColor Red
            Pause-Exit 1
        }
        Refresh-Path
        $py = Find-Python312
    }
    if (-not $py) {
        Write-Host "Python 3.12 установлен, но не найден в этой сессии. Закройте окно и запустите скрипт ещё раз." -ForegroundColor Yellow
        Pause-Exit 1
    }
    Write-Host "Готово: $py" -ForegroundColor Green
}

& $py --version
if ($NoPrompt) { Write-Host "PYTHON_OK=$py"; exit 0 }
$ans = Read-Host "Установить пакеты для агента сейчас (check_deps.py --yes)? [Д/н]"
if ($ans -eq "" -or $ans -match "^(д|да|y|yes)$") {
    $deps = Join-Path $Here "check_deps.py"
    if (Test-Path $deps) { & $py $deps --yes } else { Write-Host "check_deps.py не найден рядом со скриптом." -ForegroundColor Yellow }
}
Write-Host ""
Write-Host "Запуск агента: start_agent.bat  (или: py -3.12 voice_agent.py)" -ForegroundColor Cyan
Pause-Exit 0
