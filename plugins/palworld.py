# Плагин Nightlord агента: Palworld — управление выделенным сервером голосом (REST API игры) и запуск игры.
# Что умеет: статус сервера, кто онлайн, объявление в чат, сохранение мира, остановка с таймером, кик игрока,
# запуск сервера и игры, уведомления «зашёл / вышел», автосохранение. Игровой процесс плагин НЕ автоматизирует.
import re
import subprocess
import threading
import time
import tkinter as tk
from tkinter import ttk, messagebox

import requests

PLUGIN = {"name": "Palworld", "version": "1.1",
          "description": "Сервер Palworld голосом: «статус сервера палворлд», «кто онлайн на сервере палворлд», "
                         "«объяви на сервере палворлд …», «сохрани мир палворлд», «останови сервер палворлд через 5 минут», "
                         "«кикни Ивана с сервера палворлд», «запусти палворлд». Нужен выделенный сервер с включённым REST API."}

PAL = r"(?:п[аеэ]л ?в[ао]р?лд\w*|palworld)"
WORDS = {"одну": 1, "одна": 1, "две": 2, "два": 2, "три": 3, "четыре": 4, "пять": 5, "десять": 10, "пятнадцать": 15,
         "двадцать": 20, "тридцать": 30, "сорок пять": 45, "шесть": 6, "семь": 7, "восемь": 8, "девять": 9}
STEAM_ID = "1623730"


def fmt_uptime(sec):
    sec = int(sec or 0)
    h, m = sec // 3600, sec % 3600 // 60
    return (f"{h} ч {m} мин" if h else f"{m} мин")


def pal_error(e, status=None):
    """Понятное объяснение сбоя вместо сырого исключения."""
    if status == 401:
        return "сервер отклонил пароль администратора"
    if status == 404:
        return "сервер не знает такой запрос (старая версия сервера или REST API выключен)"
    if isinstance(e, requests.ConnectionError):
        return "сервер не отвечает: он выключен или в настройках не включён REST API (RESTAPIEnabled=True)"
    if isinstance(e, requests.Timeout):
        return "сервер не ответил вовремя"
    return str(e)[:150]


def parse_minutes(text):
    """«через 5 минут» / «через пять минут» -> 5; нет числа -> None."""
    m = re.search(r"через (\d+|[а-я]+(?: [а-я]+)?) (?:минут\w*|мин)", text)
    if not m:
        return None
    w = m.group(1)
    return int(w) if w.isdigit() else WORDS.get(w)


def match_player(players, name_norm, norm):
    """Игроки, чьё имя похоже на сказанное (без регистра, по началу слова)."""
    out = []
    for p in players:
        for nm in (p.get("name"), p.get("accountName")):
            n = norm(str(nm or ""))
            if n and (n == name_norm or n.startswith(name_norm[:max(3, len(name_norm) - 2)]) or name_norm.startswith(n[:max(3, len(n) - 2)])):
                out.append(p)
                break
    return out


