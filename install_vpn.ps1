# VPN для установщика Nightlord агента: ссылка vless/vmess/trojan/ss или подписка -> ядро Xray -> локальный HTTP-прокси 127.0.0.1:порт.
# Логика повторяет плагин агента «VPN / прокси» (vpn_proxy). Подключается из install_gui.ps1 (dot-source), без окна.
# Ссылки и подписки нигде не сохраняются: конфиг пишется во временный файл и удаляется при отключении.

function Decode-B64([string]$s) {
    $s = ($s.Trim() -replace "-", "+" -replace "_", "/")
    $s += "=" * ((4 - $s.Length % 4) % 4)
    return [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($s))
}
function Url-Dec([string]$s) { if ($null -eq $s) { return "" }; return [Uri]::UnescapeDataString(($s -replace "\+", " ")) }
function Parse-Query([string]$q) {
    $h = @{}
    if ($q) { foreach ($p in ($q -split "&")) { if ($p) { $k, $v = $p -split "=", 2; if (-not $h.ContainsKey($k)) { $h[$k] = (Url-Dec $v) } } } }
    return $h
}
function Split-List([string]$v) { return @($v.Replace("%2C", ",") -split "," | Where-Object { $_ }) }

$script:NETS = @{ tcp = "tcp"; raw = "tcp"; ws = "ws"; grpc = "grpc"; httpupgrade = "httpupgrade"; xhttp = "xhttp"; splithttp = "xhttp" }

