# Плагин Nightlord агента: мониторинг и поиск моделей, которые сейчас готовы к работе.
# Берёт провайдеров и ключи из плагина «Облачные модели» (ничего вводить заново не нужно), запрашивает список моделей
# и коротким тестовым запросом (1 запрос, до 8 токенов на модель) проверяет, какие из них отвечают прямо сейчас:
# готова / перегружена (503) / лимит (429) / недоступна (404) / нет доступа (401, 403).
# Умеет следить по таймеру, сообщать, когда текущая модель отвалилась, и переключаться на лучшую готовую.
import json
import os
import re
import threading
import time
import tkinter as tk
from tkinter import ttk, messagebox

import requests

PLUGIN = {"name": "Мониторинг моделей", "version": "1.0",
          "description": "Находит облачные модели, готовые к работе: «какие модели готовы», «проверь модели», "
                         "«переключись на рабочую модель», «найди модель флеш». Слежение по таймеру, автопереключение "
                         "на лучшую готовую модель того же провайдера. Нужен плагин «Облачные модели» с ключом."}

# провайдер: (вид, адрес по умолчанию, переменная среды, без ключа)
PRESET_INFO = {
    "Claude (Anthropic)": ("anthropic", "https://api.anthropic.com", "ANTHROPIC_API_KEY", False),
    "OpenAI": ("openai", "https://api.openai.com/v1", "OPENAI_API_KEY", False),
    "OpenRouter": ("openai", "https://openrouter.ai/api/v1", "OPENROUTER_API_KEY", False),
    "Groq": ("openai", "https://api.groq.com/openai/v1", "GROQ_API_KEY", False),
    "DeepSeek": ("openai", "https://api.deepseek.com/v1", "DEEPSEEK_API_KEY", False),
    "Mistral": ("openai", "https://api.mistral.ai/v1", "MISTRAL_API_KEY", False),
    "Google Gemini (OpenAI-режим)": ("openai", "https://generativelanguage.googleapis.com/v1beta/openai",
                                     "GEMINI_API_KEY", False),
    "Свой сервер (OpenAI-совместимый)": ("openai", "", "", True),
}
NON_CHAT = re.compile(r"embed|tts|whisper|image|imagen|veo|audio|moderation|dall-e|transcribe|realtime|aqa|rerank|guard|"
                      r"speech|-live|bge|ocr|robotics", re.I)
LABEL = {"ok": "готова", "limit": "лимит запросов", "busy": "перегружена", "gone": "недоступна", "denied": "нет доступа",
         "bad": "отказ запроса", "err": "ошибка", "timeout": "не отвечает", "net": "нет связи", "skip": "не проверялась"}
RU_EN = {"флеш": "flash", "флэш": "flash", "лайт": "lite", "про": "pro", "хайку": "haiku", "соннет": "sonnet",
         "опус": "opus", "мини": "mini", "клод": "claude", "джемини": "gemini", "гемини": "gemini", "гпт": "gpt",
         "дипсик": "deepseek", "мистраль": "mistral", "лама": "llama"}
HEADERS_ANTHROPIC = "2023-06-01"


def scrub(text, key):
    text = str(text)
    return text.replace(key, "***") if key else text


def classify(code):
    if code == 200:
        return "ok"
    if code == 429:
        return "limit"
    if code in (500, 502, 503, 504, 529):
        return "busy"
    if code == 404:
        return "gone"
    if code in (401, 403):
        return "denied"
    if code == 400:
        return "bad"
    return "err"


def err_text(r):
    try:
        d = r.json()
        if isinstance(d, list) and d:
            d = d[0]
        e = d.get("error") if isinstance(d, dict) else None
        if isinstance(e, dict):
            return str(e.get("message") or e.get("type") or "")[:200]
        if isinstance(e, str):
            return e[:200]
    except ValueError:
        pass
    return (getattr(r, "text", "") or "")[:200] or f"HTTP {r.status_code}"


def natural_key(s):
    return [(0, int(x), "") if x.isdigit() else (1, 0, x) for x in re.findall(r"\d+|\D+", s.lower())]


def to_en(q):
    q = (q or "").lower().replace("ё", "е")
    for ru, en in RU_EN.items():
        q = re.sub(rf"\b{ru}\b", en, q)
    return q.strip()


