# Плагин Nightlord агента: умный дом через Home Assistant (REST API). Свет, розетки, шторы, климат, сцены, скрипты,
# замки (по умолчанию выключены), чтение датчиков. Устройства ищутся по «понятным именам» из Home Assistant
# («Свет спальня», «Розетка принтер»). Токен: профиль пользователя в Home Assistant → «Долгосрочные токены доступа».
import re
import threading
import time
import tkinter as tk
from tkinter import ttk

import requests

PLUGIN = {"name": "Home Assistant", "version": "1.0",
          "description": "Умный дом голосом: «включи свет в спальне», «выключи розетку принтера», «закрой шторы в гостиной», "
                         "«поставь отопление в спальне на 22», «какая температура в гостиной», «запусти сцену вечер». "
                         "Каждая группа устройств включается отдельно; замки по умолчанию выключены."}

GROUPS = {"light": ("light",), "switch": ("switch", "fan", "input_boolean"), "cover": ("cover",), "climate": ("climate",),
          "scene": ("scene", "script"), "media": ("media_player",), "lock": ("lock",), "sensor": ("sensor", "binary_sensor")}
HINTS = [(r"свет|освещени|ламп|люстр|светильник|торшер|подсветк|ночник", "light"),
         (r"розетк|удлинител|выключател", "switch"), (r"вентилятор", "fan"),
         (r"штор|жалюзи|ворот|рольставн|гардин", "cover"),
         (r"кондиционер|отоплени|обогрев|термостат|батаре|климат", "climate"),
         (r"сцен", "scene"), (r"скрипт|сценари", "script"), (r"замок|дверь на замок", "lock"),
         (r"телевизор|плеер|колонк|музык", "media_player")]
STOP = set("в во на у к с со за по для мне пожалуйста там тут в-на и а это то мой моей моём моем нашей наша наш моя".split())
ALL_WORDS = {"весь", "все", "всё", "везде", "всю", "вся"}
VERBS = [(r"включи|зажги|активируй|запусти|подключи", "turn_on"), (r"выключи|погаси|потуши|отключи|вырубай", "turn_off"),
         (r"открой|подними|раскрой", "open"), (r"закрой|опусти|задвинь", "close"), (r"переключи", "toggle")]


def stem(w):
    n = len(w)
    return w if n <= 3 else w[:n - 1] if n <= 5 else w[:n - 2] if n <= 7 else w[:n - 3]


def toks(s):
    return [w for w in re.findall(r"\w+", (s or "").lower().replace("ё", "е"))]


def hint_domains(tokens):
    out = set()
    for t in tokens:
        for rx, dom in HINTS:
            if re.match(rx, t):
                out.add(dom)
    return out


def is_hint_token(t):
    return any(re.match(rx, t) for rx, _ in HINTS)


