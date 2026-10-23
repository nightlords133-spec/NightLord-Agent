# Инструмент Nightlord агента: проверка сети. Пинг, DNS, порт, статус сайта, внешний IP, скорость, общий обзор.
import platform
import re
import socket
import subprocess
import time

import requests

PLUGIN = {"name": "Проверка сети", "version": "1.0", "group": "tools",
          "description": "«проверь интернет», «пингани ya.ru», «открыт ли порт 25565 на 192.168.1.5», «работает ли сайт github.com», "
                         "«мой внешний ip», «dns для ya.ru», «проверь скорость». Показывает, на каком этапе обрыв: сеть, DNS или сам сайт."}

HOST_RE = re.compile(r"^(?:[a-z0-9а-яё](?:[a-z0-9а-яё\-]{0,61}[a-z0-9а-яё])?\.)*[a-z0-9а-яё][a-z0-9а-яё\-]{0,61}$|^\d{1,3}(?:\.\d{1,3}){3}$|^[0-9a-f:]+$", re.I)
UA = {"User-Agent": "NightlordAgent-NetCheck/1.0"}


def clean_host(h):
    h = str(h or "").strip().lower()
    h = re.sub(r"^https?://", "", h).split("/")[0].split("?")[0]
    if h.count(":") == 1:
        h = h.split(":")[0]
    if not HOST_RE.match(h) or len(h) > 253:
        raise ValueError(f"не похоже на адрес: «{h[:40]}»")
    return h


def ping_cmd(host, n=4):
    if platform.system() == "Windows":
        return ["ping", "-n", str(n), "-w", "2000", host]
    return ["ping", "-c", str(n), "-W", "2", host]


def parse_ping(out):
    sent = recv = None
    m = re.search(r"(\d+) packets transmitted, (\d+) (?:packets )?received", out)
    if m:
        sent, recv = int(m.group(1)), int(m.group(2))
    else:
        m = re.search(r"(?:Sent|Отправлено) = (\d+), (?:Received|Получено) = (\d+)", out)
        if m:
            sent, recv = int(m.group(1)), int(m.group(2))
    avg = None
    m = re.search(r"= [\d.]+/([\d.]+)/[\d.]+", out) or re.search(r"(?:Average|Среднее) = (\d+)", out)
    if m:
        avg = float(m.group(1))
    return sent, recv, avg