def safe_rx(pattern):
    pattern = (pattern or "").strip()
    if not pattern:
        return None
    try:
        return re.compile(pattern, re.I)
    except re.error:
        return re.compile(re.escape(pattern), re.I)


def pick_candidates(ids, prev, current, include, exclude, preferred, cap):
    """Какие модели проверять: только чат-модели, сначала текущая, любимые, ранее рабочие, потом новые версии."""
    ids = [i for i in dict.fromkeys(ids) if not NON_CHAT.search(i)]
    inc, exc = safe_rx(include), safe_rx(exclude)
    if inc:
        ids = [i for i in ids if inc.search(i)]
    if exc:
        ids = [i for i in ids if not exc.search(i)]
    prefs = [p.strip().lower() for p in re.split(r"[,;]+", preferred or "") if p.strip()]
    ids.sort(key=natural_key, reverse=True)

    def rank(i):
        return (0 if i == current else 1, 0 if any(p in i.lower() for p in prefs) else 1,
                0 if prev.get(i) == "ok" else 1)
    ids.sort(key=rank)
    return ids[:max(1, cap)]


def best_ready(rows, provider, preferred, favs=None, only_fav=False):
    """Лучшая готовая модель провайдера: избранные, потом по списку любимых слов, потом по скорости ответа."""
    prefs = [p.strip().lower() for p in re.split(r"[,;]+", preferred or "") if p.strip()]
    favs = favs or set()
    ok = [(m, r) for (p, m), r in rows.items() if p == provider and r["status"] == "ok" and (not only_fav or (p, m) in favs)]
    ok.sort(key=lambda x: (0 if (provider, x[0]) in favs else 1, 0 if any(p in x[0].lower() for p in prefs) else 1,
                           x[1].get("ms") or 1e9))
    return ok[0][0] if ok else None


