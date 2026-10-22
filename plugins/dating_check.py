# Плагин Nightlord агента: проверка анкет и переписки на сайтах знакомств (фейк / ИИ / мошенник или живой человек).
# Агент делает скриншот экрана, отдаёт его вашей vision-модели (LM Studio, например qwen2.5-vl) и озвучивает вердикт.
# Ничего не листает и не нажимает на сайте сам: решение всегда за вами, поэтому риска бана нет.
# Это подсказка, а не приговор: 100% надёжного определения не бывает. Лучшая проверка — короткий видеозвонок.
import base64
import io
import json
import os
import re
import subprocess
import threading
import time
import webbrowser

import requests

PLUGIN = {"name": "Проверка анкет", "version": "1.1",
          "description": "«проверь анкету», «проверь переписку», «найди фото»: ищет признаки фейка, ИИ-фото и мошенничества"}

# ---------------------------------------------------------------- красные флаги в тексте
# (шаблон, вес, пояснение). Проверяются по тексту, который модель прочитала со скриншота.
TEXT_FLAGS = [
    (r"t\.me/|телеграм|телеге|телегу|telegram|\bтг\b|\btg\b", 15, "зовёт в Telegram"),
    (r"whats\s?app|ватсап|вацап|вотсап|viber|вайбер", 15, "зовёт в WhatsApp или Viber"),
    (r"https?://|www\.|\b[a-z0-9-]+\.(?:ru|com|net|org|site|online|xyz|top|link|club)\b", 20, "ссылка на сторонний сайт"),
    (r"(?<![\w.])@[a-z0-9_]{4,}", 10, "чужой ник или контакт"),
    (r"переведи|перевод[ауе]?\b|скинь\s+(?:денег|на)|номер\s+карты|на\s+карту|оплати|оплат[уы]|займ|в\s+долг",
     30, "разговор о деньгах или переводе"),
    (r"инвест|крипт|биткоин|трейд|бирж[аеу]|заработ|пассивн\w+\s+доход", 30, "инвестиции или заработок"),
    (r"билет|кинотеатр|спектакл|купи.{0,20}такси|такси.{0,20}оплат", 25,
     "просит купить билеты или такси по ссылке (частая схема)"),
    (r"подар(?:ок|и)\s+(?:мне|на)|премиум|vip\s*статус|пополни", 15, "просит подарок или пополнение"),
    (r"вебкам|webcam|приват|onlyfans|онлифанс|18\+|эскорт", 25, "реклама платного контента или услуг"),
    (r"проверочн\w*\s+код|код\s+из\s+смс|продиктуй\s+код|скажи\s+код", 40, "просит код из СМС: точно мошенник"),
]

VERDICTS = [(35, "Похоже на настоящую анкету"), (65, "Есть сомнения"), (101, "Похоже на фейк")]

SYSTEM = (
    "Ты помогаешь пользователю сайта знакомств отличать настоящие анкеты от фейков: ботов, мошенников, "
    "анкет с чужими или сгенерированными нейросетью фото. Внешность и привлекательность человека НЕ оценивай, "
    "только признаки подлинности. Будь осторожен: не называй анкету фейком без конкретных признаков. "
    "Отвечай строго одним JSON-объектом без пояснений вокруг."
)

PROMPT_PROFILE = """На скриншоте открыта анкета на сайте знакомств.
1. Перепиши основной текст анкеты (имя, возраст, город, «о себе», цели) — до 600 символов.
2. Оцени фото на признаки генерации нейросетью или кражи: неестественно гладкая кожа, идеальный студийный свет,
   размытый или странный фон, искажённые пальцы/уши/серьги/волосы на краях, разные зрачки, текст на фоне в виде каши,
   все фото как из фотосессии без единого обычного снимка, на разных фото будто разные люди.
3. Оцени текст: шаблонность, ссылки и мессенджеры, несоответствия (возраст/город/фото).
Верни JSON:
{"text": "переписанный текст", "photo_flags": ["конкретный признак", ...], "text_flags": ["...", ...],
 "real_signs": ["признак живого человека", ...], "fake_score": число 0-100 (вероятность фейка),
 "summary": "одно короткое предложение"}"""

