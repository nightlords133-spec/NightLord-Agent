# Плагин Nightlord агента: свой прокси/VPN для интернет-запросов агента (без VPN на всём компьютере).
# Через прокси идут Telegram (плагин «Telegram»), веб-поиск, чтение страниц и (по желанию) скачивание моделей.
# LM Studio и Voicebox всегда идут напрямую. Если прокси недоступен, запросы не уходят напрямую, а дают ошибку.
import atexit
import base64
import hashlib
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tarfile
import threading
import time
import zipfile
import platform
import tkinter as tk
from pathlib import Path
from tkinter import ttk, messagebox, simpledialog
from urllib.parse import quote, unquote, urlsplit, parse_qs

import requests

PLUGIN = {"name": "VPN / прокси", "version": "1.2",
          "description": "Интернет-трафик агента через SOCKS5/HTTP-прокси или встроенное ядро Xray / sing-box "
                         "(ссылки vless, vmess, trojan, ss и подписки). Голосом: «включи впн», «выключи впн», «проверь впн»."}

SCHEMES = ["socks5", "http", "https", "socks4"]
PRESETS = {"v2rayN": ("socks5", "127.0.0.1", 10808), "Hiddify": ("socks5", "127.0.0.1", 12334),
           "NekoBox": ("socks5", "127.0.0.1", 2080), "Clash": ("socks5", "127.0.0.1", 7890),
           "HTTP :10809": ("http", "127.0.0.1", 10809)}


def build_proxy_url(scheme, host, port, user="", password=""):
    """socks5 -> socks5h: имена сайтов разрешает сам прокси (без утечки DNS)."""
    host = str(host or "").strip()
    host = re.sub(r"^[a-z0-9+.-]+://", "", host, flags=re.I).split("/")[0]
    try:
        port = int(port)
    except (TypeError, ValueError):
        return None
    if not host or not 1 <= port <= 65535 or scheme not in SCHEMES:
        return None
    sch = {"socks5": "socks5h", "socks4": "socks4a"}.get(scheme, scheme)
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    auth = f"{quote(str(user), safe='')}:{quote(str(password or ''), safe='')}@" if user else ""
    return f"{sch}://{auth}{host}:{port}"


# ---------------- встроенное ядро: разбор ссылок и конфиги ----------------
CORES = ["xray", "sing-box"]
CORE_REPO = {"xray": "XTLS/Xray-core", "sing-box": "SagerNet/sing-box"}
NETS = {"tcp": "tcp", "raw": "tcp", "ws": "ws", "grpc": "grpc", "httpupgrade": "httpupgrade", "xhttp": "xhttp",
        "splithttp": "xhttp"}


def _b64(s):
    s = str(s).strip().replace("-", "+").replace("_", "/")
    s += "=" * (-len(s) % 4)
    return base64.b64decode(s).decode("utf-8", "replace")


def _q(qs, key, default=""):
    v = qs.get(key)
    return unquote(v[0]) if v else default


def _alpn(v):
    return [x for x in str(v or "").replace("%2C", ",").split(",") if x]


