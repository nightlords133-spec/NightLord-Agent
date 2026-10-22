# Плагин Nightlord агента: игровой режим. Пока в полноэкранном режиме идёт игра (или запущен процесс из вашего списка),
# агент снижает нагрузку: закрывает 3D-аватар, перестаёт слушать микрофон, реже опрашивает мониторинг,
# понижает свой приоритет и выгружает Whisper (освобождает видеопамять). После игры всё возвращается.
# Определение игры — по полноэкранному окну на переднем плане (Windows) и по списку процессов.
import json
import os
import re
import tkinter as tk
from tkinter import ttk

import psutil
import requests

PLUGIN = {"name": "Игровой режим", "version": "1.0",
          "description": "Автоматически снижает нагрузку агента на время игры: «включи игровой режим», "
                         "«выключи игровой режим», «игровой режим статус». Каждое действие включается отдельно."}

IS_WIN = os.name == "nt"
DEFAULT_IGNORE = "explorer.exe, chrome.exe, msedge.exe, firefox.exe, opera.exe, browser.exe, vlc.exe, mpc-hc64.exe, " \
                 "potplayer64.exe, python.exe, pythonw.exe, nightlordagent.exe, voiceagent.exe, applicationframehost.exe, " \
                 "searchhost.exe, shellexperiencehost.exe, startmenuexperiencehost.exe, lockapp.exe, textinputhost.exe"


def split_names(s):
    return {x.strip().lower() for x in re.split(r"[,;\n]+", str(s or "")) if x.strip()}


def foreground_info():
    """(имя процесса, окно занимает весь монитор) для окна на переднем плане. Вне Windows — (None, False)."""
    if not IS_WIN:
        return None, False
    import ctypes
    from ctypes import wintypes
    u = ctypes.windll.user32
    hwnd = u.GetForegroundWindow()
    if not hwnd:
        return None, False
    buf = ctypes.create_unicode_buffer(256)
    u.GetClassNameW(hwnd, buf, 256)
    if buf.value in ("Progman", "WorkerW", "Shell_TrayWnd"):      # рабочий стол
        return None, False
    rect = wintypes.RECT()
    u.GetWindowRect(hwnd, ctypes.byref(rect))

    class MI(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT), ("rcWork", wintypes.RECT),
                    ("dwFlags", wintypes.DWORD)]
    mi = MI()
    mi.cbSize = ctypes.sizeof(MI)
    hmon = u.MonitorFromWindow(hwnd, 2)       # MONITOR_DEFAULTTONEAREST
    u.GetMonitorInfoW(hmon, ctypes.byref(mi))
    m = mi.rcMonitor
    full = rect.left <= m.left and rect.top <= m.top and rect.right >= m.right and rect.bottom >= m.bottom
    pid = wintypes.DWORD()
    u.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    try:
        name = psutil.Process(pid.value).name()
    except Exception:
        name = None
    return name, bool(full)


def running_names():
    out = set()
    for p in psutil.process_iter(["name"]):
        n = (p.info.get("name") or "").lower()
        if n:
            out.add(n)
    return out


def is_game_now(cfg_games, cfg_ignore, use_fullscreen):
    """Возвращает (играют ли, причина)."""
    if cfg_games:
        hit = cfg_games & running_names()
        if hit:
            return True, "процесс " + sorted(hit)[0]
    if use_fullscreen:
        name, full = foreground_info()
        if full and name and name.lower() not in cfg_ignore:
            return True, f"полноэкранное окно {name}"
    return False, ""