function Parse-VpnLink([string]$link) {
    $link = $link.Trim()
    if ($link -notmatch "^([a-zA-Z0-9]+)://") { throw "это не ссылка вида vless://… vmess://… trojan://… ss://…" }
    $proto = $Matches[1].ToLower()
    if ($proto -notin @("vless", "vmess", "trojan", "ss")) { throw "протокол ${proto}:// не поддерживается (нужны vless, vmess, trojan или ss)" }
    $ob = [ordered]@{ proto = $proto; name = ""; addr = ""; port = 0; id = ""; password = ""; method = ""; flow = ""; net = "tcp"; sec = "none"
                      sni = ""; fp = ""; alpn = @(); pbk = ""; sid = ""; spx = ""; path = ""; host = ""; service = ""; mode = ""; alter = 0; scy = "auto" }
    if ($proto -eq "vmess") {
        try { $j = (Decode-B64 (($link.Substring(8) -split "#")[0])) | ConvertFrom-Json } catch { throw "ссылка vmess:// повреждена" }
        $ob.name = [string]$j.ps; $ob.addr = [string]$j.add; $ob.port = [int]$j.port; $ob.id = [string]$j.id
        $ob.alter = [int]$j.aid; if ($j.scy) { $ob.scy = [string]$j.scy }
        $ob.path = [string]$j.path; $ob.host = [string]$j.host; $ob.sni = [string]$j.sni; $ob.fp = [string]$j.fp; $ob.alpn = @(Split-List ([string]$j.alpn))
        $net = $(if ($j.net) { ([string]$j.net).ToLower() } else { "tcp" })
        if (-not $script:NETS.ContainsKey($net) -or $script:NETS[$net] -eq "xhttp") { throw "транспорт «$net» для vmess не поддерживается" }
        $ob.net = $script:NETS[$net]
        if ($ob.net -eq "grpc") { $ob.service = $ob.path }
        if (([string]$j.tls).ToLower() -eq "tls") { $ob.sec = "tls" }
    } elseif ($proto -eq "ss") {
        $rest = $link.Substring(5); $frag = ""
        if ($rest.Contains("#")) { $i = $rest.IndexOf("#"); $frag = $rest.Substring($i + 1); $rest = $rest.Substring(0, $i) }
        $ob.name = Url-Dec $frag
        $query = ""; if ($rest.Contains("?")) { $i = $rest.IndexOf("?"); $query = $rest.Substring($i + 1); $rest = $rest.Substring(0, $i) }
        if ((Parse-Query $query).ContainsKey("plugin")) { throw "Shadowsocks с плагином не поддерживается" }
        try {
            if ($rest.Contains("@")) {
                $i = $rest.LastIndexOf("@"); $user = $rest.Substring(0, $i); $hp = $rest.Substring($i + 1)
                $ud = Url-Dec $user
                $cred = $(if ($ud.Contains(":")) { $ud } else { Decode-B64 $user })
            } else {
                $dec = Decode-B64 ($rest.TrimEnd("/")); $i = $dec.LastIndexOf("@"); $cred = $dec.Substring(0, $i); $hp = $dec.Substring($i + 1)
            }
            $i = $cred.IndexOf(":"); $ob.method = $cred.Substring(0, $i); $ob.password = $cred.Substring($i + 1)
            $hp = $hp.TrimEnd("/"); $i = $hp.LastIndexOf(":"); $ob.addr = $hp.Substring(0, $i).Trim("[", "]"); $ob.port = [int]$hp.Substring($i + 1)
        } catch { throw "ссылка ss:// повреждена" }
        if (-not $ob.method -or -not $ob.password) { throw "в ссылке ss:// нет метода или пароля" }
    } else {
        if ($link -notmatch "^[a-z0-9]+://([^@/?#]*)@(\[[^\]]+\]|[^:/?#]+)(?::(\d+))?/?(?:\?([^#]*))?(?:#(.*))?$") { throw "ссылка не читается (нет адреса сервера)" }
        $user = Url-Dec $Matches[1]; $ob.addr = $Matches[2].Trim("[", "]")
        $ob.port = $(if ($Matches[3]) { [int]$Matches[3] } else { 0 })
        $qs = Parse-Query $Matches[4]; $ob.name = Url-Dec $Matches[5]
        if ($proto -eq "vless") { $ob.id = $user } else { $ob.password = $user }
        $q = { param($k, $d = "") if ($qs.ContainsKey($k)) { return $qs[$k] } else { return $d } }
        $ob.flow = & $q "flow"
        $net = (& $q "type" "tcp"); if (-not $net) { $net = "tcp" }; $net = $net.ToLower()
        if (-not $script:NETS.ContainsKey($net)) { throw "транспорт «$net» не поддерживается (tcp, ws, grpc, httpupgrade, xhttp)" }
        $ob.net = $script:NETS[$net]
        $sec = (& $q "security" $(if ($proto -eq "trojan") { "tls" } else { "none" })); if (-not $sec) { $sec = "none" }; $sec = $sec.ToLower()
        if ($sec -notin @("none", "tls", "reality")) { throw "security=$sec не поддерживается (none, tls, reality)" }
        $ob.sec = $sec
        $ob.sni = $(if (& $q "sni") { & $q "sni" } else { & $q "peer" }); $ob.fp = & $q "fp"; $ob.alpn = @(Split-List (& $q "alpn"))
        $ob.pbk = & $q "pbk"; $ob.sid = & $q "sid"; $ob.spx = & $q "spx"; $ob.path = & $q "path"; $ob.host = & $q "host"
        $ob.service = & $q "serviceName"; $ob.mode = & $q "mode"
        if ($sec -eq "reality" -and -not $ob.pbk) { throw "в ссылке Reality нет публичного ключа (pbk)" }
    }
    if (-not $ob.addr -or $ob.port -lt 1 -or $ob.port -gt 65535) { throw "в ссылке не указан адрес или порт сервера" }
    if ($proto -in @("vless", "vmess") -and -not $ob.id) { throw "в ссылке нет UUID пользователя" }
    if ($proto -eq "trojan" -and -not $ob.password) { throw "в ссылке trojan:// нет пароля" }
    if (-not $ob.name) { $ob.name = "$($ob.addr):$($ob.port)" }
    return $ob
}