def parse_link(link):
    """Ссылка vless:// vmess:// trojan:// ss:// -> общее описание сервера. Непонятное -> ValueError с причиной."""
    link = str(link or "").strip()
    m = re.match(r"^([a-z0-9]+)://", link, re.I)
    if not m:
        raise ValueError("это не ссылка вида vless://… vmess://… trojan://… ss://…")
    proto = m.group(1).lower()
    if proto not in ("vless", "vmess", "trojan", "ss"):
        raise ValueError(f"протокол {proto}:// пока не поддерживается (нужны vless, vmess, trojan или ss)")
    ob = {"proto": proto, "name": "", "addr": "", "port": 0, "id": "", "password": "", "method": "", "flow": "",
          "net": "tcp", "sec": "none", "sni": "", "fp": "", "alpn": [], "pbk": "", "sid": "", "spx": "", "path": "",
          "host": "", "service": "", "mode": "", "alter": 0, "scy": "auto"}
    if proto == "vmess":
        try:
            j = json.loads(_b64(link[8:].split("#")[0]))
        except Exception:
            raise ValueError("ссылка vmess:// повреждена (не читается base64/JSON)") from None
        ob.update(name=str(j.get("ps", "")), addr=str(j.get("add", "")), port=int(j.get("port") or 0), id=str(j.get("id", "")),
                  alter=int(j.get("aid") or 0), scy=str(j.get("scy") or "auto"), path=str(j.get("path", "")),
                  host=str(j.get("host", "")), sni=str(j.get("sni", "")), fp=str(j.get("fp", "")), alpn=_alpn(j.get("alpn")))
        net = str(j.get("net") or "tcp").lower()
        if net not in NETS or NETS[net] == "xhttp":
            raise ValueError(f"транспорт «{net}» для vmess не поддерживается (tcp, ws, grpc, httpupgrade)")
        ob["net"] = NETS[net]
        if ob["net"] == "grpc":
            ob["service"] = ob["path"]
        ob["sec"] = "tls" if str(j.get("tls", "")).lower() == "tls" else "none"
    elif proto == "ss":
        body, _, frag = link[5:].partition("#")
        ob["name"] = unquote(frag)
        body, _, query = body.partition("?")
        if "plugin" in parse_qs(query):
            raise ValueError("Shadowsocks с плагином (obfs, v2ray-plugin) не поддерживается")
        try:
            if "@" in body:
                user, _, hp = body.rpartition("@")
                cred = unquote(user) if ":" in unquote(user) else _b64(user)
            else:
                dec = _b64(body.rstrip("/"))
                cred, _, hp = dec.rpartition("@")
            ob["method"], _, ob["password"] = cred.partition(":")
            host, _, port = hp.rstrip("/").rpartition(":")
            ob["addr"], ob["port"] = host.strip("[]"), int(port)
        except Exception:
            raise ValueError("ссылка ss:// повреждена") from None
        if not ob["method"] or not ob["password"]:
            raise ValueError("в ссылке ss:// нет метода или пароля")
    else:
        u = urlsplit(link)
        qs = parse_qs(u.query)
        try:
            port = u.port
        except ValueError:
            port = None
        ob.update(name=unquote(u.fragment), addr=u.hostname or "", port=port or 0)
        user = unquote(u.username or "")
        if proto == "vless":
            ob["id"] = user
        else:
            ob["password"] = user
        ob["flow"] = _q(qs, "flow")
        net = (_q(qs, "type", "tcp") or "tcp").lower()
        if net not in NETS:
            raise ValueError(f"транспорт «{net}» не поддерживается (tcp, ws, grpc, httpupgrade, xhttp)")
        ob["net"] = NETS[net]
        sec = (_q(qs, "security", "tls" if proto == "trojan" else "none") or "none").lower()
        if sec not in ("none", "tls", "reality"):
            raise ValueError(f"security={sec} не поддерживается (none, tls, reality)")
        ob["sec"] = sec
        ob.update(sni=_q(qs, "sni") or _q(qs, "peer"), fp=_q(qs, "fp"), alpn=_alpn(_q(qs, "alpn")), pbk=_q(qs, "pbk"),
                  sid=_q(qs, "sid"), spx=_q(qs, "spx"), path=_q(qs, "path"), host=_q(qs, "host"),
                  service=_q(qs, "serviceName"), mode=_q(qs, "mode"))
        if sec == "reality" and not ob["pbk"]:
            raise ValueError("в ссылке Reality нет публичного ключа (pbk)")
    if not ob["addr"] or not 1 <= int(ob["port"]) <= 65535:
        raise ValueError("в ссылке не указан адрес или порт сервера")
    if proto in ("vless", "vmess") and not ob["id"]:
        raise ValueError("в ссылке нет UUID пользователя")
    if proto == "trojan" and not ob["password"]:
        raise ValueError("в ссылке trojan:// нет пароля")
    if not ob["name"]:
        ob["name"] = f"{ob['addr']}:{ob['port']}"
    return ob


def parse_subscription(text):
    """Текст подписки (base64 или обычный список) -> [(название, ссылка)] только из поддерживаемых протоколов."""
    t = str(text or "").strip()
    if t and "://" not in t.splitlines()[0]:
        try:
            t = _b64("".join(t.split()))
        except Exception:
            pass
    out = []
    for line in t.splitlines():
        line = line.strip()
        if not re.match(r"^(vless|vmess|trojan|ss)://", line, re.I):
            continue
        try:
            out.append((parse_link(line)["name"], line))
        except ValueError:
            continue
    return out


