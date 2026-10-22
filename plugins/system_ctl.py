# Плагин Nightlord агента: управление компьютером голосом — системная громкость, медиа-клавиши, блокировка,
# сон, выключение, перезагрузка, гашение экрана, «сверни все окна». Только Windows. Без сторонних библиотек.
# Системная громкость задаётся клавишами громкости (шаг Windows — 2 %), поэтому значение приблизительное.
# Громкость самого агента («громкость 50», «погромче») — отдельная встроенная команда и здесь не затрагивается.
import os
import re
import subprocess
import threading
import time
import tkinter as tk
from tkinter import ttk

PLUGIN = {"name": "Управление ПК", "version": "1.0",
          "description": "«системная громкость 30», «приглуши звук на компьютере», «следующий трек», «пауза», "
                         "«заблокируй компьютер», «усыпи компьютер», «выключи компьютер через 10 минут», "
                         "«отмени выключение», «выключи экран», «сверни все окна». Каждая группа включается отдельно."}

IS_WIN = os.name == "nt"
VK = {"mute": 0xAD, "down": 0xAE, "up": 0xAF, "next": 0xB0, "prev": 0xB1, "stop": 0xB2, "play": 0xB3}
SYS = r"(?:системн\w*|компьютер\w*|пк|винд\w*|windows|в системе)"
NUMW = {"ноль": 0, "десять": 10, "двадцать": 20, "тридцать": 30, "сорок": 40, "пятьдесят": 50, "шестьдесят": 60,
        "семьдесят": 70, "восемьдесят": 80, "девяносто": 90, "сто": 100, "пять": 5, "пятнадцать": 15,
        "двадцать пять": 25, "тридцать пять": 35, "сорок пять": 45, "пятьдесят пять": 55, "шестьдесят пять": 65,
        "семьдесят пять": 75}
ACTIONS = ("volume_set", "volume_up", "volume_down", "mute", "play_pause", "next", "prev", "lock", "sleep",
           "shutdown", "restart", "cancel_shutdown", "monitor_off", "show_desktop")
GROUP = {"volume_set": "volume", "volume_up": "volume", "volume_down": "volume", "mute": "volume",
         "play_pause": "media", "next": "media", "prev": "media", "lock": "lock", "sleep": "power",
         "shutdown": "power", "restart": "power", "cancel_shutdown": "power", "monitor_off": "screen",
         "show_desktop": "screen"}


# ---- низкий уровень (в тестах подменяется) ----
def press(vk, times=1):
    import ctypes
    u = ctypes.windll.user32
    for _ in range(times):
        u.keybd_event(vk, 0, 1, 0)           # 1 = EXTENDEDKEY
        u.keybd_event(vk, 0, 1 | 2, 0)       # 2 = KEYUP
        time.sleep(0.004)


def win_d():
    import ctypes
    u = ctypes.windll.user32
    u.keybd_event(0x5B, 0, 0, 0)
    u.keybd_event(0x44, 0, 0, 0)
    u.keybd_event(0x44, 0, 2, 0)
    u.keybd_event(0x5B, 0, 2, 0)


def lock_screen():
    import ctypes
    ctypes.windll.user32.LockWorkStation()


def monitor_off():
    import ctypes
    ctypes.windll.user32.SendMessageW(0xFFFF, 0x0112, 0xF170, 2)   # WM_SYSCOMMAND, SC_MONITORPOWER, выключить


