# Плагин Nightlord агента: отправка сообщений себе в Telegram через бота.
# Настройки (токен, chat_id) хранятся в plugins/data/telegram/config.json, а не в settings.json.
import importlib.util
import json
import os
import re
import sys
import threading
import time
from pathlib import Path
import tkinter as tk
from tkinter import ttk, messagebox

import requests

PLUGIN = {"name": "Telegram", "version": "1.7",
          "description": "«Отправь в телеграм …», «отправь это в телеграм», инструмент send_telegram для модели, "
                         "дубль напоминаний. Сообщения уходят только в ваш чат. Можно включить приём команд: "
                         "пишете боту с телефона (текстом или голосовым) — агент отвечает в Telegram; "
                         "по желанию присылает скриншоты и файлы. Есть панель кнопок под полем ввода."}

API_URL = "https://api.telegram.org"
SEND_WORD = r"(?:отправь|отправить|напиши|пришли|скинь|шли|перешли)"
TG_WORD = r"(?:телеграм\w*|telegram|тг|tg)"


def match_tg(text, norm):
    """«Отправь в телеграм привет» -> ("text", "привет"); «отправь это в телеграм» -> ("last", None)."""
    t = norm(text)
    if re.match(rf"^{SEND_WORD}(?: мне)?(?: (?:это|его|последний ответ|ответ|последнее сообщение))? "
                rf"(?:в|на) {TG_WORD}$", t):
        return "last", None
    m = re.match(rf"^\s*{SEND_WORD}(?:\s+мне)?(?:\s+сообщение)?\s+(?:в|на)\s+{TG_WORD}\W*\s+(.+)$", text.strip(),
                 re.I | re.S)
    if m and m.group(1).strip():
        return "text", m.group(1).strip()
    return None


def tg_send(base, token, chat_id, text, proxies, explain, timeout=15, markup=None):
    text = str(text).strip()[:4096]
    if not text:
        raise RuntimeError("пустое сообщение")
    try:
        payload = {"chat_id": chat_id, "text": text}
        if markup:
            payload["reply_markup"] = markup
        r = requests.post(f"{base}/bot{token}/sendMessage", json=payload, timeout=timeout, proxies=proxies)
    except requests.RequestException as e:
        why = explain(e) if proxies else ""
        raise RuntimeError("нет связи с Telegram" + (f" ({why})" if why else "")) from None   # токен в текст не попадает
    try:
        data = r.json()
    except ValueError:
        data = {}
    if not data.get("ok"):
        raise RuntimeError(str(data.get("description") or f"HTTP {r.status_code}")[:150])
    return True


def tg_find_chat(base, token, proxies, explain, timeout=15):
    try:
        r = requests.get(f"{base}/bot{token}/getUpdates", params={"timeout": 0}, timeout=timeout, proxies=proxies)
        data = r.json()
    except (requests.RequestException, ValueError) as e:
        why = explain(e) if proxies else ""
        raise RuntimeError("нет связи с Telegram или неверный токен" + (f" ({why})" if why else "")) from None
    if not data.get("ok"):
        raise RuntimeError(str(data.get("description") or "неверный токен")[:150])
    for u in reversed(data.get("result") or []):
        msg = u.get("message") or u.get("edited_message") or u.get("channel_post") or {}
        ch = msg.get("chat")
        if ch and "id" in ch:
            name = ch.get("title") or " ".join(x for x in (ch.get("first_name"), ch.get("last_name")) if x) \
                or ch.get("username") or str(ch["id"])
            return ch["id"], name
    return None


def tg_updates(base, token, offset, proxies, timeout=25):
    """getUpdates с длинным опросом. Возвращает список обновлений (может быть пустым)."""
    params = {"timeout": timeout, "allowed_updates": '["message","callback_query"]'}
    if offset is not None:
        params["offset"] = offset
    r = requests.get(f"{base}/bot{token}/getUpdates", params=params, timeout=timeout + 10, proxies=proxies)
    data = r.json()
    if not data.get("ok"):
        raise RuntimeError(str(data.get("description") or f"HTTP {r.status_code}")[:150])
    return data.get("result") or []


def pick_command(update, chat):
    """Вернуть текст сообщения, если оно из нужного ЛИЧНОГО чата, иначе None."""
    msg = update.get("message") or {}
    ch = msg.get("chat") or {}
    if ch.get("type") != "private" or str(ch.get("id")) != str(chat).strip():
        return None
    t = msg.get("text")
    return t.strip() if isinstance(t, str) and t.strip() else None