function Parse-Subscription([string]$text) {
    # -> массив @{ Name; Link } только из поддерживаемых протоколов
    $t = $text.Trim()
    if ($t -and -not ($t -split "`r?`n")[0].Contains("://")) { try { $t = Decode-B64 (($t -split "\s+") -join "") } catch {} }
    $out = @()
    foreach ($line in ($t -split "`r?`n")) {
        $line = $line.Trim()
        if ($line -notmatch "^(vless|vmess|trojan|ss)://") { continue }
        try { $out += @{ Name = (Parse-VpnLink $line).name; Link = $line } } catch { continue }
    }
    return $out
}

function New-XrayConfig($ob, [int]$port) {
    $p = $ob.proto
    if ($p -eq "vless") {
        $user = [ordered]@{ id = $ob.id; encryption = "none" }; if ($ob.flow) { $user.flow = $ob.flow }
        $out = [ordered]@{ protocol = "vless"; settings = [ordered]@{ vnext = @([ordered]@{ address = $ob.addr; port = $ob.port; users = @($user) }) } }
    } elseif ($p -eq "vmess") {
        $sc = $(if ($ob.scy) { $ob.scy } else { "auto" })
        $out = [ordered]@{ protocol = "vmess"; settings = [ordered]@{ vnext = @([ordered]@{ address = $ob.addr; port = $ob.port; users = @([ordered]@{ id = $ob.id; alterId = $ob.alter; security = $sc }) }) } }
    } elseif ($p -eq "trojan") {
        $out = [ordered]@{ protocol = "trojan"; settings = [ordered]@{ servers = @([ordered]@{ address = $ob.addr; port = $ob.port; password = $ob.password }) } }
    } else {
        $out = [ordered]@{ protocol = "shadowsocks"; settings = [ordered]@{ servers = @([ordered]@{ address = $ob.addr; port = $ob.port; method = $ob.method; password = $ob.password }) } }
    }
    $ss = [ordered]@{ network = $ob.net; security = $ob.sec }
    if ($ob.sec -eq "tls") {
        $sn = $(if ($ob.sni) { $ob.sni } elseif ($ob.host) { $ob.host } else { $ob.addr })
        $ts = [ordered]@{ serverName = $sn }; if ($ob.fp) { $ts.fingerprint = $ob.fp }; if ($ob.alpn.Count) { $ts.alpn = @($ob.alpn) }
        $ss.tlsSettings = $ts
    } elseif ($ob.sec -eq "reality") {
        $ss.realitySettings = [ordered]@{ serverName = $ob.sni; fingerprint = $(if ($ob.fp) { $ob.fp } else { "chrome" }); publicKey = $ob.pbk; shortId = $ob.sid; spiderX = $ob.spx }
    }
    $path = $(if ($ob.path) { $ob.path } else { "/" })
    if ($ob.net -eq "ws") {
        $w = [ordered]@{ path = $path }; if ($ob.host) { $w.headers = [ordered]@{ Host = $ob.host } }; $ss.wsSettings = $w
    } elseif ($ob.net -eq "grpc") {
        $ss.grpcSettings = [ordered]@{ serviceName = $ob.service }
    } elseif ($ob.net -eq "httpupgrade") {
        $w = [ordered]@{ path = $path }; if ($ob.host) { $w.host = $ob.host }; $ss.httpupgradeSettings = $w
    } elseif ($ob.net -eq "xhttp") {
        $w = [ordered]@{ path = $path }; if ($ob.host) { $w.host = $ob.host }; if ($ob.mode) { $w.mode = $ob.mode }; $ss.xhttpSettings = $w
    }
    if ($p -ne "ss" -or $ob.net -ne "tcp" -or $ob.sec -ne "none") { $out.streamSettings = $ss }
    $out.tag = "proxy"
    return [ordered]@{ log = [ordered]@{ loglevel = "warning" }
                       inbounds = @([ordered]@{ tag = "in"; listen = "127.0.0.1"; port = $port; protocol = "http"; settings = [ordered]@{ allowTransparent = $false } })
                       outbounds = @($out) }
}

