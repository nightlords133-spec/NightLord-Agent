# Плагин Nightlord агента: MCP (Model Context Protocol) в обе стороны.
#  1) СЕРВЕР: раздаёт возможности агента наружу по адресам http://хост:порт/mcp (Streamable HTTP) и /sse (старый SSE).
#     Так Claude Desktop, Cursor, ComfyUI-обвязки, другие агенты и скрипты могут спросить агента или вызвать его инструменты.
#  2) КЛИЕНТ: подключает внешние MCP-серверы (по HTTP/SSE или запуском программы через stdio: npx, uvx, python…)
#     и отдаёт их инструменты модели агента.
import hashlib
import json
import os
import queue
import re
import secrets
import shlex
import shutil
import socket
import subprocess
import threading
import time
import tkinter as tk
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from tkinter import ttk, messagebox
from urllib.parse import urlparse, urljoin, parse_qs

import requests

PLUGIN = {"name": "MCP", "version": "1.0",
          "description": "MCP в обе стороны: раздаёт агента наружу по адресам /mcp и /sse (Claude Desktop, Cursor, скрипты) "
                         "и подключает внешние MCP-серверы (HTTP, SSE или программа через stdio) как инструменты агента. "
                         "Голосом: «статус mcp»."}

PROTOCOLS = ["2025-06-18", "2025-03-26", "2024-11-05"]
LATEST = "2025-03-26"
SAFE_DEFAULT = ["get_datetime", "get_system_status"]
TRANSPORTS = ["auto", "http", "sse", "stdio"]


# ------------------------------------------------------------------ общее
class McpError(Exception):
    pass


def tool_text(res):
    """Результат tools/call -> обычный текст для модели."""
    if not isinstance(res, dict):
        return str(res)
    parts = []
    for c in res.get("content") or []:
        if not isinstance(c, dict):
            parts.append(str(c))
        elif c.get("type") == "text":
            parts.append(str(c.get("text", "")))
        elif c.get("type") in ("image", "audio"):
            parts.append(f"[{c.get('type')} {c.get('mimeType', '')}, {len(str(c.get('data', '')))} симв. base64]")
        elif c.get("type") == "resource":
            r = c.get("resource") or {}
            parts.append(str(r.get("text") or r.get("uri") or "[ресурс]"))
        elif c.get("type") == "resource_link":
            parts.append(str(c.get("uri", "")))
        else:
            parts.append(json.dumps(c, ensure_ascii=False))
    if not parts and res.get("structuredContent") is not None:
        parts.append(json.dumps(res["structuredContent"], ensure_ascii=False))
    out = "\n".join(p for p in parts if p).strip() or "(пусто)"
    return ("Ошибка сервера: " + out) if res.get("isError") else out


def iter_sse(lines):
    """Разбор потока Server-Sent Events: отдаёт пары (event, data)."""
    ev, data = "message", []
    for raw in lines:
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", "replace")
        line = raw.rstrip("\r\n")
        if line == "":
            if data:
                yield ev, "\n".join(data)
            ev, data = "message", []
        elif line.startswith(":"):
            continue
        elif line.startswith("event:"):
            ev = line[6:].strip() or "message"
        elif line.startswith("data:"):
            data.append(line[5:].lstrip(" "))
    if data:
        yield ev, "\n".join(data)


def coerce_args(args, props):
    """Модель часто присылает числа/логические значения/объекты строками — приводим к типам схемы."""
    out = {}
    for k, v in (args or {}).items():
        t = (props.get(k) or {}).get("type") if isinstance(props.get(k), dict) else None
        try:
            if t == "integer" and isinstance(v, (str, float)) and str(v).strip() != "":
                v = int(float(v))
            elif t == "number" and isinstance(v, str) and v.strip() != "":
                v = float(v)
            elif t == "boolean" and isinstance(v, str):
                v = v.strip().lower() in ("1", "true", "да", "yes", "on")
            elif t in ("object", "array") and isinstance(v, str) and v.strip()[:1] in "[{":
                v = json.loads(v)
        except (ValueError, TypeError):
            pass
        out[k] = v
    return out


def parse_lines(text):
    """'Ключ: значение' или 'КЛЮЧ=значение' построчно -> словарь."""
    out = {}
    for ln in str(text or "").splitlines():
        ln = ln.strip()
        if not ln or ln.startswith("#"):
            continue
        m = re.match(r"^([^:=\s]+)\s*[:=]\s*(.*)$", ln)
        if m:
            out[m.group(1)] = m.group(2).strip()
    return out


def safe_name(server, tool):
    base = "mcp_" + re.sub(r"[^a-z0-9_]+", "_", f"{server}_{tool}".lower()).strip("_")
    if len(base) > 41 or not re.match(r"^[a-z][a-z0-9_]{1,40}$", base):
        h = hashlib.md5(f"{server}/{tool}".encode()).hexdigest()[:6]
        base = (base[:34].rstrip("_") + "_" + h)[:41]
    return base


