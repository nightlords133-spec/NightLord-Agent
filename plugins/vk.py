# Плагин Nightlord агента: ВКонтакте — отправка сообщений себе, чтение новых сообщений, уведомления.
# Работает через API ВКонтакте с токеном СООБЩЕСТВА (своё сообщество-«бот»): сначала вы пишете сообществу,
# потом оно может писать вам. Токен хранится в plugins/data/vk/config.json открытым текстом.
import random
import re
import threading
import time
import tkinter as tk
from tkinter import ttk, messagebox

import requests

PLUGIN = {"name": "ВКонтакте", "version": "1.1",
          "description": "«Отправь во вконтакте …», «отправь это в вк», «что нового в вк» (непрочитанные сообщения), "
                         "«отправь Ивану во вк …» (записная книжка), инструменты send_vk и read_vk для модели, уведомления "
                         "о новых сообщениях, дубль напоминаний. Через токен вашего сообщества."}

API_URL = "https://api.vk.com/method"
API_VER = "5.199"
SEND_WORD = r"(?:отправь|отправить|напиши|пришли|скинь|шли|перешли)"
VK_WORD = r"(?:вконтакте|контакте|вк|vk)"
ERRORS = {5: "токен недействителен или отозван", 7: "у токена нет права на сообщения (включите «Сообщения сообщества» "
          "и отметьте это право при создании ключа)", 9: "слишком много однотипных запросов, подождите",
          6: "слишком частые запросы, подождите", 15: "доступ запрещён", 100: "неверный параметр (проверьте peer_id)",
          901: "получатель не разрешил сообщения от сообщества: напишите сообществу первым",
          902: "настройки приватности получателя запрещают сообщение", 917: "у токена нет доступа к этому диалогу",
          27: "этот метод не работает с токеном сообщества", 28: "этот метод не работает с этим токеном"}


def match_vk(text, norm):
    """«Отправь во вконтакте привет» -> ("text", "привет"); «отправь это в вк» -> ("last", None);
    «что нового в вк» -> ("unread", None)."""
    t = norm(text)
    if re.match(rf"^(?:что нового|есть (?:новые )?сообщения|проверь (?:новые )?сообщения|прочитай (?:новые )?сообщения|"
                rf"покажи (?:новые )?сообщения|новые сообщения) (?:в|во|на) {VK_WORD}$", t):
        return "unread", None
    if re.match(rf"^{SEND_WORD}(?: мне)?(?: (?:это|его|последний ответ|ответ|последнее сообщение))? "
                rf"(?:в|во|на) {VK_WORD}$", t):
        return "last", None
    m = re.match(rf"^\s*{SEND_WORD}(?:\s+мне)?(?:\s+сообщение)?\s+(?:в|во|на)\s+{VK_WORD}\W*\s+(.+)$", text.strip(),
                 re.I | re.S)
    if m and m.group(1).strip():
        return "text", m.group(1).strip()
    m = re.match(rf"^{SEND_WORD} (?:это |его |последний ответ |ответ )(?P<who>{WHO}) (?:в|во|на) {VK_WORD}$", t)
    if m and not _is_self(m.group("who")):
        return "to_last", (m.group("who"), None)
    m = re.match(rf"^\s*{SEND_WORD}\s+(?:сообщение\s+)?(?P<who>{WHO})\s+(?:в|во|на)\s+{VK_WORD}\W*\s+(?P<text>.+)$",
                 text.strip(), re.I | re.S)
    if m and m.group("text").strip() and not _is_self(norm(m.group("who"))):
        return "to", (norm(m.group("who")), m.group("text").strip())
    return None


WHO = r"[\w-]+(?: [\w-]+)?"


def _is_self(who):
    return all(w in ("мне", "себе", "сообщение", "это", "его") for w in who.split())


def _stem(word):
    n = len(word)
    return word if n <= 3 else word[:-1] if n == 4 else word[:-2]


def _words_match(who, alias):
    if len(who) != len(alias):
        return False
    return all(w == a or (len(_stem(a)) >= 3 and w.startswith(_stem(a))) for w, a in zip(who, alias))