PROMPT_CHAT = """На скриншоте открыта переписка на сайте знакомств или в мессенджере.
1. Перепиши последние сообщения собеседницы — до 800 символов.
2. Оцени признаки бота, ИИ-переписчика или мошенника: зовёт в другой мессенджер или на сайт; отвечает гладко и общими
   фразами, не реагируя на конкретные вопросы; быстро переходит к деньгам, билетам, такси, инвестициям, подаркам;
   просит код из СМС; избегает встречи или видеозвонка; слишком быстро признаётся в симпатии.
3. Отметь признаки живого человека: конкретика, реакция на детали, опечатки, юмор, согласие на звонок.
Верни JSON:
{"text": "переписанные сообщения собеседницы", "photo_flags": [], "text_flags": ["конкретный признак", ...],
 "real_signs": ["...", ...], "fake_score": число 0-100 (вероятность фейка), "summary": "одно короткое предложение"}"""


def rule_flags(text):
    t = (text or "").lower().replace("ё", "е")
    return [(w, why) for rx, w, why in TEXT_FLAGS if re.search(rx, t)]


def parse_reply(raw):
    m = re.search(r"\{.*\}", raw or "", re.S)
    data = None
    if m:
        chunk = m.group(0)
        for attempt in (chunk, re.sub(r",\s*([}\]])", r"\1", chunk)):
            try:
                data = json.loads(attempt)
                break
            except ValueError:
                continue
    if not isinstance(data, dict):
        return {"text": (raw or "")[:800], "photo_flags": [], "text_flags": [], "real_signs": [],
                "fake_score": None, "summary": "", "_raw": True}

    def lst(k):
        v = data.get(k)
        return [str(x).strip() for x in v if str(x).strip()][:8] if isinstance(v, list) else []
    try:
        score = max(0, min(100, int(float(data.get("fake_score")))))
    except (TypeError, ValueError):
        score = None
    return {"text": str(data.get("text") or "")[:1500], "photo_flags": lst("photo_flags"),
            "text_flags": lst("text_flags"), "real_signs": lst("real_signs"), "fake_score": score,
            "summary": str(data.get("summary") or "").strip()[:300]}


def combine(res):
    """Итоговая оценка: мнение модели + жёсткие правила по тексту. Возвращает (балл, вердикт, причины, правила)."""
    rules = rule_flags(res["text"])
    base = res["fake_score"]
    if base is None:
        n = len(res["photo_flags"]) + len(res["text_flags"])
        base = min(80, 25 + 12 * n - 8 * len(res["real_signs"]))
    score = base + sum(w for w, _ in rules)
    if any(w >= 40 for w, _ in rules):
        score = max(score, 90)
    score = max(0, min(100, score))
    verdict = next(v for lim, v in VERDICTS if score < lim)
    reasons = [why for _, why in sorted(rules, reverse=True)]
    reasons += [f for f in res["photo_flags"] + res["text_flags"] if f not in reasons]
    return score, verdict, reasons, rules


def spoken(score, verdict, reasons, rules, res):
    say = f"{verdict}."
    if reasons:
        say += " Настораживает: " + ", ".join(r.rstrip(".") for r in reasons[:3]) + "."
    elif res["real_signs"] and score < 35:
        sign = res["real_signs"][0].rstrip(".")
        say += " " + sign[:1].upper() + sign[1:] + "."
    if any(w >= 40 for w, _ in rules):
        say += " Никому не говорите коды из СМС."
    elif score >= 35:
        say += " Предложите короткий видеозвонок: живой человек обычно соглашается."
    return say


