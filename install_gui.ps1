# Установщик Nightlord агента с окном: 1) Python 3.12  2) пакеты  3) ярлык и запуск  4) сборка в exe.
# Запуск: двойной щелчок по «Установщик.bat». Работает на встроенном PowerShell Windows 10/11, Python для запуска не нужен.
# Файлы voice_agent.py, check_deps.py, build_exe.py, install_python.ps1 должны лежать в той же папке.
param([switch]$SelfTest)        # -SelfTest: только проверка логики без окна (для разработки)
$ErrorActionPreference = "Stop"
$Here = Split-Path -Parent $MyInvocation.MyCommand.Path
$env:PYTHONIOENCODING = "utf-8"
$env:PYTHONUNBUFFERED = "1"

# ---------------------------------------------------------------- логика (без окна)
function Refresh-Path {
    $m = [Environment]::GetEnvironmentVariable("Path", "Machine"); $u = [Environment]::GetEnvironmentVariable("Path", "User")
    if ($m -or $u) { $env:Path = "$m;$u" }
}

$script:gui = $false
$script:vpnLib = Join-Path $Here "install_vpn.ps1"
if (Test-Path $script:vpnLib) { . $script:vpnLib; $script:vpnAvail = $true } else { $script:vpnAvail = $false }
$script:depsTimeoutMs = 90000
function Run-Quick([string]$exe, [string[]]$argList, [int]$timeoutMs) {
    # Запускает короткую команду и ждёт не дольше timeoutMs; окно за это время остаётся живым (DoEvents). -> @{ Code; Text; TimedOut }
    $job = Start-Job2 $exe $argList $null
    $sw = [Diagnostics.Stopwatch]::StartNew()
    while (-not $job.Proc.HasExited -and $sw.ElapsedMilliseconds -lt $timeoutMs) {
        Start-Sleep -Milliseconds 80
        if ($script:gui) { [System.Windows.Forms.Application]::DoEvents() }
    }
    $timedOut = -not $job.Proc.HasExited
    if ($timedOut) { try { $job.Proc.Kill() } catch {}; Start-Sleep -Milliseconds 200 }
    $out = Read-Tail $job "Out"; $err = Read-Tail $job "Err"
    $code = $(if ($timedOut) { -1 } else { $job.Proc.ExitCode })
    Job-Cleanup $job
    return @{ Code = $code; Text = $out; Err = $err; TimedOut = $timedOut }
}

function Find-Python312 {
    foreach ($c in @("$env:LOCALAPPDATA\Programs\Python\Python312\python.exe", "$env:ProgramFiles\Python312\python.exe")) {
        if ($c -and (Test-Path $c)) { return $c }
    }
    try {
        $r = Run-Quick "py" @("-3.12", "-c", "import sys;print(sys.executable)") 15000
        if (-not $r.TimedOut -and $r.Code -eq 0 -and $r.Text) {
            $p = ($r.Text -split "`r?`n" | Where-Object { $_.Trim() } | Select-Object -First 1).Trim()
            if ($p -and (Test-Path $p)) { return $p }
        }
    } catch {}
    return $null
}

function Get-Deps([string]$py) {
    # Состояние пакетов от check_deps.py --json; $null, если не получилось. Ждёт не дольше 90 с и не замораживает окно.
    $deps = Join-Path $Here "check_deps.py"
    $script:depsErr = ""
    if (-not $py -or -not (Test-Path $deps)) { $script:depsErr = "нет check_deps.py рядом с установщиком"; return $null }
    try {
        $r = Run-Quick $py @($deps, "--json") $script:depsTimeoutMs
        if ($r.TimedOut) {
            $tail = ($r.Err.Trim() -split "`r?`n" | Select-Object -Last 14) -join " | "
            $script:depsErr = "проверка не уложилась в $([int]($script:depsTimeoutMs / 1000)) с и была остановлена. Последнее, что она делала: $tail"
            return $null
        }
        if ($r.Code -ne 0) { $script:depsErr = "check_deps.py завершился с кодом $($r.Code): " + ($r.Err.Trim() -replace "\s+", " "); return $null }
        return $r.Text | ConvertFrom-Json
    } catch { $script:depsErr = $_.Exception.Message; return $null }
}

function Quote-Arg([string]$a) { if ($a -match '[\s"]') { return '"' + ($a -replace '"', '\"') + '"' } else { return $a } }