def parse_contacts(text, norm):
    """Строки «Иван, Ваня, Иван Петров = 12345» -> ([([имена], id)], [ошибки])."""
    out, errs = [], []
    for n, line in enumerate(str(text or "").splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        names, _, pid = line.rpartition("=")
        pid = pid.strip()
        names = [norm(x) for x in names.split(",") if norm(x)]
        if not names or not pid.isdigit() or int(pid) <= 0:
            errs.append(f"строка {n}: нужен вид «Имя, другое имя = числовой id»")
            continue
        out.append((names, pid))
    return out, errs


def find_contact(contacts, who):
    """Кого имеет в виду «ивану петрову»: [(id, имя)] — один человек, либо несколько (неоднозначно), либо пусто."""
    who_w = who.split()
    exact, loose = {}, {}
    for names, pid in contacts:
        for n in names:
            aw = n.split()
            if aw == who_w:
                exact[pid] = names[0]
            elif _words_match(who_w, aw) or (len(aw) > 1 and _words_match(who_w, aw[:1])):
                loose[pid] = names[0]
    found = exact or loose
    return sorted((pid, name.title()) for pid, name in found.items())


def scrub(text, token):
    return str(text).replace(token, "***") if token else str(text)


def vk_call(base, token, method, params, proxies, explain, ver=API_VER, timeout=15):
    """Вызов метода API. Токен идёт в теле POST (не в адресе) и в тексты ошибок не попадает."""
    data = dict(params, access_token=token, v=ver)
    try:
        r = requests.post(f"{base}/{method}", data=data, timeout=timeout, proxies=proxies)
        d = r.json()
    except requests.RequestException as e:
        why = explain(e) if proxies else ""
        raise RuntimeError("нет связи с ВКонтакте" + (f" ({why})" if why else "")) from None
    except ValueError:
        raise RuntimeError(f"ВКонтакте вернул не JSON (HTTP {r.status_code})") from None
    if "error" in d:
        e = d["error"] or {}
        code = int(e.get("error_code") or 0)
        raise RuntimeError(ERRORS.get(code) or scrub(str(e.get("error_msg") or f"ошибка {code}"), token)[:150])
    return d.get("response")


def names_from(resp):
    """id -> имя из profiles/groups расширенного ответа."""
    out = {}
    for p in (resp or {}).get("profiles") or []:
        out[p["id"]] = (str(p.get("first_name", "")) + " " + str(p.get("last_name", ""))).strip() or str(p["id"])
    for g in (resp or {}).get("groups") or []:
        out[-g["id"]] = g.get("name") or str(g["id"])
    return out


def unread_list(resp):
    """[(peer_id, last_message_id, имя, текст, число_непрочитанных)] из getConversations."""
    names, out = names_from(resp), []
    for it in (resp or {}).get("items") or []:
        c, m = it.get("conversation") or {}, it.get("last_message") or {}
        peer = (c.get("peer") or {}).get("id")
        if peer is None or m.get("out"):
            continue
        n = int(c.get("unread_count") or 0)
        if n <= 0:
            continue
        who = names.get(m.get("from_id"), names.get(peer, str(peer)))
        text = (m.get("text") or "").strip() or ("[вложение]" if m.get("attachments") else "")
        out.append((peer, m.get("id", 0), who, text, n))
    return out


def register(api):
    api.var("token", "")
    api.var("peer", "")
    api.var("confirm", True)
    api.var("confirm_others", True)
    api.var("mirror", False)
    api.var("notify", False)
    api.var("read_text", False)
    api.var("use_proxy", False)
    state = {"last": "", "seen": None, "others": []}

    def base():
        return str(api.config_get("api_url", API_URL)).rstrip("/")

    def proxies():
        return api.proxies("web") if api.config_get("use_proxy", False) else None

    def call(method, params, token=None):
        return vk_call(base(), token if token is not None else str(api.config_get("token", "")).strip(), method,
                       params, proxies(), api.explain_proxy_error, str(api.config_get("ver", API_VER)))

    def creds():
        return str(api.config_get("token", "")).strip(), str(api.config_get("peer", "")).strip()

    def vk_send(text, token=None, peer=None):
        text = str(text).strip()[:4000]
        if not text:
            raise RuntimeError("пустое сообщение")
        call("messages.send", {"peer_id": peer if peer is not None else creds()[1], "message": text,
                               "random_id": random.randint(1, 2**31 - 1)}, token)

    def contacts():
        return parse_contacts(api.config_get("contacts_text", ""), api.norm_ru)[0]

    def resolve(who):
        """(peer_id, имя) или текст ошибки."""
        found = find_contact(contacts(), api.norm_ru(who))
        if not found:
            return f"Не знаю контакта «{who}». Добавьте его в записную книжку на вкладке «Плагины»."
        if len(found) > 1:
            return "Уточните, кому: " + ", ".join(n for _, n in found) + "."
        return found[0]

    def send_checked(text, ask=True, who=None):
        token, peer = creds()
        if not token:
            return "ВКонтакте не настроен: вставьте токен на вкладке «Плагины»."
        text = str(text).strip()
        if not text:
            return "Нечего отправлять."
        name, others = "", False
        if who and not _is_self(api.norm_ru(who)):
            r = resolve(who)
            if isinstance(r, str):
                return r
            peer, name = r
            others = str(peer) != str(creds()[1])
        elif not peer:
            return "ВКонтакте не настроен: заполните peer_id на вкладке «Плагины»."
        if others:
            now = time.time()
            state["others"] = [t for t in state["others"] if now - t < 3600]
            if len(state["others"]) >= int(api.config_get("others_per_hour", 20) or 20):
                return "Слишком много сообщений другим людям за час, подождите."
            if api.config_get("confirm_others", True) and not api.confirm(
                    f"Отправить во ВКонтакте — кому: {name} (id {peer})?\n\n{text[:500]}"):
                return "Не отправляю."
        elif ask and api.config_get("confirm", True) and not api.confirm(f"Отправить во ВКонтакте?\n\n{text[:500]}"):
            return "Не отправляю."
        try:
            vk_send(text, peer=peer)
        except RuntimeError as e:
            return f"Не удалось отправить: {e}."
        if others:
            state["others"].append(time.time())
        api.log((f"{name}: " if others else "") + text[:300])
        return f"Отправлено: {name}." if others else "Отправлено во ВКонтакте."

    def fetch_unread(count=5):
        resp = call("messages.getConversations", {"filter": "unread", "count": count, "extended": 1})
        return unread_list(resp)

    def read_text(limit=3):
        token, _ = creds()
        if not token:
            return "ВКонтакте не настроен: нужен токен."
        try:
            items = fetch_unread(10)
        except RuntimeError as e:
            return f"Не удалось получить сообщения: {e}."
        if not items:
            return "Новых сообщений во ВКонтакте нет."
        total = sum(i[4] for i in items)
        parts = [f"{who}: {text[:200]}" if text else f"{who}: без текста" for _, _, who, text, _ in items[:limit]]
        more = f" Ещё диалогов: {len(items) - limit}." if len(items) > limit else ""
        return f"Непрочитанных сообщений: {total}. " + " ".join(parts) + more

    def command(text, m):
        r = match_vk(text, api.norm_ru)
        if not r:
            return None
        kind, msg = r
        if kind == "unread":
            return read_text()
        if kind in ("to", "to_last"):
            who, msg = msg
            if kind == "to_last":
                msg = state["last"]
                if not msg:
                    return "Пока нечего отправлять: я ещё ничего не отвечал."
            return send_checked(msg, who=who)
        if kind == "last":
            msg = state["last"]
            if not msg:
                return "Пока нечего отправлять: я ещё ничего не отвечал."
        return send_checked(msg)

    def tool_send(args):
        return send_checked(str(args.get("text", "")), who=str(args.get("to") or "") or None)

    def tool_read(args):
        return read_text(5)

    def mirror(text):
        if not api.config_get("mirror", False):
            return
        token, peer = creds()
        if token and peer:
            try:
                vk_send(text)
            except RuntimeError as e:
                api.log(f"дубль напоминания не отправлен: {e}")

    def poll():
        if not api.config_get("notify", False) or not creds()[0]:
            return
        try:
            items = fetch_unread(10)
        except RuntimeError:
            return
        cur = {peer: mid for peer, mid, *_ in items}
        if state["seen"] is None:                  # первый опрос: старые сообщения не озвучиваем
            state["seen"] = cur
            return
        for peer, mid, who, text, n in items:
            if state["seen"].get(peer) != mid:
                if api.config_get("read_text", False) and text:
                    api.say(f"Сообщение во ВКонтакте от {who}: {text[:200]}")
                else:
                    api.say(f"Новое сообщение во ВКонтакте от {who}.")
        state["seen"] = cur

    api.add_command(r"(?<!\w)(?:вконтакте|контакте|вк|vk)(?!\w)", command,
                    "отправь во вконтакте <текст> / отправь это в вк / что нового в вк")
    api.add_tool("send_vk", "Отправляет текстовое сообщение во ВКонтакте (с подтверждением): самому пользователю "
                 "или человеку из его записной книжки.",
                 {"text": {"type": "string", "description": "Текст сообщения"},
                  "to": {"type": "string", "description": "Кому: имя из записной книжки; пусто — самому пользователю"}},
                 tool_send, ["text"])
    api.add_tool("read_vk", "Читает непрочитанные сообщения пользователя во ВКонтакте (кто и что написал).",
                 {}, tool_read, [])
    api.on("reply", lambda t: state.update(last=t))
    api.on("reminder", mirror)
    api.add_timer(60, poll)

    def build(f):
        pad = {"padx": 6, "pady": 2}
        ttk.Label(f, text="Токен сообщества:").grid(row=0, column=0, sticky="w", **pad)
        ttk.Entry(f, textvariable=api.var("token"), width=46, show="•").grid(row=0, column=1, columnspan=3, sticky="w", **pad)
        ttk.Label(f, text="peer_id (ваш id):").grid(row=1, column=0, sticky="w", **pad)
        ttk.Entry(f, textvariable=api.var("peer"), width=22).grid(row=1, column=1, sticky="w", **pad)

        def run(title, fn):
            def work():
                try:
                    msg = fn()
                except RuntimeError as e:
                    api.post(messagebox.showerror, title, str(e))
                    return
                api.post(messagebox.showinfo, title, msg)
            threading.Thread(target=work, daemon=True).start()

        def need_token():
            tok = api.var("token").get().strip()
            if not tok:
                messagebox.showinfo("ВКонтакте", "Сначала вставьте токен сообщества.")
            return tok

        def detect():
            tok = need_token()
            if not tok:
                return

            def fn():
                resp = call("messages.getConversations", {"count": 20, "extended": 1}, tok)
                names = names_from(resp)
                for it in (resp or {}).get("items") or []:
                    peer = (it.get("conversation") or {}).get("peer") or {}
                    if peer.get("type") == "user" and peer.get("id"):
                        api.post(api.var("peer").set, str(peer["id"]))
                        return f"Найден диалог: {names.get(peer['id'], peer['id'])} (id {peer['id']}). Сохранён в поле."
                raise RuntimeError("Сообществу пока никто не писал. Откройте сообщество во ВКонтакте, напишите ему "
                                   "любое сообщение и повторите.")
            run("ВКонтакте", fn)

        def check():
            tok = need_token()
            if not tok:
                return

            def fn():
                g = call("groups.getById", {}, tok)
                g = (g.get("groups") if isinstance(g, dict) else g) or [{}]
                call("messages.getConversations", {"count": 1}, tok)
                return f"Токен работает. Сообщество: {g[0].get('name', '?')}. Сообщения доступны."
            run("ВКонтакте", fn)

        def test():
            tok, peer = api.var("token").get().strip(), api.var("peer").get().strip()
            if not tok or not peer:
                messagebox.showinfo("ВКонтакте", "Заполните токен и peer_id.")
                return

            def fn():
                vk_send("Проверка связи: Nightlord агент на связи ✅", tok, peer)
                return "Тестовое сообщение отправлено."
            run("ВКонтакте", fn)
        ttk.Button(f, text="Найти peer_id", command=detect).grid(row=1, column=2, sticky="w", **pad)
        ttk.Button(f, text="Проверить токен", command=check).grid(row=1, column=3, sticky="w", **pad)
        ttk.Button(f, text="Отправить тест", command=test).grid(row=2, column=3, sticky="w", **pad)
        ttk.Checkbutton(f, text="Спрашивать подтверждение перед отправкой себе", variable=api.var("confirm")).grid(
            row=7, column=0, columnspan=3, sticky="w", **pad)
        ttk.Checkbutton(f, text="Всегда спрашивать, кому и что уходит, когда пишу другим людям (рекомендуется)",
                        variable=api.var("confirm_others")).grid(row=2, column=0, columnspan=3, sticky="w", **pad)
        ttk.Checkbutton(f, text="Дублировать напоминания во ВКонтакте", variable=api.var("mirror")).grid(
            row=3, column=0, columnspan=4, sticky="w", **pad)
        ttk.Checkbutton(f, text="Сообщать голосом о новых сообщениях (проверка раз в минуту)", variable=api.var("notify")).grid(
            row=4, column=0, columnspan=3, sticky="w", **pad)
        ttk.Checkbutton(f, text="читать и текст", variable=api.var("read_text")).grid(row=4, column=3, sticky="w", **pad)
        ttk.Checkbutton(f, text="Запросы через прокси (плагин «VPN / прокси», вид «Поиск и страницы»)",
                        variable=api.var("use_proxy")).grid(row=5, column=0, columnspan=4, sticky="w", **pad)
        ttk.Label(f, text="Записная книжка (по строке на человека: «Имя, другое имя = id»):").grid(
            row=8, column=0, columnspan=4, sticky="w", **pad)
        box = tk.Text(f, width=70, height=6)
        box.grid(row=9, column=0, columnspan=4, sticky="we", **pad)
        box.insert("1.0", str(api.config_get("contacts_text", "")))

        def save_contacts():
            text = box.get("1.0", "end").strip()
            items, errs = parse_contacts(text, api.norm_ru)
            api.config_set("contacts_text", text)
            msg = f"Сохранено контактов: {len(items)}."
            if errs:
                messagebox.showwarning("ВКонтакте", msg + "\nПропущено:\n" + "\n".join(errs[:5]))
            else:
                messagebox.showinfo("ВКонтакте", msg)

        def add_dialogs():
            tok = need_token()
            if not tok:
                return

            def fn():
                resp = call("messages.getConversations", {"count": 50, "extended": 1}, tok)
                names, have = names_from(resp), {p for _, p in parse_contacts(box.get("1.0", "end"), api.norm_ru)[0]}
                new = []
                for it in (resp or {}).get("items") or []:
                    peer = (it.get("conversation") or {}).get("peer") or {}
                    pid = str(peer.get("id") or "")
                    if peer.get("type") == "user" and pid and pid not in have:
                        new.append(f"{names.get(peer['id'], pid)} = {pid}")
                api.post(lambda: box.insert("end", ("\n" if box.get("1.0", "end").strip() else "") + "\n".join(new)))
                return f"Добавлено строк: {len(new)}. Проверьте имена и нажмите «Сохранить контакты»."
            run("ВКонтакте", fn)
        bf = ttk.Frame(f)
        bf.grid(row=10, column=0, columnspan=4, sticky="w", **pad)
        ttk.Button(bf, text="Сохранить контакты", command=save_contacts).pack(side="left", padx=2)
        ttk.Button(bf, text="Добавить из диалогов сообщества", command=add_dialogs).pack(side="left", padx=2)
        ttk.Label(f, text=("Писать можно только тем, кто сам написал сообществу или разрешил его сообщения (иначе ВК откажет). "
                           "Имена понимаются в любом падеже: «отправь Ивану во вк…». Если подходят несколько человек, "
                           "агент спросит, кому. Другим людям не больше 20 сообщений в час."),
                  foreground="#777", wraplength=780, justify="left").grid(row=11, column=0, columnspan=4, sticky="w", **pad)
        ttk.Label(f, text=("Как настроить: 1) создайте своё сообщество ВКонтакте (или возьмите своё); 2) Управление → Сообщения → "
                           "включите сообщения сообщества; 3) Управление → Работа с API → Создать ключ с правом «Сообщения "
                           "сообщества» → скопируйте токен сюда; 4) напишите сообществу любое сообщение от своей страницы; "
                           "5) нажмите «Найти peer_id» и «Проверить токен». Сообщество (не вы) пишет вам и читает диалоги "
                           "с ним — личные переписки с другими людьми через такой токен недоступны. Токен лежит в "
                           "plugins/data/vk/config.json открытым текстом; ключ не публикуйте. Правила ВКонтакте и "
                           "названия пунктов меню могут меняться — я не проверял их вживую."),
                  foreground="#777", wraplength=780, justify="left").grid(row=12, column=0, columnspan=4, sticky="w", **pad)
    api.add_settings(build)