def xray_config(ob, port):
    """Конфиг Xray: локальный HTTP-прокси на 127.0.0.1:port -> сервер из ссылки."""
    p = ob["proto"]
    if p == "vless":
        out = {"protocol": "vless", "settings": {"vnext": [{"address": ob["addr"], "port": ob["port"], "users": [
            {"id": ob["id"], "encryption": "none", **({"flow": ob["flow"]} if ob["flow"] else {})}]}]}}
    elif p == "vmess":
        out = {"protocol": "vmess", "settings": {"vnext": [{"address": ob["addr"], "port": ob["port"], "users": [
            {"id": ob["id"], "alterId": ob["alter"], "security": ob["scy"] or "auto"}]}]}}
    elif p == "trojan":
        out = {"protocol": "trojan", "settings": {"servers": [{"address": ob["addr"], "port": ob["port"], "password": ob["password"]}]}}
    else:
        out = {"protocol": "shadowsocks", "settings": {"servers": [
            {"address": ob["addr"], "port": ob["port"], "method": ob["method"], "password": ob["password"]}]}}
    ss = {"network": ob["net"], "security": ob["sec"]}
    if ob["sec"] == "tls":
        ss["tlsSettings"] = {"serverName": ob["sni"] or ob["host"] or ob["addr"],
                             **({"fingerprint": ob["fp"]} if ob["fp"] else {}), **({"alpn": ob["alpn"]} if ob["alpn"] else {})}
    elif ob["sec"] == "reality":
        ss["realitySettings"] = {"serverName": ob["sni"], "fingerprint": ob["fp"] or "chrome", "publicKey": ob["pbk"],
                                 "shortId": ob["sid"], "spiderX": ob["spx"]}
    if ob["net"] == "ws":
        ss["wsSettings"] = {"path": ob["path"] or "/", **({"headers": {"Host": ob["host"]}} if ob["host"] else {})}
    elif ob["net"] == "grpc":
        ss["grpcSettings"] = {"serviceName": ob["service"]}
    elif ob["net"] == "httpupgrade":
        ss["httpupgradeSettings"] = {"path": ob["path"] or "/", **({"host": ob["host"]} if ob["host"] else {})}
    elif ob["net"] == "xhttp":
        ss["xhttpSettings"] = {"path": ob["path"] or "/", **({"host": ob["host"]} if ob["host"] else {}),
                               **({"mode": ob["mode"]} if ob["mode"] else {})}
    if p != "ss" or ob["net"] != "tcp" or ob["sec"] != "none":
        out["streamSettings"] = ss
    out["tag"] = "proxy"
    return {"log": {"loglevel": "warning"},
            "inbounds": [{"tag": "in", "listen": "127.0.0.1", "port": port, "protocol": "http",
                          "settings": {"allowTransparent": False}}],
            "outbounds": [out]}


def singbox_config(ob, port):
    """Конфиг sing-box: локальный HTTP-прокси на 127.0.0.1:port -> сервер из ссылки."""
    if ob["net"] == "xhttp":
        raise ValueError("транспорт xhttp умеет только Xray: выберите ядро xray")
    p = ob["proto"]
    out = {"type": "shadowsocks" if p == "ss" else p, "tag": "proxy", "server": ob["addr"], "server_port": ob["port"]}
    if p == "vless":
        out["uuid"] = ob["id"]
        if ob["flow"]:
            out["flow"] = ob["flow"]
    elif p == "vmess":
        out.update(uuid=ob["id"], security=ob["scy"] or "auto", alter_id=ob["alter"])
    elif p == "trojan":
        out["password"] = ob["password"]
    else:
        out.update(method=ob["method"], password=ob["password"])
    if ob["sec"] in ("tls", "reality"):
        tls = {"enabled": True, "server_name": ob["sni"] or ob["host"] or ob["addr"]}
        if ob["alpn"]:
            tls["alpn"] = ob["alpn"]
        if ob["fp"] or ob["sec"] == "reality":
            tls["utls"] = {"enabled": True, "fingerprint": ob["fp"] or "chrome"}
        if ob["sec"] == "reality":
            tls["reality"] = {"enabled": True, "public_key": ob["pbk"], "short_id": ob["sid"]}
        out["tls"] = tls
    if ob["net"] == "ws":
        out["transport"] = {"type": "ws", "path": ob["path"] or "/", **({"headers": {"Host": ob["host"]}} if ob["host"] else {})}
    elif ob["net"] == "grpc":
        out["transport"] = {"type": "grpc", "service_name": ob["service"]}
    elif ob["net"] == "httpupgrade":
        out["transport"] = {"type": "httpupgrade", "path": ob["path"] or "/", **({"host": ob["host"]} if ob["host"] else {})}
    return {"log": {"level": "warn"},
            "inbounds": [{"type": "http", "tag": "in", "listen": "127.0.0.1", "listen_port": port}],
            "outbounds": [out], "route": {"final": "proxy"}}


def core_config(core, ob, port):
    return xray_config(ob, port) if core == "xray" else singbox_config(ob, port)


def exe_name(core):
    return core + (".exe" if sys.platform == "win32" else "")


def pick_asset(core, names):
    """Имя нужного архива из релиза GitHub для этой ОС и процессора или None."""
    osn = "windows" if sys.platform == "win32" else "linux" if sys.platform.startswith("linux") else None
    mach = platform.machine().lower()
    arm = mach in ("arm64", "aarch64")
    if not osn:
        return None
    if core == "xray":
        want = f"Xray-{osn}-{'arm64-v8a' if arm else '64'}.zip"
        return want if want in names else None
    rx = re.compile(rf"^sing-box-[\w.\-]*?-{osn}-{'arm64' if arm else 'amd64'}\.(zip|tar\.gz)$")
    ok = [n for n in names if rx.match(n) and not re.search(r"legacy|glibc|musl|android", n)]
    return ok[0] if ok else None