# ------------------------------------------------------------------ клиент: транспорты
class Conn:
    """Общая часть клиента: запросы с ожиданием ответа по id. Конкретный транспорт реализует _send/close."""

    def __init__(self, spec):
        self.spec = spec
        self.pending = {}
        self.lock = threading.Lock()
        self._id = 0
        self.closed = False
        self.info = {}

    def _next(self):
        with self.lock:
            self._id += 1
            return self._id

    def _send(self, msg, timeout):
        raise NotImplementedError

    def request(self, method, params=None, timeout=30):
        if self.closed:
            raise McpError("соединение закрыто")
        rid = self._next()
        slot = [threading.Event(), None]
        self.pending[rid] = slot
        try:
            self._send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params or {}}, timeout)
            if not slot[0].wait(timeout):
                raise McpError(f"нет ответа на {method} за {timeout} с")
        finally:
            self.pending.pop(rid, None)
        msg = slot[1] or {}
        if "error" in msg:
            e = msg["error"] or {}
            raise McpError(str(e.get("message") or e))
        return msg.get("result") or {}

    def notify(self, method, params=None):
        try:
            self._send({"jsonrpc": "2.0", "method": method, "params": params or {}}, 15)
        except Exception:
            pass

    def deliver(self, msg):
        if isinstance(msg, list):
            for m in msg:
                self.deliver(m)
            return
        if not isinstance(msg, dict):
            return
        if "id" in msg and ("result" in msg or "error" in msg) and "method" not in msg:
            slot = self.pending.get(msg["id"])
            if slot:
                slot[1] = msg
                slot[0].set()
        elif "method" in msg and "id" in msg:      # запрос от сервера к нам: отвечаем «пусто» или «нет такого метода»
            try:
                if msg["method"] == "ping":
                    self._send({"jsonrpc": "2.0", "id": msg["id"], "result": {}}, 10)
                else:
                    self._send({"jsonrpc": "2.0", "id": msg["id"],
                                "error": {"code": -32601, "message": "не поддерживается"}}, 10)
            except Exception:
                pass

    def handshake(self):
        res = self.request("initialize", {"protocolVersion": LATEST, "capabilities": {},
                                          "clientInfo": {"name": "nightlord-agent", "version": "1.0"}}, 45)
        self.info = res.get("serverInfo") or {}
        self.notify("notifications/initialized")
        return res

    def list_tools(self):
        out, cursor = [], None
        for _ in range(20):
            res = self.request("tools/list", {"cursor": cursor} if cursor else {}, 45)
            out += [t for t in res.get("tools") or [] if isinstance(t, dict) and t.get("name")]
            cursor = res.get("nextCursor")
            if not cursor:
                break
        return out

    def call(self, name, args, timeout=180):
        return tool_text(self.request("tools/call", {"name": name, "arguments": args or {}}, timeout))

    def alive(self):
        return not self.closed

    def close(self):
        self.closed = True
        for slot in list(self.pending.values()):
            slot[0].set()


class StdioConn(Conn):
    def __init__(self, spec):
        super().__init__(spec)
        cmd = str(spec.get("command", "")).strip()
        if not cmd:
            raise McpError("не указана команда запуска")
        parts = shlex.split(cmd, posix=(os.name != "nt"))
        parts = [p[1:-1] if len(p) > 1 and p[0] == p[-1] and p[0] in "\"'" else p for p in parts]
        exe = shutil.which(parts[0]) or parts[0]
        env = dict(os.environ)
        env.update(parse_lines(spec.get("env", "")))
        kw = {}
        if os.name == "nt":
            kw["creationflags"] = 0x08000000      # без чёрного окна консоли
        cwd = str(spec.get("cwd", "")).strip() or None
        try:
            self.p = subprocess.Popen([exe] + parts[1:], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                      stderr=subprocess.PIPE, env=env, cwd=cwd, **kw)
        except OSError as e:
            raise McpError(f"не удалось запустить «{parts[0]}»: {e}")
        self.err = deque(maxlen=20)
        self.wlock = threading.Lock()
        threading.Thread(target=self._read, daemon=True).start()
        threading.Thread(target=self._read_err, daemon=True).start()

    def _read(self):
        try:
            for raw in self.p.stdout:
                line = raw.decode("utf-8", "replace").strip()
                if not line.startswith(("{", "[")):
                    continue
                try:
                    self.deliver(json.loads(line))
                except ValueError:
                    pass
        except Exception:
            pass
        self.close()

    def _read_err(self):
        try:
            for raw in self.p.stderr:
                self.err.append(raw.decode("utf-8", "replace").rstrip())
        except Exception:
            pass

    def _send(self, msg, timeout):
        data = (json.dumps(msg, ensure_ascii=False) + "\n").encode("utf-8")
        try:
            with self.wlock:
                self.p.stdin.write(data)
                self.p.stdin.flush()
        except (OSError, ValueError):
            raise McpError("программа не отвечает" + ((": " + " | ".join(list(self.err)[-3:])) if self.err else ""))

    def alive(self):
        return not self.closed and self.p.poll() is None

    def close(self):
        super().close()
        try:
            if self.p.poll() is None:
                self.p.terminate()
                try:
                    self.p.wait(3)
                except subprocess.TimeoutExpired:
                    self.p.kill()
        except Exception:
            pass


def http_headers(spec, extra=None):
    h = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
    tok = str(spec.get("token", "")).strip()
    if tok:
        h["Authorization"] = tok if tok.lower().startswith(("bearer ", "basic ")) else "Bearer " + tok
    h.update(parse_lines(spec.get("headers", "")))
    h.update(extra or {})
    return h


class HttpConn(Conn):
    """Streamable HTTP: каждое сообщение — POST; ответ приходит JSON или потоком SSE."""

    def __init__(self, spec):
        super().__init__(spec)
        self.url = str(spec.get("url", "")).strip()
        if not self.url.lower().startswith(("http://", "https://")):
            raise McpError("адрес должен начинаться с http:// или https://")
        self.s = requests.Session()
        self.s.trust_env = False                  # MCP-серверы обычно локальные: системный прокси не используем
        self.sid = None

    def _send(self, msg, timeout):
        h = http_headers(self.spec, {"Mcp-Session-Id": self.sid} if self.sid else None)
        try:
            r = self.s.post(self.url, data=json.dumps(msg, ensure_ascii=False).encode("utf-8"), headers=h,
                            timeout=(10, max(timeout, 10)), stream=True)
        except requests.RequestException as e:
            raise McpError(f"нет связи с {self.url}: {e.__class__.__name__}")
        try:
            if r.headers.get("Mcp-Session-Id"):
                self.sid = r.headers["Mcp-Session-Id"]
            if r.status_code in (401, 403):
                raise McpError(f"доступ запрещён (HTTP {r.status_code}): проверьте токен")
            if r.status_code >= 400:
                raise McpError(f"HTTP {r.status_code}")
            if r.status_code == 202 or "id" not in msg:
                return
            ct = r.headers.get("Content-Type", "")
            if "text/event-stream" in ct:
                for ev, data in iter_sse(r.iter_lines(chunk_size=1)):
                    try:
                        self.deliver(json.loads(data))
                    except ValueError:
                        continue
                    slot = self.pending.get(msg["id"])
                    if slot and slot[1] is not None:
                        break
            else:
                self.deliver(r.json())
        finally:
            r.close()

    def close(self):
        super().close()
        try:
            if self.sid:
                self.s.delete(self.url, headers=http_headers(self.spec, {"Mcp-Session-Id": self.sid}), timeout=3)
        except Exception:
            pass