def pick_voice(update, chat):
    """Голосовое/аудио из нужного ЛИЧНОГО чата -> {"file_id", "duration", "size"} или None."""
    msg = update.get("message") or {}
    ch = msg.get("chat") or {}
    if ch.get("type") != "private" or str(ch.get("id")) != str(chat).strip():
        return None
    v = msg.get("voice") or msg.get("audio")
    if not isinstance(v, dict) or not v.get("file_id"):
        return None
    return {"file_id": v["file_id"], "duration": int(v.get("duration") or 0), "size": int(v.get("file_size") or 0)}


def pick_other(update, chat):
    """Сообщение из нужного чата без текста и голоса (фото, файл, стикер…): отвечаем, что не понимаем."""
    msg = update.get("message") or {}
    ch = msg.get("chat") or {}
    if ch.get("type") != "private" or str(ch.get("id")) != str(chat).strip():
        return False
    return not msg.get("text") and not msg.get("voice") and not msg.get("audio")


def tg_api(base, token, method, payload, proxies, explain, timeout=20):
    try:
        r = requests.post(f"{base}/bot{token}/{method}", json=payload, timeout=timeout, proxies=proxies)
        data = r.json()
    except (requests.RequestException, ValueError) as e:
        why = explain(e) if proxies else ""
        raise RuntimeError("нет связи с Telegram" + (f" ({why})" if why else "")) from None
    if not data.get("ok"):
        raise RuntimeError(str(data.get("description") or f"HTTP {r.status_code}")[:150])
    return data.get("result") or {}


def tg_download(base, token, file_id, dest, proxies, explain, limit=20 * 1024 * 1024):
    """Скачивает файл из Telegram (боту доступно до 20 МБ). Возвращает путь."""
    info = tg_api(base, token, "getFile", {"file_id": file_id}, proxies, explain)
    fp = info.get("file_path")
    if not fp:
        raise RuntimeError("Telegram не отдал файл (возможно, он больше 20 МБ)")
    try:
        with requests.get(f"{base}/file/bot{token}/{fp}", stream=True, timeout=60, proxies=proxies) as r:
            if r.status_code != 200:
                raise RuntimeError(f"не скачался файл (HTTP {r.status_code})")
            size = 0
            with open(dest, "wb") as f:
                for chunk in r.iter_content(65536):
                    size += len(chunk)
                    if size > limit:
                        raise RuntimeError("файл больше 20 МБ")
                    f.write(chunk)
    except requests.RequestException as e:
        raise RuntimeError("нет связи с Telegram при скачивании") from None
    return dest


def tg_send_file(base, token, chat_id, path, proxies, explain, caption="", photo=False, markup=None, voice=False):
    path = Path(path)
    size = path.stat().st_size
    if size > 49 * 1024 * 1024:
        raise RuntimeError("файл больше 49 МБ: Telegram не принимает такие от ботов")
    photo = photo and size <= 9 * 1024 * 1024
    method, field = ("sendPhoto", "photo") if photo else ("sendDocument", "document")
    if voice and path.suffix.lower() == ".ogg":
        method, field = "sendVoice", "voice"
    try:
        with open(path, "rb") as fh:
            form = {"chat_id": chat_id, "caption": caption[:1000]}
            if markup:
                form["reply_markup"] = json.dumps(markup, ensure_ascii=False)
            r = requests.post(f"{base}/bot{token}/{method}", data=form, files={field: (path.name, fh)},
                              timeout=120, proxies=proxies)
        data = r.json()
    except (requests.RequestException, ValueError) as e:
        why = explain(e) if proxies else ""
        raise RuntimeError("нет связи с Telegram" + (f" ({why})" if why else "")) from None
    if not data.get("ok"):
        raise RuntimeError(str(data.get("description") or f"HTTP {r.status_code}")[:150])
    return True


BTN = {"status": "📊 Статус", "screen": "🖥 Скриншот", "files": "📁 Файлы", "new": "🧹 Новый диалог",
       "stop": "🔇 Стоп", "help": "❓ Помощь", "quieter": "🔉 Тише", "louder": "🔊 Громче"}
BTN_CMD = {BTN["status"]: "/status", BTN["screen"]: "/screen", BTN["files"]: "/files", BTN["new"]: "/new",
           BTN["stop"]: "/stop", BTN["help"]: "/help", BTN["quieter"]: "/quieter", BTN["louder"]: "/louder"}
COMMANDS_MENU = [("status", "Состояние агента"), ("screen", "Скриншот экрана ПК"), ("files", "Файлы агента"),
                 ("new", "Новый диалог"), ("stop", "Остановить речь и ответ"), ("menu", "Панель кнопок"),
                 ("help", "Справка")]


def parse_custom(raw):
    """«Название = действие» по строке -> [(название, действие)]. До 8 кнопок, название до 30 символов.
    Название не должно совпадать со встроенной кнопкой."""
    out, seen = [], set(BTN.values())
    for line in str(raw or "").splitlines():
        if "=" not in line:
            continue
        name, act = (x.strip() for x in line.split("=", 1))
        name = name[:30]
        if not name or not act or name in seen:
            continue
        seen.add(name)
        out.append((name, act[:500]))
        if len(out) >= 8:
            break
    return out


