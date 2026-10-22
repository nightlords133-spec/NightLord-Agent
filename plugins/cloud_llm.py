# Плагин Nightlord агента: облачные модели вместо (или вместе с) локальной LM Studio.
# Внутри плагина работает маленький мост на 127.0.0.1: агент говорит с ним так же, как с LM Studio,
# а мост пересылает запросы в Claude (Anthropic API) или в любой OpenAI-совместимый сервис.
# Ключ API хранится в plugins/data/cloud_llm/config.json открытым текстом.
import hashlib
import json
import os
import re
import threading
import time
from collections import OrderedDict, deque
import tkinter as tk
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from tkinter import ttk, messagebox

import requests

PLUGIN = {"name": "Облачные модели", "version": "1.2",
          "description": "Подключает агента к Claude (Anthropic) и OpenAI-совместимым сервисам (OpenAI, OpenRouter, "
                         "Groq, DeepSeek, Mistral, Gemini…). Голосом: «переключись на облако», «переключись на "
                         "локальную модель». Лимиты RPM/RPD, пауза между запросами, кэш и экономия. Запросы, история "
                         "и скриншоты уходят провайдеру и тратят ваш баланс."}

ANTHROPIC_VERSION = "2023-06-01"
PRESETS = {
    "Claude (Anthropic)": {"kind": "anthropic", "url": "https://api.anthropic.com", "env": "ANTHROPIC_API_KEY",
                           "model": "claude-haiku-5-5"},
    "OpenAI": {"kind": "openai", "url": "https://api.openai.com/v1", "env": "OPENAI_API_KEY", "model": ""},
    "OpenRouter": {"kind": "openai", "url": "https://openrouter.ai/api/v1", "env": "OPENROUTER_API_KEY", "model": ""},
    "Groq": {"kind": "openai", "url": "https://api.groq.com/openai/v1", "env": "GROQ_API_KEY", "model": ""},
    "DeepSeek": {"kind": "openai", "url": "https://api.deepseek.com/v1", "env": "DEEPSEEK_API_KEY", "model": ""},
    "Mistral": {"kind": "openai", "url": "https://api.mistral.ai/v1", "env": "MISTRAL_API_KEY", "model": ""},
    "Google Gemini (OpenAI-режим)": {"kind": "openai", "url": "https://generativelanguage.googleapis.com/v1beta/openai",
                                     "env": "GEMINI_API_KEY", "model": ""},
    "Свой сервер (OpenAI-совместимый)": {"kind": "openai", "url": "", "env": "", "model": "", "nokey": True},
}
FINISH = {"end_turn": "stop", "stop_sequence": "stop", "max_tokens": "length", "tool_use": "tool_calls",
          "refusal": "content_filter", "pause_turn": "stop"}
MAX_BODY = 40 * 1024 * 1024


# ---------------------------------------------------------------- перевод OpenAI -> Anthropic
def _text_of(content):
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(str(p.get("text", "")) for p in content
                         if isinstance(p, dict) and p.get("type") in ("text", "input_text"))
    return str(content)


def _safe_id(i):
    return re.sub(r"[^a-zA-Z0-9_-]", "_", str(i or "")) or "call_0"


def _blocks(content):
    """Содержимое сообщения пользователя -> блоки Anthropic (текст и картинки)."""
    if isinstance(content, str):
        return [{"type": "text", "text": content}] if content.strip() else []
    out = []
    for p in content or []:
        if isinstance(p, str):
            if p.strip():
                out.append({"type": "text", "text": p})
            continue
        if not isinstance(p, dict):
            continue
        t = p.get("type")
        if t in ("text", "input_text"):
            if str(p.get("text", "")).strip():
                out.append({"type": "text", "text": str(p["text"])})
        elif t == "image_url":
            u = p.get("image_url")
            u = u.get("url") if isinstance(u, dict) else u
            u = str(u or "")
            m = re.match(r"data:([^;,]+);base64,(.*)$", u, re.S)
            if m:
                out.append({"type": "image", "source": {"type": "base64", "media_type": m.group(1), "data": m.group(2)}})
            elif u.startswith(("http://", "https://")):
                out.append({"type": "image", "source": {"type": "url", "url": u}})
    return out


def to_anthropic(body, model, default_max=2048, send_temp=True, cache_system=False):
    """Тело запроса OpenAI chat.completions -> тело запроса Anthropic Messages."""
    system, msgs = [], []

    def push(role, blocks):
        if not blocks:
            return
        if msgs and msgs[-1]["role"] == role:
            cur = msgs[-1]["content"]
            if role == "user" and blocks[0].get("type") == "tool_result":
                n = 0
                while n < len(cur) and cur[n].get("type") == "tool_result":
                    n += 1
                cur[n:n] = blocks
            else:
                cur.extend(blocks)
        else:
            msgs.append({"role": role, "content": list(blocks)})

    for m in body.get("messages") or []:
        role = m.get("role")
        if role in ("system", "developer"):
            t = _text_of(m.get("content")).strip()
            if t:
                system.append(t)
        elif role == "user":
            push("user", _blocks(m.get("content")))
        elif role == "assistant":
            bl = []
            t = _text_of(m.get("content"))
            if t.strip():
                bl.append({"type": "text", "text": t.rstrip()})
            for i, tc in enumerate(m.get("tool_calls") or []):
                fn = tc.get("function") or {}
                raw = fn.get("arguments")
                try:
                    args = json.loads(raw) if isinstance(raw, str) and raw.strip() else (raw if isinstance(raw, dict) else {})
                except ValueError:
                    args = {"_raw": str(raw)}
                if not isinstance(args, dict):
                    args = {"value": args}
                bl.append({"type": "tool_use", "id": _safe_id(tc.get("id") or f"call_{i}"),
                           "name": fn.get("name") or "tool", "input": args})
            push("assistant", bl)
        elif role == "tool":
            push("user", [{"type": "tool_result", "tool_use_id": _safe_id(m.get("tool_call_id")),
                           "content": _text_of(m.get("content")) or "(пусто)"}])
    if not msgs or msgs[0]["role"] != "user":
        msgs.insert(0, {"role": "user", "content": [{"type": "text", "text": "(начало диалога)"}]})
    p = {"model": model, "max_tokens": int(body.get("max_tokens") or body.get("max_completion_tokens") or default_max),
         "messages": msgs}
    if system:
        p["system"] = "\n\n".join(system)
        if cache_system:       # кэш промта у Anthropic: повторяющееся начало запроса дешевле и меньше давит на лимиты
            p["system"] = [{"type": "text", "text": p["system"], "cache_control": {"type": "ephemeral"}}]
    if send_temp and body.get("temperature") is not None:
        try:
            p["temperature"] = min(1.0, max(0.0, float(body["temperature"])))
        except (TypeError, ValueError):
            pass
    stop = body.get("stop")
    if stop:
        p["stop_sequences"] = [stop] if isinstance(stop, str) else [str(x) for x in stop][:4]
    choice = body.get("tool_choice")
    tools = []
    for t in body.get("tools") or []:
        fn = t.get("function") or {}
        if t.get("type", "function") == "function" and fn.get("name"):
            sch = fn.get("parameters")
            if not isinstance(sch, dict) or not sch:
                sch = {"type": "object", "properties": {}}
            sch.setdefault("type", "object")
            tools.append({"name": fn["name"], "description": str(fn.get("description") or "")[:1000],
                          "input_schema": sch})
    if tools and choice != "none":
        p["tools"] = tools
        if choice == "required":
            p["tool_choice"] = {"type": "any"}
        elif isinstance(choice, dict) and (choice.get("function") or {}).get("name"):
            p["tool_choice"] = {"type": "tool", "name": choice["function"]["name"]}
    return p