class SseConn(Conn):
    """Старый транспорт HTTP+SSE: GET /sse держит поток, куда сервер присылает ответы; запросы шлём POST на адрес из события endpoint."""

    def __init__(self, spec):
        super().__init__(spec)
        self.url = str(spec.get("url", "")).strip()
        if not self.url.lower().startswith(("http://", "https://")):
            raise McpError("адрес должен начинаться с http:// или https://")
        self.s = requests.Session()
        self.s.trust_env = False
        self.post_url = None
        self.ready = threading.Event()
        self.err = ""
        self.resp = None
        threading.Thread(target=self._stream, daemon=True).start()
        if not self.ready.wait(15) or not self.post_url:
            self.close()
            raise McpError(self.err or "сервер не прислал адрес для запросов (событие endpoint)")

    def _stream(self):
        try:
            h = http_headers(self.spec)
            h["Accept"] = "text/event-stream"
            h.pop("Content-Type", None)
            r = self.s.get(self.url, headers=h, stream=True, timeout=(10, 3600))
            self.resp = r
            if r.status_code >= 400:
                self.err = f"HTTP {r.status_code}" + (": проверьте токен" if r.status_code in (401, 403) else "")
                return
            for ev, data in iter_sse(r.iter_lines(chunk_size=1)):
                if self.closed:
                    break
                if ev == "endpoint":
                    self.post_url = urljoin(self.url, data.strip())
                    self.ready.set()
                else:
                    try:
                        self.deliver(json.loads(data))
                    except ValueError:
                        pass
        except requests.RequestException as e:
            self.err = f"нет связи с {self.url}: {e.__class__.__name__}"
        except Exception as e:
            self.err = str(e)
        finally:
            self.ready.set()
            self.close()

    def _send(self, msg, timeout):
        if not self.post_url:
            raise McpError("нет адреса для запросов")
        try:
            r = self.s.post(self.post_url, data=json.dumps(msg, ensure_ascii=False).encode("utf-8"),
                            headers=http_headers(self.spec), timeout=(10, 30))
        except requests.RequestException as e:
            raise McpError(f"нет связи: {e.__class__.__name__}")
        if r.status_code >= 400:
            raise McpError(f"HTTP {r.status_code}")

    def close(self):
        super().close()
        try:
            if self.resp is not None:
                self.resp.close()
        except Exception:
            pass


def open_conn(spec):
    """Создаёт соединение и делает рукопожатие. Для режима auto: адрес на /sse -> SSE, иначе Streamable HTTP, а при 404/405 — SSE."""
    tr = spec.get("transport", "auto")
    if tr == "stdio":
        c = StdioConn(spec)
        c.handshake()
        return c
    url = str(spec.get("url", ""))
    order = {"http": ["http"], "sse": ["sse"]}.get(tr)
    if order is None:
        order = ["sse", "http"] if urlparse(url).path.rstrip("/").endswith("/sse") else ["http", "sse"]
    last = None
    for kind in order:
        c = None
        try:
            c = HttpConn(spec) if kind == "http" else SseConn(spec)
            c.handshake()
            return c
        except McpError as e:
            last = e
            if c is not None:
                c.close()
            if "доступ запрещён" in str(e):
                break
    raise last or McpError("не удалось подключиться")


# ------------------------------------------------------------------ сервер
def local_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))          # пакеты не отправляются, нужен только выбор интерфейса
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


def parse_listen(text):
    """'127.0.0.1:11600, 0.0.0.0:11601' -> [(host, port)]; одно число — порт на 127.0.0.1."""
    out = []
    for part in re.split(r"[,;\s]+", str(text or "").strip()):
        if not part:
            continue
        host, _, port = part.rpartition(":")
        host = host.strip("[]") or "127.0.0.1"
        try:
            port = int(port)
        except ValueError:
            raise ValueError(f"не понял адрес «{part}»: нужен вид хост:порт")
        if not 1 <= port <= 65535:
            raise ValueError(f"порт {port} вне диапазона")
        if (host, port) not in out:
            out.append((host, port))
    return out


def is_loopback(host):
    return host in ("127.0.0.1", "localhost", "::1")


class Core:
    """Протокольная часть сервера: JSON-RPC запросы -> ответы. Не зависит от HTTP."""

    def __init__(self, api):
        self.api = api
        self.ask_lock = threading.Lock()

    def cfg(self, k, d=None):
        return self.api.config_get(k, d)

    def exposed(self):
        v = self.cfg("srv_tools", None)
        return list(SAFE_DEFAULT) if v is None else list(v)

    def tool_table(self):
        """name -> (описание для клиента, схема, функция(args)->текст)."""
        t = {}
        if self.cfg("srv_ask", True):
            t["ask_agent"] = ("Задать вопрос или команду агенту Nightlord: он ответит так же, как на голосовой запрос.",
                              {"text": {"type": "string", "description": "Вопрос или команда"}}, ["text"], self._ask)
        if self.cfg("srv_say", True):
            t["say"] = ("Произнести текст голосом агента (озвучка Voicebox).",
                        {"text": {"type": "string", "description": "Что сказать"}}, ["text"], self._say)
        ex = set(self.exposed())
        try:
            items = self.api.list_tools()
        except Exception:
            items = []
        for it in items:
            if it["name"] in ex and it["plugin"] != self.api.id and it["name"] not in t:
                n = it["name"]
                t[n] = ((it["label"] + ". " + it["description"]).strip(), it["props"], it["required"],
                        lambda a, n=n: self.api.call_tool(n, a))
        return t

    def _say(self, a):
        txt = str(a.get("text", "")).strip()
        if not txt:
            raise ValueError("пустой текст")
        self.api.say(txt)
        return "Сказал."

    def _ask(self, a):
        txt = str(a.get("text", "")).strip()
        if not txt:
            raise ValueError("пустой вопрос")
        with self.ask_lock:
            box, ev = {}, threading.Event()

            def cb(r):
                box["r"] = r
                ev.set()
            ok = self.api.run_text(txt, cb, speak=bool(self.cfg("srv_speak", False)),
                                   allow_actions=bool(self.cfg("srv_actions", False)), timeout=60)
            if not ok:
                raise RuntimeError("агент занят, попробуйте позже")
            if not ev.wait(180):
                raise RuntimeError("агент не ответил за 3 минуты")
            return box.get("r") or "(ответа нет)"

    def log(self, text):
        if self.cfg("srv_log", True):
            self.api.post(lambda: self.api.log(text))

    def handle(self, msg, who=""):
        """Один JSON-RPC объект -> ответ (dict) или None для уведомлений."""
        if not isinstance(msg, dict) or "method" not in msg:
            return None
        mid, method, params = msg.get("id"), msg["method"], msg.get("params") or {}
        is_req = "id" in msg

        def ok(res):
            return {"jsonrpc": "2.0", "id": mid, "result": res}

        def err(code, text):
            return {"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": text}}
        if not is_req:
            return None
        if method == "initialize":
            pv = params.get("protocolVersion")
            return ok({"protocolVersion": pv if pv in PROTOCOLS else LATEST,
                       "capabilities": {"tools": {"listChanged": False}},
                       "serverInfo": {"name": "nightlord-agent", "version": PLUGIN["version"]},
                       "instructions": "Голосовой агент Nightlord: ask_agent — спросить агента, say — озвучить текст."})
        if method == "ping":
            return ok({})
        if method == "tools/list":
            tt = self.tool_table()
            return ok({"tools": [{"name": n, "description": d[:1000],
                                  "inputSchema": {"type": "object", "properties": p, "required": r}}
                                 for n, (d, p, r, _f) in tt.items()]})
        if method == "tools/call":
            name, args = params.get("name"), params.get("arguments") or {}
            ent = self.tool_table().get(name)
            if ent is None:
                return err(-32602, f"нет инструмента «{name}»")
            self.log(f"MCP{(' ' + who) if who else ''}: вызов {name} {json.dumps(args, ensure_ascii=False)[:120]}")
            try:
                text = str(ent[3](coerce_args(args, ent[1])))[:20000]
                return ok({"content": [{"type": "text", "text": text}], "isError": False})
            except Exception as e:
                return ok({"content": [{"type": "text", "text": f"Ошибка: {e}"}], "isError": True})
        if method in ("resources/list", "resources/templates/list", "prompts/list"):
            key = {"resources/list": "resources", "resources/templates/list": "resourceTemplates",
                   "prompts/list": "prompts"}[method]
            return ok({key: []})
        return err(-32601, f"метод {method} не поддерживается")