def extract_core(archive, core, dest):
    """Достаёт ядро (и .dat для Xray) в одну папку без вложенных путей. Возвращает путь к исполняемому файлу."""
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    keep = {exe_name(core), "geoip.dat", "geosite.dat"}
    found = None

    def put(name, fh):
        nonlocal found
        base = os.path.basename(name.replace("\\", "/"))
        if base not in keep:
            return
        tmp = dest / (base + ".part")
        with open(tmp, "wb") as o:
            shutil.copyfileobj(fh, o)
        os.replace(tmp, dest / base)
        if base == exe_name(core):
            found = dest / base
            if sys.platform != "win32":
                os.chmod(found, 0o755)
    archive = str(archive)
    if zipfile.is_zipfile(archive):
        with zipfile.ZipFile(archive) as z:
            for i in z.infolist():
                if not i.is_dir():
                    with z.open(i) as fh:
                        put(i.filename, fh)
    else:
        with tarfile.open(archive) as t:
            for m in t.getmembers():
                if m.isfile():
                    fh = t.extractfile(m)
                    if fh:
                        put(m.name, fh)
    if not found:
        raise RuntimeError(f"в архиве нет {exe_name(core)}")
    return found


def port_free(port):
    s = socket.socket()
    try:
        s.bind(("127.0.0.1", int(port)))
        return True
    except OSError:
        return False
    finally:
        s.close()


def port_open(port, timeout=0.3):
    try:
        with socket.create_connection(("127.0.0.1", int(port)), timeout=timeout):
            return True
    except OSError:
        return False


def fetch_subscription(url):
    """Скачивает подписку напрямую (системные прокси не используются) -> [(название, ссылка)]."""
    s = requests.Session()
    s.trust_env = False
    r = s.get(url, timeout=20, headers={"User-Agent": "Mozilla/5.0 (nightlord-agent)"})
    r.raise_for_status()
    items = parse_subscription(r.text)
    if not items:
        raise RuntimeError("в подписке нет поддерживаемых серверов (vless, vmess, trojan, ss)")
    return items


def fetch_core(core, core_dir, proxy_options, report, explain):
    """Скачивает последний релиз ядра с GitHub, проверяет сумму (если она опубликована) и распаковывает.
    proxy_options — по очереди пробуемые proxies (None = напрямую). Возвращает путь к исполняемому файлу."""
    core_dir = Path(core_dir)
    api_url = f"https://api.github.com/repos/{CORE_REPO[core]}/releases/latest"
    hdr = {"User-Agent": "nightlord-agent", "Accept": "application/vnd.github+json"}
    last = None
    for prox in proxy_options:
        try:
            s = requests.Session()
            s.trust_env = False
            rel = s.get(api_url, headers=hdr, timeout=25, proxies=prox)
            rel.raise_for_status()
            assets = {a["name"]: a["browser_download_url"] for a in rel.json().get("assets", [])}
            name = pick_asset(core, list(assets))
            if not name:
                raise RuntimeError("в релизе нет сборки под эту систему: скачайте ядро вручную и положите в папку ядра")
            core_dir.mkdir(parents=True, exist_ok=True)
            tmp = core_dir / ("_dl_" + name)
            report(f"скачиваю {name}…")
            h = hashlib.sha256()
            with s.get(assets[name], headers=hdr, timeout=120, stream=True, proxies=prox) as r:
                r.raise_for_status()
                with open(tmp, "wb") as o:
                    for chunk in r.iter_content(65536):
                        o.write(chunk)
                        h.update(chunk)
            if name + ".dgst" in assets:
                try:
                    txt = s.get(assets[name + ".dgst"], headers=hdr, timeout=20, proxies=prox).text
                    m = re.search(r"(?i)sha(?:2-)?256\S*\s*=?\s*([0-9a-f]{64})", txt)
                    if m and m.group(1).lower() != h.hexdigest():
                        tmp.unlink()
                        raise RuntimeError("контрольная сумма не совпала: файл повреждён или подменён")
                except requests.RequestException:
                    pass
            try:
                return extract_core(tmp, core, core_dir / core)
            finally:
                try:
                    tmp.unlink()
                except OSError:
                    pass
        except requests.RequestException as e:
            last = e
            continue
    raise RuntimeError("GitHub недоступен: " + explain(last))


def get_ip(proxies=None, timeout=10):
    """Внешний IP. proxies=None -> строго напрямую (системные настройки прокси игнорируются)."""
    s = requests.Session()
    s.trust_env = False
    r = s.get("https://api.ipify.org?format=json", proxies=proxies, timeout=timeout)
    r.raise_for_status()
    return r.json()["ip"]