def register(api):
    for k in ("ping_on", "dns_on", "port_on", "http_on", "ip_on", "speed_on", "overview_on"):
        api.var(k, True)
    api.var("use_proxy", False)
    api.var("check_hosts", "ya.ru, 1.1.1.1, github.com")
    api.var("speed_mb", 10)

    def off(k, name):
        return None if api.config_get(k, True) else f"Проверка «{name}» выключена в настройках инструмента."

    def prox():
        return api.proxies("web") if api.config_get("use_proxy", False) else None

    def get(url, **kw):
        s = requests.Session()
        s.trust_env = False
        try:
            return s.get(url, timeout=kw.pop("timeout", 10), headers=UA, proxies=prox(), **kw)
        except requests.RequestException as e:
            raise RuntimeError(api.explain_proxy_error(e)) from None

    def ping(args):
        if off("ping_on", "пинг"):
            return off("ping_on", "пинг")
        try:
            h = clean_host(args.get("host"))
        except ValueError as e:
            return f"{e}."
        try:
            p = subprocess.run(ping_cmd(h), capture_output=True, timeout=25)
        except FileNotFoundError:
            return "В системе нет команды ping."
        except subprocess.TimeoutExpired:
            return f"{h}: пинг завис, ответа нет."
        out = (p.stdout or b"").decode("cp866" if platform.system() == "Windows" else "utf-8", "replace")
        sent, recv, avg = parse_ping(out)
        if sent is None:
            return f"{h}: не разобрал ответ ping (код {p.returncode})."
        if recv == 0:
            return f"{h}: потеряно {sent} из {sent}, хост не отвечает (возможно, он просто блокирует ping)."
        return f"{h}: ответили {recv} из {sent}" + (f", в среднем {avg:.0f} мс" if avg is not None else "") + "."

    def dns(args):
        if off("dns_on", "DNS"):
            return off("dns_on", "DNS")
        try:
            h = clean_host(args.get("host"))
        except ValueError as e:
            return f"{e}."
        t = time.time()
        try:
            res = socket.getaddrinfo(h, None)
        except socket.gaierror as e:
            return f"{h}: DNS не отвечает или имени нет ({e.args[-1] if e.args else e})."
        ips = []
        for r in res:
            if r[4][0] not in ips:
                ips.append(r[4][0])
        return f"{h} → {', '.join(ips[:6])} ({(time.time() - t) * 1000:.0f} мс)."

    def port(args):
        if off("port_on", "порт"):
            return off("port_on", "порт")
        try:
            h = clean_host(args.get("host") or "127.0.0.1")
            p = int(args.get("port"))
            if not 1 <= p <= 65535:
                raise ValueError
        except (TypeError, ValueError):
            return "Нужны адрес и порт 1–65535."
        t = time.time()
        try:
            with socket.create_connection((h, p), timeout=4):
                return f"{h}:{p} открыт ({(time.time() - t) * 1000:.0f} мс)."
        except socket.timeout:
            return f"{h}:{p}: нет ответа за 4 с (закрыт файрволом или хост недоступен)."
        except ConnectionRefusedError:
            return f"{h}:{p}: соединение отклонено (хост жив, порт закрыт)."
        except OSError as e:
            return f"{h}:{p}: не вышло подключиться ({e.strerror or e})."

    def http(args):
        if off("http_on", "сайт"):
            return off("http_on", "сайт")
        u = str(args.get("url") or "").strip()
        if not re.match(r"^https?://", u):
            try:
                u = "https://" + clean_host(u)
            except ValueError as e:
                return f"{e}."
        t = time.time()
        try:
            r = get(u, stream=True, allow_redirects=True)
            r.close()
        except RuntimeError as e:
            return f"{u}: недоступен — {e}"
        ms = (time.time() - t) * 1000
        return f"{u}: код {r.status_code} за {ms:.0f} мс" + (f", после переадресации на {r.url}" if r.url.rstrip('/') != u.rstrip('/') else "") + \
            (" — сайт работает." if r.status_code < 400 else " — сайт отвечает ошибкой.")

    def myip(args):
        if off("ip_on", "внешний IP"):
            return off("ip_on", "внешний IP")
        last = "нет ответа"
        for url in ("https://api.ipify.org", "https://ifconfig.me/ip", "https://icanhazip.com"):
            try:
                r = get(url, timeout=6)
                ip = r.text.strip()
                if r.ok and re.match(r"^[0-9a-f:.]{3,45}$", ip):
                    return f"Внешний IP: {ip}" + (" (через прокси агента)" if prox() else "") + "."
            except RuntimeError as e:
                last = str(e)
        return f"Внешний IP узнать не вышло: {last}"

    def speed(args):
        if off("speed_on", "скорость"):
            return off("speed_on", "скорость")
        try:
            mb = max(1, min(int(api.config_get("speed_mb", 10) or 10), 100))
        except (TypeError, ValueError):
            mb = 10
        t = time.time()
        got = 0
        try:
            r = get(f"https://speed.cloudflare.com/__down?bytes={mb * 1000000}", stream=True, timeout=15)
            if not r.ok:
                return f"Тест скорости: сервер ответил кодом {r.status_code}."
            for ch in r.iter_content(1 << 16):
                got += len(ch)
                if time.time() - t > 20:
                    break
        except RuntimeError as e:
            return f"Тест скорости не удался: {e}"
        dt = max(time.time() - t, 0.001)
        return f"Скачивание: {got * 8 / dt / 1e6:.1f} Мбит/с (загружено {got / 1e6:.1f} МБ за {dt:.1f} с). Тест только на загрузку."

    def overview(args):
        if off("overview_on", "общий обзор"):
            return off("overview_on", "общий обзор")
        hosts = [h.strip() for h in str(api.config_get("check_hosts", "")).split(",") if h.strip()][:6] or ["1.1.1.1"]
        lines = []
        ok_ip = False
        for h in hosts:
            try:
                h = clean_host(h)
            except ValueError:
                continue
            if re.match(r"^\d", h):
                r = port({"host": h, "port": 443})
                ok_ip = ok_ip or "открыт" in r
            else:
                r = dns({"host": h})
            lines.append(r)
        net = "есть" if ok_ip else "не подтверждена по IP"
        verdict = "DNS в порядке" if any("→" in l for l in lines) else "DNS не отвечает"
        return f"Сеть: {net}; {verdict}.\n" + "\n".join(lines)

    api.add_tool("net_overview", "Общая проверка интернета: достижимость по IP и работа DNS по списку из настроек.", {}, overview)
    api.add_tool("net_ping", "Пинг хоста.", {"host": {"type": "string"}}, ping, ["host"])
    api.add_tool("net_dns", "Какие IP у доменного имени.", {"host": {"type": "string"}}, dns, ["host"])
    api.add_tool("net_port", "Открыт ли TCP-порт на хосте.", {"host": {"type": "string"}, "port": {"type": "integer"}}, port, ["host", "port"])
    api.add_tool("net_site", "Работает ли сайт: код ответа и время.", {"url": {"type": "string"}}, http, ["url"])
    api.add_tool("net_my_ip", "Внешний IP-адрес этого ПК.", {}, myip)
    api.add_tool("net_speed", "Тест скорости скачивания (Cloudflare).", {}, speed)

    def command(text, m):
        t = api.norm_ru(text).strip().rstrip("?.!")
        mm = re.match(r"^(?:пингани|пинг|запингуй|проверь пинг(?: до)?)\s+(\S+)$", t)
        if mm:
            return ping({"host": mm.group(1)})
        mm = re.match(r"^(?:dns для|днс для|какой ip у|какой айпи у|резолв)\s+(\S+)$", t)
        if mm:
            return dns({"host": mm.group(1)})
        mm = re.match(r"^(?:открыт ли )?порт (\d{1,5}) (?:открыт )?на (\S+?)(?: открыт)?$", t)
        if mm:
            return port({"host": mm.group(2), "port": mm.group(1)})
        mm = re.match(r"^(?:работает ли|открывается ли|проверь сайт|проверь)\s+(?:сайт )?([\w.\-]+\.[a-zа-я]{2,})$", t)
        if mm:
            return http({"url": mm.group(1)})
        if re.match(r"^(?:мой внешний (?:ip|айпи)|какой у меня (?:внешний )?(?:ip|айпи))$", t):
            return myip({})
        if re.match(r"^(?:проверь|замерь|тест) скорост\w*(?: интернета)?$", t):
            return speed({})
        if re.match(r"^(?:проверь|есть ли) (?:интернет|сеть)(?:\?)?$", t) or t == "проверь сеть":
            return overview({})
        return None

    api.add_command(r"(?:пингани|пинг|запингуй|проверь|dns для|днс для|какой ip у|какой айпи у|порт \d|открыт ли|работает ли|открывается ли|мой внешний|какой у меня|замерь|тест скорост|есть ли)",
                    command, "проверь интернет / пингани ya.ru / порт 25565 на 192.168.1.5 / работает ли github.com / мой внешний ip")

    def build(fr):
        from tkinter import ttk
        pad = {"padx": 6, "pady": 2}
        for i, (k, txt) in enumerate((("overview_on", "Общая проверка интернета"), ("ping_on", "Пинг"), ("dns_on", "DNS"),
                                      ("port_on", "Проверка TCP-порта"), ("http_on", "Проверка сайта (код ответа и время)"),
                                      ("ip_on", "Внешний IP (api.ipify.org и аналоги)"),
                                      ("speed_on", "Тест скорости скачивания (Cloudflare, тратит трафик)"),
                                      ("use_proxy", "HTTP-проверки (сайт, IP, скорость) идут через веб-прокси агента"))):
            ttk.Checkbutton(fr, text=txt, variable=api.var(k)).grid(row=i, column=0, columnspan=3, sticky="w", **pad)
        ttk.Label(fr, text="Хосты для общей проверки:").grid(row=8, column=0, sticky="w", **pad)
        ttk.Entry(fr, textvariable=api.var("check_hosts"), width=44).grid(row=8, column=1, columnspan=2, sticky="w", **pad)
        ttk.Label(fr, text="Размер теста скорости, МБ:").grid(row=9, column=0, sticky="w", **pad)
        ttk.Spinbox(fr, from_=1, to=100, width=5, textvariable=api.var("speed_mb")).grid(row=9, column=1, sticky="w", **pad)
        ttk.Label(fr, text=("Пинг, DNS и порты проверяются напрямую с этого ПК, мимо прокси — так видно, жива ли сама сеть. "
                            "Тест скорости измеряет только загрузку. Внешние сервисы (ipify, Cloudflare) увидят ваш IP."),
                  foreground="#777", wraplength=720, justify="left").grid(row=10, column=0, columnspan=3, sticky="w", **pad)
    api.add_settings(build)