def register(api):
    for k, d in (("auto", True), ("fullscreen", True), ("no_3d", True), ("no_listen", True), ("slow_monitor", True),
                 ("low_priority", True), ("free_whisper", True), ("announce", False), ("to_cloud", False), ("back_local", True)):
        api.var(k, d)
    api.var("games", "")
    api.var("ignore", DEFAULT_IGNORE)
    st = {"on": False, "manual": None, "hits": 0, "miss": 0, "why": "", "label": None}

    def opts():
        return {k: bool(api.config_get(k, True)) for k in ("no_3d", "no_listen", "slow_monitor", "low_priority",
                                                            "free_whisper")}

    def cloud_info():
        """Настройки плагина «Облачные модели»: подтверждал ли пользователь облако, адрес моста, выбранная модель."""
        try:
            d = json.loads((api.data_dir.parent / "cloud_llm" / "config.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        prof = ((d.get("profiles") or {}).get(d.get("active", "")) or {})
        try:
            port = int(d.get("port", "11500"))
        except (TypeError, ValueError):
            port = 11500
        return {"ack": bool(d.get("ack")), "url": f"http://127.0.0.1:{port}", "model": str(prof.get("model") or "").strip()}

    def bridge_alive(url):
        try:
            s = requests.Session()
            s.trust_env = False
            return s.get(url + "/health", timeout=2).status_code == 200
        except requests.RequestException:
            return False

    def go_cloud():
        """Переключить агента на облачный мост. Возвращает текст для журнала или None, если не вышло/не нужно."""
        ci = cloud_info()
        if ci is None:
            api.log("облако не настроено: плагин «Облачные модели» не найден или не настроен")
            return None
        if not ci["ack"]:
            api.log("облако ни разу не подключалось вручную — подключите его на вкладке «Облачные модели» (там подтверждается отправка данных)")
            return None
        if not ci["model"]:
            api.log("в «Облачных моделях» не выбрана модель")
            return None
        url, model = api.get_lm()
        if url.rstrip("/") == ci["url"]:
            return None                                          # уже в облаке
        if not bridge_alive(ci["url"]):
            api.log("мост облачных моделей не отвечает — остаюсь на локальной модели")
            return None
        st["prev_lm"] = (url, model)
        api.set_lm(ci["url"], ci["model"])
        st["cloud_url"] = ci["url"]
        return f"облачная модель {ci['model']}"

    def back_local():
        prev = st.pop("prev_lm", None)
        if not prev:
            return None
        url, _ = api.get_lm()
        if url.rstrip("/") != st.get("cloud_url", ""):
            api.log("вы сами сменили модель во время игры — не трогаю")
            return None
        api.set_lm(prev[0], prev[1])
        return "локальная модель"

    def enter(why):
        done = api.perf_mode(True, **opts())
        if api.config_get("to_cloud", False):
            c = go_cloud()
            if c:
                done = list(done) + [c]
        st.update(on=True, why=why)
        api.log(f"игровой режим включён ({why}): " + (", ".join(done) or "ничего не пришлось менять"))
        if api.config_get("announce", False):
            api.say("Игровой режим включён.")
        refresh()

    def leave():
        back = api.perf_mode(False)
        if st.get("prev_lm"):
            if api.config_get("back_local", True):
                b = back_local()
                if b:
                    back = list(back) + [b]
            else:
                st.pop("prev_lm", None)
        st.update(on=False, why="")
        api.log("игровой режим выключен" + (": вернул " + ", ".join(back) if back else ""))
        if api.config_get("announce", False):
            api.say("Игровой режим выключен.")
        refresh()

    def tick():
        if st["manual"] is not None:          # ручное включение/выключение сильнее автоматики
            return
        if not api.config_get("auto", True):
            if st["on"]:
                leave()
            return
        game, why = is_game_now(split_names(api.config_get("games", "")), split_names(api.config_get("ignore", DEFAULT_IGNORE)),
                                bool(api.config_get("fullscreen", True)))
        if game:
            st["hits"], st["miss"] = st["hits"] + 1, 0
            if not st["on"] and st["hits"] >= 2:             # два замера подряд — не реагируем на мигание окон
                enter(why)
        else:
            st["miss"], st["hits"] = st["miss"] + 1, 0
            if st["on"] and st["miss"] >= 2:
                leave()

    def command(text, m):
        t = api.norm_ru(text)
        if not re.search(r"игров\w+ режим", t):
            return None
        if re.match(r"^(?:включи|активируй|запусти|поставь) игров\w+ режим$", t):
            st["manual"] = True
            if not st["on"]:
                enter("вручную")
            return "Игровой режим включён. Автоматика не вмешивается, пока вы его не выключите."
        if re.match(r"^(?:выключи|отключи|останови|заверши) игров\w+ режим$", t):
            st["manual"] = None
            st["hits"] = st["miss"] = 0
            if st["on"]:
                leave()
            return "Игровой режим выключен." + (" Автоопределение игр работает." if api.config_get("auto", True) else "")
        if re.match(r"^(?:игров\w+ режим(?: статус)?|статус игров\w+ режима|включен ли игров\w+ режим)$", t):
            return ("Игровой режим включён" + (f" ({st['why']})" if st["why"] else "") + ".") if st["on"] \
                else "Игровой режим выключен."
        return None

    def refresh():
        lab = st.get("label")
        if lab is not None:
            try:
                api.post(lambda: lab.config(text="Сейчас: " + (f"игровой режим включён ({st['why']})" if st["on"]
                                                              else "обычный режим")))
            except Exception:
                pass

    api.add_command(r"игров\w+ режим", command, "включи / выключи игровой режим, статус")
    api.add_timer(5, tick)
    def unload():
        if st["on"]:
            api.perf_mode(False)
        if st.get("prev_lm") and api.config_get("back_local", True):
            back_local()
    api.on_unload(unload)

    def build(f):
        pad = {"padx": 6, "pady": 2}
        rows = (("auto", "Включать автоматически, когда начинается игра"),
                ("fullscreen", "…считать игрой любое полноэкранное окно (кроме списка исключений)"),
                ("no_3d", "В игровом режиме: закрывать 3D-аватар"),
                ("no_listen", "В игровом режиме: не слушать микрофон (вернётся после игры)"),
                ("slow_monitor", "В игровом режиме: реже опрашивать мониторинг ресурсов"),
                ("low_priority", "В игровом режиме: понизить приоритет агента"),
                ("free_whisper", "В игровом режиме: выгрузить Whisper из памяти (освобождает видеопамять)"),
                ("to_cloud", "В игровом режиме перейти на облачную модель (разгрузит видеокарту; запросы пойдут провайдеру)"),
                ("back_local", "…и вернуться на локальную модель после игры"),
                ("announce", "Говорить голосом о входе и выходе из игрового режима"))
        for i, (k, t) in enumerate(rows):
            ttk.Checkbutton(f, text=t, variable=api.var(k)).grid(row=i, column=0, columnspan=2, sticky="w", **pad)
        n = len(rows)
        ttk.Label(f, text="Процессы игр (через запятую, например palworld-win64-shipping.exe):").grid(
            row=n, column=0, columnspan=2, sticky="w", **pad)
        ttk.Entry(f, textvariable=api.var("games"), width=70).grid(row=n + 1, column=0, columnspan=2, sticky="w", **pad)
        ttk.Label(f, text="Не считать игрой (полноэкранные программы-исключения):").grid(
            row=n + 2, column=0, columnspan=2, sticky="w", **pad)
        ttk.Entry(f, textvariable=api.var("ignore"), width=70).grid(row=n + 3, column=0, columnspan=2, sticky="w", **pad)
        st["label"] = ttk.Label(f, text="Сейчас: обычный режим")
        st["label"].grid(row=n + 4, column=0, columnspan=2, sticky="w", **pad)
        refresh()
        ttk.Label(f, text=("Облако берётся из плагина «Облачные модели» (должно быть хоть раз подключено вручную, мост запущен); LM Studio при этом сам "
                           "модель из видеопамяти не выгружает — включите в нём авто-выгрузку по таймеру. "
                           "Проверка раз в 5 секунд, режим включается после двух замеров подряд. Настройки здесь ничего не "
                           "переписывают в основных: при выходе из игры всё возвращается как было. Полноэкранное видео в "
                           "браузере по умолчанию в исключениях."),
                  foreground="#777", wraplength=780, justify="left").grid(row=n + 5, column=0, columnspan=2, sticky="w", **pad)
    api.add_settings(build)