class Server:
    """HTTP-сервера по списку адресов: /mcp (Streamable HTTP), /sse + /messages (старый SSE)."""

    def __init__(self, core):
        self.core = core
        self.httpds = []
        self.stop = threading.Event()
        self.sessions = {}
        self.slock = threading.Lock()
        self.running = []
        self.errors = []

    def token(self):
        return str(self.core.cfg("srv_token", "") or "").strip()

    def start(self, addrs):
        self.stop.clear()
        srv = self
        for host, port in addrs:
            class H(BaseHTTPRequestHandler):
                server_version = "NightlordMCP/1.0"

                def log_message(self, *a):
                    pass

                def _deny(self):
                    tok = srv.token()
                    q = parse_qs(urlparse(self.path).query)
                    got = self.headers.get("Authorization", "")
                    cand = got[7:].strip() if got.lower().startswith("bearer ") else (q.get("token") or [""])[0]
                    if tok:
                        if not secrets.compare_digest(cand.encode(), tok.encode()):
                            return 401
                        return 0
                    org = self.headers.get("Origin")
                    if org and urlparse(org).hostname not in ("localhost", "127.0.0.1", "::1"):
                        return 403                           # защита от запросов из браузерных страниц
                    return 0

                def _send(self, code, body=b"", ctype="application/json", extra=None):
                    self.send_response(code)
                    self.send_header("Content-Type", ctype)
                    self.send_header("Content-Length", str(len(body)))
                    for k, v in (extra or {}).items():
                        self.send_header(k, v)
                    self.end_headers()
                    if body:
                        self.wfile.write(body)

                def _json(self, code, obj, extra=None):
                    self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"), extra=extra)

                def _body(self):
                    n = int(self.headers.get("Content-Length") or 0)
                    if n > 4_000_000:
                        raise ValueError("слишком большой запрос")
                    return json.loads(self.rfile.read(n).decode("utf-8")) if n else None

                def do_GET(self):
                    path = urlparse(self.path).path.rstrip("/")
                    if path == "" or path == "/health":
                        return self._json(200, {"name": "nightlord-agent", "mcp": ["/mcp", "/sse"]})
                    d = self._deny()
                    if d:
                        return self._json(d, {"error": "unauthorized" if d == 401 else "forbidden"})
                    if path == "/sse":
                        return self._sse()
                    if path == "/mcp":
                        return self._send(405, b"", extra={"Allow": "POST, DELETE"})
                    self._json(404, {"error": "not found"})

                def do_DELETE(self):
                    self._send(200)

                def _sse(self):
                    sid = secrets.token_hex(12)
                    q = queue.Queue()
                    with srv.slock:
                        srv.sessions[sid] = q
                    try:
                        self.send_response(200)
                        self.send_header("Content-Type", "text/event-stream")
                        self.send_header("Cache-Control", "no-cache")
                        self.send_header("Connection", "close")
                        self.end_headers()
                        tok = srv.token()
                        ep = f"/messages?sessionId={sid}" + (f"&token={tok}" if tok else "")
                        self.wfile.write(f"event: endpoint\ndata: {ep}\n\n".encode())
                        self.wfile.flush()
                        while not srv.stop.is_set():
                            try:
                                m = q.get(timeout=10)
                                self.wfile.write(b"event: message\ndata: " + json.dumps(m, ensure_ascii=False).encode() + b"\n\n")
                            except queue.Empty:
                                self.wfile.write(b": ping\n\n")
                            self.wfile.flush()
                    except (OSError, ValueError):
                        pass
                    finally:
                        with srv.slock:
                            srv.sessions.pop(sid, None)

                def do_POST(self):
                    d = self._deny()
                    if d:
                        return self._json(d, {"error": "unauthorized" if d == 401 else "forbidden"})
                    u = urlparse(self.path)
                    path = u.path.rstrip("/")
                    try:
                        body = self._body()
                    except ValueError:
                        return self._json(400, {"jsonrpc": "2.0", "id": None,
                                                "error": {"code": -32700, "message": "не JSON"}})
                    if path == "/mcp":
                        batch = body if isinstance(body, list) else [body]
                        out = [r for r in (srv.core.handle(m, self.client_address[0]) for m in batch) if r]
                        if not out:
                            return self._send(202)
                        return self._json(200, out if isinstance(body, list) else out[0])
                    if path == "/messages":
                        sid = (parse_qs(u.query).get("sessionId") or [""])[0]
                        with srv.slock:
                            q = srv.sessions.get(sid)
                        if q is None:
                            return self._json(404, {"error": "сессия не найдена"})
                        self._send(202, b"Accepted", "text/plain")

                        def work(m=body):
                            for mm in (m if isinstance(m, list) else [m]):
                                r = srv.core.handle(mm, self.client_address[0])
                                if r:
                                    q.put(r)
                        threading.Thread(target=work, daemon=True).start()
                        return
                    self._json(404, {"error": "not found"})
            try:
                httpd = ThreadingHTTPServer((host, port), H)
                httpd.daemon_threads = True
            except OSError as e:
                self.errors.append(f"{host}:{port}: {e.strerror or e}")
                continue
            self.httpds.append(httpd)
            self.running.append((host, port))
            threading.Thread(target=httpd.serve_forever, daemon=True).start()

    def shutdown(self):
        self.stop.set()
        ths = [threading.Thread(target=h.shutdown, daemon=True) for h in self.httpds]
        for t in ths:
            t.start()
        for t in ths:
            t.join(3)
        for h in self.httpds:
            try:
                h.server_close()
            except Exception:
                pass
        self.httpds, self.running, self.errors = [], [], []