function Classify-VpnInput([string]$s) {
    # link | subscription | proxy | none
    $s = $s.Trim()
    if (-not $s) { return "none" }
    if ($s -match "^(vless|vmess|trojan|ss)://") { return "link" }
    if ($s -match "^socks\w*://") { return "socks" }
    if ($s -match "^(http|https)://[^/]+/?$" -or $s -match "^[\w.\-]+:\d{2,5}$") { return "proxy" }
    if ($s -match "^https?://[^/]+/.+") { return "subscription" }
    return "none"
}

function Normalize-Proxy([string]$s) {
    $s = $s.Trim().TrimEnd("/")
    if ($s -notmatch "^[a-z0-9]+://") { $s = "http://$s" }
    return $s
}

function Get-XrayAssetName([bool]$arm) { if ($arm) { return "Xray-windows-arm64-v8a.zip" } else { return "Xray-windows-64.zip" } }

function Test-PortFree([int]$port) {
    try { $l = New-Object Net.Sockets.TcpListener([Net.IPAddress]::Loopback, $port); $l.Start(); $l.Stop(); return $true } catch { return $false }
}
function Test-PortOpen([int]$port) {
    try { $c = New-Object Net.Sockets.TcpClient; $r = $c.BeginConnect("127.0.0.1", $port, $null, $null); $ok = $r.AsyncWaitHandle.WaitOne(300); if ($ok) { $c.EndConnect($r) }; $c.Close(); return $ok } catch { return $false }
}
function Find-FreePort([int]$start = 10809) { for ($p = $start; $p -lt $start + 50; $p++) { if (Test-PortFree $p) { return $p } }; throw "нет свободного порта рядом с $start" }

function Find-Xray([string]$here) {
    foreach ($c in @((Join-Path $here "vpn_core\xray.exe"), (Join-Path $here "plugins\data\vpn_proxy\core\xray\xray.exe"),
                     (Join-Path $here "plugins\data\vpn_proxy\core\xray.exe"))) { if (Test-Path $c) { return $c } }
    $w = Get-Command xray.exe -ErrorAction SilentlyContinue
    if ($w) { return $w.Source }
    return $null
}

function Install-Xray([string]$here, [scriptblock]$report) {
    # Скачивает последний релиз Xray с GitHub (sha256 проверяется, если опубликован) в vpn_core\. Бросает исключение с причиной.
    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
    $hdr = @{ "User-Agent" = "nightlord-installer"; "Accept" = "application/vnd.github+json" }
    & $report "Ищу последнюю версию Xray на GitHub…"
    try { $rel = Invoke-RestMethod -Uri "https://api.github.com/repos/XTLS/Xray-core/releases/latest" -Headers $hdr -TimeoutSec 25 }
    catch { throw "GitHub недоступен ($($_.Exception.Message)). Скачайте Xray-windows-64.zip вручную, распакуйте xray.exe (и geoip.dat, geosite.dat) в папку vpn_core рядом с установщиком" }
    $arm = ($env:PROCESSOR_ARCHITECTURE -eq "ARM64")
    $name = Get-XrayAssetName $arm
    $asset = $rel.assets | Where-Object { $_.name -eq $name } | Select-Object -First 1
    if (-not $asset) { throw "в релизе нет файла $name" }
    $dir = Join-Path $here "vpn_core"; New-Item -ItemType Directory -Force $dir | Out-Null
    $zip = Join-Path $dir "_dl_$name"
    & $report "Скачиваю $name…"
    try { Invoke-WebRequest -Uri $asset.browser_download_url -OutFile $zip -UseBasicParsing -Headers $hdr -TimeoutSec 180 }
    catch { Remove-Item $zip -ErrorAction SilentlyContinue; throw "не удалось скачать $name ($($_.Exception.Message))" }
    $dg = $rel.assets | Where-Object { $_.name -eq "$name.dgst" } | Select-Object -First 1
    if ($dg) {
        try {
            $txt = (Invoke-WebRequest -Uri $dg.browser_download_url -UseBasicParsing -Headers $hdr -TimeoutSec 20).Content
            if ($txt -is [byte[]]) { $txt = [Text.Encoding]::UTF8.GetString($txt) }
            if ($txt -match "(?i)sha(?:2-)?256\S*\s*=?\s*([0-9a-f]{64})") {
                $h = (Get-FileHash -Algorithm SHA256 $zip).Hash.ToLower()
                if ($h -ne $Matches[1].ToLower()) { Remove-Item $zip -ErrorAction SilentlyContinue; throw "контрольная сумма не совпала: файл повреждён или подменён" }
            }
        } catch { if ($_.Exception.Message -like "контрольная*") { throw } }
    }
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $z = [IO.Compression.ZipFile]::OpenRead($zip)
    try {
        foreach ($e in $z.Entries) {
            $base = [IO.Path]::GetFileName($e.FullName)
            if ($base -in @("xray.exe", "geoip.dat", "geosite.dat")) { [IO.Compression.ZipFileExtensions]::ExtractToFile($e, (Join-Path $dir $base), $true) }
        }
    } finally { $z.Dispose(); Remove-Item $zip -ErrorAction SilentlyContinue }
    $exe = Join-Path $dir "xray.exe"
    if (-not (Test-Path $exe)) { throw "в архиве нет xray.exe" }
    return $exe
}