def register(api):
    api.var("capture", "screen")     # "screen" — весь основной экран, "window" — только активное окно
    api.var("delay", 0)              # задержка перед снимком, секунд
    api.var("max_side", 1600)        # до скольких пикселей уменьшать скриншот для модели
    api.var("allow_cloud", False)    # разрешить слать скриншот в облачную модель (по умолчанию — нет)
    api.var("save_shot", True)       # хранить последний скриншот для «найди фото»
    lock = threading.Lock()
    shot_path = api.data_dir / "last_shot.png"

    # ---------------------------------------------------------------- скриншот
    def foreground_rect():
        try:
            import ctypes
            from ctypes import wintypes
            user32 = ctypes.windll.user32
            hwnd = user32.GetForegroundWindow()
            r = wintypes.RECT()
            if hwnd and user32.GetWindowRect(hwnd, ctypes.byref(r)) and r.right - r.left > 200:
                return (max(r.left, 0), max(r.top, 0), r.right, r.bottom)
        except Exception:
            pass
        return None

    def grab():
        try:
            from PIL import ImageGrab
        except ImportError:
            raise RuntimeError("нет пакета Pillow: py -3.12 -m pip install pillow")
        try:
            delay = float(api.config_get("delay", 0) or 0)
        except (TypeError, ValueError):
            delay = 0
        if delay > 0:
            time.sleep(min(delay, 10))
        bbox = foreground_rect() if api.config_get("capture", "screen") == "window" else None
        img = ImageGrab.grab(bbox=bbox) if bbox else ImageGrab.grab()
        return img.convert("RGB")

    def encode(img):
        try:
            side = max(640, min(int(api.config_get("max_side", 1600) or 1600), 3000))
        except (TypeError, ValueError):
            side = 1600
        im = img.copy()
        im.thumbnail((side, side))
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=88)
        return base64.b64encode(buf.getvalue()).decode("ascii")

    # ---------------------------------------------------------------- vision-модель
    def is_remote(url):
        """Модель не на этом ПК/в локальной сети или это облачный мост (cloud_llm)."""
        import ipaddress
        from urllib.parse import urlparse
        host = (urlparse(url).hostname or "").lower()
        local = host in ("localhost", "") or host.endswith(".local")
        if not local:
            try:
                ip = ipaddress.ip_address(host)
                local = ip.is_loopback or ip.is_private or ip.is_link_local
            except ValueError:
                local = False
        if not local:
            return True
        try:
            s = requests.Session()
            s.trust_env = False
            j = s.get(url.rstrip("/") + "/health", timeout=2).json()
            return isinstance(j, dict) and j.get("plugin") == "cloud_llm"
        except Exception:
            return False

    def ask_vision(prompt, b64):
        cfg = api.ui(api._h._make_cfg)
        if not api.config_get("allow_cloud", False) and is_remote(cfg["lm_url"]):
            raise RuntimeError("сейчас агент работает через облако или удалённую модель, а скриншот экрана с анкетой туда "
                               "отправлять нельзя. Переключитесь на LM Studio или разрешите это в настройках плагина")
        body = {"messages": [{"role": "system", "content": SYSTEM},
                             {"role": "user", "content": [
                                 {"type": "text", "text": prompt},
                                 {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + b64}}]}],
                "temperature": 0.2, "stream": False, "max_tokens": 1200}
        if cfg.get("lm_model"):
            body["model"] = cfg["lm_model"]
        try:
            r = requests.post(f"{cfg['lm_url']}/v1/chat/completions", json=body, timeout=240)
        except requests.RequestException as e:
            raise RuntimeError(f"модель не отвечает: {e}")
        if r.status_code >= 400:
            hint = ""
            low = r.text.lower()
            if "image" in low or "vision" in low:
                hint = " Похоже, загруженная модель не умеет смотреть на картинки: нужна vision-модель, например qwen2.5-vl."
            raise RuntimeError(f"модель вернула ошибку {r.status_code}: {r.text[:200]}.{hint}")
        txt = r.json()["choices"][0]["message"].get("content") or ""
        return re.sub(r"<think>.*?</think>", "", txt, flags=re.S).strip()

    # ---------------------------------------------------------------- проверка
    def check(mode):
        if not lock.acquire(blocking=False):
            return "Я ещё проверяю предыдущую анкету, подождите."
        try:
            try:
                img = grab()
            except Exception as e:
                return f"Не удалось сделать скриншот: {e}"
            if api.config_get("save_shot", True):
                try:
                    api.data_dir.mkdir(parents=True, exist_ok=True)
                    img.save(shot_path)
                except OSError:
                    pass
            api.log("смотрю на экран…")
            try:
                raw = ask_vision(PROMPT_CHAT if mode == "chat" else PROMPT_PROFILE, encode(img))
            except RuntimeError as e:
                return f"Не получилось проверить: {e}"
            res = parse_reply(raw)
            score, verdict, reasons, rules = combine(res)
            lines = [f"{'Переписка' if mode == 'chat' else 'Анкета'}: {verdict} (подозрительность {score} из 100)"]
            if res["summary"]:
                lines.append("Кратко: " + res["summary"])
            if reasons:
                lines.append("Настораживает: " + "; ".join(reasons))
            if res["real_signs"]:
                lines.append("За то, что человек живой: " + "; ".join(res["real_signs"]))
            if res.get("_raw"):
                lines.append("(модель ответила не в формате JSON, оценка грубая)")
            if res["text"]:
                lines.append("Прочитанный текст: " + res["text"][:500])
            api.log("\n".join(lines))
            return spoken(score, verdict, reasons, rules, res)
        finally:
            lock.release()

    def reverse_search():
        """Открывает поиск по картинке в Яндексе и папку со скриншотом: файл перетаскивается в окно поиска."""
        if not shot_path.exists() or time.time() - shot_path.stat().st_mtime > 600:
            try:
                api.data_dir.mkdir(parents=True, exist_ok=True)
                grab().save(shot_path)
            except Exception as e:
                return f"Не удалось сделать скриншот: {e}"
        webbrowser.open("https://ya.ru/images/")
        try:
            subprocess.Popen(["explorer", "/select,", str(shot_path)])
        except Exception:
            try:
                os.startfile(str(api.data_dir))
            except Exception:
                pass
        return ("Открыла поиск по картинке и папку со скриншотом. Перетащите файл в окно поиска и выделите на нём фото. "
                "Если фото найдётся на других сайтах под другими именами, это фейк.")

    # ---------------------------------------------------------------- команды и инструмент
    api.add_command(r"(?:проверь|оцени|посмотри|глянь)\s+(?:эту\s+)?(?:анкету|профиль|девушку|страницу)",
                    lambda text, m: check("profile"), "проверь анкету")
    api.add_command(r"^(?:это\s+)?(?:фейк|бот)(?:\s+или\s+(?:нет|настоящая|живая))?$",
                    lambda text, m: check("profile"), "это фейк?")
    api.add_command(r"(?:проверь|оцени|посмотри|глянь)\s+(?:эту\s+)?(?:переписку|чат|диалог|сообщения)",
                    lambda text, m: check("chat"), "проверь переписку")
    api.add_command(r"(?:найди|поищи|ищи|пробей)\s+(?:это\s+|её\s+|ее\s+)?фото|поиск\s+по\s+(?:фото|картинке)",
                    lambda text, m: reverse_search(), "найди фото")

    api.add_tool("check_dating_profile",
                 "Смотрит на экран и проверяет открытую анкету или переписку на сайте знакомств на признаки фейка, "
                 "ИИ-фото и мошенничества. Возвращает вердикт с причинами.",
                 {"mode": {"type": "string", "description": "profile — анкета, chat — переписка"}},
                 lambda a: check("chat" if str(a.get("mode", "")).lower().startswith("c") else "profile"))

    # ---------------------------------------------------------------- настройки на вкладке «Плагины»
    def build(frame):
        import tkinter.ttk as ttk
        pad = {"padx": 6, "pady": 2}
        ttk.Label(frame, text="Что снимать:").grid(row=0, column=0, sticky="w", **pad)
        ttk.Radiobutton(frame, text="весь экран", value="screen", variable=api.var("capture")).grid(
            row=0, column=1, sticky="w", **pad)
        ttk.Radiobutton(frame, text="только активное окно", value="window", variable=api.var("capture")).grid(
            row=0, column=2, sticky="w", **pad)
        ttk.Label(frame, text="Задержка перед снимком, с:").grid(row=1, column=0, sticky="w", **pad)
        ttk.Spinbox(frame, from_=0, to=10, width=5, textvariable=api.var("delay")).grid(row=1, column=1, sticky="w", **pad)
        ttk.Label(frame, text="Размер для модели, px:").grid(row=2, column=0, sticky="w", **pad)
        ttk.Spinbox(frame, from_=640, to=3000, increment=160, width=6, textvariable=api.var("max_side")).grid(
            row=2, column=1, sticky="w", **pad)
        ttk.Checkbutton(frame, text="Хранить последний скриншот для «найди фото» (один файл, перезаписывается)",
                        variable=api.var("save_shot")).grid(row=3, column=0, columnspan=3, sticky="w", **pad)
        ttk.Checkbutton(frame, text="Разрешить отправку скриншота в облачную / удалённую модель (по умолчанию выключено)",
                        variable=api.var("allow_cloud")).grid(row=5, column=0, columnspan=3, sticky="w", **pad)
        ttk.Label(frame, text=("Команды: «проверь анкету», «это фейк», «проверь переписку», «найди фото». Нужна "
                               "vision-модель в LM Studio (qwen2.5-vl подходит). Агент сам ничего на сайте не нажимает. "
                               "Оценка — подсказка, а не приговор: лучшая проверка — короткий видеозвонок."),
                  foreground="#777", wraplength=760, justify="left").grid(row=4, column=0, columnspan=3, sticky="w", **pad)
    api.add_settings(build)