def _usage_in(u):
    u = u or {}
    return int(u.get("input_tokens") or 0) + int(u.get("cache_creation_input_tokens") or 0) + \
        int(u.get("cache_read_input_tokens") or 0)


def from_anthropic(resp, model):
    """Ответ Anthropic Messages -> ответ OpenAI chat.completion."""
    text, calls = "", []
    for b in resp.get("content") or []:
        if b.get("type") == "text":
            text += b.get("text", "")
        elif b.get("type") == "tool_use":
            calls.append({"id": b.get("id") or f"call_{len(calls)}", "type": "function",
                          "function": {"name": b.get("name", ""),
                                       "arguments": json.dumps(b.get("input") or {}, ensure_ascii=False)}})
    msg = {"role": "assistant", "content": text or (None if calls else "")}
    if calls:
        msg["tool_calls"] = calls
    i, o = _usage_in(resp.get("usage")), int((resp.get("usage") or {}).get("output_tokens") or 0)
    return {"id": resp.get("id") or "chatcmpl-cloud", "object": "chat.completion", "created": int(time.time()),
            "model": resp.get("model") or model,
            "choices": [{"index": 0, "message": msg, "finish_reason": FINISH.get(resp.get("stop_reason"), "stop")}],
            "usage": {"prompt_tokens": i, "completion_tokens": o, "total_tokens": i + o}}


def trim_messages(body, keep):
    """Оставляет системные сообщения и последние keep остальных; начало — всегда реплика пользователя
    (чтобы не разорвать пару «вызов инструмента — результат»). keep=0 — не обрезать."""
    msgs = body.get("messages") or []
    if keep <= 0:
        return body
    rest = [m for m in msgs if m.get("role") not in ("system", "developer")]
    if len(rest) <= keep:
        return body
    tail = rest[-keep:]
    while tail and tail[0].get("role") != "user":
        tail = tail[1:]
    if not tail:
        return body
    out = dict(body)
    out["messages"] = [m for m in msgs if m.get("role") in ("system", "developer")] + tail
    return out


class Limiter:
    """Скользящее окно «не больше rpm запросов за 60 с» плюс минимальная пауза gap между запросами."""

    def __init__(self):
        self.lock, self.stamps, self.last = threading.Lock(), deque(), 0.0

    def reserve(self, rpm, gap, max_wait, now=None):
        """Сколько секунд подождать перед запросом (0 — сразу) или None, если ждать дольше max_wait."""
        with self.lock:
            now = time.time() if now is None else now
            while self.stamps and self.stamps[0] <= now - 60:
                self.stamps.popleft()
            t = max(now, self.last)
            if gap > 0:
                t = max(t, self.last + gap)
            if rpm > 0 and len(self.stamps) >= rpm:
                t = max(t, self.stamps[-rpm] + 60)
            wait = t - now
            if wait > max_wait:
                return None
            self.stamps.append(t)
            self.last = t
            return wait


def _num(x, cast=int):
    try:
        return max(0, cast(float(str(x).strip().replace(",", ".") or 0)))
    except (TypeError, ValueError):
        return 0


class Fail(Exception):
    """Сбой провайдера до начала ответа клиенту: можно пробовать резервную модель."""
    def __init__(self, code, msg):
        super().__init__(msg)
        self.code, self.msg = code, msg


RETRY_CODES = {401, 403, 404, 408, 425, 429, 500, 502, 503, 504, 529}
CONFIG_CODES = {401, 403, 404}          # ключ/модель не подходят: долго не пробуем


class AnthError(Exception):
    pass