def panel_keyboard(screen, files, custom=()):
    """Постоянная панель под полем ввода; кнопки скриншота и файлов — только если эти функции включены."""
    row1 = [BTN["status"]] + ([BTN["screen"]] if screen else []) + ([BTN["files"]] if files else [])
    rows = [row1, [BTN["new"], BTN["stop"], BTN["help"]], [BTN["quieter"], BTN["louder"]]]
    names = [n for n, _ in custom]
    for i in range(0, len(names), 2):
        rows.append(names[i:i + 2])
    return {"keyboard": [[{"text": t} for t in r] for r in rows], "resize_keyboard": True, "is_persistent": True,
            "input_field_placeholder": "Напишите или нажмите кнопку"}


def files_inline(names):
    """Кнопки-файлы под сообщением (по две в ряд); в callback_data только номер."""
    rows, row = [], []
    for i, n in enumerate(names[:20]):
        row.append({"text": ("📄 " + n)[:40], "callback_data": f"f:{i}"})
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    return {"inline_keyboard": rows}


def pick_callback(update, chat):
    """Нажатие инлайн-кнопки из нужного ЛИЧНОГО чата -> (id, data) или None."""
    cb = update.get("callback_query")
    if not isinstance(cb, dict):
        return None
    msg = cb.get("message") or {}
    ch = msg.get("chat") or {}
    if ch.get("type") != "private" or str(ch.get("id")) != str(chat).strip():
        return None
    return cb.get("id"), str(cb.get("data") or "")


SAFE_EXT = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".pdf", ".txt", ".md", ".csv", ".log"}


def under(path, roots):
    """Файл лежит внутри одной из разрешённых папок? Без выхода через .., без скрытых папок/файлов (.git, .env),
    а в служебной папке данных плагинов (roots[1]) — только картинки и документы, не config.json с токенами."""
    try:
        p = Path(path).resolve()
    except OSError:
        return False
    for i, r in enumerate(roots):
        try:
            rel = p.relative_to(Path(r).resolve())
        except (ValueError, OSError):
            continue
        if any(part.startswith(".") for part in rel.parts):
            return False
        if i > 0 and p.suffix.lower() not in SAFE_EXT:
            return False
        return p.is_file()
    return False


SKIP_DIRS = {"node_modules", "__pycache__", "venv", ".venv", "$recycle.bin", "system volume information"}


def list_files(root, limit=3000):
    """Относительные пути файлов папки. Обход os.walk: нет доступа к подпапке — пропускаем, скрытые и служебные не берём."""
    root = Path(root)
    out = []
    if not root.is_dir():
        return out
    for dp, dns, fns in os.walk(root, onerror=lambda e: None):
        dns[:] = sorted(d for d in dns if not d.startswith(".") and d.lower() not in SKIP_DIRS)
        for fn in sorted(fns):
            if fn.startswith("."):
                continue
            out.append(str((Path(dp) / fn).relative_to(root)))
            if len(out) >= limit:
                return out
    return out


def find_file(root, query, limit=3000):
    """Точный относительный путь или единственное совпадение по части имени. -> (путь|None, [варианты])"""
    root = Path(root)
    q = query.strip().strip('"').strip()
    if not q:
        return None, []
    cand = root / q
    if under(cand, [root]):
        return cand.resolve(), []
    low = q.lower()
    hits = [r for r in list_files(root, limit) if low in Path(r).name.lower()]
    if len(hits) == 1:
        return (root / hits[0]).resolve(), []
    return None, hits[:10]


WHERE = "Плагины → Telegram → «Настройки»"
HELP = ("Пишите обычным текстом или голосовым — агент ответит сюда.\n/help — эта подсказка\n/status — состояние агента\n"
        "/diag — проверка: папка файлов, голосовые, права\n"
        "/screen — скриншот экрана ПК (если разрешено)\n/files — файлы агента\n/file имя — прислать файл (если разрешено)\n"
        "/new — новый диалог, /stop — остановить, /menu — панель кнопок")