function Start-Job2([string]$exe, [string[]]$argList, [string]$workDir) {
    # Запускает процесс в фоне, вывод — в временные файлы; Read-Job2 забирает новое, Job-Finished проверяет конец.
    $tag = [Guid]::NewGuid().ToString("N").Substring(0, 8)
    $o = Join-Path ([IO.Path]::GetTempPath()) "nl_$tag.out"; $e = Join-Path ([IO.Path]::GetTempPath()) "nl_$tag.err"
    $line = ($argList | ForEach-Object { Quote-Arg $_ }) -join " "
    $sp = @{ FilePath = $exe; RedirectStandardOutput = $o; RedirectStandardError = $e; PassThru = $true }
    if ($IsWindows -ne $false) { $sp.WindowStyle = "Hidden" }
    if ($line) { $sp.ArgumentList = $line }
    if ($workDir) { $sp.WorkingDirectory = $workDir }
    $proc = Start-Process @sp
    $null = $proc.Handle                      # без этого ExitCode иногда пустой
    return @{ Proc = $proc; Out = $o; Err = $e; OutPos = 0L; ErrPos = 0L
              DecO = (New-Object System.Text.UTF8Encoding($false)).GetDecoder(); DecE = (New-Object System.Text.UTF8Encoding($false)).GetDecoder() }
}

function Read-Tail($job, [string]$which) {
    $path = $job[$which]; $posKey = $which + "Pos"; $dec = $job["Dec" + $which.Substring(0, 1)]
    if (-not (Test-Path $path)) { return "" }
    $fs = [IO.File]::Open($path, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::ReadWrite)
    try {
        $fs.Seek($job[$posKey], [IO.SeekOrigin]::Begin) | Out-Null
        $buf = New-Object byte[] 65536; $sb = New-Object System.Text.StringBuilder
        while (($n = $fs.Read($buf, 0, $buf.Length)) -gt 0) {
            $chars = New-Object char[] ($n + 4); $c = $dec.GetChars($buf, 0, $n, $chars, 0)
            [void]$sb.Append($chars, 0, $c); $job[$posKey] += $n
        }
        return $sb.ToString()
    } finally { $fs.Dispose() }
}

function Read-Job2($job) {
    $t = (Read-Tail $job "Out") + (Read-Tail $job "Err")
    $t = $t -replace "\x1b\[[0-9;?]*[A-Za-z]", "" -replace "\r(?!\n)", "`n" -replace "\r", ""
    return $t
}

function Job-Finished($job) { return $job.Proc.HasExited }
function Job-Code($job) { return $job.Proc.ExitCode }
function Job-Cleanup($job) { foreach ($f in @($job.Out, $job.Err)) { Remove-Item $f -ErrorAction SilentlyContinue } }

function Get-Pyw([string]$py) { $w = Join-Path (Split-Path -Parent $py) "pythonw.exe"; if (Test-Path $w) { return $w } else { return $py } }

function Get-ExeResult {
    foreach ($c in @((Join-Path $Here "dist\NightlordAgent\NightlordAgent.exe"), (Join-Path $Here "dist\NightlordAgent.exe"))) {
        if (Test-Path $c) { return $c }
    }
    return $null
}

function Make-Shortcut([string]$py, [bool]$console) {
    $desk = [Environment]::GetFolderPath("Desktop")
    $lnk = Join-Path $desk "Nightlord агент.lnk"
    $sh = New-Object -ComObject WScript.Shell
    $s = $sh.CreateShortcut($lnk)
    $s.TargetPath = $(if ($console) { $py } else { Get-Pyw $py })
    $s.Arguments = '"' + (Join-Path $Here "voice_agent.py") + '"'
    $s.WorkingDirectory = $Here
    $ico = Join-Path $Here "nightlord.ico"
    if (Test-Path $ico) { $s.IconLocation = $ico }
    $s.Description = "Nightlord агент"
    $s.Save()
    return $lnk
}

if ($SelfTest) { return }

# ---------------------------------------------------------------- окно
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
[System.Windows.Forms.Application]::EnableVisualStyles()
$script:gui = $true