function Start-XrayCore([string]$exe, $ob, [int]$port) {
    # -> @{ Proc; Config }
    $cfg = Join-Path ([IO.Path]::GetTempPath()) ("nl_xray_" + [Guid]::NewGuid().ToString("N").Substring(0, 8) + ".json")
    [IO.File]::WriteAllText($cfg, ((New-XrayConfig $ob $port) | ConvertTo-Json -Depth 12), (New-Object Text.UTF8Encoding($false)))
    $sp = @{ FilePath = $exe; ArgumentList = "run -c `"$cfg`""; PassThru = $true; WorkingDirectory = (Split-Path -Parent $exe) }
    if ($IsWindows -ne $false) { $sp.WindowStyle = "Hidden" }
    $proc = Start-Process @sp
    return @{ Proc = $proc; Config = $cfg }
}

function Stop-XrayCore($core) {
    if (-not $core) { return }
    try { if ($core.Proc -and -not $core.Proc.HasExited) { $core.Proc.Kill() } } catch {}
    try { if ($core.Config) { Remove-Item $core.Config -ErrorAction SilentlyContinue } } catch {}
}

function Set-ProxyEnv([string]$url) {
    # Прокси для всех дочерних процессов установщика (pip, install_python.ps1, сборка exe).
    $env:HTTP_PROXY = $url; $env:HTTPS_PROXY = $url; $env:ALL_PROXY = $url; $env:http_proxy = $url; $env:https_proxy = $url
    $env:NO_PROXY = "localhost,127.0.0.1"; $env:no_proxy = "localhost,127.0.0.1"
    $env:NL_PROXY = $url
    [Net.WebRequest]::DefaultWebProxy = New-Object Net.WebProxy($url, $true)
}
function Clear-ProxyEnv {
    foreach ($n in "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "NO_PROXY", "no_proxy", "NL_PROXY") { Remove-Item "Env:$n" -ErrorAction SilentlyContinue }
    [Net.WebRequest]::DefaultWebProxy = [Net.WebRequest]::GetSystemWebProxy()
}

function Get-AgentVpnLink([string]$here) {
    # Сервер, уже настроенный в плагине агента «VPN / прокси» -> ссылка или $null
    $f = Join-Path $here "plugins\data\vpn_proxy\config.json"
    if (-not (Test-Path $f)) { return $null }
    try {
        $j = Get-Content $f -Raw -Encoding UTF8 | ConvertFrom-Json
        if ($j.servers) {
            $sel = $(if ($j.server) { $j.server } else { "" })
            if ($sel -and $j.servers.PSObject.Properties[$sel]) { return [string]$j.servers.$sel }
            $first = $j.servers.PSObject.Properties | Select-Object -First 1
            if ($first) { return [string]$first.Value }
        }
        if ($j.sub_url) { return [string]$j.sub_url }
    } catch {}
    return $null
}


function Test-ProxyWorks([string]$url, [scriptblock]$report) {
    # Через прокси открываем python.org и pypi.org; -> $true, если оба ответили.
    $okAll = $true
    foreach ($u in @("https://www.python.org/", "https://pypi.org/simple/pip/")) {
        try {
            $r = Invoke-WebRequest -Uri $u -Method Head -Proxy $url -UseBasicParsing -TimeoutSec 25
            & $report "через прокси открывается $u (код $($r.StatusCode))"
        } catch { $okAll = $false; & $report "через прокси НЕ открывается ${u}: $($_.Exception.Message)" }
    }
    return $okAll
}

function Connect-Vpn([string]$here, [string]$text, $state) {
    # Выполняется в фоновом потоке. $state — синхронизированная хэш-таблица: Status, Log (ArrayList), Done, Error, Proxy, Core.
    $report = { param($m) $state.Status = $m; [void]$state.Log.Add($m) }
    try {
        $kind = Classify-VpnInput $text
        if ($kind -eq "socks") { throw "для скачивания нужен HTTP-прокси (в v2rayN/Hiddify это порт HTTP, обычно 10809), а не SOCKS. Или вставьте ссылку vless:// / подписку: установщик запустит своё ядро" }
        if ($kind -eq "none") { throw "не понял, что вставлено. Нужна ссылка vless:// vmess:// trojan:// ss://, адрес подписки (http://…/путь) или готовый HTTP-прокси (127.0.0.1:10809)" }
        if ($kind -eq "proxy") {
            $url = Normalize-Proxy $text
            & $report "Проверяю прокси $url…"
            if (-not (Test-ProxyWorks $url $report)) { throw "прокси $url не пропускает python.org / pypi.org" }
            $state.Proxy = $url; return
        }
        $link = $text.Trim()
        if ($kind -eq "subscription") {
            & $report "Загружаю подписку…"
            [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
            try { $resp = Invoke-WebRequest -Uri $text.Trim() -UseBasicParsing -TimeoutSec 25 -Headers @{ "User-Agent" = "Mozilla/5.0 (nightlord-installer)" } }
            catch { throw "подписка не загрузилась ($($_.Exception.Message))" }
            $body = $resp.Content; if ($body -is [byte[]]) { $body = [Text.Encoding]::UTF8.GetString($body) }
            $items = @(Parse-Subscription $body)
            if (-not $items.Count) { throw "в подписке нет поддерживаемых серверов (vless, vmess, trojan, ss)" }
            & $report "В подписке серверов: $($items.Count). Беру первый (остальные попробую, если он не заработает)."
            $candidates = @($items | ForEach-Object { $_.Link })
        } else { $candidates = @($link) }
        $exe = Find-Xray $here
        if (-not $exe) { $exe = Install-Xray $here $report }
        & $report "Ядро: $exe"
        $lastErr = ""
        foreach ($cl in ($candidates | Select-Object -First 4)) {
            try { $ob = Parse-VpnLink $cl } catch { $lastErr = $_.Exception.Message; continue }
            $port = Find-FreePort 10809
            & $report "Запускаю ядро для сервера «$($ob.name)» на 127.0.0.1:$port…"
            $core = Start-XrayCore $exe $ob $port
            $t0 = Get-Date
            while (((Get-Date) - $t0).TotalSeconds -lt 12 -and -not $core.Proc.HasExited -and -not (Test-PortOpen $port)) { Start-Sleep -Milliseconds 250 }
            if ($core.Proc.HasExited -or -not (Test-PortOpen $port)) { Stop-XrayCore $core; $lastErr = "ядро не запустилось (проверьте ссылку)"; continue }
            $url = "http://127.0.0.1:$port"
            if (Test-ProxyWorks $url $report) { $state.Core = $core; $state.Proxy = $url; return }
            Stop-XrayCore $core; $lastErr = "через сервер «$($ob.name)» python.org / pypi.org не открываются"
        }
        throw $(if ($lastErr) { $lastErr } else { "ни один сервер не заработал" })
    } catch { $state.Error = $_.Exception.Message }
    finally { $state.Done = $true }
}