class HA:
    def __init__(self, url, token, timeout=8):
        self.base = str(url).rstrip("/")
        self.s = requests.Session()
        self.s.trust_env = False                    # локальный адрес — мимо системных прокси
        self.s.headers.update({"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
        self.timeout = timeout

    def _check(self, r):
        if r.status_code == 401:
            raise RuntimeError("Home Assistant отклонил токен")
        if r.status_code == 404:
            raise RuntimeError("Home Assistant не знает такого запроса (проверьте адрес и что API включён)")
        if r.status_code >= 400:
            raise RuntimeError(f"Home Assistant ответил {r.status_code}: {r.text[:100]}")
        return r

    def _req(self, method, path, **kw):
        try:
            return self._check(self.s.request(method, self.base + path, timeout=self.timeout, **kw))
        except requests.ConnectionError:
            raise RuntimeError("Home Assistant не отвечает: проверьте адрес и что он включён") from None
        except requests.Timeout:
            raise RuntimeError("Home Assistant не ответил вовремя") from None
        except requests.RequestException as e:
            raise RuntimeError(str(e)[:120]) from None

    def ping(self):
        return self._req("GET", "/api/").json().get("message", "ok")

    def states(self):
        return self._req("GET", "/api/states").json()

    def call(self, domain, service, data):
        return self._req("POST", f"/api/services/{domain}/{service}", json=data).json()


def parse_aliases(text):
    out = {}
    for ln in re.split(r"[;\n]+", str(text or "")):
        if "=" in ln:
            a, b = ln.split("=", 1)
            a, b = " ".join(toks(a)), b.strip()
            if a and "." in b:
                out[a] = b
    return out


def match_entities(target_tokens, entities, allowed_domains):
    """Подбор устройств по словам из фразы. Возвращает список (entity, балл) лучших совпадений."""
    words = [t for t in target_tokens if t not in STOP and t not in ALL_WORDS]
    hints = hint_domains(words)
    plain = [t for t in words if not is_hint_token(t)]
    hint_words = [t for t in words if is_hint_token(t)]
    cands = []
    for e in entities:
        dom = e["entity_id"].split(".")[0]
        if dom not in allowed_domains:
            continue
        if hints and dom not in hints and not (plain and dom in ("light", "switch", "cover", "climate")):
            # слово-подсказка задаёт тип; но если назван конкретный предмет — типы не режем жёстко
            continue
        nm = toks(e.get("attributes", {}).get("friendly_name") or e["entity_id"])
        stems = [stem(t) for t in plain]
        if not all(any(w.startswith(s) for w in nm) for s in stems):
            continue
        score = len(plain) * 2 + (1 if hints else 0) + \
            (sum(1 for t in hint_words if any(w.startswith(stem(t)) for w in nm)) if plain else 0)
        cands.append((e, score))
    if not cands:
        return []
    best = max(s for _, s in cands)
    return [(e, s) for e, s in cands if s == best]


def say_state(e):
    st = e.get("state")
    dom = e["entity_id"].split(".")[0]
    name = e.get("attributes", {}).get("friendly_name") or e["entity_id"]
    if st in ("unavailable", "unknown"):
        return f"{name}: сейчас недоступно."
    if dom in ("light", "switch", "fan", "input_boolean"):
        return f"{name}: {'включено' if st == 'on' else 'выключено'}."
    if dom == "cover":
        return f"{name}: {({'open': 'открыто', 'closed': 'закрыто', 'opening': 'открывается', 'closing': 'закрывается'}).get(st, st)}."
    if dom == "lock":
        return f"{name}: {'закрыт' if st == 'locked' else 'открыт' if st == 'unlocked' else st}."
    if dom == "climate":
        a = e.get("attributes", {})
        cur, tgt = a.get("current_temperature"), a.get("temperature")
        return f"{name}: {st}" + (f", сейчас {cur}" if cur is not None else "") + (f", цель {tgt}" if tgt is not None else "") + "."
    if dom == "binary_sensor":
        return f"{name}: {'да' if st == 'on' else 'нет'}."
    unit = e.get("attributes", {}).get("unit_of_measurement", "")
    return f"{name}: {st}{(' ' + unit) if unit else ''}."


def register(api):
    for k, d in (("light", True), ("switch", True), ("cover", True), ("climate", True), ("scene", True), ("media", True),
                 ("lock", False), ("sensor", True), ("confirm_lock", True), ("voice", True)):
        api.var(k, d)
    api.var("url", "http://homeassistant.local:8123")
    api.var("token", "")
    api.var("aliases", "")
    cache = {"states": [], "t": 0.0, "label": None, "msg": ""}
    lk = threading.Lock()

    def client():
        url, tok = str(api.config_get("url", "")).strip(), str(api.config_get("token", "")).strip()
        if not url or not tok:
            return None
        return HA(url, tok)

    def allowed():
        out = set()
        for g, doms in GROUPS.items():
            if api.config_get(g, g != "lock"):
                out.update(doms)
        return out

    def states(force=False):
        with lk:
            if not force and time.time() - cache.get("fail", 0) < 30:
                raise RuntimeError(cache["err"])               # недавно не отвечал — не тормозим каждую команду
            if force or time.time() - cache["t"] > 60 or not cache["states"]:
                try:
                    cache["states"] = client().states()
                except RuntimeError as e:
                    cache["fail"], cache["err"] = time.time(), str(e)
                    raise
                cache["t"] = time.time()
                cache["fail"] = 0
            return cache["states"]

    def find(target_text, doms=None):
        tt = toks(target_text)
        aliases = parse_aliases(api.config_get("aliases", ""))
        key = " ".join(t for t in tt if t not in STOP)
        sts = states()
        if key in aliases:
            eid = aliases[key]
            hit = [e for e in sts if e["entity_id"] == eid]
            return [(e, 9) for e in hit], True
        return match_entities(tt, sts, doms or allowed()), bool(set(tt) & ALL_WORDS)

    def friendly(e):
        return e.get("attributes", {}).get("friendly_name") or e["entity_id"]

    def act(action, target_text, value=None, quiet_none=False):
        """Выполнить действие. Возвращает текст ответа или None, если подходящего устройства нет."""
        c = client()
        if c is None:
            return None if quiet_none else "Home Assistant не настроен: укажите адрес и токен на вкладке «Плагины»."
        try:
            found, all_flag = find(target_text)
            if not found:
                return None if quiet_none else f"Не нашёл устройство «{target_text.strip()}» в Home Assistant."
            if len(found) > 1 and not all_flag:
                names = ", ".join(friendly(e) for e, _ in found[:5])
                return f"Подходит несколько: {names}. Уточните название или скажите «весь …»."
            if len(found) > 12:
                return "Подходит слишком много устройств, уточните."
            done = []
            for e, _ in found:
                eid = e["entity_id"]
                dom = eid.split(".")[0]
                svc, data = None, {"entity_id": eid}
                if action == "brightness":
                    if dom != "light":
                        return f"{friendly(e)} — не лампа, яркость поменять нельзя."
                    svc, data = ("light", "turn_on"), {"entity_id": eid, "brightness_pct": max(1, min(100, int(value)))}
                elif action == "set_temperature":
                    if dom != "climate":
                        return f"{friendly(e)} — не климатическое устройство."
                    svc, data = ("climate", "set_temperature"), {"entity_id": eid, "temperature": float(value)}
                elif dom == "cover":
                    svc = ("cover", {"turn_on": "open_cover", "open": "open_cover", "turn_off": "close_cover",
                                     "close": "close_cover", "toggle": "toggle"}[action])
                elif dom == "lock":
                    if action in ("turn_on", "open"):                      # «открой замок» = отпереть
                        if api.config_get("confirm_lock", True) and not api.confirm(f"Открыть замок «{friendly(e)}»?"):
                            return "Не буду открывать замок."
                        svc = ("lock", "unlock")
                    elif action in ("turn_off", "close"):
                        svc = ("lock", "lock")
                    else:
                        return "С замком можно только открыть или закрыть."
                elif dom in ("scene", "script"):
                    if action not in ("turn_on", "open"):
                        return f"«{friendly(e)}» можно только запустить."
                    svc = (dom, "turn_on")
                elif dom == "media_player":
                    svc = ("media_player", {"turn_on": "turn_on", "turn_off": "turn_off", "toggle": "toggle",
                                            "open": "turn_on", "close": "turn_off"}[action])
                else:
                    svc = (dom, {"turn_on": "turn_on", "open": "turn_on", "turn_off": "turn_off", "close": "turn_off",
                                 "toggle": "toggle"}[action])
                c.call(svc[0], svc[1], data)
                done.append(friendly(e))
            states(force=True)
            api.log(f"{action} → {', '.join(done)}")
            word = {"turn_on": "Включил", "turn_off": "Выключил", "open": "Открыл", "close": "Закрыл", "toggle": "Переключил",
                    "brightness": f"Яркость {int(value) if value is not None else ''}% —" if value is not None else "Яркость —",
                    "set_temperature": f"Установил {value} градусов:"}[action]
            if action in ("turn_on", "turn_off") and all(f.split(".")[0] in ("scene", "script") for f in [x[0]["entity_id"] for x in found]):
                word = "Запустил"
            return f"{word}: {', '.join(done)}."
        except RuntimeError as e:
            if quiet_none:                                      # обычное «включи …» не должно превращаться в жалобы на дом
                api.log(f"{e}")
                return None
            return f"{e}."
        except (ValueError, TypeError):
            return "Не понял значение."

    def read(target_text, kind=None):
        c = client()
        if c is None:
            return "Home Assistant не настроен."
        try:
            sts = states()
            tt = toks(target_text)
            want_dom = allowed()
            if kind in ("temp", "hum"):
                dc = "temperature" if kind == "temp" else "humidity"
                plain = [stem(t) for t in tt if t not in STOP and not is_hint_token(t)]
                pool = []
                for e in sts:
                    dom = e["entity_id"].split(".")[0]
                    a = e.get("attributes", {})
                    ok_sensor = dom == "sensor" and "sensor" in {g for g in GROUPS if api.config_get(g, g != "lock")} \
                        and a.get("device_class") == dc
                    ok_climate = dom == "climate" and kind == "temp" and "climate" in {g for g in GROUPS if api.config_get(g, g != "lock")}
                    if not (ok_sensor or ok_climate):
                        continue
                    nm = toks(friendly(e))
                    if all(any(w.startswith(s) for w in nm) for s in plain):
                        pool.append(e)
                if not pool:
                    return "Не нашёл подходящий датчик."
                outs = []
                for e in pool[:3]:
                    if e["entity_id"].startswith("climate."):
                        v = e.get("attributes", {}).get("current_temperature")
                        outs.append(f"{friendly(e)}: {v} градусов" if v is not None else say_state(e))
                    else:
                        outs.append(say_state(e))
                return " ".join(outs)
            found, _ = find(target_text, want_dom | {"sensor", "binary_sensor"} if api.config_get("sensor", True) else want_dom)
            if not found:
                return f"Не нашёл устройство «{target_text.strip()}»."
            return " ".join(say_state(e) for e, _ in found[:4])
        except RuntimeError as e:
            return f"{e}."

    def command(text, m):
        if not api.config_get("voice", True) or client() is None:
            return None
        t = api.norm_ru(text)
        if len(t.split()) > 14:
            return None
        mm = re.match(r"^(?:какая|сколько) (температур\w*|влажност\w*)(?: сейчас)?(?: (?:в|на|у))? (.+)$", t) or \
            re.match(r"^(?:какая|сколько) (температур\w*|влажност\w*)(?: сейчас)?$", t)
        if mm:
            return read(mm.group(2) if mm.lastindex and mm.lastindex >= 2 else "", "temp" if mm.group(1).startswith("т") else "hum")
        mm = re.match(r"^(?:горит ли|включен ли|включён ли|выключен ли|работает ли|открыт ли|закрыт ли|открыты ли|закрыты ли|"
                      r"заперт ли|закрыта ли|открыта ли) (.+)$", t)
        if mm:
            return read(mm.group(1))
        mm = re.match(r"^(?:яркость|сделай яркость|поставь яркость|установи яркость) (.+?) (?:на )?(\d{1,3})(?: ?%| ?процент\w*)?$", t) or \
            re.match(r"^(?:сделай|поставь|установи) (.+?) (?:на )?(\d{1,3}) ?(?:%|процент\w*)$", t)
        if mm:
            r = act("brightness", mm.group(1), int(mm.group(2)), quiet_none=True)
            if r is not None:
                return r
        mm = re.match(r"^(?:поставь|установи|сделай|выставь) (.+?) (?:на )?(\d{1,2}(?:[.,]\d)?)(?: ?градус\w*)?$", t)
        if mm and "climate" in hint_domains(toks(mm.group(1))):
            return act("set_temperature", mm.group(1), mm.group(2).replace(",", "."), quiet_none=True)
        for rx, action in VERBS:
            mm = re.match(rf"^(?:пожалуйста )?(?:{rx}) (.+)$", t)
            if mm:
                return act(action, mm.group(1), quiet_none=True)
        return None

    def tool_control(args):
        a = str(args.get("action", "")).strip()
        amap = {"turn_on": "turn_on", "on": "turn_on", "turn_off": "turn_off", "off": "turn_off", "toggle": "toggle",
                "open": "open", "close": "close", "brightness": "brightness", "set_temperature": "set_temperature"}
        if a not in amap:
            return "action: turn_on, turn_off, toggle, open, close, brightness, set_temperature."
        return act(amap[a], str(args.get("target", "")), args.get("value")) or "Не нашёл устройство."

    def tool_state(args):
        return read(str(args.get("target", "")))

    api.add_command(r"(?:включ|выключ|зажг|погас|потуш|отключ|открой|закрой|подними|опусти|запусти|переключ|поставь|установи|"
                    r"сделай|выставь|яркост|температур|влажност|горит ли|открыт|закрыт|заперт|работает ли|активируй|вырубай|"
                    r"раскрой|задвинь|подключи)", command, "свет, розетки, шторы, климат, сцены, датчики")
    api.add_tool("home_control", "Управляет умным домом (Home Assistant): свет, розетки, шторы, климат, сцены. "
                 "action: turn_on, turn_off, toggle, open, close, brightness (value 1-100), set_temperature (value в °C). "
                 "target — название устройства по-русски, например «свет в спальне».",
                 {"action": {"type": "string", "description": "turn_on, turn_off, toggle, open, close, brightness, set_temperature"},
                  "target": {"type": "string", "description": "Название устройства"},
                  "value": {"type": "number", "description": "Яркость % или температура, если нужно"}}, tool_control, ["action", "target"])
    api.add_tool("home_state", "Состояние устройства или датчика Home Assistant («свет на кухне», «температура в гостиной»).",
                 {"target": {"type": "string", "description": "Название устройства или датчика"}}, tool_state, ["target"])

    def set_msg(t):
        cache["msg"] = t
        lab = cache["label"]
        if lab is not None:
            api.post(lambda: lab.config(text=t))

    def build(f):
        pad = {"padx": 6, "pady": 2}
        ttk.Label(f, text="Адрес Home Assistant:").grid(row=0, column=0, sticky="w", **pad)
        ttk.Entry(f, textvariable=api.var("url"), width=46).grid(row=0, column=1, columnspan=2, sticky="w", **pad)
        ttk.Label(f, text="Токен:").grid(row=1, column=0, sticky="w", **pad)
        ttk.Entry(f, textvariable=api.var("token"), width=46, show="•").grid(row=1, column=1, columnspan=2, sticky="w", **pad)

        def check():
            def work():
                try:
                    c = client()
                    if c is None:
                        set_msg("Заполните адрес и токен.")
                        return
                    c.ping()
                    sts = states(force=True)
                    doms = {}
                    for e in sts:
                        doms[e["entity_id"].split(".")[0]] = doms.get(e["entity_id"].split(".")[0], 0) + 1
                    set_msg(f"Связь есть. Устройств: {len(sts)} (свет {doms.get('light', 0)}, розетки {doms.get('switch', 0)}, "
                            f"шторы {doms.get('cover', 0)}, климат {doms.get('climate', 0)}, сцены {doms.get('scene', 0)}).")
                except RuntimeError as e:
                    set_msg(f"{e}.")
            threading.Thread(target=work, daemon=True).start()
        ttk.Button(f, text="Проверить связь", command=check).grid(row=2, column=0, sticky="w", **pad)
        cache["label"] = ttk.Label(f, text=cache["msg"] or "Связь не проверялась.")
        cache["label"].grid(row=2, column=1, columnspan=2, sticky="w", **pad)
        rows = (("light", "Свет и лампы"), ("switch", "Розетки, выключатели, вентиляторы"), ("cover", "Шторы, жалюзи, ворота"),
                ("climate", "Климат: отопление, кондиционеры"), ("scene", "Сцены и скрипты"),
                ("media", "Медиаплееры и телевизоры"), ("sensor", "Чтение датчиков (температура, влажность, состояния)"),
                ("lock", "Замки (опасно: открывать/закрывать замки голосом)"),
                ("confirm_lock", "Спрашивать подтверждение перед открытием замка"),
                ("voice", "Голосовые команды умного дома"))
        for i, (k, t) in enumerate(rows):
            ttk.Checkbutton(f, text=t, variable=api.var(k)).grid(row=3 + i, column=0, columnspan=3, sticky="w", **pad)
        n = 3 + len(rows)
        ttk.Label(f, text="Свои названия через « ; » (например: ночник = light.bedside_lamp ; камин = switch.fireplace):").grid(row=n, column=0, columnspan=3, sticky="w", **pad)
        ent = ttk.Entry(f, textvariable=api.var("aliases"), width=70)
        ent.grid(row=n + 1, column=0, columnspan=3, sticky="w", **pad)
        ttk.Label(f, text=("Название ищется среди «понятных имён» устройств в Home Assistant, поэтому называйте их с комнатой: "
                           "«Свет спальня», «Розетка принтер». Токен лежит в plugins/data/home_assistant/config.json открытым "
                           "текстом. Работает по локальной сети, облако не нужно. Чтобы вызывать из Telegram, включите там "
                           "«разрешать действия»."),
                  foreground="#777", wraplength=780, justify="left").grid(row=n + 2, column=0, columnspan=3, sticky="w", **pad)
    api.add_settings(build)