def run_cmd(args):
    subprocess.Popen(args, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


# ---- разбор фраз ----
def to_percent(t):
    m = re.search(r"(\d{1,3})\s*(?:%|процент\w*)?", t)
    if m:
        return max(0, min(100, int(m.group(1))))
    for w in sorted(NUMW, key=len, reverse=True):
        if re.search(rf"\b{w}\b", t):
            return NUMW[w]
    return None


def parse(text, norm):
    """Фраза -> (действие, значение) или None."""
    t = norm(text)
    if not t or len(t.split()) > 12:
        return None
    sysmark = re.search(rf"\b{SYS}\b", t)
    vol = re.search(r"\b(?:громкост\w*|звук\w*)\b", t)
    if sysmark and vol:
        if re.search(r"\b(?:выключи|отключи|заглуши|убери|включи|верни)\b.*\bзвук\w*|\bмьют\b|\bбез звука\b", t) \
                and not re.search(r"\d|процент", t):
            return "mute", None
        p = to_percent(t)
        if p is not None and re.search(r"\b(?:на|до|установи|поставь|сделай)\b|\bгромкост\w* \d", t) \
                and not re.search(r"\b(?:прибавь|добавь|убавь|уменьши|увеличь)\b", t):
            return "volume_set", p
        if re.search(r"\b(?:погромче|громче|прибавь|добавь|увеличь|повысь|выше)\b", t):
            return "volume_up", (p if p is not None else 10)
        if re.search(r"\b(?:потише|тише|убавь|уменьши|понизь|приглуши|ниже)\b", t):
            return "volume_down", (p if p is not None else 10)
        return None
    if re.search(r"\b(?:следующ\w+|дальше|переключи на следующ\w+|пропусти)\b.*\b(?:трек|песн\w+|композици\w+)\b|"
                 r"\bследующ\w+ (?:трек|песн\w+|композици\w+)\b", t):
        return "next", None
    if re.search(r"\b(?:предыдущ\w+|прошлый|верни прошлый)\b.*\b(?:трек|песн\w+|композици\w+)\b", t):
        return "prev", None
    if re.match(r"^(?:поставь на паузу|пауза|на паузу|продолжи|возобнови|play)$|"
                r"^(?:поставь на паузу|останови|продолжи|возобнови) (?:музыку|воспроизведение|плеер|видео)$|"
                r"^(?:поставь )?(?:музыку|плеер|видео) на паузу$", t):
        return "play_pause", None
    if re.match(r"^(?:заблокируй|блокировка)(?: мой)? (?:компьютер|пк|экран|систему|windows|винду)$", t):
        return "lock", None
    if re.match(r"^(?:усыпи|переведи в сон|переведи в спящий режим) ?(?:компьютер|пк)?$|^спящий режим$|"
                r"^(?:компьютер|пк) в (?:спящий режим|сон)$|^режим сна$", t):
        return "sleep", None
    if re.match(r"^отмени (?:выключение|перезагрузку|завершение работы)(?: компьютера| пк)?$", t):
        return "cancel_shutdown", None
    m = re.match(r"^(?:выключи|заверши работу|вырубай|вырубить)(?: мой)? (?:компьютер|пк)"
                 r"(?: через (\d+|[а-я]+(?: [а-я]+)?) мин\w*)?$", t)
    if m:
        return "shutdown", _minutes(m.group(1))
    m = re.match(r"^перезагрузи(?: мой)? (?:компьютер|пк|систему|windows)(?: через (\d+|[а-я]+(?: [а-я]+)?) мин\w*)?$", t)
    if m:
        return "restart", _minutes(m.group(1))
    if re.match(r"^(?:выключи|погаси|отключи) (?:экран|монитор\w*|дисплей)$", t):
        return "monitor_off", None
    if re.match(r"^(?:сверни все окна|покажи рабочий стол|сверни окна)$", t):
        return "show_desktop", None
    return None


def _minutes(w):
    if not w:
        return 0
    if w.isdigit():
        return int(w)
    return NUMW.get(w, 0) or {"одну": 1, "две": 2, "три": 3, "четыре": 4}.get(w, 0)


def register(api):
    for k, d in (("volume", True), ("media", True), ("lock", True), ("power", True), ("screen", True),
                 ("confirm_power", True), ("confirm_lock", False)):
        api.var(k, d)
    api.var("step_pct", 2)
    state = {"vol": None}

    def on(group):
        return bool(api.config_get(group, True))

    def power(kind, minutes):
        secs = max(0, int(minutes or 0)) * 60 or 20
        word = {"shutdown": "выключу", "restart": "перезагружу"}[kind]
        when = f"через {minutes} мин" if minutes else "через 20 секунд"
        if api.config_get("confirm_power", True) and not api.confirm(f"Агент хочет: {word} компьютер {when}. Разрешить?"):
            return "Не буду."
        flag = "/s" if kind == "shutdown" else "/r"
        run_cmd(["shutdown", flag, "/t", str(secs), "/c", "Nightlord агент: " + word + " компьютер"])
        api.log(f"{kind} {when}")
        return f"Хорошо, {word} компьютер {when}. Скажите «отмени выключение», чтобы передумать."

    def do(action, value=None):
        if action not in ACTIONS:
            return f"Не знаю действия «{action}»."
        if not IS_WIN:
            return "Управление ПК работает только в Windows."
        g = GROUP[action]
        if not on(g):
            return {"volume": "Управление системной громкостью выключено в настройках плагина.",
                    "media": "Медиа-клавиши выключены в настройках плагина.",
                    "lock": "Блокировка выключена в настройках плагина.",
                    "power": "Выключение, сон и перезагрузка выключены в настройках плагина.",
                    "screen": "Управление экраном и окнами выключено в настройках плагина."}[g]
        step = max(1, int(api.config_get("step_pct", 2) or 2))
        try:
            if action == "volume_set":
                p = max(0, min(100, int(value if value is not None else 30)))
                press(VK["down"], 100 // step + 2)
                press(VK["up"], round(p / step))
                state["vol"] = p
                return f"Системная громкость примерно {p} процентов."
            if action in ("volume_up", "volume_down"):
                n = max(1, min(100, int(value if value is not None else 10)))
                press(VK["up" if action == "volume_up" else "down"], max(1, round(n / step)))
                if state["vol"] is not None:
                    state["vol"] = max(0, min(100, state["vol"] + (n if action == "volume_up" else -n)))
                return "Сделал " + ("громче." if action == "volume_up" else "тише.")
            if action == "mute":
                press(VK["mute"])
                return "Переключил звук (выкл/вкл)."
            if action == "play_pause":
                press(VK["play"])
                return "Пауза/воспроизведение."
            if action == "next":
                press(VK["next"])
                return "Следующий трек."
            if action == "prev":
                press(VK["prev"])
                return "Предыдущий трек."
            if action == "lock":
                if api.config_get("confirm_lock", False) and not api.confirm("Заблокировать компьютер?"):
                    return "Не буду."
                lock_screen()
                return "Блокирую."
            if action == "sleep":
                if api.config_get("confirm_power", True) and not api.confirm("Перевести компьютер в сон?"):
                    return "Не буду."
                run_cmd(["rundll32.exe", "powrprof.dll,SetSuspendState", "0,1,0"])
                return "Усыпляю компьютер."
            if action in ("shutdown", "restart"):
                return power(action, value)
            if action == "cancel_shutdown":
                run_cmd(["shutdown", "/a"])
                return "Отменил выключение, если оно было назначено."
            if action == "monitor_off":
                monitor_off()
                return "Экран погашен."
            if action == "show_desktop":
                win_d()
                return "Свернул окна."
        except Exception as e:
            return f"Не получилось: {str(e)[:120]}."

    def command(text, m):
        r = parse(text, api.norm_ru)
        if not r:
            return None
        return do(*r)

    def tool(args):
        a = str(args.get("action", "")).strip()
        v = args.get("value")
        try:
            v = None if v in (None, "") else int(v)
        except (TypeError, ValueError):
            v = None
        return do(a, v)

    api.add_command(r"(?:громкост|звук|трек|песн|композици|пауз|плеер|музык|видео|заблокир|блокировк|усып|сон\b|"
                    r"спящ|выключ|заверши|вырубай|перезагруз|отмени|экран|монитор|дисплей|сверни|рабочий стол|play)",
                    command, "системная громкость, медиа, блокировка, сон, выключение")
    api.add_tool("system_control",
                 "Управление компьютером: системная громкость, медиа-клавиши, блокировка, сон, выключение, экран. "
                 "action: " + ", ".join(ACTIONS) + ". value — процент громкости или минуты до выключения.",
                 {"action": {"type": "string", "description": "одно из: " + ", ".join(ACTIONS), "enum": list(ACTIONS)},
                  "value": {"type": "integer", "description": "число (процент или минуты), если нужно"}},
                 tool, ["action"])

    def build(f):
        pad = {"padx": 6, "pady": 2}
        rows = (("volume", "Системная громкость и звук («системная громкость 30», «потише на компьютере»)"),
                ("media", "Медиа-клавиши («следующий трек», «пауза»)"),
                ("lock", "Блокировка компьютера"),
                ("power", "Сон, выключение, перезагрузка, отмена выключения"),
                ("screen", "Погасить экран, свернуть все окна"),
                ("confirm_power", "Спрашивать подтверждение перед сном, выключением и перезагрузкой"),
                ("confirm_lock", "Спрашивать подтверждение перед блокировкой"))
        for i, (k, txt) in enumerate(rows):
            ttk.Checkbutton(f, text=txt, variable=api.var(k)).grid(row=i, column=0, columnspan=3, sticky="w", **pad)
        ttk.Label(f, text="Шаг громкости Windows, %:").grid(row=len(rows), column=0, sticky="w", **pad)
        ttk.Spinbox(f, from_=1, to=10, width=4, textvariable=api.var("step_pct")).grid(row=len(rows), column=1,
                                                                                         sticky="w", **pad)
        ttk.Label(f, text=("Если подтверждение выключено, выключение всё равно идёт с задержкой 20 секунд — её можно отменить "
                           "фразой «отмени выключение». Если управляете агентом из Telegram, оставьте подтверждение выключенным, "
                           "иначе окно будет ждать вас у компьютера. Громкость агента («погромче», «громкость 50») — отдельная "
                           "встроенная команда."),
                  foreground="#777", wraplength=780, justify="left").grid(row=len(rows) + 1, column=0, columnspan=3,
                                                                          sticky="w", **pad)
    api.add_settings(build)