def register(api):
    if not api.config_get("migrated"):
        for new, old, dflt in (("on", "proxy_on", False), ("scheme", "proxy_scheme", "socks5"),
                               ("host", "proxy_host", "127.0.0.1"), ("port", "proxy_port", "10808"),
                               ("user", "proxy_user", ""), ("pw", "proxy_pass", ""), ("tg", "proxy_tg", True),
                               ("web", "proxy_web", True), ("models", "proxy_models", False)):
            api.config_set(new, api.legacy(old, dflt))
        api.config_set("migrated", True)

    names = {"on": False, "scheme": "socks5", "host": "127.0.0.1", "port": "10808", "user": "", "pw": "",
             "tg": True, "web": True, "models": False, "mode": "manual", "core": "xray", "core_port": 10818,
             "server": "", "sub_url": ""}
    vars_ = {k: api.var(k, v) for k, v in names.items()}
    for k in ("port",):
        vars_[k].set(str(vars_[k].get()))

    def cfg_url():
        g = api.config_get
        if not g("on", False):
            return None
        if g("mode", "manual") == "core":
            return build_proxy_url("http", "127.0.0.1", g("core_port", 10818))   # HTTP-вход ядра: пакет PySocks не нужен
        return build_proxy_url(g("scheme", "socks5"), g("host", ""), g("port", ""), g("user", ""), g("pw", ""))

    def sync(*a):
        url = cfg_url()
        d = {"http": url, "https": url} if url else None
        g = api.config_get
        api.set_proxies(web=d if g("web", True) else None, tg=d if g("tg", True) else None, ip=d,
                        models=url if g("models", False) else None)
    core_dir = api.data_dir / "core"
    R = {"proc": None, "sig": None, "log": None, "lock": threading.RLock(), "status": None, "dead": False}

    def say_status(text):
        api.log(f"прокси-ядро: {text}")
        sv = R["status"]
        if sv is not None:
            api.post(sv.set, text)

    def servers():
        d = api.config_get("servers", {})
        return d if isinstance(d, dict) else {}

    def find_core(core):
        for c in (core_dir / exe_name(core), core_dir / core / exe_name(core)):
            if c.is_file():
                return c
        w = shutil.which(core)
        return Path(w) if w else None

    def running():
        pr = R["proc"]
        return pr is not None and pr.poll() is None

    def stop_core():
        with R["lock"]:
            pr, R["proc"], R["sig"] = R["proc"], None, None
            if pr is not None and pr.poll() is None:
                pr.terminate()
                try:
                    pr.wait(3)
                except subprocess.TimeoutExpired:
                    pr.kill()
            if R["log"]:
                try:
                    R["log"].close()
                except OSError:
                    pass
                R["log"] = None

    def log_tail():
        try:
            return (core_dir / "core.log").read_text("utf-8", "replace").strip()[-400:]
        except OSError:
            return ""

    def start_core():
        """Запускает ядро под выбранный сервер (или перезапускает, если сервер/ядро/порт сменились). Бросает RuntimeError."""
        g = api.config_get
        with R["lock"]:
            if R["dead"]:
                return                      # плагин выгружен или перезагружен: новое ядро не запускаем
            name = g("server", "")
            link = servers().get(name)
            if not link:
                raise RuntimeError("не выбран сервер: добавьте ссылку или обновите подписку и выберите сервер")
            try:
                ob = parse_link(link)
            except ValueError as e:
                raise RuntimeError(f"сервер «{name}»: {e}") from None
            core = g("core", "xray") if g("core", "xray") in CORES else "xray"
            port = int(g("core_port", 10818) or 10818)
            sig = (core, link, port)
            if running() and R["sig"] == sig and port_open(port):
                return
            stop_core()
            exe = find_core(core)
            if not exe:
                raise RuntimeError(f"ядро {core} не найдено: нажмите «Скачать ядро» или положите {exe_name(core)} в папку "
                                   f"{core_dir}")
            if not port_free(port):
                for cand in range(port + 1, port + 40):
                    if port_free(cand):
                        port = cand
                        api.config_set("core_port", port)
                        api.post(vars_["core_port"].set, port)
                        break
                else:
                    raise RuntimeError("не нашёл свободный порт для ядра")
                sig = (core, link, port)
            try:
                conf = core_config(core, ob, port)
            except ValueError as e:
                raise RuntimeError(str(e)) from None
            core_dir.mkdir(parents=True, exist_ok=True)
            cfile = core_dir / "run.json"
            cfile.write_text(json.dumps(conf, ensure_ascii=False, indent=1), "utf-8")
            try:
                os.chmod(cfile, 0o600)
            except OSError:
                pass
            flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            check = [str(exe), "run", "-test", "-c", str(cfile)] if core == "xray" else [str(exe), "check", "-c", str(cfile)]
            try:
                r = subprocess.run(check, capture_output=True, timeout=20, creationflags=flags, cwd=str(exe.parent))
                if r.returncode != 0:
                    out = (r.stdout + r.stderr).decode("utf-8", "replace").strip()[-300:]
                    raise RuntimeError(f"ядро не приняло конфиг: {out}")
            except (subprocess.TimeoutExpired, OSError) as e:
                api.log(f"проверка конфига пропущена: {e}")
            R["log"] = open(core_dir / "core.log", "wb")
            cmd = [str(exe), "run", "-c", str(cfile)]
            try:
                R["proc"] = subprocess.Popen(cmd, stdout=R["log"], stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                             creationflags=flags, cwd=str(exe.parent))
            except OSError as e:
                R["proc"] = None
                raise RuntimeError(f"не удалось запустить {exe.name}: {e}") from None
            R["sig"] = sig
            for _ in range(60):
                if R["proc"].poll() is not None:
                    tail = log_tail()
                    R["proc"] = None
                    raise RuntimeError(f"ядро сразу завершилось. {tail}" if tail else "ядро сразу завершилось")
                if port_open(port):
                    say_status(f"работает: {core}, сервер «{name}», порт {port}")
                    return
                time.sleep(0.2)
            stop_core()
            raise RuntimeError("ядро не открыло порт за 12 секунд. " + log_tail())

    def apply_core():
        """Приводит ядро в нужное состояние. Возвращает None или текст ошибки."""
        g = api.config_get
        try:
            if g("on", False) and g("mode", "manual") == "core":
                start_core()
            elif R["proc"] is not None:
                stop_core()
                say_status("остановлено")
        except RuntimeError as e:
            say_status(f"ошибка: {e}")
            return str(e)
        return None

    def on_change(*a):
        sync()
        threading.Thread(target=apply_core, daemon=True).start()
    for k, v in vars_.items():
        v.trace_add("write", on_change if k in ("on", "mode", "core", "core_port", "server") else sync)
    sync()
    def shutdown():
        R["dead"] = True
        stop_core()
    api.on_unload(shutdown)
    atexit.register(shutdown)
    if api.config_get("on", False) and api.config_get("mode", "manual") == "core":
        threading.Thread(target=apply_core, daemon=True).start()

    def fetch_sub(url):
        """Скачивает подписку (с вашего ПК, напрямую) и обновляет список серверов. Возвращает число серверов."""
        items = fetch_subscription(url)
        d, seen = {}, {}
        for nm, ln in items:
            seen[nm] = seen.get(nm, 0) + 1
            d[nm if seen[nm] == 1 else f"{nm} ({seen[nm]})"] = ln
        keep = {k: v for k, v in servers().items() if k.startswith("★ ")}
        api.config_set("servers", {**d, **keep})
        return len(d)

    def download_core(core, report):
        return fetch_core(core, core_dir, [None, api.proxies("web")], report, api.explain_proxy_error)

    def check_text():
        url = cfg_url()
        if not url:
            return False, "Прокси не настроен или выключен."
        try:
            direct = get_ip(None)
        except Exception:
            direct = None
        try:
            via = get_ip({"http": url, "https": url})
        except Exception as e:
            return False, "Через прокси не получилось: " + api.explain_proxy_error(e)
        if direct and direct == via:
            return False, f"IP совпадает ({via}): трафик идёт напрямую или сервер выпускает с вашего адреса."
        return True, f"Через прокси: {via}" + (f", без прокси: {direct}" if direct else "") + ". Всё работает."

    def command(text, m):
        t = api.norm_ru(text)
        mm = re.match(r"^(включи|выключи|отключи|подключи|проверь)\s+(?:мне\s+)?(?:впн|vpn|прокси)$", t)
        if not mm:
            return None
        verb = mm.group(1)
        if verb == "проверь":
            if not api.config_get("on", False):
                return "Прокси сейчас выключен."
            if api.config_get("mode", "manual") == "core" and not running():
                err = apply_core()
                if err:
                    return "Проблема. Ядро не запустилось: " + err
            ok, msg = check_text()
            return ("Работает. " if ok else "Проблема. ") + msg
        on = verb in ("включи", "подключи")
        if on and api.config_get("mode", "manual") != "core" and not str(api.config_get("host", "")).strip():
            return "Прокси не настроен: укажите адрес на вкладке «Плагины»."
        api.ui(lambda: vars_["on"].set(on))
        if not on:
            return "Прокси выключен: интернет-запросы агента идут напрямую."
        if api.config_get("mode", "manual") == "core":
            err = apply_core()
            if err:
                return "Прокси включён, но ядро не запустилось: " + err
        ok, msg = check_text()
        return ("Прокси включён. " if ok else "Прокси включён, но есть проблема. ") + msg
    api.add_command(r"(?<!\w)(?:впн|vpn|прокси)(?!\w)", command, "включи/выключи/проверь впн")

    def build(f):
        pad = {"padx": 6, "pady": 2}
        ttk.Checkbutton(f, text="Направлять интернет-трафик агента через прокси", variable=vars_["on"]).grid(
            row=0, column=0, columnspan=6, sticky="w", **pad)
        ttk.Label(f, text="Сохранённые:").grid(row=1, column=0, sticky="w", **pad)
        sel = tk.StringVar(value="")
        combo = ttk.Combobox(f, textvariable=sel, values=list(api.config_get("profiles", {})), width=22, state="readonly")
        combo.grid(row=1, column=1, columnspan=2, sticky="w", **pad)

        def use(*a):
            p = api.config_get("profiles", {}).get(sel.get())
            if p:
                for k in ("scheme", "host", "port", "user", "pw"):
                    vars_[k].set(str(p.get(k, "")))

        def save():
            name = simpledialog.askstring("Прокси", "Название сервера:")
            if name and name.strip():
                d = dict(api.config_get("profiles", {}))
                d[name.strip()] = {k: vars_[k].get() for k in ("scheme", "host", "port", "user", "pw")}
                api.config_set("profiles", d)
                combo["values"] = list(d)
                sel.set(name.strip())

        def delete():
            d = dict(api.config_get("profiles", {}))
            if d.pop(sel.get(), None) is not None:
                api.config_set("profiles", d)
                combo["values"] = list(d)
                sel.set("")
        combo.bind("<<ComboboxSelected>>", use)
        ttk.Button(f, text="Сохранить как…", command=save).grid(row=1, column=3, sticky="w", **pad)
        ttk.Button(f, text="Удалить", command=delete).grid(row=1, column=4, sticky="w", **pad)
        ttk.Label(f, text="Тип / адрес / порт:").grid(row=2, column=0, sticky="w", **pad)
        ttk.Combobox(f, textvariable=vars_["scheme"], values=SCHEMES, width=8, state="readonly").grid(
            row=2, column=1, sticky="w", **pad)
        ttk.Entry(f, textvariable=vars_["host"], width=22).grid(row=2, column=2, sticky="w", **pad)
        ttk.Entry(f, textvariable=vars_["port"], width=7).grid(row=2, column=3, sticky="w", **pad)
        ttk.Label(f, text="Логин / пароль:").grid(row=3, column=0, sticky="w", **pad)
        ttk.Entry(f, textvariable=vars_["user"], width=14).grid(row=3, column=1, columnspan=2, sticky="w", **pad)
        ttk.Entry(f, textvariable=vars_["pw"], width=14, show="•").grid(row=3, column=3, columnspan=2, sticky="w", **pad)
        ttk.Label(f, text="Шаблоны клиентов:").grid(row=4, column=0, sticky="w", **pad)
        pf = ttk.Frame(f)
        pf.grid(row=4, column=1, columnspan=5, sticky="w")

        def preset(n):
            sch, host, port = PRESETS[n]
            vars_["scheme"].set(sch)
            vars_["host"].set(host)
            vars_["port"].set(str(port))
            vars_["user"].set("")
            vars_["pw"].set("")
        for n in PRESETS:
            ttk.Button(pf, text=n, command=lambda n=n: preset(n)).pack(side="left", padx=2)
        ttk.Label(f, text="Через прокси:").grid(row=5, column=0, sticky="w", **pad)
        sf = ttk.Frame(f)
        sf.grid(row=5, column=1, columnspan=5, sticky="w")
        ttk.Checkbutton(sf, text="Telegram", variable=vars_["tg"]).pack(side="left", padx=4)
        ttk.Checkbutton(sf, text="Поиск и чтение страниц", variable=vars_["web"]).pack(side="left", padx=4)
        ttk.Checkbutton(sf, text="Скачивание моделей Whisper", variable=vars_["models"]).pack(side="left", padx=4)

        def check_ui():
            if not vars_["on"].get():
                messagebox.showinfo("Прокси", "Сначала включите галочку «Направлять интернет-трафик агента через прокси».")
                return

            def work():
                ok, text = check_text()
                api.post(messagebox.showinfo if ok else messagebox.showwarning, "Прокси", text)
            threading.Thread(target=work, daemon=True).start()
        ttk.Button(f, text="Проверить IP", command=check_ui).grid(row=6, column=0, sticky="w", **pad)
        ttk.Label(f, text=("Агент подключается к прокси сам, как расширение браузера. Если у вас ссылка vless/vmess/trojan — "
                           "используйте встроенное ядро ниже или запустите клиент (v2rayN, Hiddify…), но системный прокси/VPN в нём включать не надо: "
                           "достаточно, чтобы он слушал порт. Если есть готовый SOCKS5/HTTP-прокси сервера — укажите его "
                           "напрямую. Для SOCKS нужен пакет pysocks. Пароль лежит в plugins/data/vpn_proxy/config.json "
                           "открытым текстом."),
                  foreground="#777", wraplength=780, justify="left").grid(row=7, column=0, columnspan=6, sticky="w", **pad)

        # ---- встроенное ядро
        cf = ttk.LabelFrame(f, text="Встроенное ядро (Xray / sing-box): ссылки vless, vmess, trojan, ss и подписки")
        cf.grid(row=8, column=0, columnspan=6, sticky="we", padx=6, pady=6)
        R["status"] = tk.StringVar(value="ядро не запущено" if not running() else "работает")
        r0 = ttk.Frame(cf)
        r0.grid(row=0, column=0, columnspan=6, sticky="w", **pad)
        ttk.Radiobutton(r0, text="Использовать адрес и порт выше (свой клиент или сервер)", variable=vars_["mode"],
                        value="manual").pack(side="left", padx=4)
        ttk.Radiobutton(r0, text="Использовать встроенное ядро", variable=vars_["mode"], value="core").pack(side="left", padx=4)
        ttk.Label(cf, text="Ядро / порт:").grid(row=1, column=0, sticky="w", **pad)
        c1 = ttk.Frame(cf)
        c1.grid(row=1, column=1, columnspan=5, sticky="w")
        ttk.Combobox(c1, textvariable=vars_["core"], values=CORES, width=9, state="readonly").pack(side="left", padx=4)
        ttk.Entry(c1, textvariable=vars_["core_port"], width=7).pack(side="left", padx=4)

        def dl_core():
            core = vars_["core"].get()

            def work():
                try:
                    exe = download_core(core, say_status)
                    say_status(f"ядро установлено: {exe}")
                    api.post(messagebox.showinfo, "Прокси", f"Ядро {core} установлено:\n{exe}")
                except Exception as e:
                    say_status(f"ошибка: {e}")
                    api.post(messagebox.showerror, "Прокси", f"Не удалось скачать ядро: {e}")
            threading.Thread(target=work, daemon=True).start()
        ttk.Button(c1, text="Скачать ядро", command=dl_core).pack(side="left", padx=4)

        def open_dir():
            core_dir.mkdir(parents=True, exist_ok=True)
            try:
                if sys.platform == "win32":
                    os.startfile(str(core_dir))
                else:
                    subprocess.Popen(["xdg-open", str(core_dir)])
            except Exception as e:
                messagebox.showinfo("Прокси", f"Папка ядра: {core_dir}\n({e})")
        ttk.Button(c1, text="Папка ядра", command=open_dir).pack(side="left", padx=4)
        ttk.Label(cf, text="Подписка (URL):").grid(row=2, column=0, sticky="w", **pad)
        ttk.Entry(cf, textvariable=vars_["sub_url"], width=52).grid(row=2, column=1, columnspan=3, sticky="w", **pad)

        combo2 = ttk.Combobox(cf, textvariable=vars_["server"], values=list(servers()), width=50, state="readonly")

        def refresh_servers():
            combo2["values"] = list(servers())

        def upd_sub():
            url = vars_["sub_url"].get().strip()
            if not url:
                return messagebox.showinfo("Прокси", "Вставьте адрес подписки.")

            def work():
                try:
                    n = fetch_sub(url)
                    api.post(refresh_servers)
                    say_status(f"подписка обновлена: серверов {n}")
                    api.post(messagebox.showinfo, "Прокси", f"Серверов в подписке: {n}. Выберите сервер из списка.")
                except Exception as e:
                    say_status(f"ошибка подписки: {e}")
                    api.post(messagebox.showerror, "Прокси", f"Не удалось получить подписку: {e}")
            threading.Thread(target=work, daemon=True).start()
        ttk.Button(cf, text="Обновить подписку", command=upd_sub).grid(row=2, column=4, sticky="w", **pad)
        ttk.Label(cf, text="Сервер:").grid(row=3, column=0, sticky="w", **pad)
        combo2.grid(row=3, column=1, columnspan=4, sticky="w", **pad)
        ttk.Label(cf, text="Своя ссылка:").grid(row=4, column=0, sticky="w", **pad)
        link_var = tk.StringVar(value="")
        ttk.Entry(cf, textvariable=link_var, width=52, show="•").grid(row=4, column=1, columnspan=3, sticky="w", **pad)

        def add_link():
            try:
                ob = parse_link(link_var.get())
            except ValueError as e:
                return messagebox.showerror("Прокси", f"Ссылка не подходит: {e}")
            d = dict(servers())
            nm = "★ " + ob["name"]
            d[nm] = link_var.get().strip()
            api.config_set("servers", d)
            refresh_servers()
            vars_["server"].set(nm)
            link_var.set("")
        ttk.Button(cf, text="Добавить ссылку", command=add_link).grid(row=4, column=4, sticky="w", **pad)

        def del_server():
            d = dict(servers())
            if d.pop(vars_["server"].get(), None) is not None:
                api.config_set("servers", d)
                refresh_servers()
                vars_["server"].set("")
        ttk.Button(cf, text="Удалить сервер", command=del_server).grid(row=5, column=1, sticky="w", **pad)

        def restart():
            def work():
                stop_core()
                err = apply_core()
                if err:
                    api.post(messagebox.showerror, "Прокси", err)
            threading.Thread(target=work, daemon=True).start()
        ttk.Button(cf, text="Перезапустить ядро", command=restart).grid(row=5, column=2, sticky="w", **pad)
        ttk.Label(cf, textvariable=R["status"], foreground="#777", wraplength=700, justify="left").grid(
            row=6, column=0, columnspan=6, sticky="w", **pad)
        ttk.Label(cf, text=("Как пользоваться: нажмите «Скачать ядро», вставьте адрес подписки и нажмите «Обновить подписку», "
                            "выберите сервер, выберите «Использовать встроенное ядро» и включите галочку наверху. Ядро слушает "
                            "только 127.0.0.1 и закрывается вместе с агентом. Ссылки и подписка лежат в plugins/data/vpn_proxy/"
                            "config.json открытым текстом. Если GitHub недоступен, положите xray.exe или sing-box.exe "
                            "в «Папку ядра» вручную."),
                  foreground="#777", wraplength=760, justify="left").grid(row=7, column=0, columnspan=6, sticky="w", **pad)
    api.add_settings(build)