try {
$script:py = $null
$script:deps = $null
$script:queue = New-Object System.Collections.ArrayList     # очередь команд: @{Title; Exe; Args; Dir; OnDone}
$script:job = $null
$script:cur = $null

$form = New-Object System.Windows.Forms.Form
$form.Text = "Установщик Nightlord агента"
$form.StartPosition = "CenterScreen"
$form.ClientSize = New-Object System.Drawing.Size(780, 924)
$form.MinimumSize = New-Object System.Drawing.Size(780, 784)
$form.Font = New-Object System.Drawing.Font("Segoe UI", 9.5)
$ico = Join-Path $Here "nightlord.ico"
if (Test-Path $ico) { try { $form.Icon = New-Object System.Drawing.Icon($ico) } catch {} }

$script:dy = 0
function New-Group($text, $y, $h) {
    $g = New-Object System.Windows.Forms.GroupBox
    $g.Text = $text; $g.Location = New-Object System.Drawing.Point(12, ($y + $script:dy)); $g.Size = New-Object System.Drawing.Size(756, $h)
    $g.Anchor = "Top,Left,Right"
    $form.Controls.Add($g); return $g
}
function New-Btn($parent, $text, $x, $y, $w) {
    $b = New-Object System.Windows.Forms.Button
    $b.Text = $text; $b.Location = New-Object System.Drawing.Point($x, $y); $b.Size = New-Object System.Drawing.Size($w, 30)
    $parent.Controls.Add($b); return $b
}
function New-Lbl($parent, $text, $x, $y, $w, $h) {
    $l = New-Object System.Windows.Forms.Label
    $l.Text = $text; $l.Location = New-Object System.Drawing.Point($x, $y); $l.Size = New-Object System.Drawing.Size($w, $h)
    $parent.Controls.Add($l); return $l
}
function New-Chk($parent, $text, $x, $y, $w) {
    $c = New-Object System.Windows.Forms.CheckBox
    $c.Text = $text; $c.Location = New-Object System.Drawing.Point($x, $y); $c.Size = New-Object System.Drawing.Size($w, 24)
    $parent.Controls.Add($c); return $c
}

# --- 0. доступ к интернету (VPN)
$g0 = New-Group "Доступ к интернету (если python.org, PyPI или GitHub не открываются)" 4 128
$lblV = New-Lbl $g0 "Ссылка vless:// (vmess, trojan, ss), адрес подписки или готовый HTTP-прокси 127.0.0.1:10809:" 14 22 726 20
$txtV = New-Object System.Windows.Forms.TextBox
$txtV.Location = New-Object System.Drawing.Point(14, 44); $txtV.Size = New-Object System.Drawing.Size(560, 26); $txtV.UseSystemPasswordChar = $true
$g0.Controls.Add($txtV)
$chkShow = New-Chk $g0 "показать" 584 44 150
$btnVc = New-Btn $g0 "Подключить" 14 68 130
$btnVf = New-Btn $g0 "Найти прокси / клиент" 152 68 190
$btnVa = New-Btn $g0 "Взять из агента" 350 68 150
$btnVd = New-Btn $g0 "Отключить" 508 68 120
$lblVs = New-Lbl $g0 "Не подключено (нужно только если сайты не открываются). Можно вписать просто номер порта клиента, например 10809." 14 102 726 20
$lblVs.ForeColor = [System.Drawing.Color]::Gray
$g0.Size = New-Object System.Drawing.Size(756, 128)
$script:dy = 130

# --- 1. Python
$g1 = New-Group "1. Python 3.12" 10 78
$lblPy = New-Lbl $g1 "Проверяю…" 14 26 520 40
$btnPy = New-Btn $g1 "Установить Python 3.12" 548 26 192

# --- 2. пакеты
$g2 = New-Group "2. Пакеты для агента" 94 278
$clb = New-Object System.Windows.Forms.CheckedListBox
$clb.Location = New-Object System.Drawing.Point(14, 24); $clb.Size = New-Object System.Drawing.Size(726, 190)
$clb.CheckOnClick = $true; $clb.IntegralHeight = $false; $clb.Anchor = "Top,Left,Right"
$g2.Controls.Add($clb)
$script:depMap = New-Object System.Collections.ArrayList
$clb.Add_ItemCheck({ param($s, $e) $m = $script:depMap; if ($e.Index -lt $m.Count -and -not $m[$e.Index]) { $e.NewValue = $e.CurrentValue } })    # заголовки разделов не отмечаются
$btnAll = New-Btn $g2 "Отметить недостающее" 14 222 190
$btnRe = New-Btn $g2 "Проверить заново" 212 222 160
$btnDeps = New-Btn $g2 "Установить отмеченное" 548 222 192
$lblDeps = New-Lbl $g2 "" 14 254 726 20
$lblDeps.ForeColor = [System.Drawing.Color]::Gray

# --- 3. запуск
$g3 = New-Group "3. Запуск" 380 84
$chkCon = New-Chk $g3 "Ярлык с окном консоли (видны ошибки)" 14 22 330
$btnLnk = New-Btn $g3 "Создать ярлык на рабочем столе" 14 48 260
$btnRun = New-Btn $g3 "Запустить агента" 284 48 170

# --- 4. exe
$g4 = New-Group "4. Сборка в exe (необязательно)" 472 110
$chkOne = New-Chk $g4 "Один файл (медленнее стартует, антивирусы ругаются чаще)" 14 22 460
$chkFull = New-Chk $g4 "Включить в exe необязательные пакеты (если установлены)" 14 46 460
$btnExe = New-Btn $g4 "Собрать exe" 14 72 170
$btnDir = New-Btn $g4 "Открыть папку с exe" 194 72 190
$lblExe = New-Lbl $g4 "Сборка занимает несколько минут." 396 78 350 22
$lblExe.ForeColor = [System.Drawing.Color]::Gray

# --- журнал
$bar = New-Object System.Windows.Forms.ProgressBar
$bar.Style = "Marquee"; $bar.MarqueeAnimationSpeed = 0; $bar.Location = New-Object System.Drawing.Point(12, 720)
$bar.Size = New-Object System.Drawing.Size(756, 8); $bar.Anchor = "Top,Left,Right"
$form.Controls.Add($bar)
$log = New-Object System.Windows.Forms.TextBox
$log.Multiline = $true; $log.ReadOnly = $true; $log.ScrollBars = "Vertical"; $log.Font = New-Object System.Drawing.Font("Consolas", 9)
$log.Location = New-Object System.Drawing.Point(12, 734); $log.Size = New-Object System.Drawing.Size(756, 184)
$log.Anchor = "Top,Bottom,Left,Right"; $log.BackColor = [System.Drawing.Color]::White
$form.Controls.Add($log)

function Log([string]$t) {
    if (-not $t) { return }
    $log.AppendText(($t -replace "`r?`n", "`r`n")); if (-not $t.EndsWith("`n")) { $log.AppendText("`r`n") }
}
function Apply-Enabled {
    $busy = [bool]($script:job -or $script:queue.Count)
    $havePy = [bool]$script:py
    $btnPy.Enabled = -not $busy; $btnRe.Enabled = -not $busy
    foreach ($b in @($btnAll, $btnDeps, $btnLnk, $btnRun, $btnExe)) { $b.Enabled = ((-not $busy) -and $havePy) }
    $bar.MarqueeAnimationSpeed = $(if ($busy -or $script:vpnBusy) { 30 } else { 0 })
    $vp = [bool]$script:vpnAvail
    $btnVc.Enabled = ($vp -and -not $busy -and -not $script:vpnBusy -and -not $script:vpnOn)
    $btnVa.Enabled = $btnVc.Enabled; $btnVf.Enabled = $btnVc.Enabled; $txtV.Enabled = (-not $script:vpnOn -and -not $script:vpnBusy)
    $btnVd.Enabled = ($vp -and $script:vpnOn -and -not $busy)
}
function Set-Busy([bool]$on) { Apply-Enabled }

# ---- очередь задач: по таймеру забирает вывод и запускает следующий шаг
$timer = New-Object System.Windows.Forms.Timer
$timer.Interval = 400
function Start-Next {
    if ($script:queue.Count -eq 0) { Set-Busy $false; $script:cur = $null; $script:job = $null; return }
    $item = $script:queue[0]; $script:queue.RemoveAt(0)
    $script:cur = $item
    Log "`r`n>>> $($item.Title)"
    try {
        $script:job = Start-Job2 $item.Exe $item.Args $item.Dir
    } catch {
        Log "Не удалось запустить: $($_.Exception.Message)"
        $script:job = $null
        if ($item.OnDone) { & $item.OnDone 1 }
        Start-Next; return
    }
    $timer.Start()
}
$timer.Add_Tick({
    if ($script:refreshing -or $script:inTick) { return }
    $script:inTick = $true
    try {
    if (-not $script:job) { $timer.Stop(); return }
    try { Log (Read-Job2 $script:job) } catch {}
    if (Job-Finished $script:job) {
        $timer.Stop()
        Start-Sleep -Milliseconds 150
        try { Log (Read-Job2 $script:job) } catch {}
        $code = Job-Code $script:job
        $done = $script:cur.OnDone
        Job-Cleanup $script:job
        $script:job = $null
        if ($code -ne 0) { Log "Завершилось с кодом $code" }
        if ($done) { try { & $done $code } catch { Log "Ошибка: $($_.Exception.Message)" } }
        if (-not $script:job) { Start-Next }
    }
    } finally { $script:inTick = $false }
})
function Enqueue($title, $exe, $argList, $dir, $onDone) {
    [void]$script:queue.Add(@{ Title = $title; Exe = $exe; Args = $argList; Dir = $dir; OnDone = $onDone })
    if (-not $script:job) { Set-Busy $true; Start-Next }
}

# ---- обновление состояния
function Refresh-All {
    if ($script:refreshing) { return }
    $script:refreshing = $true
    try { Refresh-All-Inner } finally { $script:refreshing = $false; Apply-Enabled }
}
function Refresh-All-Inner {
    Refresh-Path
    $clb.Items.Clear(); [void]$clb.Items.Add("Проверяю Python и пакеты… (до минуты, окно можно двигать)")
    $lblPy.Text = "Ищу Python 3.12…"; $lblPy.ForeColor = [System.Drawing.Color]::DimGray
    $btnPy.Enabled = $false; $btnRe.Enabled = $false; $bar.MarqueeAnimationSpeed = 30
    $form.Refresh()
    $sw = [Diagnostics.Stopwatch]::StartNew()
    $script:py = Find-Python312
    if ($script:py) {
        $lblPy.Text = "Python 3.12 установлен: $script:py"; $lblPy.ForeColor = [System.Drawing.Color]::DarkGreen
        $btnPy.Text = "Переустановить / обновить"
    } else {
        $lblPy.Text = "Python 3.12 не найден. Нажмите кнопку справа: он скачается и установится для вашего пользователя (права администратора не нужны)."
        $lblPy.ForeColor = [System.Drawing.Color]::DarkRed
        $btnPy.Text = "Установить Python 3.12"
    }
    $clb.Items.Clear(); $script:deps = $null; $script:depMap = New-Object System.Collections.ArrayList; $lblDeps.Text = ""
    Apply-Enabled
    if (-not $script:py) { [void]$clb.Items.Add("Сначала установите Python (шаг 1)"); return }
    $clb.Items.Clear(); [void]$clb.Items.Add("Проверяю пакеты…"); $form.Refresh()
    $d = Get-Deps $script:py
    $clb.Items.Clear()
    if (-not $d) { [void]$clb.Items.Add("Не удалось получить список пакетов: $script:depsErr"); Log "Проверка пакетов: $script:depsErr"; return }
    Log ("Проверка заняла {0:N1} с" -f $sw.Elapsed.TotalSeconds)
    $script:deps = $d
    $script:depMap = New-Object System.Collections.ArrayList
    foreach ($grp in @(@{ Req = $true; Title = "ОБЯЗАТЕЛЬНЫЕ — без них агент не запустится" }, @{ Req = $false; Title = "НЕОБЯЗАТЕЛЬНЫЕ — добавляют возможности, можно поставить позже" })) {
        $items = @($d.packages | Where-Object { [bool]$_.required -eq $grp.Req })
        if (-not $items.Count) { continue }
        $have = @($items | Where-Object { $_.installed }).Count
        [void]$clb.Items.Add("━━  $($grp.Title)  (установлено $have из $($items.Count))  ━━"); [void]$script:depMap.Add($null)
        foreach ($p in $items) {
            $state = $(if ($p.installed) { "[есть $($p.version)]" } else { "[НЕТ]" })
            $for = $(if ($p.plugins) { "  (для: $($p.plugins))" } else { "" })
            $idx = $clb.Items.Add(("{0}  {1}  —  {2}{3}" -f $state, $p.pip, $p.why, $for)); [void]$script:depMap.Add($p)
            if (-not $p.installed -and $p.default) { $clb.SetItemChecked($idx, $true) }
        }
    }
    $miss = @($d.packages | Where-Object { -not $_.installed -and $_.required }).Count
    $lblDeps.Text = "Python $($d.python)" + $(if ($d.tkinter) { "" } else { ", но НЕТ tkinter — переустановите Python (шаг 1)" }) +
        $(if ($miss) { ". Не хватает обязательных: $miss" } else { ". Все обязательные пакеты на месте" })
    if (Test-Path (Join-Path $Here "voice_agent.py")) { } else { $lblDeps.Text += ". ВНИМАНИЕ: voice_agent.py рядом не найден" }
}

# ---- VPN
$script:vpnOn = $false; $script:vpnBusy = $false; $script:scanState = $null; $script:vpnCore = $null; $script:vpnState = $null; $script:vpnPs = $null; $script:vpnLogged = 0
$chkShow.Add_CheckedChanged({ $txtV.UseSystemPasswordChar = -not $chkShow.Checked })
$vpnTimer = New-Object System.Windows.Forms.Timer; $vpnTimer.Interval = 400
$vpnTimer.Add_Tick({
    $st = $script:vpnState; if (-not $st) { $vpnTimer.Stop(); return }
    while ($script:vpnLogged -lt $st.Log.Count) { Log $st.Log[$script:vpnLogged]; $script:vpnLogged++ }
    $lblVs.Text = [string]$st.Status
    if (-not $st.Done) { return }
    $vpnTimer.Stop()
    try { $script:vpnPs.EndInvoke($script:vpnAsync) } catch {}
    try { $script:vpnPs.Runspace.Close(); $script:vpnPs.Dispose() } catch {}
    $script:vpnBusy = $false
    if ($st.Error) {
        $lblVs.Text = "Не подключилось"; $lblVs.ForeColor = [System.Drawing.Color]::DarkRed
        Log "VPN: $($st.Error)"
    } else {
        $script:vpnOn = $true; $script:vpnCore = $st.Core; $script:vpnUrl = $st.Proxy; Set-ProxyEnv $st.Proxy
        $lblVs.Text = "Подключено: $($st.Proxy)"; $lblVs.ForeColor = [System.Drawing.Color]::DarkGreen
        Log "VPN подключён. Скачивание Python и пакетов пойдёт через $($st.Proxy). Теперь нажмите «Проверить заново»."
    }
    $script:vpnState = $null; Apply-Enabled
})
$btnVc.Add_Click({
    $text = $txtV.Text.Trim()
    if (-not $text) { [System.Windows.Forms.MessageBox]::Show("Вставьте ссылку vless://…, адрес подписки или адрес HTTP-прокси.", "Установщик") | Out-Null; return }
    $script:vpnBusy = $true; $lblVs.ForeColor = [System.Drawing.Color]::DimGray; $lblVs.Text = "Подключаюсь…"; Apply-Enabled
    $st = [hashtable]::Synchronized(@{ Status = "Подключаюсь…"; Log = [System.Collections.ArrayList]::Synchronized((New-Object System.Collections.ArrayList)); Done = $false; Error = ""; Proxy = ""; Core = $null })
    $script:vpnState = $st; $script:vpnLogged = 0
    $rs = [RunspaceFactory]::CreateRunspace(); $rs.ApartmentState = "STA"; $rs.Open()
    $ps = [PowerShell]::Create(); $ps.Runspace = $rs
    [void]$ps.AddScript({ param($lib, $here, $text, $st) . $lib; Connect-Vpn $here $text $st }).AddArgument($script:vpnLib).AddArgument($Here).AddArgument($text).AddArgument($st)
    $script:vpnPs = $ps; $script:vpnAsync = $ps.BeginInvoke(); $vpnTimer.Start()
})
$btnVf.Add_Click({
    $script:vpnBusy = $true; $lblVs.ForeColor = [System.Drawing.Color]::DimGray; $lblVs.Text = "Ищу прокси на этом ПК…"; Apply-Enabled
    $st = [hashtable]::Synchronized(@{ Status = "Ищу…"; Log = [System.Collections.ArrayList]::Synchronized((New-Object System.Collections.ArrayList)); Done = $false; Error = ""; Result = @() })
    $script:scanState = $st; $script:scanLogged = 0
    $rs = [RunspaceFactory]::CreateRunspace(); $rs.ApartmentState = "STA"; $rs.Open()
    $ps = [PowerShell]::Create(); $ps.Runspace = $rs
    [void]$ps.AddScript({ param($lib, $st) . $lib; Scan-LocalProxies $st }).AddArgument($script:vpnLib).AddArgument($st)
    $script:scanPs = $ps; $script:scanAsync = $ps.BeginInvoke(); $scanTimer.Start()
})
function Pick-Proxy($items) {
    $f = New-Object System.Windows.Forms.Form
    $f.Text = "Найденные прокси"; $f.StartPosition = "CenterParent"; $f.ClientSize = New-Object System.Drawing.Size(520, 250); $f.FormBorderStyle = "FixedDialog"
    $f.MaximizeBox = $false; $f.MinimizeBox = $false; $f.Font = $form.Font
    $l = New-Object System.Windows.Forms.Label; $l.Text = "Выберите прокси для скачивания:"; $l.Location = New-Object System.Drawing.Point(12, 10); $l.Size = New-Object System.Drawing.Size(490, 22); $f.Controls.Add($l)
    $lb = New-Object System.Windows.Forms.ListBox; $lb.Location = New-Object System.Drawing.Point(12, 36); $lb.Size = New-Object System.Drawing.Size(496, 160); $f.Controls.Add($lb)
    foreach ($i in $items) { [void]$lb.Items.Add("127.0.0.1:$($i.Port)  —  $($i.Client)") }
    $lb.SelectedIndex = 0
    $ok = New-Object System.Windows.Forms.Button; $ok.Text = "Выбрать"; $ok.Location = New-Object System.Drawing.Point(300, 208); $ok.Size = New-Object System.Drawing.Size(100, 30); $ok.DialogResult = "OK"; $f.Controls.Add($ok)
    $cn = New-Object System.Windows.Forms.Button; $cn.Text = "Отмена"; $cn.Location = New-Object System.Drawing.Point(408, 208); $cn.Size = New-Object System.Drawing.Size(100, 30); $cn.DialogResult = "Cancel"; $f.Controls.Add($cn)
    $f.AcceptButton = $ok; $f.CancelButton = $cn
    $r = $f.ShowDialog($form); $idx = $lb.SelectedIndex; $f.Dispose()
    if ($r -eq "OK" -and $idx -ge 0) { return $items[$idx] } else { return $null }
}
$scanTimer = New-Object System.Windows.Forms.Timer; $scanTimer.Interval = 300
$scanTimer.Add_Tick({
    $st = $script:scanState; if (-not $st) { $scanTimer.Stop(); return }
    while ($script:scanLogged -lt $st.Log.Count) { $script:scanLogged++ }
    $lblVs.Text = [string]$st.Status
    if (-not $st.Done) { return }
    $scanTimer.Stop()
    try { $script:scanPs.EndInvoke($script:scanAsync) } catch {}
    try { $script:scanPs.Runspace.Close(); $script:scanPs.Dispose() } catch {}
    $script:vpnBusy = $false; $script:scanState = $null
    $found = @($st.Result)
    $http = @($found | Where-Object { $_.Kind -eq "http" }); $other = @($found | Where-Object { $_.Kind -ne "http" })
    if ($st.Error) { $lblVs.Text = "Поиск не удался"; Log "Поиск прокси: $($st.Error)" }
    elseif ($http.Count) {
        $pick = $(if ($http.Count -eq 1) { $http[0] } else { Pick-Proxy $http })
        if ($pick) {
            $txtV.Text = "http://127.0.0.1:$($pick.Port)"; $lblVs.ForeColor = [System.Drawing.Color]::DarkGreen
            $lblVs.Text = "Нашёл: 127.0.0.1:$($pick.Port) ($($pick.Client)). Нажмите «Подключить»."
            Log "Нашёл рабочий HTTP-прокси: 127.0.0.1:$($pick.Port) — $($pick.Client)"
        } else { $lblVs.Text = "Выбор отменён." }
    } else {
        $lblVs.ForeColor = [System.Drawing.Color]::DarkRed; $lblVs.Text = "Рабочий HTTP-прокси не найден."
        $msg = "Рабочий HTTP-прокси на этом ПК не найден."
        if ($other.Count) { $msg += "`n`nНайдено, но не подходит:`n" + (($other | ForEach-Object { "127.0.0.1:$($_.Port) — $($_.Client): " + $(if ($_.Kind -eq "socks") { "это SOCKS; нужен HTTP (в v2rayN — порт 10809, в Hiddify/Clash/NekoBox включите «смешанный» порт)" } else { "это HTTP-прокси, но он не открывает python.org (VPN в клиенте выключен?)" }) }) -join "`n") }
        else { $msg += "`n`nЗапустите клиент (v2rayN, Hiddify, NekoBox, Clash…), включите в нём VPN и нажмите поиск ещё раз, либо вставьте ссылку vless:// или подписку." }
        [System.Windows.Forms.MessageBox]::Show($msg, "Поиск прокси") | Out-Null
    }
    Apply-Enabled
})
$btnVa.Add_Click({
    $l = Get-AgentVpnLink $Here
    if ($l) { $txtV.Text = $l; Log "Взял настройки VPN из плагина агента." } else { [System.Windows.Forms.MessageBox]::Show("В папке агента нет настроек VPN (plugins\data\vpn_proxy). Вставьте ссылку вручную.", "Установщик") | Out-Null }
})
$btnVd.Add_Click({
    Stop-XrayCore $script:vpnCore; $script:vpnCore = $null; Clear-ProxyEnv; $script:vpnOn = $false
    $lblVs.Text = "Отключено."; $lblVs.ForeColor = [System.Drawing.Color]::Gray; Log "VPN отключён."; Apply-Enabled
})

# ---- кнопки
$btnPy.Add_Click({
    $ps1 = Join-Path $Here "install_python.ps1"
    if (-not (Test-Path $ps1)) { [System.Windows.Forms.MessageBox]::Show("Рядом нет install_python.ps1", "Установщик") | Out-Null; return }
    Enqueue "Установка Python 3.12 (winget или установщик с python.org с проверкой хэша и подписи)" "powershell.exe" @("-NoProfile", "-ExecutionPolicy", "Bypass", "-File", $ps1, "-NoPrompt") $Here {
        param($code)
        Refresh-All
        if ($code -eq 0 -and $script:py) { Log "Python готов. Теперь шаг 2." } else { Log "Python установить не удалось. Скачайте вручную с python.org (версия 3.12, 64-бит)." }
    }
})
$btnRe.Add_Click({ Refresh-All })
$btnAll.Add_Click({
    if (-not $script:deps) { return }
    for ($i = 0; $i -lt $script:depMap.Count; $i++) {
        $p = $script:depMap[$i]; if (-not $p) { continue }
        $clb.SetItemChecked($i, [bool](-not $p.installed -and $p.default))
    }
})
$btnDeps.Add_Click({
    if (-not $script:deps) { return }
    $sel = @()
    for ($i = 0; $i -lt $script:depMap.Count; $i++) { if ($script:depMap[$i] -and $clb.GetItemChecked($i)) { $sel += $script:depMap[$i].pip } }
    if ($sel.Count -eq 0) { [System.Windows.Forms.MessageBox]::Show("Ничего не отмечено.", "Установщик") | Out-Null; return }
    $script:selPkgs = $sel
    $all = @("-m", "pip", "install", "--upgrade-strategy", "only-if-needed") + $sel
    Enqueue "Установка пакетов: $($sel -join ', ')" $script:py $all $Here {
        param($code)
        if ($code -ne 0) {
            Log "Общая установка не удалась — ставлю по одному, чтобы понять, какой пакет мешает."
            foreach ($one in $script:selPkgs) {
                Enqueue "pip install $one" $script:py @("-m", "pip", "install", "--upgrade-strategy", "only-if-needed", $one) $Here { param($c) }
            }
        }
        Enqueue "Проверка результата" $script:py @("-c", "print('проверка...')") $Here { param($c) Refresh-All }
    }
})
$btnLnk.Add_Click({
    if (-not (Test-Path (Join-Path $Here "voice_agent.py"))) { [System.Windows.Forms.MessageBox]::Show("Рядом нет voice_agent.py", "Установщик") | Out-Null; return }
    try { $l = Make-Shortcut $script:py $chkCon.Checked; Log "Ярлык создан: $l" }
    catch { Log "Не удалось создать ярлык: $($_.Exception.Message)" }
})
$btnRun.Add_Click({
    $va = Join-Path $Here "voice_agent.py"
    if (-not (Test-Path $va)) { [System.Windows.Forms.MessageBox]::Show("Рядом нет voice_agent.py", "Установщик") | Out-Null; return }
    $exe = $(if ($chkCon.Checked) { $script:py } else { Get-Pyw $script:py })
    if ($script:vpnOn) { Clear-ProxyEnv }            # агенту прокси установщика не передаём: у него свой плагин «VPN / прокси»
    Start-Process -FilePath $exe -ArgumentList ('"' + $va + '"') -WorkingDirectory $Here
    if ($script:vpnOn) { Set-ProxyEnv $script:vpnUrl }
    Log "Агент запущен."
})
$btnExe.Add_Click({
    $be = Join-Path $Here "build_exe.py"
    if (-not (Test-Path $be)) { [System.Windows.Forms.MessageBox]::Show("Рядом нет build_exe.py", "Установщик") | Out-Null; return }
    $flags = @(); if ($chkOne.Checked) { $flags += "--onefile" }; if ($chkFull.Checked) { $flags += "--full" }
    $has = $false
    try { & $script:py -c "import PyInstaller" 2>$null; $has = ($LASTEXITCODE -eq 0) } catch {}
    if (-not $has) { Enqueue "Установка PyInstaller" $script:py @("-m", "pip", "install", "pyinstaller") $Here { param($c) if ($c -ne 0) { Log "PyInstaller не поставился — сборка может не получиться." } } }
    Enqueue "Сборка exe (несколько минут; окно не завис)" $script:py (@($be) + $flags) $Here {
        param($code)
        $r = Get-ExeResult
        if ($code -eq 0 -and $r) { Log "Готово: $r"; $lblExe.Text = "Готово: $r"; $lblExe.ForeColor = [System.Drawing.Color]::DarkGreen }
        else { Log "Сборка не удалась (см. журнал выше)."; $lblExe.Text = "Сборка не удалась"; $lblExe.ForeColor = [System.Drawing.Color]::DarkRed }
    }
})
$btnDir.Add_Click({
    $r = Get-ExeResult
    if ($r) { Start-Process explorer.exe "/select,`"$r`"" }
    elseif (Test-Path (Join-Path $Here "dist")) { Start-Process explorer.exe (Join-Path $Here "dist") }
    else { [System.Windows.Forms.MessageBox]::Show("exe ещё не собран.", "Установщик") | Out-Null }
})

$form.Add_FormClosing({
    try { Stop-XrayCore $script:vpnCore } catch {}
    if ($script:job -and -not (Job-Finished $script:job)) {
        $a = [System.Windows.Forms.MessageBox]::Show("Идёт установка. Закрыть окно и прервать её?", "Установщик", "YesNo", "Question")
        if ($a -ne "Yes") { $_.Cancel = $true } else { try { $script:job.Proc.Kill() } catch {} }
    }
})
$form.Add_Shown({ Refresh-All; Log "Установщик Nightlord агента. Папка: $Here" })
[void]$form.ShowDialog()
} catch {
    [System.Windows.Forms.MessageBox]::Show("Ошибка установщика:`n$($_.Exception.Message)", "Установщик Nightlord агента") | Out-Null
}