class AnthStream:
    """Превращает события SSE Anthropic в куски OpenAI chat.completion.chunk."""

    def __init__(self, model):
        self.model, self.id, self.created = model, "chatcmpl-cloud", int(time.time())
        self.tool = {}          # индекс блока Anthropic -> индекс вызова OpenAI
        self.got_json = set()
        self.finish = None
        self.tok_in = self.tok_out = 0

    def chunk(self, delta, finish=None):
        return {"id": self.id, "object": "chat.completion.chunk", "created": self.created, "model": self.model,
                "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}

    def feed(self, event, d):
        out = []
        if event == "message_start":
            m = d.get("message") or {}
            self.id = m.get("id") or self.id
            self.tok_in = _usage_in(m.get("usage"))
            out.append(self.chunk({"role": "assistant", "content": ""}))
        elif event == "content_block_start":
            b, idx = d.get("content_block") or {}, d.get("index", 0)
            if b.get("type") == "tool_use":
                n = len(self.tool)
                self.tool[idx] = n
                out.append(self.chunk({"tool_calls": [{"index": n, "id": b.get("id") or f"call_{n}", "type": "function",
                                                       "function": {"name": b.get("name", ""), "arguments": ""}}]}))
            elif b.get("type") == "text" and b.get("text"):
                out.append(self.chunk({"content": b["text"]}))
        elif event == "content_block_delta":
            dl, idx = d.get("delta") or {}, d.get("index", 0)
            if dl.get("type") == "text_delta" and dl.get("text"):
                out.append(self.chunk({"content": dl["text"]}))
            elif dl.get("type") == "input_json_delta" and idx in self.tool:
                if dl.get("partial_json"):
                    self.got_json.add(idx)
                    out.append(self.chunk({"tool_calls": [{"index": self.tool[idx],
                                                           "function": {"arguments": dl["partial_json"]}}]}))
        elif event == "content_block_stop":
            idx = d.get("index", 0)
            if idx in self.tool and idx not in self.got_json:
                self.got_json.add(idx)
                out.append(self.chunk({"tool_calls": [{"index": self.tool[idx], "function": {"arguments": "{}"}}]}))
        elif event == "message_delta":
            self.finish = FINISH.get((d.get("delta") or {}).get("stop_reason"), self.finish or "stop")
            self.tok_out = int((d.get("usage") or {}).get("output_tokens") or self.tok_out)
        elif event == "error":
            e = d.get("error") or {}
            raise AnthError(str(e.get("message") or e.get("type") or "ошибка потока"))
        return out

    def last(self):
        return self.chunk({}, self.finish or ("tool_calls" if self.tool else "stop"))


def sse_events(lines):
    """Итератор по строкам SSE -> (event, data-dict)."""
    ev = None
    for line in lines:
        if line is None:
            continue
        if isinstance(line, bytes):
            line = line.decode("utf-8", "replace")
        if not line:
            ev = None
            continue
        if line.startswith("event:"):
            ev = line[6:].strip()
        elif line.startswith("data:"):
            try:
                d = json.loads(line[5:].strip())
            except ValueError:
                continue
            yield ev or d.get("type"), d


def err_message(r):
    """Текст ошибки из ответа провайдера (Anthropic и OpenAI-формат)."""
    try:
        d = r.json()
    except ValueError:
        return (r.text or "")[:200] or f"HTTP {r.status_code}"
    e = d.get("error") if isinstance(d, dict) else None
    if isinstance(e, dict):
        return str(e.get("message") or e.get("type") or d)[:300]
    if isinstance(e, str):
        return e[:300]
    if isinstance(d, list) and d and isinstance(d[0], dict):      # Gemini иногда отдаёт список
        e = d[0].get("error")
        if isinstance(e, dict):
            return str(e.get("message", ""))[:300]
    return str(d)[:300]


def scrub(text, key):
    text = str(text)
    return text.replace(key, "***") if key else text


# ---------------------------------------------------------------- плагин
def register(api):
    st = {"server": None, "port": None, "err": "", "lock": threading.Lock(), "models": (0, [], ""),
          "no_temp": set(), "last_log": 0.0, "cache": OrderedDict(), "limiters": {},
          "cool": {}, "fo_last": None, "fo_text": ""}
    W = {}      # виджеты текущей перерисовки

    v_active = api.var("active", "Claude (Anthropic)")
    v_max = api.var("max_tokens", 2048)
    v_wait = api.var("max_wait", 20)
    v_ttl = api.var("cache_ttl", 60)
    v_trim = api.var("trim", 0)
    v_pcache = api.var("prompt_cache", True)
    v_limit = api.var("day_limit", 0)
    v_proxy = api.var("use_proxy", False)
    v_fo = api.var("fo_on", False)
    v_fo_favs = api.var("fo_favs", True)
    v_fo_local = api.var("fo_local", True)
    v_fo_say = api.var("fo_say", False)
    v_fo_wait = api.var("fo_wait", 40)
    v_fo_cool = api.var("fo_cool", 5)
    v_port = api.var("port", "11500")
    v_url, v_key, v_model = tk.StringVar(), tk.StringVar(), tk.StringVar()
    v_rpm, v_rpd, v_gap = tk.StringVar(), tk.StringVar(), tk.StringVar()      # лимиты провайдера
    loading = [False]

    # ----- профили (по одному набору полей на провайдера) -----
    def preset(name=None):
        return PRESETS.get(name or api.config_get("active", "Claude (Anthropic)"), PRESETS["Свой сервер (OpenAI-совместимый)"])

    def profile(name=None):
        name = name or api.config_get("active", "Claude (Anthropic)")
        pr, p = preset(name), (api.config_get("profiles", {}) or {}).get(name, {})
        key = (p.get("key") or "").strip() or (os.environ.get(pr["env"], "").strip() if pr["env"] else "")
        return {"name": name, "kind": pr["kind"], "url": (p.get("url") or pr["url"]).strip().rstrip("/"), "key": key,
                "model": (p.get("model") or pr["model"]).strip(), "nokey": bool(pr.get("nokey")),
                "rpm": _num(p.get("rpm")), "rpd": _num(p.get("rpd")), "gap": _num(p.get("gap"), float)}

    def load_fields(*a):
        loading[0] = True
        try:
            name = v_active.get()
            pr, p = preset(name), (api.config_get("profiles", {}) or {}).get(name, {})
            v_url.set(p.get("url", pr["url"]))
            v_key.set(p.get("key", ""))
            v_model.set(p.get("model", pr["model"]))
            v_rpm.set(str(p.get("rpm", "")))
            v_rpd.set(str(p.get("rpd", "")))
            v_gap.set(str(p.get("gap", "")))
        finally:
            loading[0] = False

    def save_fields(*a):
        if loading[0]:
            return
        d = dict(api.config_get("profiles", {}) or {})
        d[v_active.get()] = {"url": v_url.get().strip(), "key": v_key.get().strip(), "model": v_model.get().strip(),
                             "rpm": v_rpm.get().strip(), "rpd": v_rpd.get().strip(), "gap": v_gap.get().strip()}
        api.config_set("profiles", d)
        try:
            if st["server"] and v_model.get().strip() and is_connected():
                api.set_lm(model=v_model.get().strip())
        except Exception:
            pass
    load_fields()
    for v in (v_url, v_key, v_model, v_rpm, v_rpd, v_gap):
        v.trace_add("write", save_fields)
    v_active.trace_add("write", load_fields)

    # ----- учёт расхода -----
    usage_lock = threading.Lock()

    def usage():
        u = dict(api.config_get("usage", {}) or {})
        if u.get("day") != str(date.today()):
            u.update(day=str(date.today()), day_in=0, day_out=0, req_by={})
        for k in ("tot_in", "tot_out", "req"):
            u.setdefault(k, 0)
        u["req_by"] = dict(u.get("req_by") or {})
        return u

    def add_usage(i, o):
        with usage_lock:
            u = usage()
            u["day_in"] += i
            u["day_out"] += o
            u["tot_in"] += i
            u["tot_out"] += o
            u["req"] += 1
            api.config_set("usage", u)
        api.post(refresh_labels)

    def over_limit():
        try:
            lim = int(api.config_get("day_limit", 0) or 0)
        except (TypeError, ValueError):
            lim = 0
        if lim <= 0:
            return False
        u = usage()
        return u["day_in"] + u["day_out"] >= lim

    # ----- запросы к провайдеру -----
    def proxies():
        if not api.config_get("use_proxy", False):
            return None
        px = api.proxies("web")
        if not px:
            raise RuntimeError("включена галочка «через прокси», но плагин «VPN / прокси» не включён или не настроен")
        return px

    def upstream_headers(pf):
        if pf["kind"] == "anthropic":
            return {"x-api-key": pf["key"], "anthropic-version": ANTHROPIC_VERSION, "content-type": "application/json"}
        h = {"content-type": "application/json"}
        if pf["key"]:
            h["authorization"] = "Bearer " + pf["key"]
        return h

    def need_ready(pf):
        if not pf["url"]:
            raise RuntimeError("не указан адрес API")
        if not pf["key"] and not pf["nokey"]:
            raise RuntimeError("не указан ключ API (поле «Ключ» или переменная среды)")

    def post_retry(pf, url, payload, stream, rt=300):
        """POST с одной-двумя повторами, если модель не принимает temperature / max_tokens."""
        px = None if pf.get("local") else proxies()
        waited = False
        for _ in range(4):
            r = requests.post(url, headers=upstream_headers(pf), json=payload, stream=stream,
                              timeout=(10, rt), proxies=px)
            if r.status_code in (429, 529) and not waited and not pf.get("more"):       # провайдер просит подождать — ждём один раз
                try:
                    ra = float(r.headers.get("retry-after") or 3)
                except ValueError:
                    ra = 3.0
                if 0 < ra <= _num(api.config_get("max_wait", 20), float):
                    waited = True
                    r.close()
                    report(f"⏳ {pf['name']} просит подождать {ra:.0f} с — жду и повторяю")
                    time.sleep(ra)
                    continue
            if r.status_code == 400:
                msg = err_message(r).lower()
                if "temperature" in msg and "temperature" in payload:
                    payload.pop("temperature")
                    st["no_temp"].add(pf["model"])
                    r.close()
                    continue
                if "max_tokens" in msg and "max_completion_tokens" in msg and "max_tokens" in payload:
                    payload["max_completion_tokens"] = payload.pop("max_tokens")
                    r.close()
                    continue
            return r
        return r

    def report(msg):
        now = time.time()
        if now - st["last_log"] > 5:
            st["last_log"] = now
            api.log(msg)

    def list_models(pf):
        need_ready(pf)
        now, ck = time.time(), pf["name"] + "|" + pf["url"]
        if now - st["models"][0] < 120 and st["models"][1] and st["models"][2] == ck:
            return st["models"][1]
        url = pf["url"] + ("/v1/models?limit=1000" if pf["kind"] == "anthropic" else "/models")
        r = requests.get(url, headers=upstream_headers(pf), timeout=15, proxies=proxies())
        if r.status_code != 200:
            raise RuntimeError(f"{r.status_code}: {err_message(r)}")
        ids = [str(m.get("id") or m.get("name") or "") for m in (r.json().get("data") or r.json().get("models") or [])]
        ids = sorted(i.replace("models/", "", 1) for i in ids if i)
        st["models"] = (now, ids, ck)
        return ids

    # ----- кэш одинаковых запросов -----
    def cache_key(pf, body):
        raw = json.dumps({"p": pf["name"], "m": pf["model"], "b": body}, sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def cache_get(key):
        ttl = _num(api.config_get("cache_ttl", 60), float)
        e = st["cache"].get(key)
        if ttl <= 0 or not e or time.time() - e[0] > ttl:
            return None
        return e[1], e[2]

    def cache_put(key, kind, payload):
        if _num(api.config_get("cache_ttl", 60), float) <= 0 or not key:
            return
        st["cache"][key] = (time.time(), kind, payload)
        while len(st["cache"]) > 50:
            st["cache"].popitem(last=False)

    # ----- лимиты запросов -----
    def gate(pf):
        """Проверка RPD и ожидание по RPM / паузе. Возвращает текст ошибки или None."""
        name = pf["name"]
        with usage_lock:
            u = usage()
            used = u["req_by"].get(name, 0)
            if pf["rpd"] > 0 and used >= pf["rpd"]:
                return (f"суточный лимит запросов ({pf['rpd']}) для «{name}» исчерпан — подождите до завтра "
                        f"или переключитесь на локальную модель")
        wait = st["limiters"].setdefault(name, Limiter()).reserve(pf["rpm"], pf["gap"],
                                                                 _num(api.config_get("max_wait", 20), float))
        if wait is None:
            return (f"лимит запросов в минуту ({pf['rpm']}) для «{name}»: ждать пришлось бы дольше "
                    f"{api.config_get('max_wait', 20)} с. Повторите чуть позже")
        if wait > 0.05:
            if wait > 2:
                report(f"⏳ жду {wait:.0f} с из-за лимита запросов «{name}»")
            time.sleep(wait)
        with usage_lock:
            u = usage()
            u["req_by"][name] = u["req_by"].get(name, 0) + 1
            api.config_set("usage", u)
        if pf["rpd"] > 0 and u["req_by"][name] == int(pf["rpd"] * 0.9):
            report(f"⚠ использовано 90% суточного лимита запросов «{name}» ({u['req_by'][name]} из {pf['rpd']})")
        api.post(refresh_labels)
        return None

    # ----- резерв: избранные модели, затем LM Studio -----
    def fo_text(t):
        st["fo_text"] = t
        api.post(refresh_labels)

    def fav_pairs():
        """Избранные из «Мониторинга моделей»: сначала проверенные готовыми (быстрее — раньше), известные нерабочие пропускаем."""
        base = api.data_dir.parent / "model_watch"
        try:
            favs = json.loads((base / "config.json").read_text(encoding="utf-8")).get("favorites") or []
        except (OSError, ValueError, AttributeError):
            return []
        try:
            rows = json.loads((base / "status.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            rows = {}
        out = []
        for x in favs:
            if not (isinstance(x, str) and "\t" in x):
                continue
            prov, model = x.split("\t", 1)
            r = rows.get(x) or {}
            stt = r.get("status")
            if stt in ("gone", "denied"):
                continue
            out.append((0 if stt == "ok" else 1, r.get("ms") or 1e9, prov, model))
        out.sort()
        return [(p, m) for _, _, p, m in out]

    def local_pf():
        url = (api.config_get("local_url", "") or "http://localhost:1234").rstrip("/")
        if url.endswith("/v1"):
            url = url[:-3]
        return {"name": "LM Studio", "kind": "openai", "url": url + "/v1", "key": "", "nokey": True, "rpm": 0, "rpd": 0,
                "gap": 0.0, "model": api.config_get("local_model", "") or "", "local": True}

    def tkey(t):
        return f"{t['name']}|{t['model']}"

    def cooled(t):
        return st["cool"].get(tkey(t), 0) > time.time()

    def cool(t, code):
        mins = 30 if code in CONFIG_CODES else max(0.05, _num(api.config_get("fo_cool", 5), float))
        st["cool"][tkey(t)] = time.time() + mins * 60

    def make_chain(primary, only_local=False):
        """Порядок попыток: основная → избранные → LM Studio. Выключено — только основная."""
        if not api.config_get("fo_on", False):
            return [primary]
        chain, seen = [], set()

        def add(t):
            if tkey(t) not in seen:
                seen.add(tkey(t))
                chain.append(t)
        if not only_local:
            if not cooled(primary):
                add(primary)
            else:
                seen.add(tkey(primary))
            if api.config_get("fo_favs", True):
                for prov, model in fav_pairs():
                    if len(chain) >= 4:
                        break
                    try:
                        t = profile(prov)
                        t["model"] = model
                        need_ready(t)
                    except (RuntimeError, KeyError):
                        continue
                    if tkey(t) in seen or cooled(t):
                        continue
                    add(t)
        if api.config_get("fo_local", True):
            lp = local_pf()
            if lp["url"] and not cooled(lp):
                add(lp)
        return chain or [primary]

    def note_used(t, primary):
        """Сообщает, когда агент ушёл на резерв или вернулся на основную модель."""
        k = tkey(t)
        if st["fo_last"] == k:
            return
        was, st["fo_last"] = st["fo_last"], k
        if not api.config_get("fo_on", False):
            return
        if k == tkey(primary):
            if was is not None:
                api.log(f"основная модель снова отвечает: {t['name']} {t['model']}")
                fo_text(f"Основная модель снова отвечает ({t['name']}).")
                if api.config_get("fo_say", False):
                    api.say("Основная модель снова отвечает.")
            return
        what = "LM Studio" if t.get("local") else f"{t['name']} {t['model']}"
        api.log(f"основная модель не отвечает, отвечает резерв: {what}")
        fo_text(f"Сейчас отвечает резерв: {what} (с {time.strftime('%H:%M')}).")
        if api.config_get("fo_say", False):
            api.say("Основная модель не отвечает, перешёл на " + ("LM Studio." if t.get("local") else t["name"] + "."))

    # ----- обработчик HTTP -----
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.0"

        _more = False
        _started = False
        _over = False

        def log_message(self, *a):
            pass

        def send_response(self, code, message=None):
            self._started = True
            super().send_response(code, message)

        def _send(self, code, obj):
            b = json.dumps(obj, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)

        def _err(self, code, msg):
            self._send(code, {"error": {"message": msg, "type": "cloud_bridge_error"}})

        def _guard(self):
            """Страницы в браузере не должны тратить ваши деньги: нет Origin, только свой Host."""
            if self.headers.get("Origin") or self.headers.get("Sec-Fetch-Site") in ("cross-site", "same-site"):
                self._err(403, "запросы из браузера запрещены")
                return False
            host = (self.headers.get("Host") or "").lower()
            if host not in (f"127.0.0.1:{st['port']}", f"localhost:{st['port']}"):
                self._err(403, "неверный Host")
                return False
            return True

        def do_OPTIONS(self):
            self._err(403, "CORS выключен")

        def do_GET(self):
            if not self._guard():
                return
            path = self.path.split("?")[0].rstrip("/")
            try:
                if path in ("/v1/models", "/models"):
                    ids = list_models(profile())
                    return self._send(200, {"object": "list", "data": [{"id": i, "object": "model"} for i in ids]})
                if path in ("", "/health"):
                    return self._send(200, {"ok": True, "plugin": "cloud_llm", "provider": api.config_get("active")})
            except Exception as e:
                return self._err(502, scrub(e, profile()["key"]))
            self._err(404, "нет такого адреса")

        def do_POST(self):
            if not self._guard():
                return
            if self.path.split("?")[0].rstrip("/") not in ("/v1/chat/completions", "/chat/completions"):
                return self._err(404, "нет такого адреса")
            if not (self.headers.get("Content-Type") or "").lower().startswith("application/json"):
                return self._err(415, "нужен Content-Type: application/json")
            try:
                n = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                n = 0
            if n <= 0 or n > MAX_BODY:
                return self._err(413, "пустой или слишком большой запрос")
            try:
                body = json.loads(self.rfile.read(n).decode("utf-8"))
            except ValueError:
                return self._err(400, "не JSON")
            pf = profile()
            try:
                try:
                    need_ready(pf)
                except RuntimeError:
                    if not api.config_get("fo_on", False):
                        raise
                    cool(pf, 401)              # основная модель не настроена (нет ключа/адреса): сразу на резерв
                self._over = over_limit()
                if self._over and not (api.config_get("fo_on", False) and api.config_get("fo_local", True)):
                    return self._err(429, "суточный лимит токенов исчерпан (плагин «Облачные модели»)")
                model = (body.get("model") or "").strip()
                if not model or model == api.config_get("local_model", None):    # имя старой локальной модели
                    model = pf["model"]
                if not model:
                    return self._err(400, "не выбрана модель")
                pf["model"] = model
                body = trim_messages(body, _num(api.config_get("trim", 0)))
                self._rec, self._key = [], cache_key(pf, body)
                hit = cache_get(self._key)
                if hit:                                    # такой же запрос недавно уже был — ответ из кэша, без вызова
                    kind, payload = hit
                    if kind == "json":
                        return self._send(200, payload)
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream; charset=utf-8")
                    self.end_headers()
                    for b in payload:
                        self.wfile.write(b)
                    return self.wfile.flush()
                self._run_chain(pf, body)
            except requests.RequestException as e:
                px = api.config_get("use_proxy", False)
                why = api.explain_proxy_error(e) if px else type(e).__name__
                msg = f"нет связи с провайдером ({why})"
                report("⚠ " + msg)
                self._err(502, msg)
            except Exception as e:
                msg = scrub(e, pf["key"])
                report("⚠ " + msg)
                try:
                    self._err(502, msg)
                except OSError:
                    pass

        def _rt(self, pf):
            """Сколько ждать ответа: у основной модели при включённом резерве — не дольше заданного, у последней — как раньше."""
            return max(5, _num(api.config_get("fo_wait", 40), float)) if pf.get("more") else 300

        def _run_chain(self, primary, body):
            """Основная модель, при сбое до начала ответа — избранные и LM Studio (если включено в настройках)."""
            over = self._over
            chain = make_chain(primary, only_local=over)
            last = None
            for i, t in enumerate(chain):
                more = i < len(chain) - 1
                self._more, self._started = more, False
                if i or t is not primary:
                    self._key = None                       # ответы резерва в кэш не кладём
                t["more"] = more
                try:
                    if not t.get("local"):
                        err = gate(t)
                        if err:
                            if more:
                                raise Fail(429, err)
                            report("⚠ " + err)
                            return self._err(429, err)
                    if t["kind"] == "anthropic":
                        self._anthropic(t, body)
                    else:
                        self._openai(t, body)
                    if self._started:
                        note_used(t, primary)
                    return
                except Fail as f:
                    last = f
                    cool(t, f.code)
                    api.log(f"{t['name']} {t['model']}: {f.msg[:120]}" + (" — пробую следующую" if more else ""))
                except requests.RequestException as e:
                    if self._started or not more:
                        raise
                    last = Fail(502, "нет связи с провайдером: " + type(e).__name__)
                    cool(t, 502)
                    api.log(f"{t['name']} {t['model']}: нет связи ({type(e).__name__}) — пробую следующую")
            if last:
                report("⚠ " + last.msg)
                self._err(last.code, last.msg)

        def _upstream_fail(self, r, pf):
            msg = scrub(err_message(r), pf["key"])
            if pf.get("more") and r.status_code in RETRY_CODES:
                raise Fail(r.status_code, f"{r.status_code} {msg}")
            report(f"⚠ {pf['name']}: {r.status_code} {msg}")
            self._err(r.status_code if 400 <= r.status_code < 600 else 502, msg)

        # --- Anthropic ---
        def _anthropic(self, pf, body):
            stream = bool(body.get("stream"))
            payload = to_anthropic(body, pf["model"], int(api.config_get("max_tokens", 2048) or 2048),
                                   pf["model"] not in st["no_temp"], bool(api.config_get("prompt_cache", True)))
            if stream:
                payload["stream"] = True
            r = post_retry(pf, pf["url"] + "/v1/messages", payload, stream, self._rt(pf))
            try:
                if r.status_code != 200:
                    return self._upstream_fail(r, pf)
                if not stream:
                    out = from_anthropic(r.json(), pf["model"])
                    if not pf.get("local"):
                        add_usage(out["usage"]["prompt_tokens"], out["usage"]["completion_tokens"])
                    cache_put(self._key, "json", out)
                    return self._send(200, out)
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream; charset=utf-8")
                self.send_header("Cache-Control", "no-cache")
                self.end_headers()
                conv = AnthStream(pf["model"])

                def emit(obj):
                    b = b"data: " + (json.dumps(obj, ensure_ascii=False) if obj != "[DONE]" else "[DONE]") \
                        .encode("utf-8") + b"\n\n"
                    self._rec.append(b)
                    self.wfile.write(b)
                    self.wfile.flush()
                clean = True
                try:
                    for ev, d in sse_events(r.iter_lines()):
                        for c in conv.feed(ev, d):
                            emit(c)
                    emit(conv.last())
                except (AnthError, requests.RequestException) as e:
                    report("⚠ " + scrub(e, pf["key"]))
                    emit(conv.chunk({"content": f" [ошибка облачной модели: {scrub(e, pf['key'])}]"}))
                    emit(conv.chunk({}, "stop"))
                    clean = False
                except (BrokenPipeError, ConnectionError):
                    return          # агент оборвал запрос (новая реплика) — это нормально
                emit("[DONE]")
                if not pf.get("local"):
                    add_usage(conv.tok_in, conv.tok_out)
                if clean:
                    cache_put(self._key, "sse", list(self._rec))
            finally:
                r.close()

        # --- OpenAI-совместимые: пересылка как есть ---
        def _openai(self, pf, body):
            body = dict(body)
            if pf["model"]:
                body["model"] = pf["model"]
            else:
                body.pop("model", None)           # локальный сервер сам выберет загруженную модель
            if pf["model"] in st["no_temp"]:
                body.pop("temperature", None)
            stream = bool(body.get("stream"))
            if stream:
                body["stream_options"] = {"include_usage": True}
            r = post_retry(pf, pf["url"] + "/chat/completions", body, stream, self._rt(pf))
            try:
                if r.status_code != 200 and stream and "stream_options" in body and r.status_code == 400:
                    r.close()
                    body.pop("stream_options", None)
                    r = post_retry(pf, pf["url"] + "/chat/completions", body, stream, self._rt(pf))
                if r.status_code != 200:
                    return self._upstream_fail(r, pf)
                if not stream:
                    d = r.json()
                    u = d.get("usage") or {}
                    if not pf.get("local"):
                        add_usage(int(u.get("prompt_tokens") or 0), int(u.get("completion_tokens") or 0))
                    cache_put(self._key, "json", d)
                    return self._send(200, d)
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream; charset=utf-8")
                self.send_header("Cache-Control", "no-cache")
                self.end_headers()
                ti = to = 0
                try:
                    for line in r.iter_lines():
                        if not line:
                            continue
                        if line.startswith(b"data:") and b'"usage"' in line:
                            try:
                                u = json.loads(line[5:].strip()).get("usage") or {}
                                ti, to = int(u.get("prompt_tokens") or ti), int(u.get("completion_tokens") or to)
                            except (ValueError, AttributeError):
                                pass
                        self._rec.append(line + b"\n\n")
                        self.wfile.write(line + b"\n\n")
                        self.wfile.flush()
                except (BrokenPipeError, ConnectionError):
                    return
                if not pf.get("local"):
                    add_usage(ti, to)
                if any(b.strip() == b"data: [DONE]" for b in self._rec[-2:]):
                    cache_put(self._key, "sse", list(self._rec))
            finally:
                r.close()

    # ----- запуск и остановка моста -----
    def start():
        stop()
        try:
            port = int(api.config_get("port", "11500"))
            srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        except (OSError, ValueError) as e:
            st["err"] = f"мост не запустился: {e}"
            return False
        srv.daemon_threads = True
        st["server"], st["port"], st["err"] = srv, srv.server_port, ""
        threading.Thread(target=srv.serve_forever, daemon=True, name="cloud_llm_bridge").start()
        return True

    def stop():
        srv, st["server"] = st["server"], None
        if srv:
            try:
                srv.shutdown()
                srv.server_close()
            except OSError:
                pass

    def bridge_url():
        return f"http://127.0.0.1:{st['port']}" if st["server"] else None

    cur = {"url": ""}       # куда подключён агент (обновляется не из потока интерфейса, чтобы не зависать)

    def poll():
        try:
            cur["url"] = api.get_lm()[0]
        except Exception:
            pass

    def is_connected():
        return bool(st["server"]) and cur["url"] == bridge_url()

    # ----- подключение агента -----
    def connect(from_voice=False):
        if not st["server"]:
            return False, st["err"] or "мост не запущен"
        pf = profile()
        try:
            need_ready(pf)
        except RuntimeError as e:
            return False, str(e)
        if not pf["model"]:
            return False, "не выбрана модель"
        url, model = api.get_lm()
        if url != bridge_url():
            api.config_set("local_url", url)
            api.config_set("local_model", model)
        api.set_lm(bridge_url(), pf["model"])
        cur["url"] = bridge_url()
        api.config_set("ack", True)
        return True, f"Агент подключён: {pf['name']}, модель {pf['model']}."

    def disconnect():
        url = api.config_get("local_url", "") or "http://localhost:1234"
        api.set_lm(url, api.config_get("local_model", ""))
        cur["url"] = url
        return "Агент снова на локальной модели."

    def on_unload():
        poll()
        was = is_connected()
        stop()
        if was:
            api.config_set("reconnect_until", time.time() + 20)
            try:
                disconnect()
            except Exception:
                pass
    api.on_unload(on_unload)

    poll()
    started = start()
    if started and time.time() < float(api.config_get("reconnect_until", 0) or 0):
        api.config_set("reconnect_until", 0)
        connect()

    # ----- голосовые команды -----
    def command(text, m):
        t = api.norm_ru(text)
        if re.match(r"^(?:переключись|перейди|подключись|используй|включи)(?: на)? (?:облак\w*|клод\w*|claude)(?: модел\w*)?$", t):
            if not api.config_get("ack", False):
                return "Сначала один раз подключите облако вручную на вкладке «Плагины»."
            ok, msg = connect(True)
            return msg if ok else "Не получилось: " + msg
        if re.match(r"^(?:переключись|перейди|подключись|используй|верни)(?: на| обратно на)? (?:локальн\w*|лм студи\w*|lm studio)(?: модел\w*)?$", t):
            return disconnect()
        return None
    api.add_command(r"(?:облак|клод|claude|локальн|лм студи|lm studio)", command,
                    "«переключись на облако», «переключись на локальную модель»")

    # ----- интерфейс -----
    def refresh_labels():
        try:
            if "bridge" in W and W["bridge"].winfo_exists():
                W["bridge"].config(text=(f"Мост работает: http://127.0.0.1:{st['port']}" if st["server"]
                                         else st["err"] or "Мост не запущен"))
                W["conn"].config(text=("Агент сейчас: ☁ облако (" + profile()["name"] + ")") if is_connected()
                                 else "Агент сейчас: локальная модель (или другой адрес)")
                u = usage()
                pf = profile()
                if "fo" in W:
                    W["fo"].config(text=st["fo_text"] or "Резерв: пока не использовался.")
                W["use"].config(text=f"Токенов сегодня: {u['day_in']} вход / {u['day_out']} выход.  Всего: "
                                     f"{u['tot_in']} / {u['tot_out']}, запросов: {u['req']}.  Запросов сегодня "
                                     f"({pf['name']}): {u['req_by'].get(pf['name'], 0)}" + (f" из {pf['rpd']}" if pf["rpd"] else ""))
        except tk.TclError:
            pass

    def setres(text):
        def do():
            try:
                W["res"].config(text=text)
            except (tk.TclError, KeyError):
                pass
        api.post(do)

    def build(f):
        pad = {"padx": 6, "pady": 2}
        ttk.Label(f, text="Провайдер:").grid(row=0, column=0, sticky="w", **pad)
        ttk.Combobox(f, textvariable=v_active, values=list(PRESETS), width=34, state="readonly").grid(
            row=0, column=1, columnspan=3, sticky="w", **pad)
        ttk.Label(f, text="Адрес API:").grid(row=1, column=0, sticky="w", **pad)
        ttk.Entry(f, textvariable=v_url, width=52).grid(row=1, column=1, columnspan=3, sticky="w", **pad)
        ttk.Label(f, text="Ключ:").grid(row=2, column=0, sticky="w", **pad)
        ent = ttk.Entry(f, textvariable=v_key, width=40, show="•")
        ent.grid(row=2, column=1, sticky="w", **pad)
        show = tk.BooleanVar(value=False)
        ttk.Checkbutton(f, text="показать", variable=show,
                        command=lambda: ent.config(show="" if show.get() else "•")).grid(row=2, column=2, sticky="w", **pad)
        ttk.Label(f, text="Модель:").grid(row=3, column=0, sticky="w", **pad)
        combo = ttk.Combobox(f, textvariable=v_model, width=40)
        combo.grid(row=3, column=1, sticky="w", **pad)

        def models_ui():
            def work():
                try:
                    ids = list_models(dict(profile(), model=""))
                    api.post(lambda: combo.config(values=ids))
                    setres(f"Моделей у провайдера: {len(ids)}. Выберите из списка.")
                except Exception as e:
                    setres("Список не получен: " + scrub(e, profile()["key"]))
            st["models"] = (0, [], "")
            threading.Thread(target=work, daemon=True).start()
        ttk.Button(f, text="Список моделей", command=models_ui).grid(row=3, column=2, sticky="w", **pad)
        ttk.Label(f, text="Макс. токенов ответа:").grid(row=4, column=0, sticky="w", **pad)
        ttk.Entry(f, textvariable=v_max, width=8).grid(row=4, column=1, sticky="w", **pad)
        ttk.Label(f, text="Лимит токенов в сутки (0 — без лимита):").grid(row=5, column=0, columnspan=2, sticky="w", **pad)
        ttk.Entry(f, textvariable=v_limit, width=10).grid(row=5, column=2, sticky="w", **pad)
        ttk.Checkbutton(f, text="Запросы идут через прокси (плагин «VPN / прокси», вид «Поиск и страницы»)",
                        variable=v_proxy).grid(row=6, column=0, columnspan=4, sticky="w", **pad)
        ttk.Label(f, text="Порт моста:").grid(row=7, column=0, sticky="w", **pad)
        ttk.Entry(f, textvariable=v_port, width=8).grid(row=7, column=1, sticky="w", **pad)

        def restart():
            start()
            refresh_labels()
        ttk.Button(f, text="Перезапустить мост", command=restart).grid(row=7, column=2, sticky="w", **pad)

        def test_ui():
            W["res"].config(text="Проверяю…")
            pf = profile()

            def work():
                try:
                    need_ready(pf)
                    if not pf["model"]:
                        raise RuntimeError("не выбрана модель")
                    body = {"messages": [{"role": "user", "content": "Ответь одним словом: ок"}], "max_tokens": 20}
                    if pf["kind"] == "anthropic":
                        r = post_retry(pf, pf["url"] + "/v1/messages", to_anthropic(body, pf["model"], 20, False), False)
                    else:
                        r = post_retry(pf, pf["url"] + "/chat/completions", dict(body, model=pf["model"]), False)
                    if r.status_code != 200:
                        raise RuntimeError(f"{r.status_code}: {err_message(r)}")
                    d = from_anthropic(r.json(), pf["model"]) if pf["kind"] == "anthropic" else r.json()
                    txt = (d["choices"][0]["message"].get("content") or "").strip()
                    msg = f"Работает. {pf['name']} / {pf['model']} ответила: «{txt[:60]}»"
                except requests.RequestException as e:
                    msg = "Нет связи: " + (api.explain_proxy_error(e) if api.config_get("use_proxy", False) else type(e).__name__)
                except Exception as e:
                    msg = "Не работает: " + scrub(e, pf["key"])
                setres(msg)
            threading.Thread(target=work, daemon=True).start()
        ttk.Button(f, text="Проверить ключ и модель", command=test_ui).grid(row=8, column=0, sticky="w", **pad)

        def connect_ui():
            pf = profile()
            if not api.config_get("ack", False):
                if not messagebox.askokcancel(
                        "Облачная модель",
                        f"Агент будет отправлять в «{pf['name']}»: ваши реплики, историю диалога, долгую память, "
                        f"а при включённом зрении — скриншоты экрана и картинки.\n\nЗапросы платные (расход виден ниже, "
                        f"но цену считает провайдер), действуют правила и региональные ограничения провайдера.\n\n"
                        f"Подключить?"):
                    return
            ok, msg = connect()
            W["res"].config(text=msg)
            refresh_labels()
        ttk.Button(f, text="Подключить агента к облаку", command=connect_ui).grid(row=8, column=1, sticky="w", **pad)

        def back_ui():
            W["res"].config(text=disconnect())
            refresh_labels()
        ttk.Button(f, text="Вернуть локальную модель", command=back_ui).grid(row=8, column=2, sticky="w", **pad)

        def reset_use():
            api.config_set("usage", {})
            refresh_labels()
        ttk.Button(f, text="Сбросить счётчик", command=reset_use).grid(row=8, column=3, sticky="w", **pad)
        W["bridge"] = ttk.Label(f, text="")
        W["bridge"].grid(row=9, column=0, columnspan=4, sticky="w", **pad)
        W["conn"] = ttk.Label(f, text="")
        W["conn"].grid(row=10, column=0, columnspan=4, sticky="w", **pad)
        W["use"] = ttk.Label(f, text="")
        W["use"].grid(row=11, column=0, columnspan=4, sticky="w", **pad)
        W["res"] = ttk.Label(f, text="", wraplength=780, justify="left")
        W["res"].grid(row=12, column=0, columnspan=4, sticky="w", **pad)
        ttk.Label(f, text="Лимиты этого провайдера (0 — без лимита):").grid(row=14, column=0, columnspan=4, sticky="w", **pad)
        lf = ttk.Frame(f)
        lf.grid(row=15, column=0, columnspan=4, sticky="w", **pad)
        for txt, var, w in (("запросов в минуту (RPM)", v_rpm, 5), ("в сутки (RPD)", v_rpd, 7), ("пауза между запросами, с", v_gap, 5)):
            ttk.Label(lf, text=txt).pack(side="left", padx=(0, 3))
            ttk.Entry(lf, textvariable=var, width=w).pack(side="left", padx=(0, 12))
        lf2 = ttk.Frame(f)
        lf2.grid(row=16, column=0, columnspan=4, sticky="w", **pad)
        for txt, var, w in (("ждать при лимите не дольше, с", v_wait, 5), ("кэш одинаковых запросов, с (0 — выкл)", v_ttl, 5),
                            ("помнить последних сообщений (0 — все)", v_trim, 5)):
            ttk.Label(lf2, text=txt).pack(side="left", padx=(0, 3))
            ttk.Entry(lf2, textvariable=var, width=w).pack(side="left", padx=(0, 12))
        ttk.Checkbutton(f, text="Кэш системного промта у Claude (дешевле и мягче к лимитам)", variable=v_pcache).grid(
            row=17, column=0, columnspan=4, sticky="w", **pad)
        ttk.Label(f, text=("Как не упереться в лимиты: впишите свои RPM/RPD из кабинета провайдера (у бесплатных тарифов "
                           "обычно 10–30 запросов в минуту и ограниченное число в сутки; у платных они зависят от уровня). Мост сам "
                           "выдерживает паузы между запросами и, если нужно, ждёт; если ждать пришлось бы дольше заданного — "
                           "вернёт ошибку, а не зависнет. Когда провайдер отвечает «подождите» (429), мост ждёт указанное им время "
                           "и повторяет один раз. Кэш отдаёт ответ на точно такой же запрос без вызова. «Помнить последних сообщений» "
                           "обрезает историю в запросе и экономит токены. Чтобы вообще не тратить облако на пустяки, держите "
                           "локальные голосовые команды включёнными."),
                  foreground="#777", wraplength=780, justify="left").grid(row=18, column=0, columnspan=4, sticky="w", **pad)
        ttk.Label(f, text=("Как это работает: плагин запускает на вашем компьютере мост (127.0.0.1) и подставляет его адрес "
                           "вместо LM Studio — всё остальное (инструменты, зрение, память, голос) работает как раньше. "
                           "Ключ можно не вводить, а задать переменной среды (ANTHROPIC_API_KEY, OPENAI_API_KEY…). "
                           "Ключ хранится в plugins/data/cloud_llm/config.json открытым текстом; не отправляйте этот файл "
                           "и не выкладывайте резервные копии настроек. Расход токенов тут — оценка по данным провайдера; "
                           "стоимость и квоты смотрите в его кабинете. Список моделей берётся у провайдера кнопкой выше."),
                  foreground="#777", wraplength=780, justify="left").grid(row=19, column=0, columnspan=4, sticky="w", **pad)
        fo = ttk.LabelFrame(f, text="Резерв, если облачная модель не отвечает")
        fo.grid(row=20, column=0, columnspan=4, sticky="we", padx=6, pady=6)
        ttk.Checkbutton(fo, text="Автоматически переключаться при сбое (ошибка, таймаут, лимит запросов, нет связи)",
                        variable=v_fo).pack(anchor="w", padx=6, pady=1)
        ttk.Checkbutton(fo, text="…сначала пробовать избранные модели (звёздочка в «Мониторинге моделей»)",
                        variable=v_fo_favs).pack(anchor="w", padx=26, pady=1)
        ttk.Checkbutton(fo, text="…затем отвечать локальной моделью LM Studio", variable=v_fo_local).pack(
            anchor="w", padx=26, pady=1)
        ttk.Checkbutton(fo, text="…и говорить вслух, когда переключился на резерв или вернулся", variable=v_fo_say).pack(
            anchor="w", padx=26, pady=1)
        fr = ttk.Frame(fo)
        fr.pack(anchor="w", padx=26, pady=2)
        ttk.Label(fr, text="Ждать ответа основной модели, с:").pack(side="left")
        ttk.Entry(fr, textvariable=v_fo_wait, width=5).pack(side="left", padx=(3, 12))
        ttk.Label(fr, text="Не трогать упавшую модель, мин:").pack(side="left")
        ttk.Entry(fr, textvariable=v_fo_cool, width=5).pack(side="left", padx=3)
        W["fo"] = ttk.Label(fo, text="", foreground="#777", wraplength=740, justify="left")
        W["fo"].pack(anchor="w", padx=6, pady=2)
        ttk.Label(fo, text=("Порядок: основная модель → избранные (сначала те, что по последней проверке готовы) → LM Studio. "
                            "Переключение идёт сразу на самом запросе, без ожидания проверки по таймеру. Упавшую модель "
                            "несколько минут не пробуем, поэтому следующие реплики идут без задержки; потом снова пробуем "
                            "основную. Ответы резерва не кэшируются и не считаются в расход облака. Если у основной модели "
                            "исчерпан суточный лимит токенов, ответ даёт только LM Studio (избранные — тоже платные, их не трогаем). "
                            "Если ответ уже пошёл и оборвался на середине, повторить его на другой модели нельзя."),
                  foreground="#777", wraplength=740, justify="left").pack(anchor="w", padx=6, pady=2)
        refresh_labels()
    api.add_settings(build)
    api.add_timer(15, lambda: (poll(), api.post(refresh_labels)))
