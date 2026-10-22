# Плагин Nightlord агента: управление плеером — что играет, пауза / дальше / назад, перемотка, выбор плеера, «включи … в Яндекс Музыке».
# Работает через системный медиа-центр Windows (тот же, что показывает карточку трека над громкостью): подходят Яндекс Музыка,
# Spotify, VLC, браузерные плееры и YouTube в Chrome/Edge — всё, что умеет показывать эту карточку.
# Нужен пакет:  py -3.12 -m pip install winrt-runtime winrt-Windows.Media.Control winrt-Windows.Foundation   (или: pip install winsdk)
# Без пакета «что играет» подскажет установку, а «дальше / пауза» остаются на медиа-клавишах плагина «Системное управление».
import asyncio
import re
import threading
import time
import tkinter as tk
import webbrowser
from tkinter import ttk, messagebox
from urllib.parse import quote

PLUGIN = {"name": "Плеер", "version": "1.0",
          "description": "«что играет», «следующий трек», «пауза», «перемотай на 30 секунд», «какие плееры открыты», "
                         "«включи Linkin Park в Яндекс Музыке / Spotify / на ютубе». Windows, нужен пакет winrt (см. настройки)."}

STATUS = {0: "закрыт", 1: "открыт", 2: "переключается", 3: "остановлен", 4: "играет", 5: "на паузе"}
SERVICES = {"yandex": "Яндекс Музыка", "spotify": "Spotify", "youtube": "YouTube", "vk": "VK Музыка"}
SERVICE_RE = [("yandex", r"яндекс\w*\s*музык\w*|yandex\s*music"), ("spotify", r"спотифа\w*|spotify"),
              ("youtube", r"ютуб\w*|youtube|ютьюб\w*"), ("vk", r"(?:вк|vk|вконтакте)\s*музык\w*|в\s*вк\b|во?\s*вконтакте")]
APP_HINTS = {"spotify": "spotify", "yandex": "yandex", "vlc": "vlc", "chrome": "chrome", "edge": "msedge", "firefox": "firefox",
             "yandexmusic": "yandexmusic"}


def search_url(service, q):
    q = quote(q.strip())
    return {"yandex": f"https://music.yandex.ru/search?text={q}",
            "spotify": f"https://open.spotify.com/search/{q}",
            "youtube": f"https://www.youtube.com/results?search_query={q}",
            "vk": f"https://vk.com/audio?q={q}"}[service]


def parse_service(t):
    """-> (service|None, текст без упоминания сервиса)."""
    for key, rx in SERVICE_RE:
        m = re.search(rf"\s+(?:в|во|на|через|из)?\s*(?:{rx})\W*$", t)
        if m:
            return key, t[:m.start()].strip()
    return None, t.strip()


NUM_WORDS = {"пять": 5, "десять": 10, "пятнадцать": 15, "двадцать": 20, "тридцать": 30, "сорок": 40, "сорок пять": 45,
             "минуту": 60, "полминуты": 30, "одну минуту": 60, "две минуты": 120, "три минуты": 180, "пять минут": 300}


def parse_seek(t):
    """«перемотай на 30 секунд вперёд» -> +30; «назад на минуту» -> -60; «с начала» -> 'start'; нет -> None."""
    if re.search(r"\bс начала\b|\bсначала\b|\bзаново\b|\bв начало\b", t):
        return "start"
    m = re.search(r"(\d+|(?:сорок пять|одну минуту|две минуты|три минуты|пять минут|полминуты|пятнадцать|двадцать|тридцать|десять|сорок|пять|минуту))\s*(секунд\w*|сек\b|минут\w*|мин\b)?", t)
    sec = None
    if m:
        w = m.group(1)
        n = int(w) if w.isdigit() else NUM_WORDS.get(w)
        if n is not None:
            unit = m.group(2) or ""
            sec = n * 60 if unit.startswith("мин") and "минут" not in w and w != "полминуты" else n
    if sec is None:
        sec = 10                                    # «перемотай вперёд» без числа
    back = bool(re.search(r"\bназад\b|\bвернись\b|\bоткрути\b", t))
    return -sec if back else sec


def fmt_time(sec):
    sec = max(0, int(sec))
    return f"{sec // 60}:{sec % 60:02d}"


def app_name(aumid):
    a = (aumid or "").lower()
    for k, v in (("yandexmusic", "Яндекс Музыка"), ("spotify", "Spotify"), ("vlc", "VLC"), ("chrome", "Chrome"),
                 ("msedge", "Edge"), ("firefox", "Firefox"), ("yandex", "Яндекс Браузер"), ("zune", "Media Player"),
                 ("groove", "Groove"), ("telegram", "Telegram"), ("discord", "Discord")):
        if k in a:
            return v
    return (aumid or "плеер").split("!")[0].split(".")[-1][:30]