def endpoints(addrs, token=""):
    out = []
    for host, port in addrs:
        shown = local_ip() if host in ("0.0.0.0", "::") else host
        out.append((f"http://{shown}:{port}/mcp", f"http://{shown}:{port}/sse"))
    return out


# ------------------------------------------------------------------ плагин
def register(api):
    api.var("srv_on", False)
    api.var("srv_listen", "127.0.0.1:11600")
    api.var("srv_token", "")
    api.var("srv_ask", True)
    api.var("srv_say", True)
    api.var("srv_speak", False)
    api.var("srv_actions", False)
    api.var("srv_log", True)
    api.var("gateway", True)

    core = Core(api)
    server = Server(core)
    state = {"stopped": False, "refresh": None}
    conns = {}                       # имя сервера -> {"conn","tools","status","error","names"}
    registered = {}                  # имя инструмента агента -> (сервер, исходное имя)
    clock = threading.Lock()

    def servers():
        v = api.config_get("servers", [])
        return [s for s in v if isinstance(s, dict) and s.get("name")] if isinstance(v, list) else []

    def refresh():
        fn = state.get("refresh")
        if fn:
            api.post(fn)

    # ---------- клиент ----------
    def set_status(name, status, error=""):
        with clock:
            ent = conns.setdefault(name, {"conn": None, "tools": [], "names": [], "status": "", "error": ""})
            ent["status"], ent["error"] = status, error
        refresh()

    def pick_tools(spec, tools):
        flt = [x.strip().lower() for x in str(spec.get("filter", "")).split(",") if x.strip()]
        if flt:
            tools = [t for t in tools if any(f in t["name"].lower() for f in flt)]
        try:
            lim = int(spec.get("max_tools", 25))
        except (TypeError, ValueError):
            lim = 25
        return tools[:max(lim, 0)], len(tools)

    def register_tool(srv, t):
        name = safe_name(srv, t["name"])
        with clock:
            if name in registered:
                return
        schema = t.get("inputSchema") or {}
        props = schema.get("properties") if isinstance(schema.get("properties"), dict) else {}
        props = {k: (v if isinstance(v, dict) else {"type": "string"}) for k, v in props.items()}
        req = [r for r in (schema.get("required") or []) if r in props]
        desc = (str(t.get("description") or t.get("title") or t["name"]).strip()[:400]) + f" [MCP: {srv}]"

        def handler(a, srv=srv, tn=t["name"], props=props):
            return call_remote(srv, tn, coerce_args(a, props))

        def do():
            if state["stopped"]:
                return
            api.add_tool(name, desc, props, handler, req)
        try:
            api.ui(do)
            with clock:
                registered[name] = (srv, t["name"])
        except Exception as e:
            api.post(lambda: api.log(f"MCP: не удалось добавить инструмент {name}: {e}"))

    def connect(spec):
        name = spec["name"]
        if state["stopped"]:
            return
        close_one(name)
        if not spec.get("enabled", True):
            set_status(name, "выключен")
            return
        set_status(name, "подключение…")
        c = tools = None
        for attempt in range(3):          # сервер мог ещё не успеть начать слушать (например, наш же)
            try:
                c = open_conn(spec)
                tools = c.list_tools()
                break
            except McpError as e:
                err = str(e)
                if attempt < 2 and re.search(r"нет связи|не отвечает|сервер не прислал", err) and not state["stopped"]:
                    time.sleep(1.5)
                    continue
                set_status(name, "ошибка", err)
                return
            except Exception as e:
                set_status(name, "ошибка", f"{e.__class__.__name__}: {e}")
                return
        if state["stopped"]:
            c.close()
            return
        use, total = pick_tools(spec, tools)
        with clock:
            ent = conns.setdefault(name, {"names": []})
            ent.update({"conn": c, "tools": tools, "status": f"подключён: {len(use)} из {total}", "error": ""})
        for t in use:
            register_tool(name, t)
        refresh()

    def close_one(name):
        with clock:
            ent = conns.get(name)
            c = ent.get("conn") if ent else None
            if ent:
                ent["conn"], ent["tools"] = None, []
        if c:
            try:
                c.close()
            except Exception:
                pass

    def spec_by_name(name):
        return next((s for s in servers() if s["name"] == name), None)

    def call_remote(srv, tool, args):
        with clock:
            ent = conns.get(srv)
            c = ent.get("conn") if ent else None
        spec = spec_by_name(srv)
        if spec is None:
            return f"Сервер MCP «{srv}» удалён из настроек."
        if not spec.get("enabled", True):
            return f"Сервер MCP «{srv}» выключен."
        for attempt in (0, 1):
            if c is None or not c.alive():
                connect(spec)
                with clock:
                    c = (conns.get(srv) or {}).get("conn")
                if c is None:
                    return f"Сервер MCP «{srv}» недоступен: {(conns.get(srv) or {}).get('error', '')}"
            try:
                return c.call(tool, args)
            except McpError as e:
                if attempt == 0 and not c.alive():
                    c = None
                    continue
                return f"Ошибка MCP «{srv}»: {e}"
        return "Ошибка MCP"

    def connect_all():
        for spec in servers():
            if state["stopped"]:
                return
            threading.Thread(target=connect, args=(spec,), daemon=True).start()

    def save_servers(lst):
        api.config_set("servers", lst)

    def upsert(spec):
        lst = servers()
        for i, s in enumerate(lst):
            if s["name"] == spec["name"] or s["name"] == spec.get("_old"):
                lst[i] = {k: v for k, v in spec.items() if k != "_old"}
                break
        else:
            lst.append({k: v for k, v in spec.items() if k != "_old"})
        save_servers(lst)
        threading.Thread(target=connect, args=(spec_by_name(spec["name"]),), daemon=True).start()

    def remove(name):
        close_one(name)
        save_servers([s for s in servers() if s["name"] != name])
        with clock:
            conns.pop(name, None)
        refresh()

    # ---------- шлюз: два инструмента для любых серверов ----------
    def t_servers(a):
        lines = []
        for s in servers():
            ent = conns.get(s["name"]) or {}
            names = [t["name"] for t in ent.get("tools", [])]
            lines.append(f"{s['name']} — {ent.get('status') or 'не подключён'}" + (f" ({ent['error']})" if ent.get("error") else "")
                         + (": " + ", ".join(names[:60]) if names else ""))
        return "\n".join(lines) or "Внешние MCP-серверы не настроены."

    def t_call(a):
        srv, tool = str(a.get("server", "")).strip(), str(a.get("tool", "")).strip()
        if not srv or not tool:
            return "Нужны server и tool (список — mcp_servers)."
        args = a.get("arguments") or {}
        if isinstance(args, str):
            try:
                args = json.loads(args) if args.strip() else {}
            except ValueError:
                return "arguments должен быть JSON-объектом."
        ent = conns.get(srv) or {}
        schema = next((t.get("inputSchema") or {} for t in ent.get("tools", []) if t["name"] == tool), {})
        return call_remote(srv, tool, coerce_args(args, schema.get("properties") or {}))

    if api.config_get("gateway", True):
        api.add_tool("mcp_servers", "Список подключённых внешних MCP-серверов и их инструментов.", {}, t_servers, [])
        api.add_tool("mcp_call", "Вызвать инструмент внешнего MCP-сервера (имена — из mcp_servers).",
                     {"server": {"type": "string", "description": "Имя сервера"},
                      "tool": {"type": "string", "description": "Имя инструмента"},
                      "arguments": {"type": "string", "description": "Аргументы JSON-объектом, например {\"a\": 1}"}},
                     t_call, ["server", "tool"])

    # ---------- сервер ----------
    def apply_server():
        server.shutdown()
        server.errors = []
        if not api.config_get("srv_on", False) or state["stopped"]:
            refresh()
            return "выключен"
        try:
            addrs = parse_listen(api.config_get("srv_listen", "127.0.0.1:11600"))
        except ValueError as e:
            server.errors.append(str(e))
            refresh()
            return str(e)
        if any(not is_loopback(h) for h, _ in addrs) and not str(api.config_get("srv_token", "")).strip():
            api.config_set("srv_token", secrets.token_urlsafe(18))     # наружу без токена не отдаём
            v = api._vars.get("srv_token")
            if v is not None:
                api.post(lambda: v.set(api.config_get("srv_token", "")))
        server.start(addrs)
        refresh()
        return "ok"

    def tg_server(*a):
        threading.Thread(target=apply_server, daemon=True).start()

    # ---------- голосовая команда ----------
    def command(text, m):
        t = api.norm_ru(text)
        if not re.search(r"\b(?:статус|состояние|проверь)\b.*\b(?:mcp|мцп|эм ?си ?пи)\b", t):
            return None
        out = []
        if server.running:
            out.append("сервер слушает " + ", ".join(f"{h} порт {p}" for h, p in server.running))
        else:
            out.append("сервер MCP выключен")
        ok_ = [n for n, e in conns.items() if e.get("conn")]
        out.append(f"подключено внешних серверов: {len(ok_)} из {len(servers())}")
        return ". ".join(out) + "."
    api.add_command(r"(?:mcp|мцп|эм ?си ?пи)", command, "статус mcp")

    def unload():
        state["stopped"] = True
        server.shutdown()
        for n in list(conns):
            close_one(n)
    api.on_unload(unload)

    # ---------- настройки ----------
    def build(frame):
        def alive(w):
            try:
                return bool(w.winfo_exists())
            except Exception:
                return False

        srv = ttk.LabelFrame(frame, text="Сервер MCP — отдаёт агента наружу", padding=8)
        srv.pack(fill="x", pady=(0, 8))
        on = api.var("srv_on", False)
        ttk.Checkbutton(srv, text="Включить сервер", variable=on, command=tg_server).grid(
            row=0, column=0, sticky="w", columnspan=3)
        ttk.Label(srv, text="Адреса (хост:порт через запятую)").grid(row=1, column=0, sticky="w", pady=3)
        ttk.Entry(srv, textvariable=api.var("srv_listen", "127.0.0.1:11600"), width=34).grid(
            row=1, column=1, sticky="we", padx=6)
        ttk.Button(srv, text="Применить", command=tg_server).grid(row=1, column=2)
        ttk.Label(srv, text="Токен доступа").grid(row=2, column=0, sticky="w", pady=3)
        ttk.Entry(srv, textvariable=api.var("srv_token", ""), width=34, show="•").grid(
            row=2, column=1, sticky="we", padx=6)

        def new_token():
            v = api.var("srv_token", "")
            v.set(secrets.token_urlsafe(18))
            tg_server()
        ttk.Button(srv, text="Новый", command=new_token).grid(row=2, column=2)
        srv.columnconfigure(1, weight=1)
        ttk.Label(srv, text="Пустой токен допустим только на 127.0.0.1. Любой другой адрес (0.0.0.0 — вся сеть) "
                            "получит токен автоматически.", foreground="#777", wraplength=640, justify="left").grid(
            row=3, column=0, columnspan=3, sticky="w")
        opts = ttk.Frame(srv)
        opts.grid(row=4, column=0, columnspan=3, sticky="w", pady=(6, 0))
        for i, (k, t) in enumerate((("srv_ask", "ask_agent: спросить агента"), ("srv_say", "say: сказать вслух"),
                                    ("srv_speak", "озвучивать ответы ask_agent"), ("srv_log", "писать вызовы в журнал"))):
            ttk.Checkbutton(opts, text=t, variable=api.var(k, k in ("srv_ask", "srv_say", "srv_log"))).grid(
                row=i // 2, column=i % 2, sticky="w", padx=(0, 16))
        act = api.var("srv_actions", False)

        def act_chg(*a):
            try:
                if act.get() and not messagebox.askyesno(
                        "MCP", "Разрешить внешним клиентам через ask_agent запускать действия на этом компьютере "
                               "(программы, файлы, мышь)? Любой, у кого есть адрес и токен, получит эти права."):
                    act.set(False)
            except tk.TclError:
                pass
        act.trace_add("write", act_chg)
        ttk.Checkbutton(srv, text="ask_agent может выполнять действия на ПК (опасно)", variable=act).grid(
            row=5, column=0, columnspan=3, sticky="w", pady=(4, 0))
        st = tk.StringVar(value="")
        ttk.Label(srv, textvariable=st, wraplength=640, justify="left").grid(
            row=6, column=0, columnspan=3, sticky="w", pady=(6, 0))
        btns = ttk.Frame(srv)
        btns.grid(row=7, column=0, columnspan=3, sticky="w", pady=(6, 0))

        def cur_addrs():
            try:
                return parse_listen(api.config_get("srv_listen", "127.0.0.1:11600"))
            except ValueError:
                return []

        def copy(text):
            try:
                frame.clipboard_clear()
                frame.clipboard_append(text)
            except tk.TclError:
                pass

        def addr_text():
            tok = str(api.config_get("srv_token", "") or "").strip()
            lines = []
            for a, b in endpoints(cur_addrs()):
                lines += [a, b]
            return "\n".join(lines) + (f"\nТокен: {tok}  (заголовок Authorization: Bearer …)" if tok else "")

        def client_cfg():
            tok = str(api.config_get("srv_token", "") or "").strip()
            eps = endpoints(cur_addrs()) or [("http://127.0.0.1:11600/mcp", "http://127.0.0.1:11600/sse")]
            http_, sse = eps[0]
            a = {"mcpServers": {"nightlord": {"url": http_}}}
            b = {"mcpServers": {"nightlord": {"command": "npx", "args": ["-y", "mcp-remote", sse]}}}
            if tok:
                a["mcpServers"]["nightlord"]["headers"] = {"Authorization": "Bearer " + tok}
                b["mcpServers"]["nightlord"]["args"] += ["--header", "Authorization: Bearer " + tok]
            return ("// Клиенты с поддержкой HTTP (Cursor, Cline и др.):\n" + json.dumps(a, indent=2, ensure_ascii=False)
                    + "\n\n// Клиенты только со stdio (Claude Desktop) — через мост mcp-remote:\n"
                    + json.dumps(b, indent=2, ensure_ascii=False))

        def show_cfg():
            w = tk.Toplevel(frame)
            w.title("Конфиг для клиента MCP")
            w.geometry("640x420")
            tx = tk.Text(w, wrap="none", font=("Consolas", 10))
            tx.pack(fill="both", expand=True)
            tx.insert("1.0", client_cfg())
            ttk.Button(w, text="Копировать", command=lambda: copy(tx.get("1.0", "end").strip())).pack(pady=4)

        def pick_tools_win():
            w = tk.Toplevel(frame)
            w.title("Инструменты агента, доступные снаружи")
            w.geometry("520x520")
            ttk.Label(w, text="Отмеченные инструменты увидят и смогут вызвать внешние клиенты. "
                              "Подтверждения действий на ПК работают как обычно.", wraplength=480,
                      justify="left").pack(anchor="w", padx=10, pady=8)
            holder = ttk.Frame(w)
            holder.pack(fill="both", expand=True, padx=10)
            cv = tk.Canvas(holder, highlightthickness=0)
            sb = ttk.Scrollbar(holder, orient="vertical", command=cv.yview)
            cv.configure(yscrollcommand=sb.set)
            sb.pack(side="right", fill="y")
            cv.pack(side="left", fill="both", expand=True)
            inner = ttk.Frame(cv)
            cv.create_window((0, 0), window=inner, anchor="nw")
            inner.bind("<Configure>", lambda e: cv.configure(scrollregion=cv.bbox("all")))
            ex = set(core.exposed())
            vars_ = {}
            try:
                items = [i for i in api.list_tools() if i["plugin"] != api.id]
            except Exception:
                items = []
            for it in items:
                v = tk.BooleanVar(value=it["name"] in ex)
                vars_[it["name"]] = v
                ttk.Checkbutton(inner, text=f"{it['label'][:70]}  ({it['name']})", variable=v).pack(anchor="w")

            def save():
                api.config_set("srv_tools", [n for n, v in vars_.items() if v.get()])
                w.destroy()
            ttk.Button(w, text="Сохранить", command=save).pack(pady=8)

        ttk.Button(btns, text="Инструменты…", command=pick_tools_win).pack(side="left", padx=(0, 6))
        ttk.Button(btns, text="Копировать адреса", command=lambda: copy(addr_text())).pack(side="left", padx=(0, 6))
        ttk.Button(btns, text="Конфиг для клиента…", command=show_cfg).pack(side="left")

        cl = ttk.LabelFrame(frame, text="Внешние MCP-серверы — подключаем к агенту", padding=8)
        cl.pack(fill="x")
        cols = ("name", "addr", "status")
        tv = ttk.Treeview(cl, columns=cols, show="headings", height=6, selectmode="browse")
        for c, t, w_ in (("name", "Имя", 120), ("addr", "Адрес / команда", 300), ("status", "Состояние", 220)):
            tv.heading(c, text=t)
            tv.column(c, width=w_, anchor="w")
        tv.pack(fill="x")
        msg = tk.StringVar(value="")
        ttk.Label(cl, textvariable=msg, foreground="#c0392b", wraplength=640, justify="left").pack(anchor="w")
        bar = ttk.Frame(cl)
        bar.pack(fill="x", pady=(6, 0))

        def selected():
            try:
                sel = tv.selection()
                return tv.item(sel[0], "values")[0] if sel else None
            except Exception:
                return None

        def redraw():
            if not alive(tv):
                state["refresh"] = None
                return
            try:
                sel = selected()
                tv.delete(*tv.get_children())
                for s in servers():
                    e = conns.get(s["name"]) or {}
                    stt = e.get("status") or ("выключен" if not s.get("enabled", True) else "не подключён")
                    tv.insert("", "end", iid=s["name"], values=(s["name"], s.get("url") or s.get("command", ""), stt))
                    if s["name"] == sel:
                        tv.selection_set(s["name"])
                errs = [f"{n}: {e['error']}" for n, e in conns.items() if e.get("error")]
                msg.set("; ".join(errs)[:300])
                if alive_st():
                    if server.running:
                        eps = endpoints(server.running)
                        st.set("Слушает: " + "  |  ".join(a for a, _ in eps) + "   (SSE: …/sse)")
                    elif server.errors:
                        st.set("Не запустился: " + "; ".join(server.errors))
                    else:
                        st.set("Сервер выключен.")
            except tk.TclError:
                state["refresh"] = None

        def alive_st():
            return alive(srv)
        state["refresh"] = redraw

        def edit(name=None):
            old = spec_by_name(name) if name else None
            w = tk.Toplevel(frame)
            w.title("Сервер MCP")
            w.geometry("620x520")
            f = ttk.Frame(w, padding=10)
            f.pack(fill="both", expand=True)
            v = {k: tk.StringVar(value=str((old or {}).get(k, d))) for k, d in
                 (("name", ""), ("transport", "auto"), ("url", ""), ("command", ""), ("token", ""),
                  ("max_tools", "25"), ("filter", ""), ("cwd", ""))}
            en = tk.BooleanVar(value=bool((old or {}).get("enabled", True)))
            r = 0
            for k, t in (("name", "Имя (латиница, цифры)"), ("transport", "Тип"), ("url", "Адрес (http/sse)"),
                         ("command", "Команда (stdio)"), ("cwd", "Рабочая папка (stdio)"),
                         ("token", "Токен (необязательно)"), ("max_tools", "Макс. инструментов"),
                         ("filter", "Только с подстрокой (через запятую)")):
                ttk.Label(f, text=t).grid(row=r, column=0, sticky="w", pady=3)
                if k == "transport":
                    ttk.Combobox(f, textvariable=v[k], values=TRANSPORTS, state="readonly", width=12).grid(
                        row=r, column=1, sticky="w", padx=6)
                else:
                    ttk.Entry(f, textvariable=v[k], width=52).grid(row=r, column=1, sticky="we", padx=6)
                r += 1
            ttk.Label(f, text="Заголовки (по строке «Имя: значение»)").grid(row=r, column=0, sticky="nw", pady=3)
            th = tk.Text(f, height=3, width=52)
            th.grid(row=r, column=1, sticky="we", padx=6)
            th.insert("1.0", str((old or {}).get("headers", "")))
            r += 1
            ttk.Label(f, text="Переменные среды (stdio, «КЛЮЧ=значение»)").grid(row=r, column=0, sticky="nw", pady=3)
            te = tk.Text(f, height=3, width=52)
            te.grid(row=r, column=1, sticky="we", padx=6)
            te.insert("1.0", str((old or {}).get("env", "")))
            r += 1
            ttk.Checkbutton(f, text="Подключать при запуске", variable=en).grid(row=r, column=1, sticky="w", padx=6)
            r += 1
            ttk.Label(f, text="Примеры: адрес http://127.0.0.1:8000/mcp или …/sse; команда  npx -y "
                              "@modelcontextprotocol/server-filesystem C:\\Docs  или  uvx имя-пакета.\n"
                              "Для ComfyUI берите адрес или команду из описания того MCP-сервера, который вы поставили.",
                      foreground="#777", wraplength=580, justify="left").grid(row=r, column=0, columnspan=2,
                                                                                sticky="w", pady=6)
            f.columnconfigure(1, weight=1)

            def save():
                nm = re.sub(r"[^A-Za-z0-9_-]+", "", v["name"].get().strip())
                tr = v["transport"].get()
                if not nm:
                    return messagebox.showerror("MCP", "Нужно имя из латиницы и цифр.", parent=w)
                if tr == "stdio" and not v["command"].get().strip():
                    return messagebox.showerror("MCP", "Для типа stdio укажите команду.", parent=w)
                if tr != "stdio" and not v["url"].get().strip():
                    return messagebox.showerror("MCP", "Укажите адрес сервера.", parent=w)
                if any(s["name"] == nm and (not old or s["name"] != old["name"]) for s in servers()):
                    return messagebox.showerror("MCP", "Сервер с таким именем уже есть.", parent=w)
                if tr == "stdio" and not messagebox.askyesno(
                        "MCP", "Агент будет запускать эту программу на вашем компьютере:\n\n" +
                        v["command"].get().strip() + "\n\nЗапускать только то, чему доверяете. Продолжить?", parent=w):
                    return
                spec = {"name": nm, "transport": tr, "url": v["url"].get().strip(),
                        "command": v["command"].get().strip(), "cwd": v["cwd"].get().strip(),
                        "token": v["token"].get().strip(), "max_tools": v["max_tools"].get().strip() or "25",
                        "filter": v["filter"].get().strip(), "headers": th.get("1.0", "end").strip(),
                        "env": te.get("1.0", "end").strip(), "enabled": bool(en.get())}
                if old and old["name"] != nm:
                    spec["_old"] = old["name"]
                    close_one(old["name"])
                upsert(spec)
                w.destroy()
            ttk.Button(f, text="Сохранить и подключить", command=save).grid(row=r + 1, column=1, sticky="e", pady=8)

        def toggle():
            n = selected()
            s = spec_by_name(n) if n else None
            if s:
                s = dict(s, enabled=not s.get("enabled", True))
                upsert(s)

        def reconnect():
            n = selected()
            s = spec_by_name(n) if n else None
            if s:
                threading.Thread(target=connect, args=(s,), daemon=True).start()

        def delete():
            n = selected()
            if n and messagebox.askyesno("MCP", f"Удалить сервер «{n}»? Его инструменты исчезнут после перезагрузки плагинов."):
                remove(n)
        for t, fn in (("Добавить", lambda: edit()), ("Изменить", lambda: edit(selected()) if selected() else None),
                      ("Вкл/выкл", toggle), ("Переподключить", reconnect), ("Удалить", delete)):
            ttk.Button(bar, text=t, command=fn).pack(side="left", padx=(0, 6))
        ttk.Label(cl, text="Новые инструменты подключаются сразу; убранные исчезают из списка модели после "
                           "перезагрузки плагинов (вкладка «Плагины» → «Обновить список»).",
                  foreground="#777", wraplength=640, justify="left").pack(anchor="w", pady=(6, 0))
        redraw()
    api.add_settings(build)

    if api.config_get("srv_on", False):
        threading.Thread(target=apply_server, daemon=True).start()
    connect_all()