def register(api):
    for k, d in (("probe", True), ("only_fav", False), ("show_fav", False), ("only_active", False), ("autoswitch", False), ("announce", False), ("voice", True)):
        api.var(k, d)
    api.var("auto_min", 30)
    api.var("max_probe", 20)
    api.var("gap", 2)
    api.var("include", "")
    api.var("exclude", "")
    api.var("preferred", "flash, haiku, mini")
    st = {"rows": {}, "busy": False, "last": 0.0, "msg": "", "tree": None, "label": None, "search": tk.StringVar(value=""), "show_fav": tk.BooleanVar(value=False)}
    lock = threading.Lock()
    status_file = api.data_dir / "status.json"
    try:
        for k, v in json.loads(status_file.read_text(encoding="utf-8")).items():
            p, m = k.split("\t", 1)
            st["rows"][(p, m)] = v
    except (OSError, ValueError):
        pass
    st["last"] = time.time() - max(1, int(api.config_get("auto_min", 30) or 30)) * 60 + 120     # первая проверка через ~2 мин

    def favs():
        out = set()
        for x in api.config_get("favorites", []) or []:
            if isinstance(x, str) and "\t" in x:
                p, m = x.split("\t", 1)
                out.add((p, m))
        return out

    def save_favs(fs):
        api.config_set("favorites", sorted(f"{p}\t{m}" for p, m in fs))

    def best(provider):
        return best_ready(st["rows"], provider, api.config_get("preferred", ""), favs(), bool(api.config_get("only_fav", False)))

    def save_rows():
        try:
            api.data_dir.mkdir(parents=True, exist_ok=True)
            status_file.write_text(json.dumps({f"{p}\t{m}": r for (p, m), r in st["rows"].items()}, ensure_ascii=False),
                                   encoding="utf-8")
        except OSError:
            pass

    def cloud_cfg():
        try:
            return json.loads((api.data_dir.parent / "cloud_llm" / "config.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def providers():
        d = cloud_cfg()
        profs = d.get("profiles") or {}
        out = []
        for name, (kind, url, env, nokey) in PRESET_INFO.items():
            if api.config_get("only_active", False) and name != d.get("active", "Claude (Anthropic)"):
                continue
            p = profs.get(name) or {}
            u = (p.get("url") or url).strip().rstrip("/")
            key = (p.get("key") or "").strip() or (os.environ.get(env, "").strip() if env else "")
            if not u or (not key and not nokey):
                continue
            try:
                rpm = int(float(p.get("rpm") or 0))
            except (TypeError, ValueError):
                rpm = 0
            out.append({"name": name, "kind": kind, "url": u, "key": key, "rpm": rpm})
        return out

    def active_name():
        return cloud_cfg().get("active", "Claude (Anthropic)")

    def bridge_url():
        try:
            return f"http://127.0.0.1:{int(cloud_cfg().get('port', '11500'))}"
        except (TypeError, ValueError):
            return "http://127.0.0.1:11500"

    def px():
        if not cloud_cfg().get("use_proxy", False):
            return None
        p = api.proxies("web")
        if not p:
            raise RuntimeError("в «Облачных моделях» включено «через прокси», но плагин «VPN / прокси» не настроен")
        return p

    def headers(p):
        if p["kind"] == "anthropic":
            return {"x-api-key": p["key"], "anthropic-version": HEADERS_ANTHROPIC, "content-type": "application/json"}
        h = {"content-type": "application/json"}
        if p["key"]:
            h["authorization"] = "Bearer " + p["key"]
        return h

    def list_models(p, proxies):
        url = p["url"] + ("/v1/models?limit=1000" if p["kind"] == "anthropic" else "/models")
        r = requests.get(url, headers=headers(p), timeout=15, proxies=proxies)
        if r.status_code != 200:
            raise RuntimeError(f"{r.status_code}: {err_text(r)}")
        d = r.json()
        items = d.get("data") or d.get("models") or [] if isinstance(d, dict) else []
        ids = [str(m.get("id") or m.get("name") or "") for m in items if isinstance(m, dict)]
        return [i.replace("models/", "", 1) for i in ids if i]

    def probe(p, model, proxies):
        """Один короткий запрос. Возвращает {"status", "ms", "msg"}."""
        t0 = time.time()
        try:
            if p["kind"] == "anthropic":
                r = requests.post(p["url"] + "/v1/messages", headers=headers(p), proxies=proxies, timeout=25,
                                  json={"model": model, "max_tokens": 8, "messages": [{"role": "user", "content": "ок"}]})
            else:
                body = {"model": model, "max_tokens": 8, "messages": [{"role": "user", "content": "ок"}]}
                r = requests.post(p["url"] + "/chat/completions", headers=headers(p), json=body, proxies=proxies, timeout=25)
                if r.status_code == 400 and "max_completion_tokens" in err_text(r):
                    body["max_completion_tokens"] = body.pop("max_tokens")
                    r = requests.post(p["url"] + "/chat/completions", headers=headers(p), json=body, proxies=proxies, timeout=25)
        except requests.Timeout:
            return {"status": "timeout", "ms": None, "msg": "нет ответа за 25 с"}
        except requests.RequestException as e:
            return {"status": "net", "ms": None, "msg": scrub(type(e).__name__, p["key"])}
        ms = int((time.time() - t0) * 1000)
        return {"status": classify(r.status_code), "ms": ms if r.status_code == 200 else None,
                "msg": "" if r.status_code == 200 else scrub(err_text(r), p["key"])[:160]}

    def set_msg(t):
        st["msg"] = t
        lab = st["label"]
        if lab is not None:
            api.post(lambda: _cfg_label(lab, t))

    def _cfg_label(lab, t):
        try:
            lab.config(text=t)
        except tk.TclError:
            pass

    def current():
        try:
            return api.get_lm()
        except Exception:
            return "", ""

    def connected_to_bridge():
        return current()[0].rstrip("/") == bridge_url()

    def apply_model(model):
        api.set_lm(model=model)

    def probe_list(p, cands, proxies):
        """Проверить модели провайдера по очереди с паузами. Возвращает (проверено, готово)."""
        gap = max(float(api.config_get("gap", 2) or 0), 60.0 / p["rpm"] if p["rpm"] else 0.0)
        stop_prov, limits, n_all, n_ok = False, 0, 0, 0
        for i, m in enumerate(cands):
            if stop_prov:
                st["rows"][(p["name"], m)] = {"status": "skip", "ms": None, "msg": "остановлено: три отказа по лимиту подряд", "t": time.time()}
                continue
            set_msg(f"Проверяю {p['name']}: {m} ({i + 1}/{len(cands)})…")
            res = probe(p, m, proxies)
            res["t"] = time.time()
            st["rows"][(p["name"], m)] = res
            n_all += 1
            n_ok += res["status"] == "ok"
            api.post(refresh_table)
            if res["status"] == "limit":      # у многих провайдеров лимиты свои на каждую модель: одна 429 — не конец
                limits += 1
                if limits >= 3:                                                # три подряд — это общий лимит, останавливаемся
                    stop_prov = True
                elif i < len(cands) - 1:
                    time.sleep(gap + 4)
            else:
                limits = 0
                if i < len(cands) - 1:
                    time.sleep(gap)
        return n_all, n_ok

    def run_check(only=None):
        """Список моделей → проверка → сравнение с прошлым → уведомления и автопереключение. Вызывать в потоке.
        only: None — по настройкам; "fav" — только избранные; список пар (провайдер, модель) — только они."""
        if not lock.acquire(blocking=False):
            return False
        try:
            st["busy"] = True
            provs = providers()
            if not provs:
                set_msg("Нет провайдеров с ключом: введите ключ на вкладке «Облачные модели».")
                return True
            try:
                proxies = px()
            except RuntimeError as e:
                set_msg(str(e))
                return True
            before = {k: v["status"] for k, v in st["rows"].items()}
            cur_url, cur_model = current()
            active = active_name()
            n_ok = n_all = 0
            targets = None
            if isinstance(only, (list, set, tuple)):
                targets = set(only)
            elif only == "fav" or (only is None and api.config_get("only_fav", False)):
                targets = favs()
            if targets is not None and not targets:
                set_msg("Избранных моделей нет: выделите строки в таблице и нажмите «★ В избранное».")
                return True
            for p in provs:
                if targets is not None:                                  # точечная проверка: без запроса списка, сразу по моделям
                    cands = sorted((m for (pn, m) in targets if pn == p["name"]), key=natural_key, reverse=True)
                    cands.sort(key=lambda m: 0 if (p["name"] == active and m == cur_model) else 1)
                    if cands:
                        a, b = probe_list(p, cands, proxies)
                        n_all, n_ok = n_all + a, n_ok + b
                    continue
                set_msg(f"Запрашиваю список моделей: {p['name']}…")
                try:
                    ids = list_models(p, proxies)
                except (requests.RequestException, RuntimeError) as e:
                    set_msg(f"{p['name']}: список не получен ({scrub(e, p['key'])[:120]})")
                    continue
                ids = [i for i in dict.fromkeys(ids) if not NON_CHAT.search(i)]       # озвучка/картинки/эмбеддинги в таблицу не берём
                prev = {m: r["status"] for (pn, m), r in st["rows"].items() if pn == p["name"]}
                cands = pick_candidates(ids, prev, cur_model if p["name"] == active else "",
                                        api.config_get("include", ""), api.config_get("exclude", ""),
                                        api.config_get("preferred", ""), int(api.config_get("max_probe", 20) or 20))
                for k in [k for k in st["rows"] if k[0] == p["name"] and k[1] not in ids]:
                    del st["rows"][k]                                          # модель пропала из списка провайдера
                if not api.config_get("probe", True):
                    for m in ids:
                        st["rows"].setdefault((p["name"], m), {"status": "skip", "ms": None, "msg": "", "t": time.time()})
                    continue
                a, b = probe_list(p, cands, proxies)
                n_all, n_ok = n_all + a, n_ok + b
                for m in ids:
                    if (p["name"], m) not in st["rows"]:
                        st["rows"][(p["name"], m)] = {"status": "skip", "ms": None, "msg": "", "t": time.time()}
            save_rows()
            st["last"] = time.time()
            set_msg(f"Проверено {n_all}, готово к работе: {n_ok}, всего моделей в списке: {len(st['rows'])} ({time.strftime('%H:%M')}).")
            api.post(refresh_table)
            after_current(before, active, cur_url, cur_model)
        except Exception as e:
            set_msg("Ошибка проверки: " + str(e)[:150])
        finally:
            st["busy"] = False
            lock.release()
        return True

    def after_current(before, active, cur_url, cur_model):
        """Сообщает об изменении состояния текущей модели и (если разрешено) переключает на лучшую готовую."""
        if not cur_model or cur_url.rstrip("/") != bridge_url():
            return
        row = st["rows"].get((active, cur_model))
        if not row:
            return
        old, new = before.get((active, cur_model)), row["status"]
        if new == old:
            return
        if new == "ok":
            api.log(f"текущая модель {cur_model} снова готова ({row['ms']} мс)")
            if api.config_get("announce", False):
                api.say("Текущая модель снова в строю.")
            return
        api.log(f"текущая модель {cur_model}: {LABEL.get(new, new)}" + (f" — {row['msg']}" if row["msg"] else ""))
        if new in ("busy", "gone", "denied", "timeout", "err", "net") and api.config_get("autoswitch", False):
            b = best(active)
            if b and b != cur_model:
                apply_model(b)
                api.log(f"переключился на {b}")
                if api.config_get("announce", False):
                    api.say(f"Модель {cur_model} недоступна, переключился на {b}.")
                return
            api.log("готовых моделей у этого провайдера не нашёл — остаюсь на текущей")
        if api.config_get("announce", False):
            api.say(f"Текущая модель {cur_model}: {LABEL.get(new, new)}.")

    def start_check(only=None):
        if st["busy"]:
            return False
        threading.Thread(target=run_check, args=(only,), daemon=True).start()
        return True

    def tick():
        every = int(api.config_get("auto_min", 30) or 0) * 60
        if every > 0 and not st["busy"] and time.time() - st["last"] >= every:
            run_check()
    api.add_timer(60, tick)

    # ----- таблица -----
    def sorted_rows():
        q = to_en(st["search"].get())
        out = []
        fs = favs()
        only_f = bool(st["show_fav"].get())
        for (p, m), r in st["rows"].items():
            if only_f and (p, m) not in fs:
                continue
            if q and q not in m.lower() and q not in p.lower() and q not in LABEL.get(r["status"], "").lower():
                continue
            out.append((p, m, r))
        order = {"ok": 0, "limit": 1, "busy": 2, "timeout": 3, "net": 3, "skip": 5}
        out.sort(key=lambda x: (order.get(x[2]["status"], 4), x[2].get("ms") or 1e9, x[0], x[1]))
        return out

    def refresh_table():
        tree = st["tree"]
        if tree is None:
            return
        try:
            keep = set(selected_pairs())
            tree.delete(*tree.get_children())
            fs, pick = favs(), []
            for p, m, r in sorted_rows():
                when = time.strftime("%H:%M", time.localtime(r["t"])) if r.get("t") else ""
                iid = tree.insert("", "end", values=("★" if (p, m) in fs else "", p, m, LABEL.get(r["status"], r["status"]),
                                                     r["ms"] if r.get("ms") else "", when, r.get("msg", "")))
                if (p, m) in keep:
                    pick.append(iid)
            if pick:
                tree.selection_set(pick)
        except tk.TclError:
            pass

    def selected_pairs():
        tree = st["tree"]
        if tree is None:
            return []
        try:
            out = []
            for iid in tree.selection():
                v = tree.item(iid, "values")
                if v and len(v) > 2:
                    out.append((str(v[1]), str(v[2])))
            return out
        except tk.TclError:
            return []
    st["search"].trace_add("write", lambda *a: refresh_table())
    st["show_fav"].trace_add("write", lambda *a: refresh_table())

    # ----- голос -----
    def ready_names(provider, n=3):
        ok = [(m, r) for (p, m), r in st["rows"].items() if p == provider and r["status"] == "ok"]
        ok.sort(key=lambda x: x[1].get("ms") or 1e9)
        return [m for m, _ in ok[:n]], len(ok)

    def command(text, m):
        if not api.config_get("voice", True):
            return None
        t = api.norm_ru(text)
        if re.match(r"^(?:какие|какая) модел\w* (?:сейчас )?(?:готов\w*|работа\w*|доступн\w*)(?: к работе)?$|"
                    r"^что (?:из моделей )?(?:сейчас )?(?:работает|готово)$|^статус моделей$", t):
            act = active_name()
            if not st["rows"]:
                return "Проверок ещё не было. Скажите «проверь модели»."
            names, n = ready_names(act)
            cur = current()[1]
            row = st["rows"].get((act, cur))
            tail = f" Текущая {cur}: {LABEL.get(row['status'], row['status'])}." if row else ""
            if not names:
                return f"У провайдера {act} сейчас нет готовых моделей.{tail}"
            return f"У {act} готово моделей: {n}. Быстрее всех: {', '.join(names)}.{tail}"
        if re.match(r"^(?:проверь|обнови) избранн\w+ модел\w*$", t):
            return "Проверяю избранные модели." if start_check("fav") else "Проверка уже идёт."
        if re.match(r"^какие избранн\w+ модел\w* (?:сейчас )?(?:готов\w*|работа\w*|доступн\w*)$", t):
            fs = favs()
            if not fs:
                return "Избранных моделей нет."
            ok = [m for (p, m) in sorted(fs) if st["rows"].get((p, m), {}).get("status") == "ok"]
            bad = [m for (p, m) in sorted(fs) if st["rows"].get((p, m), {}).get("status") not in ("ok", None, "skip")]
            return (f"Избранных {len(fs)}, готовы: {', '.join(ok) if ok else 'нет'}." + (f" Не отвечают: {', '.join(bad)}." if bad else ""))
        if re.match(r"^(?:проверь|обнови|просканируй) (?:все )?модел\w*$|^проверка моделей$", t):
            return "Проверяю модели, это займёт около минуты." if start_check() else "Проверка уже идёт."
        if re.match(r"^(?:переключись|перейди|используй|выбери)(?: на)? (?:рабоч\w+|лучш\w+|готов\w+|живую) модел\w*$", t):
            act = active_name()
            if not connected_to_bridge():
                return "Агент сейчас не подключён к облаку, переключать нечего."
            b = best(act)
            if not b:
                return "Готовых моделей в списке нет. Скажите «проверь модели»."
            apply_model(b)
            return f"Переключился на {b}."
        mm = re.match(r"^найди модел\w* (.+)$", t)
        if mm:
            q = to_en(mm.group(1))
            hits = [(p, mdl, r) for p, mdl, r in sorted_rows() if q in mdl.lower()]
            if not hits:
                return f"Моделей со словом «{q}» в списке нет."
            ok = [h for h in hits if h[2]["status"] == "ok"]
            return (f"Нашёл {len(hits)}, готовы {len(ok)}." + (f" Лучшая: {ok[0][1]}." if ok else
                                                              f" Первая в списке {hits[0][1]}: {LABEL.get(hits[0][2]['status'])}."))
        return None
    api.add_command(r"(?:модел|что сейчас работает|статус)", command, "какие модели готовы, проверь модели, переключись на рабочую модель, найди модель …")

    # ----- интерфейс -----
    def build(f):
        pad = {"padx": 6, "pady": 2}
        bar = ttk.Frame(f)
        bar.grid(row=0, column=0, columnspan=4, sticky="w", **pad)
        ttk.Button(bar, text="Проверить сейчас", command=lambda: start_check()).pack(side="left", padx=(0, 8))

        def selected():
            sp = selected_pairs()
            return sp[0] if sp else None

        def toggle_fav():
            sp = selected_pairs()
            if not sp:
                messagebox.showinfo("Модели", "Выделите одну или несколько строк в таблице (Ctrl/Shift — несколько).")
                return
            fs = favs()
            if all(x in fs for x in sp):
                fs -= set(sp)
            else:
                fs |= set(sp)
            save_favs(fs)
            refresh_table()
            set_msg(f"В избранном моделей: {len(fs)}.")

        def check_selected():
            sp = selected_pairs()
            if not sp:
                messagebox.showinfo("Модели", "Выделите строки в таблице.")
                return
            start_check(sp)

        def use_model(pair):
            if not pair:
                messagebox.showinfo("Модели", "Выберите строку в таблице.")
                return
            prov, model = pair
            if prov != active_name():
                messagebox.showinfo("Модели", f"Эта модель у провайдера «{prov}», а в «Облачных моделях» выбран «{active_name()}». "
                                              f"Сначала переключите провайдера там.")
                return
            if not connected_to_bridge():
                messagebox.showinfo("Модели", "Агент не подключён к облаку: нажмите «Подключить агента к облаку» на вкладке "
                                              "«Облачные модели».")
                return
            apply_model(model)
            set_msg(f"Агент теперь использует {model}.")

        ttk.Button(bar, text="Использовать выбранную", command=lambda: use_model(selected())).pack(side="left", padx=(0, 8))
        ttk.Button(bar, text="Лучшая готовая", command=lambda: use_model(
            (active_name(), best(active_name())) if best(active_name()) else None)).pack(side="left", padx=(0, 8))
        ttk.Label(bar, text="Поиск:").pack(side="left", padx=(8, 3))
        ttk.Entry(bar, textvariable=st["search"], width=22).pack(side="left")
        bar2 = ttk.Frame(f)
        bar2.grid(row=1, column=0, columnspan=4, sticky="w", **pad)
        ttk.Button(bar2, text="★ В избранное / убрать", command=toggle_fav).pack(side="left", padx=(0, 8))
        ttk.Button(bar2, text="Проверить выделенные", command=check_selected).pack(side="left", padx=(0, 8))
        ttk.Button(bar2, text="Проверить избранные", command=lambda: start_check("fav")).pack(side="left", padx=(0, 8))
        ttk.Checkbutton(bar2, text="показывать только избранные", variable=st["show_fav"]).pack(side="left", padx=(8, 0))
        cols = ("fav", "provider", "model", "status", "ms", "time", "note")
        holder = ttk.Frame(f)
        holder.grid(row=2, column=0, columnspan=4, sticky="we", **pad)
        tree = ttk.Treeview(holder, columns=cols, show="headings", height=14)
        sb = ttk.Scrollbar(holder, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=sb.set)
        for c, t, w in (("fav", "★", 28), ("provider", "Провайдер", 150), ("model", "Модель", 230), ("status", "Состояние", 110),
                        ("ms", "мс", 55), ("time", "Когда", 50), ("note", "Пояснение", 240)):
            tree.heading(c, text=t)
            tree.column(c, width=w, anchor="w")
        tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")

        def wheel(e):                       # колесо крутит таблицу, а не всю страницу вкладки
            tree.yview_scroll(-1 if e.delta > 0 else 1, "units")
            return "break"
        tree.bind("<MouseWheel>", wheel)
        st["tree"] = tree
        st["label"] = ttk.Label(f, text=st["msg"] or "Проверок ещё не было.", wraplength=780, justify="left")
        st["label"].grid(row=3, column=0, columnspan=4, sticky="w", **pad)
        rows = (("only_fav", "Проверять только избранные модели (остальные не трогать: меньше запросов и лимитов)"),
                ("probe", "Проверять запросом (выключите — будет только список моделей, без расхода запросов)"),
                ("only_active", "Следить только за выбранным в «Облачных моделях» провайдером"),
                ("autoswitch", "Если текущая модель отвалилась — переключиться на лучшую готовую (того же провайдера)"),
                ("announce", "Сообщать голосом об изменениях"), ("voice", "Голосовые команды"))
        for i, (k, t) in enumerate(rows):
            ttk.Checkbutton(f, text=t, variable=api.var(k)).grid(row=4 + i, column=0, columnspan=4, sticky="w", **pad)
        n = 4 + len(rows)
        lf = ttk.Frame(f)
        lf.grid(row=n, column=0, columnspan=4, sticky="w", **pad)
        for txt, key, w in (("проверять раз в, мин (0 — только вручную)", "auto_min", 5), ("моделей на провайдера", "max_probe", 5),
                            ("пауза между запросами, с", "gap", 5)):
            ttk.Label(lf, text=txt).pack(side="left", padx=(0, 3))
            ttk.Entry(lf, textvariable=api.var(key), width=w).pack(side="left", padx=(0, 12))
        lf2 = ttk.Frame(f)
        lf2.grid(row=n + 1, column=0, columnspan=4, sticky="w", **pad)
        for txt, key, w in (("только эти (regex)", "include", 14), ("кроме (regex)", "exclude", 14), ("любимые (по слову)", "preferred", 18)):
            ttk.Label(lf2, text=txt).pack(side="left", padx=(0, 3))
            ttk.Entry(lf2, textvariable=api.var(key), width=w).pack(side="left", padx=(0, 12))
        ttk.Label(f, text=("Ключи и провайдеры берутся из плагина «Облачные модели». Каждая проверка — один короткий запрос на модель "
                           "(до 8 токенов): он учитывается в лимитах провайдера, поэтому между запросами идёт пауза, а при ответе "
                           "429 проверка этого провайдера останавливается. «Перегружена» (503) обычно проходит сама. Переключение "
                           "возможно только внутри выбранного провайдера и только когда агент подключён к облаку; список моделей "
                           "фильтруется от эмбеддингов, озвучки и картинок."),
                  foreground="#777", wraplength=780, justify="left").grid(row=n + 2, column=0, columnspan=4, sticky="w", **pad)
        refresh_table()
    api.add_settings(build)