INSTALL = ("Для управления плеером нужен пакет: py -3.12 -m pip install winrt-runtime winrt-Windows.Media.Control "
           "winrt-Windows.Foundation (или pip install winsdk), затем перезапустите агента.")


def load_api():
    """Возвращает класс менеджера сессий или None."""
    for mod in ("winrt.windows.media.control", "winsdk.windows.media.control"):
        try:
            m = __import__(mod, fromlist=["GlobalSystemMediaTransportControlsSessionManager"])
            return m.GlobalSystemMediaTransportControlsSessionManager
        except Exception:
            continue
    return None


def register(api):
    api.var("control", True)           # пауза / дальше / назад / перемотка
    api.var("now", True)               # «что играет»
    api.var("search", True)            # «включи … в …»
    api.var("announce", False)         # сообщать голосом, когда трек сменился
    api.var("prefer", "")              # предпочитаемый плеер: часть названия (spotify, yandex, vlc, chrome)
    api.var("service", "yandex")       # куда идти за «включи …», если сервис не назван
    st = {"last": None, "api_ok": None}

    def run(coro_fn):
        """Запускает async-код WinRT в отдельном цикле этого потока."""
        return asyncio.run(coro_fn())

    def pick(mgr):
        sessions = list(mgr.get_sessions())
        if not sessions:
            return None, []
        pref = str(api.config_get("prefer", "")).strip().lower()
        if pref:
            for s in sessions:
                if pref in (s.source_app_user_model_id or "").lower():
                    return s, sessions
        cur = mgr.get_current_session()
        return (cur or sessions[0]), sessions

    async def info_of(s):
        props = await s.try_get_media_properties_async()
        pb = s.get_playback_info()
        tl = s.get_timeline_properties()
        try:
            pos, end = tl.position.total_seconds(), tl.end_time.total_seconds()
        except Exception:
            pos = end = 0
        return {"title": (props.title or "").strip(), "artist": (props.artist or "").strip(),
                "app": app_name(s.source_app_user_model_id), "status": int(pb.playback_status), "pos": pos, "end": end,
                "id": s.source_app_user_model_id}

    def need_api():
        cls = load_api()
        if cls is None:
            raise RuntimeError(INSTALL)
        return cls

    def track_line(i):
        who = f"{i['artist']} — {i['title']}" if i["artist"] and i["title"] else (i["title"] or i["artist"] or "без названия")
        s = f"{who} ({i['app']}, {STATUS.get(i['status'], '?')}"
        if i["end"] > 5:
            s += f", {fmt_time(i['pos'])} из {fmt_time(i['end'])}"
        return s + ")"

    def now_playing():
        if not api.config_get("now", True):
            return "«Что играет» выключено в настройках плагина."
        cls = need_api()

        async def go():
            mgr = await cls.request_async()
            s, sessions = pick(mgr)
            if not s:
                return "Сейчас ничего не играет: нет ни одного открытого плеера."
            i = await info_of(s)
            out = "Сейчас: " + track_line(i) + "."
            others = [x for x in sessions if x is not s]
            if others:
                out += " Ещё открыты: " + ", ".join(app_name(x.source_app_user_model_id) for x in others) + "."
            return out
        return run(go)

    def list_players():
        cls = need_api()

        async def go():
            mgr = await cls.request_async()
            sessions = list(mgr.get_sessions())
            if not sessions:
                return "Открытых плееров нет."
            lines = []
            for s in sessions:
                lines.append(track_line(await info_of(s)))
            return "Плееры: " + "; ".join(lines) + "."
        return run(go)

    def control(action, value=None):
        if not api.config_get("control", True):
            return "Управление плеером выключено в настройках плагина."
        cls = need_api()

        async def go():
            mgr = await cls.request_async()
            s, _ = pick(mgr)
            if not s:
                return "Нет открытого плеера: запустите музыку или видео."
            ok, msg = False, ""
            if action == "toggle":
                ok = await s.try_toggle_play_pause_async(); msg = "Переключил."
            elif action == "play":
                ok = await s.try_play_async(); msg = "Играю."
            elif action == "pause":
                ok = await s.try_pause_async(); msg = "Пауза."
            elif action == "stop":
                ok = await s.try_stop_async(); msg = "Остановил."
            elif action == "next":
                ok = await s.try_skip_next_async(); msg = "Следующий трек."
            elif action == "prev":
                ok = await s.try_skip_previous_async(); msg = "Предыдущий трек."
            elif action == "seek":
                i = await info_of(s)
                if value == "start":
                    target = 0
                else:
                    target = i["pos"] + float(value)
                if i["end"] > 0:
                    target = min(target, max(i["end"] - 1, 0))
                target = max(target, 0)
                ok = await s.try_change_playback_position_async(int(target * 10_000_000))   # единицы по 100 нс
                msg = "С начала." if value == "start" else f"Перемотал на {fmt_time(target)}."
            else:
                return "Не знаю такого действия."
            if not ok:
                return "Этот плеер не принял команду (бывает, когда он не поддерживает перемотку или трек не загружен)."
            return msg
        return run(go)

    def search_play(query, service=None):
        if not api.config_get("search", True):
            return "Поиск и запуск музыки выключены в настройках плагина."
        query = str(query or "").strip()[:200]
        if not query:
            return "Что включить?"
        service = service if service in SERVICES else str(api.config_get("service", "yandex"))
        if service not in SERVICES:
            service = "yandex"
        url = search_url(service, query)
        try:
            if service == "spotify":
                import os
                try:
                    os.startfile("spotify:search:" + quote(query))        # откроет приложение Spotify, если оно есть
                    return f"Открыл поиск «{query}» в Spotify. Нажмите на трек, чтобы запустить."
                except (AttributeError, OSError):
                    pass
            webbrowser.open(url)
        except Exception as e:
            return f"Не удалось открыть {SERVICES[service]}: {e}."
        return f"Открыл поиск «{query}» в сервисе {SERVICES[service]}. Выберите трек — дальше управляю голосом."

    def guard(fn, *a):
        try:
            return fn(*a)
        except RuntimeError as e:
            return f"{e}" if str(e).startswith("Для управления") else f"Плеер: {e}."
        except Exception as e:
            return f"Плеер не ответил: {type(e).__name__}: {str(e)[:120]}."

    def ctrl(action, value=None):
        """Управление через медиа-центр; без пакета winrt команду отдаём плагину «Системное управление» (медиа-клавиши)."""
        if load_api() is None:
            return None
        return guard(control, action, value)

    def command(text, m):
        t = api.norm_ru(text)
        if re.search(r"(?:перемотай|промотай|отмотай|перемотать|сначала|с начала|заново)", t) and re.search(r"(?:трек|песн|музык|видео|вперед|назад|начал|плеер|заново|сначала)", t):
            v = parse_seek(t)
            if v is not None:
                return ctrl("seek", v)
        mm = re.match(r"^(?:пожалуйста )?(?:включи|поставь|запусти|найди|поищи|сыграй|врубай|включай)\s+(?:мне\s+)?(?:песню|трек|музыку|композицию|альбом|исполнителя)?\s*(.+)$", t)
        if mm and not re.match(r"^(?:музык\w*|плеер\w*|видео|звук\w*|громкост\w*)$", mm.group(1).strip()):
            svc, q = parse_service(mm.group(1))
            if svc or re.search(r"(?:песню|трек|альбом|композицию|исполнителя)", t):
                if q:
                    return search_play(q, svc)
        if re.search(r"какие плееры|что открыто в плеер|список плееров", t):
            return guard(list_players)
        if re.search(r"что (?:сейчас )?играет|какой (?:сейчас )?трек|что за (?:песня|трек|музыка)|как называется (?:эта |данная )?(?:песня|композиция|трек)|кто (?:сейчас )?поет|что (?:это )?за (?:песня|трек)", t):
            return guard(now_playing)
        if re.search(r"(?:следующ\w+|дальше|пропусти)\b.*\b(?:трек|песн\w+|композици\w+)\b|^следующ\w+ (?:трек|песн\w+)$", t):
            return ctrl("next")
        if re.search(r"(?:предыдущ\w+|прошлый|верни прошлый)\b.*\b(?:трек|песн\w+|композици\w+)\b", t):
            return ctrl("prev")
        if re.match(r"^(?:поставь )?(?:музыку |плеер |видео )?на паузу$|^пауза$|^поставь на паузу(?: музыку| плеер| видео)?$", t):
            return ctrl("pause")
        if re.match(r"^(?:продолжи|возобнови|играй|включи) (?:музыку|воспроизведение|плеер|видео)$|^продолжай$", t):
            return ctrl("play")
        if re.match(r"^(?:останови|выключи|стоп) (?:музыку|плеер|воспроизведение)$", t):
            return ctrl("stop")
        return None

    api.add_command(r"(?:включи|поставь|запусти|найди|поищи|сыграй|играет|трек|песн|музык|плеер|пауз|перемот|промот|отмот|сначала|с начала|продолжи|"
                    r"возобнови|продолжай|предыдущ|следующ|дальше|кто поет|что за)",
                    command, "что играет / следующий / предыдущий трек / пауза / перемотай / какие плееры / включи … в Яндекс Музыке")
    api.add_tool("media_now_playing", "Что сейчас играет в плеере Windows: исполнитель, название, приложение, позиция.", {},
                 lambda a: guard(now_playing), [])
    api.add_tool("media_control", "Управление плеером: toggle, play, pause, stop, next, prev, seek (value — секунды вперёд/назад, "
                 "отрицательное — назад).",
                 {"action": {"type": "string", "enum": ["toggle", "play", "pause", "stop", "next", "prev", "seek"]},
                  "value": {"type": "number", "description": "для seek: сколько секунд перемотать"}},
                 lambda a: "Для seek нужен value." if a.get("action") == "seek" and a.get("value") is None
                 else guard(control, a.get("action"), a.get("value") or 0), ["action"])
    api.add_tool("media_search_play", "Открыть поиск музыки в сервисе (yandex, spotify, youtube, vk) — человек сам выберет трек.",
                 {"query": {"type": "string"}, "service": {"type": "string", "enum": list(SERVICES)}},
                 lambda a: search_play(a.get("query"), a.get("service")), ["query"])

    def poll():
        if not api.config_get("announce", False):
            return
        try:
            cls = load_api()
            if cls is None:
                return

            async def go():
                mgr = await cls.request_async()
                s, _ = pick(mgr)
                return await info_of(s) if s else None
            i = run(go)
        except Exception:
            return
        if not i or i["status"] != 4 or not (i["title"] or i["artist"]):
            return
        key = (i["app"], i["artist"], i["title"])
        if st["last"] is not None and key != st["last"]:
            api.say("Играет: " + (f"{i['artist']} — {i['title']}" if i["artist"] else i["title"]))
        st["last"] = key
    api.add_timer(8, poll)

    def build(f):
        pad = {"padx": 6, "pady": 2}
        for i, (k, txt) in enumerate((("now", "«Что играет» (название, исполнитель, плеер, позиция)"),
                                      ("control", "Управление: пауза, дальше, назад, остановить, перемотка"),
                                      ("search", "«Включи <исполнитель или песня> в Яндекс Музыке / Spotify / на ютубе» (откроет поиск)"),
                                      ("announce", "Сообщать голосом, когда начался новый трек (проверка раз в 8 секунд)"))):
            ttk.Checkbutton(f, text=txt, variable=api.var(k)).grid(row=i, column=0, columnspan=4, sticky="w", **pad)
        ttk.Label(f, text="Главный плеер (часть названия: spotify, yandex, vlc, chrome):").grid(row=4, column=0, sticky="w", **pad)
        ttk.Entry(f, textvariable=api.var("prefer"), width=16).grid(row=4, column=1, sticky="w", **pad)
        ttk.Label(f, text="Сервис по умолчанию для «включи …»:").grid(row=5, column=0, sticky="w", **pad)
        names = list(SERVICES)
        cb = ttk.Combobox(f, values=[SERVICES[k] for k in names], state="readonly", width=16)
        cb.set(SERVICES.get(str(api.config_get("service", "yandex")), SERVICES["yandex"]))
        cb.grid(row=5, column=1, sticky="w", **pad)
        cb.bind("<<ComboboxSelected>>", lambda e: api.var("service").set(names[cb.current()]))

        def check():
            def work():
                try:
                    msg = now_playing()
                except RuntimeError as e:
                    msg = str(e)
                except Exception as e:
                    msg = f"Ошибка: {type(e).__name__}: {e}"
                api.post(messagebox.showinfo, "Плеер", msg)
            threading.Thread(target=work, daemon=True).start()
        ttk.Button(f, text="Проверить: что играет", command=check).grid(row=6, column=0, sticky="w", **pad)
        ttk.Label(f, text=("Работает через системный медиа-центр Windows 10/11: любой плеер, который показывает карточку трека "
                           "(Яндекс Музыка, Spotify, VLC, YouTube в браузере). Установка: py -3.12 -m pip install winrt-runtime "
                           "winrt-Windows.Media.Control winrt-Windows.Foundation (или pip install winsdk). «Включи …» открывает поиск "
                           "в сервисе, а не запускает трек сама: у сервисов нет открытого способа нажать «играть» без их входа. "
                           "Перемотка работает не во всех плеерах. Плагин не проверен на настоящем Windows."),
                  foreground="#777", wraplength=720, justify="left").grid(row=7, column=0, columnspan=4, sticky="w", **pad)
    api.add_settings(build)