def register(api):
    # перенос настроек из прежней версии (когда Telegram был встроен в агента)
    if not api.config_get("migrated"):
        for new, old, dflt in (("token", "tg_token", ""), ("chat", "tg_chat", ""), ("confirm", "tg_confirm", True),
                               ("mirror", "tg_rem", False)):
            api.config_set(new, api.legacy(old, dflt))
        api.config_set("migrated", True)

    api.var("token", "")
    api.var("chat", "")
    api.var("confirm", True)
    api.var("mirror", False)
    api.var("incoming", False)
    api.var("allow_actions", False)
    api.var("speak_in", False)
    api.var("voice_in", True)
    api.var("voice_reply", False)
    api.var("voice_reply_all", False)
    api.var("voice_max", 600)
    api.var("allow_screen", False)
    api.var("allow_files", False)
    api.var("panel", True)
    state = {"last": "", "offset": None, "gen": 0, "err": "", "flist": []}
    stop = threading.Event()

    def base():
        return str(api.config_get("api_url", API_URL)).rstrip("/")

    def creds():
        return str(api.config_get("token", "")).strip(), str(api.config_get("chat", "")).strip()

    def send_checked(text, ask=True):
        token, chat = creds()
        if not token or not chat:
            return "Telegram не настроен: заполните токен и chat_id на вкладке «Плагины»."
        text = str(text).strip()
        if not text:
            return "Нечего отправлять."
        if ask and api.config_get("confirm", True) and not api.confirm(f"Отправить в Телеграм?\n\n{text[:500]}"):
            return "Не отправляю."
        try:
            tg_send(base(), token, chat, text, api.proxies("tg"), api.explain_proxy_error)
        except RuntimeError as e:
            return f"Не удалось отправить: {e}."
        api.log(text[:300])
        return "Отправлено в Телеграм."

    def command(text, m):
        r = match_tg(text, api.norm_ru)
        if not r:
            return None
        kind, msg = r
        if kind == "last":
            msg = state["last"]
            if not msg:
                return "Пока нечего отправлять: я ещё ничего не отвечал."
        return send_checked(msg)

    def tool(args):
        return send_checked(str(args.get("text", "")))

    def tool_file(args):
        if not api.config_get("allow_files", False):
            return "Отправка файлов выключена в настройках плагина Telegram."
        token, chat = creds()
        if not token or not chat:
            return "Telegram не настроен."
        raw = str(args.get("path", "")).strip().strip('"')
        p = Path(raw)
        if not p.is_absolute():
            p = api.files_dir() / raw
        if not under(p, roots()):
            found, opts = find_file(api.files_dir(), raw)
            if found is None:
                return "Отправлять можно только файлы из папки файлов агента и данных плагинов; такого файла нет."
            p = found
        if api.config_get("confirm", True) and not api.confirm(f"Отправить файл в Телеграм?\n\n{p.name}"):
            return "Не отправляю."
        try:
            send_path(p, str(args.get("caption", "")), photo=p.suffix.lower() in (".png", ".jpg", ".jpeg"))
        except Exception as e:
            return f"Не удалось отправить: {e}."
        return f"Файл «{p.name}» отправлен в Телеграм."

    def tool_screen(args):
        if not api.config_get("allow_screen", False):
            return "Отправка скриншотов выключена в настройках плагина Telegram."
        if api.config_get("confirm", True) and not api.confirm("Отправить скриншот экрана в Телеграм?"):
            return "Не отправляю."
        try:
            send_path(grab_screen(), "Экран ПК", photo=True)
        except Exception as e:
            return f"Не удалось: {e}."
        return "Скриншот отправлен в Телеграм."

    def mirror(text):
        if not api.config_get("mirror", False):
            return
        token, chat = creds()
        if token and chat:
            try:
                tg_send(base(), token, chat, text, api.proxies("tg"), api.explain_proxy_error)
            except RuntimeError as e:
                api.log(f"дубль напоминания не отправлен: {e}")

    def panel_markup():
        if not api.config_get("panel", True):
            return None
        return panel_keyboard(api.config_get("allow_screen", False), api.config_get("allow_files", False), custom_btns())

    def custom_btns():
        return parse_custom(api.config_get("custom_buttons", ""))

    def reply_to(text, markup="panel"):
        token, chat = creds()
        if markup == "panel":
            markup = panel_markup()
        try:
            tg_send(base(), token, chat, text or "(пустой ответ)", api.proxies("tg"), api.explain_proxy_error,
                    markup=markup)
        except RuntimeError as e:
            api.log(f"ответ в Telegram не отправлен: {e}")

    def trim_for_voice(text):
        """Не озвучивать простыню: обрезаем по концу предложения в пределах voice_max символов."""
        text = (text or "").strip()
        try:
            n = max(100, int(api.config_get("voice_max", 600)))
        except (TypeError, ValueError):
            n = 600
        if len(text) <= n:
            return text
        cut = text[:n]
        i = max(cut.rfind(". "), cut.rfind("! "), cut.rfind("? "), cut.rfind("\n"))
        return (cut[:i + 1] if i > n // 2 else cut).strip()

    def send_voice_reply(text):
        text = trim_for_voice(text)
        if not text:
            return
        path = None
        try:
            path = api.synth_file(text, api.data_dir / "out")
            token, chat = creds()
            tg_send_file(base(), token, chat, path, api.proxies("tg"), api.explain_proxy_error, voice=True)
        except Exception as e:
            api.log(f"голосовой ответ не отправлен: {e.__class__.__name__}: {e}")
            reply_to(f"Голосом ответить не вышло: {str(e)[:200]}")
        finally:
            if path:
                try:
                    Path(path).unlink()
                except OSError:
                    pass

    def handle(text, via_voice=False):
        t0 = text.strip()
        if t0 not in BTN_CMD:
            text = dict(custom_btns()).get(t0, text)      # своя кнопка -> её действие (один раз, без рекурсии)
        text = BTN_CMD.get(text.strip(), text)
        low = text.lower()
        if low in ("/start", "/help", "/menu"):
            if low == "/menu" and not api.config_get("panel", True):
                return reply_to("Панель кнопок выключена в настройках плагина.", {"remove_keyboard": True})
            return reply_to(HELP if low != "/menu" else "Панель кнопок включена.")
        if low == "/diag":
            return diag()
        if low in ("/new", "/stop", "/louder", "/quieter"):
            return reply_to(api.control({"/new": "clear", "/stop": "stop", "/louder": "louder", "/quieter": "quieter"}[low]))
        if low == "/status":
            url, model = api.get_lm()
            return reply_to(f"Агент на связи.\nМодель: {model or '—'}\nСервер: {url}\n"
                            f"Действия на ПК из Telegram: {'разрешены' if api.config_get('allow_actions', False) else 'выключены'}")
        if low == "/screen":
            return cmd_screen()
        if low == "/files":
            return cmd_files()
        if low == "/file" or low.startswith("/file "):
            return cmd_file(text[5:].strip())
        if text.startswith("/"):
            return reply_to("Не знаю такой команды. /help")
        api.log(f"из Telegram: {text[:200]}")
        want_voice = bool(api.config_get("voice_reply_all", False) or (via_voice and api.config_get("voice_reply", False)))

        def answer(ans):
            def work():
                reply_to(ans or "Ответа нет.")
                if want_voice and ans:
                    send_voice_reply(ans)
            threading.Thread(target=work, daemon=True).start()
        done = api.run_text(text, answer,
                            speak=bool(api.config_get("speak_in", False)),
                            allow_actions=bool(api.config_get("allow_actions", False)), timeout=90)
        if not done:
            reply_to("Агент сейчас занят, попробуйте позже.")

    def send_path(path, caption="", photo=False, markup=None):
        token, chat = creds()
        tg_send_file(base(), token, chat, path, api.proxies("tg"), api.explain_proxy_error, caption, photo, markup)

    def grab_screen():
        from PIL import ImageGrab
        img = ImageGrab.grab().convert("RGB")
        api.data_dir.mkdir(parents=True, exist_ok=True)
        p = api.data_dir / "screen.jpg"
        img.save(p, "JPEG", quality=85)
        return p

    def roots():
        return [api.files_dir(), api.data_dir.parent]

    def cmd_screen():
        if not api.config_get("allow_screen", False):
            return reply_to(f"Скриншоты из Telegram выключены. Включить: {WHERE} → «Разрешить /screen…».")
        try:
            send_path(grab_screen(), "Экран ПК", photo=True,
                      markup={"inline_keyboard": [[{"text": "🔄 Обновить", "callback_data": "scr"}]]})
        except Exception as e:
            reply_to(f"Не удалось сделать или отправить скриншот: {e}")

    def cmd_files():
        if not api.config_get("allow_files", False):
            return reply_to(f"Просмотр и отправка файлов из Telegram выключены. Включить: {WHERE} → «Разрешить /files, /file…».")
        root = api.files_dir()
        if not root.is_dir():
            return reply_to(f"Папки файлов агента нет: {root}\nУкажите существующую на вкладке «Функции» → «Файлы».")
        names = list_files(root, 40)
        state["flist"] = names[:20]
        if not names:
            return reply_to(f"В папке файлов агента пусто: {root}\nПоложите туда файлы или выберите другую папку "
                            f"на вкладке «Функции» → «Файлы».")
        reply_to(f"Папка: {root}\n" + "\n".join(names) + "\n\nНажмите на файл или напишите /file имя",
                 files_inline(names))

    def cmd_file(name):
        if not api.config_get("allow_files", False):
            return reply_to(f"Просмотр и отправка файлов из Telegram выключены. Включить: {WHERE} → «Разрешить /files, /file…».")
        if not name:
            return reply_to("Укажите имя: /file отчёт.docx (список: /files)")
        path, opts = find_file(api.files_dir(), name)
        if path is None:
            return reply_to("Нашёл несколько, уточните:\n" + "\n".join(opts) if opts else f"Файла «{name}» нет в папке агента.")
        try:
            send_path(path, path.name)
        except Exception as e:
            reply_to(f"Не удалось отправить: {e}")

    def diag():
        try:
            root = api.files_dir()
            cnt = len(list_files(root, 5000)) if root.is_dir() else 0
            folder = f"{root} ({'есть' if root.is_dir() else 'НЕТ такой папки'}, файлов: {cnt})"
        except Exception as e:
            folder = f"ошибка: {e}"
        try:
            wm = api.ui(lambda: (api._h.w_model.get(), api._h.w_device.get()))
            whisper = f"{wm[0]} на {wm[1]}"
        except Exception as e:
            whisper = f"не определить ({e})"
        def have(m):
            try:
                return "есть" if importlib.util.find_spec(m) else "НЕТ"
            except (ValueError, ImportError):
                return "есть" if m in sys.modules else "НЕТ"
        on = lambda k, d=False: "вкл" if api.config_get(k, d) else "выкл"
        reply_to("Telegram-плагин 1.7\n"
                 f"Папка файлов: {folder}\n"
                 f"Файлы: {on('allow_files')}, скриншоты: {on('allow_screen')}, голосовые: {on('voice_in', True)}, "
                 f"голосом в ответ: {on('voice_reply')}/{on('voice_reply_all')}, "
                 f"панель: {on('panel', True)}, действия на ПК: {on('allow_actions')}\n"
                 f"Whisper агента: {whisper}\n"
                 f"Модули: faster_whisper — {have('faster_whisper')}, av — {have('av')}, soundfile — {have('soundfile')}\n"
                 "Если голосовое не распознаётся — пришлите мне текст ошибки из чата.")

    def handle_voice(v):
        if not api.config_get("voice_in", True):
            return reply_to(f"Голосовые выключены. Включить: {WHERE} → «понимать голосовые сообщения».")
        if v["duration"] > 600:
            return reply_to("Голосовое длиннее 10 минут: пришлите покороче.")
        token, chat = creds()
        d = api.data_dir / "in"
        d.mkdir(parents=True, exist_ok=True)
        dest = d / f"v{int(time.time() * 1000)}.ogg"
        try:
            tg_download(base(), token, v["file_id"], dest, api.proxies("tg"), api.explain_proxy_error)
            text = api.transcribe_file(dest)
        finally:
            try:
                dest.unlink()
            except OSError:
                pass
        if not text.strip():
            return reply_to("Речь не распознана (тишина или слишком тихо). Повторите или напишите текстом. /diag — проверка.")
        reply_to("🎤 " + text.strip())
        handle(text.strip(), via_voice=True)

    def handle_callback(cid, data):
        token, chat = creds()
        try:
            tg_api(base(), token, "answerCallbackQuery", {"callback_query_id": cid}, api.proxies("tg"),
                   api.explain_proxy_error, 10)
        except RuntimeError:
            pass
        if data == "scr":
            return cmd_screen()
        if data.startswith("f:") and data[2:].isdigit():
            i = int(data[2:])
            if not api.config_get("allow_files", False):
                return reply_to(f"Просмотр и отправка файлов из Telegram выключены. Включить: {WHERE} → «Разрешить /files, /file…».")
            if i >= len(state["flist"]):
                return reply_to("Список устарел: нажмите «Файлы» ещё раз.")
            return cmd_file(state["flist"][i])

    def set_menu():
        token, chat = creds()
        try:
            tg_api(base(), token, "setMyCommands", {"commands": [{"command": c, "description": d}
                                                                for c, d in COMMANDS_MENU]},
                   api.proxies("tg"), api.explain_proxy_error, 10)
        except RuntimeError:
            pass

    def poll_loop(gen):
        first = True
        while not stop.is_set() and state["gen"] == gen:
            token, chat = creds()
            if not api.config_get("incoming", False) or not token or not chat:
                stop.wait(3)
                continue
            try:
                if first:                      # старые сообщения не выполняем
                    ups = tg_updates(base(), token, -1, api.proxies("tg"), timeout=0)
                    if ups:
                        state["offset"] = ups[-1]["update_id"] + 1
                    first = False
                    state["err"] = ""
                    set_menu()
                    continue
                ups = tg_updates(base(), token, state["offset"], api.proxies("tg"))
                state["err"] = ""
            except (requests.RequestException, ValueError, RuntimeError) as e:
                msg = "нет связи" if not isinstance(e, RuntimeError) else str(e)
                if msg != state["err"]:
                    state["err"] = msg
                    api.log(f"приём команд: {msg}")
                stop.wait(8)
                continue
            if stop.is_set() or state["gen"] != gen:      # плагин перезагружен: эти сообщения обработает новый опрос
                return
            for u in ups:
                state["offset"] = u["update_id"] + 1
                if not api.config_get("incoming", False):
                    continue
                text = pick_command(u, chat)
                try:
                    cbk = pick_callback(u, chat)
                    if cbk is not None:
                        handle_callback(*cbk)
                        continue
                    if text is not None:
                        handle(text)
                        continue
                    v = pick_voice(u, chat)
                    if v is not None:
                        handle_voice(v)
                    elif pick_other(u, chat):
                        reply_to("Пока принимаю текст и голосовые сообщения.")
                except Exception as e:
                    what = "голосовое" if (text is None and pick_voice(u, chat)) else "команду"
                    api.log(f"ошибка ({what}): {e.__class__.__name__}: {e}")
                    try:
                        reply_to(f"Не получилось обработать {what}: {e.__class__.__name__}: {str(e)[:200]}\n"
                                 f"Проверка: /diag")
                    except Exception:
                        pass

    def start_poll():
        state["gen"] += 1
        threading.Thread(target=poll_loop, args=(state["gen"],), daemon=True).start()
    start_poll()
    api.on_unload(lambda: (stop.set(), state.update(gen=state["gen"] + 1)))

    api.add_command(r"(?<!\w)(?:телеграм\w*|telegram|тг|tg)(?!\w)", command, "отправь в телеграм <текст> / отправь это в телеграм")
    api.add_tool("send_telegram", "Отправляет текстовое сообщение пользователю в его Telegram (с подтверждением).",
                 {"text": {"type": "string", "description": "Текст сообщения"}}, tool, ["text"])
    api.add_tool("send_telegram_file", "Отправляет файл из папки файлов агента (или скриншот плагина Chrome) пользователю в "
                 "Telegram (с подтверждением).", {"path": {"type": "string", "description": "Имя или путь файла"},
                                                  "caption": {"type": "string", "description": "Подпись"}}, tool_file, ["path"])
    api.add_tool("send_telegram_screenshot", "Отправляет скриншот экрана ПК пользователю в Telegram (с подтверждением).",
                 {}, tool_screen, [])
    api.on("reply", lambda t: state.update(last=t))
    api.on("reminder", mirror)

    def build(f):
        pad = {"padx": 6, "pady": 2}
        ttk.Label(f, text="Токен бота:").grid(row=0, column=0, sticky="w", **pad)
        ttk.Entry(f, textvariable=api.var("token"), width=46, show="•").grid(row=0, column=1, columnspan=3, sticky="w", **pad)
        ttk.Label(f, text="chat_id:").grid(row=1, column=0, sticky="w", **pad)
        ttk.Entry(f, textvariable=api.var("chat"), width=22).grid(row=1, column=1, sticky="w", **pad)

        def detect():
            token = api.var("token").get().strip()
            if not token:
                messagebox.showinfo("Telegram", "Сначала вставьте токен бота.")
                return
            proxies = api.proxies("tg")

            def work():
                try:
                    r = tg_find_chat(base(), token, proxies, api.explain_proxy_error)
                except RuntimeError as e:
                    api.post(messagebox.showerror, "Telegram", str(e))
                    return
                if not r:
                    api.post(messagebox.showinfo, "Telegram", "Бот пока никто не писал. Откройте его в Telegram, нажмите "
                                                              "«Старт» и повторите.")
                    return
                api.post(api.var("chat").set, str(r[0]))
                api.post(messagebox.showinfo, "Telegram", f"Найден чат: {r[1]} (id {r[0]}). Сохранён в поле.")
            threading.Thread(target=work, daemon=True).start()

        def test():
            token, chat = api.var("token").get().strip(), api.var("chat").get().strip()
            proxies = api.proxies("tg")
            if not token or not chat:
                messagebox.showinfo("Telegram", "Заполните токен и chat_id.")
                return

            def work():
                try:
                    tg_send(base(), token, chat, "Проверка связи: Nightlord агент на связи ✅", proxies,
                            api.explain_proxy_error)
                    api.post(messagebox.showinfo, "Telegram", "Тестовое сообщение отправлено.")
                except RuntimeError as e:
                    api.post(messagebox.showerror, "Telegram", str(e))
            threading.Thread(target=work, daemon=True).start()
        ttk.Button(f, text="Определить chat_id", command=detect).grid(row=1, column=2, sticky="w", **pad)
        ttk.Button(f, text="Отправить тест", command=test).grid(row=1, column=3, sticky="w", **pad)
        ttk.Checkbutton(f, text="Спрашивать подтверждение перед отправкой", variable=api.var("confirm")).grid(
            row=2, column=0, columnspan=4, sticky="w", **pad)
        ttk.Checkbutton(f, text="Дублировать напоминания в Телеграм", variable=api.var("mirror")).grid(
            row=3, column=0, columnspan=4, sticky="w", **pad)
        ttk.Checkbutton(f, text="Принимать команды из Telegram (бот отвечает только вашему личному чату)",
                        variable=api.var("incoming")).grid(row=4, column=0, columnspan=4, sticky="w", **pad)
        ttk.Checkbutton(f, text="…и разрешать действия на ПК, инструменты и горячие команды (иначе только разговор)",
                        variable=api.var("allow_actions")).grid(row=5, column=0, columnspan=4, sticky="w", padx=26, pady=2)
        ttk.Checkbutton(f, text="…и озвучивать ответ на ПК", variable=api.var("speak_in")).grid(
            row=6, column=0, columnspan=4, sticky="w", padx=26, pady=2)
        ttk.Checkbutton(f, text="…и понимать голосовые сообщения (распознаёт Whisper агента)", variable=api.var("voice_in")).grid(
            row=7, column=0, columnspan=4, sticky="w", padx=26, pady=2)
        vr = ttk.Frame(f)
        vr.grid(row=17, column=0, columnspan=4, sticky="w", padx=26, pady=2)
        ttk.Checkbutton(vr, text="Отвечать голосовым сообщением на мои голосовые (голос Voicebox; нужен метод /generate)",
                        variable=api.var("voice_reply")).pack(anchor="w")
        ttk.Checkbutton(vr, text="…и на текстовые тоже", variable=api.var("voice_reply_all")).pack(anchor="w", padx=20)
        mx = ttk.Frame(vr)
        mx.pack(anchor="w", padx=20)
        ttk.Label(mx, text="Озвучивать не больше символов:").pack(side="left")
        ttk.Spinbox(mx, from_=100, to=3000, increment=100, width=6, textvariable=api.var("voice_max")).pack(side="left", padx=6)
        ttk.Checkbutton(f, text="Разрешить /screen и отправку скриншота экрана в чат (изображение экрана уйдёт в Telegram)",
                        variable=api.var("allow_screen")).grid(row=8, column=0, columnspan=4, sticky="w", **pad)
        ttk.Checkbutton(f, text="Разрешить /files, /file и отправку файлов из папки файлов агента",
                        variable=api.var("allow_files")).grid(row=9, column=0, columnspan=4, sticky="w", **pad)
        ttk.Checkbutton(f, text="Показывать панель кнопок под полем ввода (Статус, Скриншот, Файлы, Новый диалог, Стоп, "
                                "Тише/Громче, Помощь)", variable=api.var("panel")).grid(
            row=10, column=0, columnspan=4, sticky="w", **pad)

        ttk.Label(f, text="Свои кнопки панели — по строке «Название = действие» (до 8). Действие: команда (/status) или "
                          "обычная фраза агенту, например «включи свет в гостиной» (фразы подчиняются галке «действия на ПК»):"
                  ).grid(row=13, column=0, columnspan=4, sticky="w", **pad)
        box = tk.Text(f, width=70, height=5)
        box.grid(row=14, column=0, columnspan=4, sticky="w", **pad)
        box.insert("1.0", api.config_get("custom_buttons", ""))

        def save_custom():
            api.config_set("custom_buttons", box.get("1.0", "end").strip())
            n = len(parse_custom(box.get("1.0", "end")))
            messagebox.showinfo("Telegram", f"Сохранено кнопок: {n}. Нажмите «Показать панель в чате», чтобы обновить её в Telegram.")
        ttk.Button(f, text="Сохранить свои кнопки", command=save_custom).grid(row=15, column=0, columnspan=2, sticky="w", **pad)

        def show_panel():
            token, chat = creds()
            if not token or not chat:
                return messagebox.showinfo("Telegram", "Заполните токен и chat_id.")

            def work():
                try:
                    tg_send(base(), token, chat, "Панель кнопок обновлена.", api.proxies("tg"), api.explain_proxy_error,
                            markup=panel_markup() or {"remove_keyboard": True})
                    set_menu()
                    api.post(messagebox.showinfo, "Telegram", "Панель отправлена: откройте чат с ботом.")
                except RuntimeError as e:
                    api.post(messagebox.showerror, "Telegram", str(e))
            threading.Thread(target=work, daemon=True).start()
        ttk.Button(f, text="Показать панель в чате", command=show_panel).grid(row=11, column=0, columnspan=2, sticky="w", **pad)
        ttk.Label(f, text=("Как настроить: в Telegram откройте @BotFather → /newbot → скопируйте токен сюда; затем откройте "
                           "своего бота, нажмите «Старт» и «Определить chat_id». Токен лежит в plugins/data/telegram/"
                           "config.json открытым текстом. Инструмент send_telegram доступен модели, когда включены "
                           "«Инструменты» на вкладке «Функции»."),
                  foreground="#777", wraplength=780, justify="left").grid(row=16, column=0, columnspan=4, sticky="w", **pad)
    api.add_settings(build)