def register(api):
    api.var("host", "127.0.0.1")
    api.var("port", "8212")
    api.var("user", "admin")
    api.var("password", "")
    api.var("https", False)
    api.var("confirm", True)
    api.var("notify", False)
    api.var("autosave", 0)
    api.var("server_cmd", "")
    api.var("restart_on", False)        # перезапуск сервера по расписанию
    api.var("restart_times", "04:00")   # время(на) через запятую
    api.var("restart_warn", 5)          # за сколько минут предупредить игроков
    api.var("restart_start", True)      # после остановки снова запустить server_cmd
    api.var("game_id", STEAM_ID)
    state = {"names": None, "last_save": time.time(), "restarting": False, "done": {}}

    def base():
        scheme = "https" if api.config_get("https", False) else "http"
        host = str(api.config_get("host", "127.0.0.1")).strip() or "127.0.0.1"
        return f"{scheme}://{host}:{str(api.config_get('port', '8212')).strip()}/v1/api"

    def call(method, path, body=None, timeout=6):
        pw = str(api.config_get("password", ""))
        if not pw:
            raise RuntimeError("не задан пароль администратора сервера (вкладка «Плагины»)")
        s = requests.Session()
        s.trust_env = False                 # локальный сервер — мимо системных прокси
        try:
            r = s.request(method, base() + path, json=body, auth=(str(api.config_get("user", "admin")), pw),
                          timeout=timeout, verify=False)
        except requests.RequestException as e:
            raise RuntimeError(pal_error(e)) from None
        if r.status_code >= 400:
            raise RuntimeError(pal_error(None, r.status_code) if r.status_code in (401, 404) else f"ошибка сервера {r.status_code}")
        try:
            return r.json()
        except ValueError:
            return {}

    def players():
        return (call("GET", "/players") or {}).get("players") or []

    def status_text():
        info, met = call("GET", "/info") or {}, call("GET", "/metrics") or {}
        name = info.get("servername") or "Palworld"
        s = f"Сервер «{name}»: онлайн {met.get('currentplayernum', '?')} из {met.get('maxplayernum', '?')}"
        if met.get("serverfps"):
            s += f", FPS {met['serverfps']}"
        if met.get("uptime") is not None:
            s += f", работает {fmt_uptime(met['uptime'])}"
        if met.get("days") is not None:
            s += f", день мира {met['days']}"
        return s + "."

    def players_text():
        ps = players()
        if not ps:
            return "На сервере сейчас никого нет."
        names = [str(p.get("name") or p.get("accountName") or "?") for p in ps]
        return f"На сервере {len(ps)}: " + ", ".join(names[:8]) + (f" и ещё {len(names) - 8}" if len(names) > 8 else "") + "."

    def ask(text):
        return (not api.config_get("confirm", True)) or api.confirm(text)

    def guard(fn):
        try:
            return fn()
        except RuntimeError as e:
            return f"Не получилось: {e}."

    def do_announce(msg):
        msg = str(msg).strip()[:300]
        if not msg:
            return "Нечего объявлять."
        if not ask(f"Объявить на сервере Palworld?\n\n{msg}"):
            return "Не объявляю."
        call("POST", "/announce", {"message": msg})
        return "Объявил на сервере."

    def do_save():
        call("POST", "/save", timeout=30)
        state["last_save"] = time.time()
        return "Мир сохранён."

    def do_shutdown(minutes):
        minutes = 1 if minutes is None else max(0, min(int(minutes), 120))
        if not ask(f"Остановить сервер Palworld через {minutes} мин?\n\nИгроки получат предупреждение."):
            return "Не останавливаю."
        call("POST", "/shutdown", {"waittime": minutes * 60, "message": f"Сервер выключится через {minutes} мин. Сохраняйтесь!"})
        return f"Сервер остановится через {minutes} мин."

    def do_kick(name_norm):
        ps = players()
        found = match_player(ps, name_norm, api.norm_ru)
        if not found:
            return f"Не вижу на сервере игрока «{name_norm}»."
        if len(found) > 1:
            return "Подходят несколько игроков: " + ", ".join(str(p.get("name")) for p in found[:5]) + ". Уточните."
        p = found[0]
        if not ask(f"Выгнать игрока {p.get('name')} с сервера Palworld?"):
            return "Не выгоняю."
        call("POST", "/kick", {"userid": p.get("userId"), "message": "Вас отключил администратор"})
        return f"Игрок {p.get('name')} отключён."

    def start_server():
        cmd = str(api.config_get("server_cmd", "")).strip()
        if not cmd:
            return "Не задан путь к запуску сервера в настройках плагина."
        if not ask(f"Запустить сервер Palworld?\n\n{cmd}"):
            return "Не запускаю."
        try:
            subprocess.Popen(cmd, shell=True, cwd=None)
        except OSError as e:
            return f"Не запустилось: {e}."
        return "Запускаю сервер. Загрузка занимает минуту-две."

    def restart_job(warn_min, start_after):
        """Предупредить, сохранить, остановить с таймером и (если задано) запустить заново."""
        state["restarting"] = True
        try:
            warn_min = max(1, min(int(warn_min), 30))
            try:
                call("POST", "/announce", {"message": f"Плановый перезапуск сервера через {warn_min} мин. Сохраняйтесь!"})
                call("POST", "/save", timeout=30)
                call("POST", "/shutdown", {"waittime": warn_min * 60, "message": f"Перезапуск через {warn_min} мин."})
            except RuntimeError as e:
                api.log(f"перезапуск не удался: {e}")
                return
            api.log(f"перезапуск сервера через {warn_min} мин")
            time.sleep(warn_min * 60 + 15)
            for _ in range(24):                       # ждём, пока сервер реально остановится (до 2 минут)
                try:
                    call("GET", "/info", timeout=3)
                    time.sleep(5)
                except RuntimeError:
                    break
            cmd = str(api.config_get("server_cmd", "")).strip()
            if start_after and cmd:
                time.sleep(5)
                try:
                    subprocess.Popen(cmd, shell=True, cwd=None)
                    api.log("сервер запущен заново")
                except OSError as e:
                    api.log(f"не удалось запустить сервер: {e}")
            elif start_after:
                api.log("перезапуск: не задана команда запуска сервера, сервер остановлен")
        finally:
            state["restarting"] = False

    def do_restart(minutes):
        if state["restarting"]:
            return "Перезапуск уже идёт."
        minutes = 1 if minutes is None else max(1, min(int(minutes), 30))
        cmd = str(api.config_get("server_cmd", "")).strip()
        if not ask(f"Перезапустить сервер Palworld через {minutes} мин?" +
                   ("" if cmd else "\n\nКоманда запуска не задана — сервер только остановится.")):
            return "Не перезапускаю."
        threading.Thread(target=restart_job, args=(minutes, True), daemon=True).start()
        return f"Перезапущу сервер через {minutes} мин, игроков предупредил."

    def scheduled_restart():
        if not api.config_get("restart_on", False) or state["restarting"]:
            return
        now = time.localtime()
        today = time.strftime("%Y-%m-%d", now)
        try:
            warn = int(api.config_get("restart_warn", 5) or 5)
        except (TypeError, ValueError):
            warn = 5
        for item in str(api.config_get("restart_times", "")).replace(";", ",").split(","):
            m = re.match(r"^\s*(\d{1,2})[:.](\d{2})\s*$", item)
            if not m or not (0 <= int(m.group(1)) < 24 and int(m.group(2)) < 60):
                continue
            key = f"{today} {int(m.group(1)):02d}:{m.group(2)}"
            # стартуем за warn минут до заданного времени, чтобы сервер остановился ровно в него
            due = int(m.group(1)) * 60 + int(m.group(2)) - warn
            cur = now.tm_hour * 60 + now.tm_min
            if due < 0:
                due += 1440
            if cur == due and key not in state["done"]:
                state["done"] = {k: 1 for k in state["done"] if k.startswith(today)}
                state["done"][key] = 1
                threading.Thread(target=restart_job, args=(warn, bool(api.config_get("restart_start", True))),
                                 daemon=True).start()
                return

    def start_game():
        gid = str(api.config_get("game_id", STEAM_ID)).strip() or STEAM_ID
        if not gid.isdigit():
            return "Неверный номер игры в Steam."
        try:
            import os
            os.startfile(f"steam://rungameid/{gid}")        # только Windows; Steam должен быть установлен
        except (AttributeError, OSError) as e:
            return f"Не получилось открыть Steam: {e}."
        return "Запускаю Palworld через Steam."

    def command(text, m):
        t = api.norm_ru(text)
        if not re.search(PAL, t):
            return None
        mm = re.match(rf"^\s*(?:объяви|скажи всем|сообщи всем|напиши в чат)\s+(?:на\s+)?(?:сервер\w*\s+)?{PAL}\W*\s+(.+)$", text.strip(), re.I | re.S)
        if mm:
            return guard(lambda: do_announce(mm.group(1)))
        if re.search(r"(?:перезапусти|перезагрузи|рестарт)\w* сервер", t):
            return guard(lambda: do_restart(parse_minutes(t)))
        if re.search(r"(?:запусти|включи|подними)\w* сервер", t):
            return start_server()
        if re.search(r"(?:останови|выключи|заверши|закрой)\w* сервер", t):
            return guard(lambda: do_shutdown(parse_minutes(t)))
        mm = re.match(rf"^(?:кикни|выгони|выкинь|отключи) (?:игрока )?(.+?) (?:с|со|из) сервера {PAL}$", t)
        if mm:
            return guard(lambda: do_kick(mm.group(1)))
        if re.search(r"(?:сохрани|сохранить|save)\w*", t) and re.search(r"(?:мир|сервер|игр)", t):
            return guard(do_save)
        if re.search(r"(?:кто|сколько|список|игроки|игроков|народу|людей|человек)", t) and re.search(r"(?:онлайн|на сервере|играет|играют|игрок)", t):
            return guard(players_text)
        if "сервер" in t and re.search(r"(?:статус|состояние|проверь|как там|как дела|работает)", t):
            return guard(status_text)
        if re.match(rf"^(?:запусти|открой|включи|стартани) (?:игру )?{PAL}$", t):
            return start_game()
        return None

    api.add_command(r"(?:п[аеэ]л ?в[ао]р?лд|palworld)", command,
                    "статус / кто онлайн / объяви / сохрани мир / останови сервер / кикни … палворлд / запусти палворлд")
    api.add_tool("palworld_status", "Состояние сервера Palworld: игроки онлайн, FPS, время работы.", {},
                 lambda a: guard(status_text), [])
    api.add_tool("palworld_players", "Кто сейчас играет на сервере Palworld.", {}, lambda a: guard(players_text), [])
    api.add_tool("palworld_announce", "Отправляет объявление в чат сервера Palworld (с подтверждением).",
                 {"message": {"type": "string", "description": "Текст объявления"}},
                 lambda a: guard(lambda: do_announce(a.get("message", ""))), ["message"])
    api.add_tool("palworld_save", "Сохраняет мир на сервере Palworld.", {}, lambda a: guard(do_save), [])

    def poll():
        """Уведомления «зашёл / вышел» и автосохранение."""
        if not str(api.config_get("password", "")):
            return
        scheduled_restart()
        try:
            names = {str(p.get("name") or p.get("accountName") or "?") for p in players()}
        except RuntimeError:
            state["names"] = None
            return
        if api.config_get("notify", False):
            if state["names"] is not None:
                for n in sorted(names - state["names"]):
                    api.say(f"На сервере Palworld зашёл {n}.")
                for n in sorted(state["names"] - names):
                    api.say(f"С сервера Palworld вышел {n}.")
        state["names"] = names
        try:
            mins = int(api.config_get("autosave", 0) or 0)
        except (TypeError, ValueError):
            mins = 0
        if mins > 0 and time.time() - state["last_save"] >= mins * 60:
            try:
                do_save()
                api.log("автосохранение мира")
            except RuntimeError as e:
                api.log(f"автосохранение не удалось: {e}")
    api.add_timer(30, poll)

    def build(f):
        pad = {"padx": 6, "pady": 2}
        ttk.Label(f, text="Адрес / порт REST API:").grid(row=0, column=0, sticky="w", **pad)
        ttk.Entry(f, textvariable=api.var("host"), width=22).grid(row=0, column=1, sticky="w", **pad)
        ttk.Entry(f, textvariable=api.var("port"), width=7).grid(row=0, column=2, sticky="w", **pad)
        ttk.Checkbutton(f, text="https", variable=api.var("https")).grid(row=0, column=3, sticky="w", **pad)
        ttk.Label(f, text="Логин / пароль админа:").grid(row=1, column=0, sticky="w", **pad)
        ttk.Entry(f, textvariable=api.var("user"), width=12).grid(row=1, column=1, sticky="w", **pad)
        ttk.Entry(f, textvariable=api.var("password"), width=22, show="•").grid(row=1, column=2, columnspan=2, sticky="w", **pad)

        def check():
            def work():
                try:
                    msg = status_text()
                except RuntimeError as e:
                    api.post(messagebox.showerror, "Palworld", str(e))
                    return
                api.post(messagebox.showinfo, "Palworld", msg)
            threading.Thread(target=work, daemon=True).start()
        ttk.Button(f, text="Проверить сервер", command=check).grid(row=2, column=0, sticky="w", **pad)
        ttk.Checkbutton(f, text="Спрашивать подтверждение (объявление, остановка, кик, запуск)", variable=api.var("confirm")).grid(
            row=3, column=0, columnspan=4, sticky="w", **pad)
        ttk.Checkbutton(f, text="Сообщать голосом, кто зашёл / вышел (проверка раз в 30 с)", variable=api.var("notify")).grid(
            row=4, column=0, columnspan=4, sticky="w", **pad)
        ttk.Label(f, text="Автосохранение мира, минут (0 — выкл):").grid(row=5, column=0, columnspan=2, sticky="w", **pad)
        ttk.Entry(f, textvariable=api.var("autosave"), width=6).grid(row=5, column=2, sticky="w", **pad)
        ttk.Label(f, text="Запуск сервера (команда или .bat):").grid(row=6, column=0, sticky="w", **pad)
        ttk.Entry(f, textvariable=api.var("server_cmd"), width=52).grid(row=6, column=1, columnspan=3, sticky="w", **pad)
        ttk.Label(f, text="Номер игры в Steam:").grid(row=7, column=0, sticky="w", **pad)
        ttk.Entry(f, textvariable=api.var("game_id"), width=12).grid(row=7, column=1, sticky="w", **pad)
        ttk.Checkbutton(f, text="Перезапускать сервер по расписанию (предупреждение, сохранение, остановка, запуск):",
                        variable=api.var("restart_on")).grid(row=9, column=0, columnspan=4, sticky="w", **pad)
        ttk.Label(f, text="Время (ЧЧ:ММ через запятую) / предупредить за, мин:").grid(row=10, column=0, sticky="w", **pad)
        ttk.Entry(f, textvariable=api.var("restart_times"), width=22).grid(row=10, column=1, sticky="w", **pad)
        ttk.Spinbox(f, from_=1, to=30, width=5, textvariable=api.var("restart_warn")).grid(row=10, column=2, sticky="w", **pad)
        ttk.Checkbutton(f, text="После остановки запускать сервер командой выше", variable=api.var("restart_start")).grid(
            row=11, column=0, columnspan=4, sticky="w", **pad)
        ttk.Label(f, text=("Как включить REST API: в PalWorldSettings.ini сервера задайте RESTAPIEnabled=True, RESTAPIPort=8212 и "
                           "AdminPassword=\"ваш_пароль\", перезапустите сервер. Не открывайте порт REST API в интернет: это админ-доступ. "
                           "Пароль лежит в plugins/data/palworld/config.json открытым текстом. Плагин управляет сервером и запускает игру, "
                           "но не играет за вас. Имена запросов REST API я знаю по документации игры и вживую не проверял; номер Steam "
                           "1623730 тоже из памяти — сверьте со Steam. У обычной игры (не сервера) REST API нет: команды про сервер "
                           "работают только с выделенным сервером или тем, что вы хостите сами."),
                  foreground="#777", wraplength=780, justify="left").grid(row=12, column=0, columnspan=4, sticky="w", **pad)
    api.add_settings(build)
