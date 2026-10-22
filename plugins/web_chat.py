# Плагин Nightlord агента: веб-чат. Пишете агенту из браузера — на этом ПК или с телефона в той же сети (Wi-Fi).
# Голосовые сообщения в обе стороны: запись на странице -> Whisper агента; ответ -> Voicebox -> голосовое на странице.
# На странице — 3D-аватар агента (та же модель, что на вкладке «Аватар»), рот двигается в такт ответу.
# История: plugins/data/web_chat/history.json, голосовые: plugins/data/web_chat/audio/, токен: config.json.
import io
import json
import re
import secrets
import socket
import ssl
import sys
import threading
import time
import queue
import wave
import webbrowser
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from tkinter import messagebox, ttk
from urllib.parse import parse_qs, unquote, urlparse

import numpy as np

PLUGIN = {"name": "Веб-чат", "version": "2.0",
          "description": "Чат с агентом в браузере (ПК или телефон): текст и голосовые сообщения в обе стороны, "
                         "3D-аватар на странице. Голосовая команда «открой веб-чат». Доступ по ссылке с токеном."}

MAX_MSGS = 300
MAX_TEXT = 4000
MAX_VOICE_SEC = 120
MAX_VOICE_BYTES = 25_000_000
SR = 16000
OLD_PORT, DEFAULT_PORT = 8765, 8780        # 8765 занят 3D-аватаром агента
COOKIE = "nl_webchat"
FILE_RE = re.compile(r"^\d+-(?:me|bot)\.wav$")


def lan_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


# ---------- звук ----------
def to_mono(x):
    x = np.asarray(x, dtype=np.float32)
    return x.mean(axis=1) if x.ndim > 1 else x


def resample(x, sr_from, sr_to):
    if sr_from == sr_to or len(x) == 0:
        return x
    n = max(1, int(round(len(x) * sr_to / sr_from)))
    return np.interp(np.linspace(0, len(x) - 1, n), np.arange(len(x)), x).astype(np.float32)


def wav_bytes(x, sr):
    pcm = (np.clip(to_mono(x), -1, 1) * 32767).astype("<i2").tobytes()
    b = io.BytesIO()
    with wave.open(b, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(int(sr))
        w.writeframes(pcm)
    return b.getvalue()


def decode_audio(raw, M):
    """Запись со страницы -> float32 моно 16 кГц. Страница присылает WAV; другие форматы пробуем через soundfile."""
    try:
        with wave.open(io.BytesIO(raw)) as w:
            sr, ch, sw = w.getframerate(), w.getnchannels(), w.getsampwidth()
            frames = w.readframes(w.getnframes())
        if sw != 2:
            raise ValueError("нужен 16-битный WAV")
        x = np.frombuffer(frames, "<i2").astype(np.float32) / 32768.0
        if ch > 1:
            x = x[: len(x) // ch * ch].reshape(-1, ch).mean(axis=1)
    except (wave.Error, EOFError, ValueError):
        sf = getattr(M, "sf", None)
        if sf is None:
            raise ValueError("формат записи не поддерживается")
        try:
            x, sr = sf.read(io.BytesIO(raw), dtype="float32")
        except Exception:
            raise ValueError("формат записи не поддерживается — запишите голосовое кнопкой 🎤 на странице")
        x = to_mono(x)
    return resample(x, sr, SR)


def split_for_tts(text, limit=320):
    parts, cur = [], ""
    for s in re.split(r"(?<=[.!?…])\s+|\n+", text):
        s = s.strip()
        if not s:
            continue
        while len(s) > limit:                       # очень длинное предложение режем по пробелу
            cut = s.rfind(" ", 0, limit)
            cut = cut if cut > limit // 2 else limit
            if cur:
                parts.append(cur)
                cur = ""
            parts.append(s[:cut])
            s = s[cut:].strip()
        if len(cur) + len(s) + 1 > limit and cur:
            parts.append(cur)
            cur = s
        else:
            cur = (cur + " " + s).strip()
    if cur:
        parts.append(cur)
    return parts


def make_cert(cert_path, key_path, ip):
    """Самоподписанный сертификат для HTTPS (нужен пакет cryptography)."""
    import datetime
    import ipaddress
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Nightlord web chat")])
    san = [x509.DNSName("localhost"), x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]
    if ip and ip != "127.0.0.1":
        san.append(x509.IPAddress(ipaddress.ip_address(ip)))
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(days=1))
            .not_valid_after(now + datetime.timedelta(days=3650))
            .add_extension(x509.SubjectAlternativeName(san), critical=False)
            .sign(key, hashes.SHA256()))
    cert_path.parent.mkdir(parents=True, exist_ok=True)
    key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                           serialization.NoEncryption()))
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))


def has_cryptography():
    import importlib.util
    return importlib.util.find_spec("cryptography") is not None


class QuietServer(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address):
        pass                                        # обрывы соединений и ошибки TLS не печатаем


class Chat:
    def __init__(self, api):
        self.api = api
        self.h = getattr(api, "_h", None)          # само приложение агента (Whisper, Voicebox, аватар)
        self.M = sys.modules.get(type(self.h).__module__) if self.h is not None else None
        self.lock = threading.Lock()
        self.stt_lock = threading.Lock()
        self.tts_lock = threading.Lock()
        self.msgs = []
        self.next_id = 1
        self.rev = 0
        self.epoch = secrets.token_hex(4)
        self.pending = False
        self.httpd = None
        self.scheme = "http"
        self.err = ""
        self.stopped = threading.Event()
        self._load()

    # ---------- история ----------
    def _path(self):
        return self.api.data_dir / "history.json"

    def audio_dir(self):
        return self.api.data_dir / "audio"

    def _load(self):
        try:
            d = json.loads(self._path().read_text("utf-8"))
            self.msgs = [m for m in d if isinstance(m, dict) and "id" in m][-MAX_MSGS:]
        except (OSError, ValueError, TypeError):
            self.msgs = []
        for i, m in enumerate(self.msgs, 1):
            m.pop("stt", None)
            m.pop("tts", None)
            m["rev"] = i
        self.rev = len(self.msgs)
        self.next_id = max([int(m.get("id", 0)) for m in self.msgs] + [0]) + 1

    def _save(self):
        try:
            self.api.data_dir.mkdir(parents=True, exist_ok=True)
            self._path().write_text(json.dumps(self.msgs, ensure_ascii=False), "utf-8")
        except OSError:
            pass

    def _drop_files(self, m):
        for name in (m.get("audio"), f"{m['id']}-bot.json"):
            if name:
                try:
                    (self.audio_dir() / name).unlink()
                except OSError:
                    pass

    def add(self, role, text, src="", allow_empty=False, **extra):
        """Новое сообщение (или уже существующее такое же — события агента и ответ могут прийти дважды)."""
        text = str(text or "").strip()
        if not text and not allow_empty:
            return None
        now = time.time()
        with self.lock:
            if text:
                for m in self.msgs[-4:]:
                    if m["role"] == role and m["text"] == text and now - m["t"] < 20:
                        return m
            self.rev += 1
            m = {"id": self.next_id, "role": role, "text": text[:20000], "src": src, "t": now, "rev": self.rev}
            m.update({k: v for k, v in extra.items() if v is not None})
            self.next_id += 1
            self.msgs.append(m)
            for old in self.msgs[:-MAX_MSGS]:
                self._drop_files(old)
            del self.msgs[:-MAX_MSGS]
            self._save()
            return m

    def update(self, mid, **kw):
        with self.lock:
            for m in self.msgs:
                if m["id"] == mid:
                    for k, v in kw.items():
                        if v is None:
                            m.pop(k, None)
                        else:
                            m[k] = v
                    self.rev += 1
                    m["rev"] = self.rev
                    self._save()
                    return m
        return None

    def get(self, mid):
        with self.lock:
            return next((dict(m) for m in self.msgs if m["id"] == mid), None)

    def since(self, rev, epoch):
        with self.lock:
            full = epoch != self.epoch
            msgs = [dict(m) for m in self.msgs if full or m.get("rev", 0) > rev]
            return {"epoch": self.epoch, "rev": self.rev, "msgs": msgs, "busy": self.pending}

    def clear(self):
        with self.lock:
            for m in self.msgs:
                self._drop_files(m)
            self.msgs = []
            self.epoch = secrets.token_hex(4)
            self.rev += 1
            self._save()

    # ---------- текст ----------
    def _claim(self):
        with self.lock:
            if self.pending:
                return False
            self.pending = True
            return True

    def send_text(self, text, cid, mode):
        if not self._claim():
            return "Агент ещё отвечает на прошлое сообщение."
        self.add("user", text, "web")
        threading.Thread(target=self._ask, args=(text, cid, mode == "always"), daemon=True).start()
        return ""

    def _ask(self, text, cid, want_voice):
        def cb(ans):
            try:
                if ans:
                    m = self.add("assistant", ans)
                    if want_voice and m:
                        self.voice_for(m["id"], cid)
                else:
                    self.add("system", "Ответа нет.")
            finally:
                self.pending = False
        try:
            ok = self.api.run_text(text, cb, speak=bool(self.api.config_get("speak", False)),
                                   allow_actions=bool(self.api.config_get("allow_actions", False)), timeout=60)
        except Exception as e:
            ok = False
            self.api.log(f"ошибка отправки: {e}")
        if not ok:
            self.add("system", "Агент занят другим разговором. Попробуйте ещё раз чуть позже.")
            self.pending = False

    # ---------- голос: от вас ----------
    def send_voice(self, raw, cid, mode):
        if self.h is None or not hasattr(self.h, "_stt"):
            return "Эта версия агента не умеет распознавать речь из плагина."
        if not self._claim():
            return "Агент ещё отвечает на прошлое сообщение."
        threading.Thread(target=self._voice_job, args=(raw, cid, mode), daemon=True).start()
        return ""

    def _voice_job(self, raw, cid, mode):
        text = self._voice_to_text(raw)
        if not text:
            self.pending = False
            return
        self._ask(text[:MAX_TEXT], cid, mode in ("voice", "always"))   # pending сбросит ответ агента

    def _voice_to_text(self, raw):
        """Сохраняет голосовое в чат и распознаёт. Возвращает текст или "" (причина уже показана в чате)."""
        try:
            try:
                x = decode_audio(raw, self.M)
            except Exception as e:
                self.add("system", f"Не удалось прочитать запись: {e}")
                return
            dur = len(x) / SR
            if dur < 0.3:
                self.add("system", "Слишком короткое голосовое.")
                return
            if dur > MAX_VOICE_SEC:
                self.add("system", f"Голосовое длиннее {MAX_VOICE_SEC // 60} минут — запишите покороче.")
                return
            m = self.add("user", "", "web", allow_empty=True, stt="pending", dur=round(dur, 1))
            name = f"{m['id']}-me.wav"
            try:
                self.audio_dir().mkdir(parents=True, exist_ok=True)
                (self.audio_dir() / name).write_bytes(wav_bytes(x, SR))
                self.update(m["id"], audio=name)
            except OSError:
                pass
            try:
                text = self._stt(x)
            except Exception as e:
                self.update(m["id"], stt=None)
                self.add("system", f"Ошибка распознавания: {type(e).__name__}: {e}")
                return
            if not text:
                self.update(m["id"], stt=None)
                self.add("system", "Речь не распознана. Попробуйте ещё раз, ближе к микрофону.")
                return
            self.update(m["id"], text=text[:MAX_TEXT], stt=None)
            return text
        except Exception as e:
            self.add("system", f"Ошибка голосового: {type(e).__name__}: {e}")
            return ""

    def _stt(self, x):
        h = self.h
        cfg = self.api.ui(h._make_cfg)
        with self.stt_lock:
            text = h._stt(cfg, x)
        try:
            if not getattr(h, "busy", False):
                h.set_status("Готов")
        except Exception:
            pass
        return str(text or "").strip()

    # ---------- голос: от агента ----------
    def voice_for(self, mid, cid):
        """Озвучить ответ mid через Voicebox и прикрепить к сообщению (в отдельном потоке)."""
        m = self.get(mid)
        if not m or m["role"] != "assistant" or m.get("tts") == "pending":
            return
        if m.get("audio"):
            self.update(mid, **{"for": cid})
            return
        if not self.api.config_get("voice_out", True):
            return
        self.update(mid, tts="pending", tts_err=None, **{"for": cid})
        threading.Thread(target=self._tts_job, args=(mid, m["text"]), daemon=True).start()

    def _tts_job(self, mid, text):
        try:
            with self.tts_lock:
                x, sr = self._synth(text)
            name = f"{mid}-bot.wav"
            self.audio_dir().mkdir(parents=True, exist_ok=True)
            (self.audio_dir() / name).write_bytes(wav_bytes(x, sr))
            lip = {"env": [], "vis": None}
            M = self.M
            try:
                lip["env"] = np.round(M.make_envelope(x, sr), 2).tolist()
                if getattr(self.h, "_vis_on", False):
                    lip["vis"] = np.round(M.make_visemes(x, sr), 2).tolist()
            except Exception:
                pass
            (self.audio_dir() / f"{mid}-bot.json").write_text(json.dumps(lip), "utf-8")
            emo = "neutral"
            try:
                emo = M.guess_emotion(text) or "neutral"
            except Exception:
                pass
            self.update(mid, audio=name, dur=round(len(x) / sr, 1), emo=emo, tts=None)
        except Exception as e:
            self.update(mid, tts=None, tts_err=str(e)[:200])

    def _synth(self, text):
        h = self.h
        if h is None or not hasattr(h, "_synth"):
            raise RuntimeError("эта версия агента не умеет озвучивать из плагина")
        cfg = self.api.ui(h._make_cfg)
        parts, sr0 = [], None
        for chunk in split_for_tts(text):
            r = h._synth(cfg, chunk)
            if r is None:
                continue
            data, sr = r
            if data is None:
                raise RuntimeError("Voicebox сам проиграл звук на ПК (способ /speak) и не отдал файл — "
                                   "выберите способ generate в настройках Voicebox агента")
            x = to_mono(data)
            if sr0 is None:
                sr0 = int(sr)
            elif int(sr) != sr0:
                x = resample(x, int(sr), sr0)
            parts += [x, np.zeros(int(sr0 * 0.12), dtype=np.float32)]
        if not parts:
            raise RuntimeError("нечего озвучивать")
        return np.concatenate(parts), sr0

    # ---------- аватар ----------
    def av(self):
        return getattr(self.h, "avatar3d", None)

    def av_cfg(self):
        av = self.av()
        if av is None:
            return {}
        try:
            self.api.ui(av.refresh_cfg, timeout=5)
        except Exception:
            pass
        return dict(getattr(av, "cfg", {}) or {})

    # ---------- сервер ----------
    def token(self):
        return str(self.api.config_get("token", "") or "")

    def port(self):
        try:
            return int(self.api.config_get("port", DEFAULT_PORT))
        except (TypeError, ValueError):
            return DEFAULT_PORT

    def url(self, local=False):
        lan = bool(self.api.config_get("lan", False)) and not local
        host = lan_ip() if lan else ("localhost" if self.scheme == "https" else "127.0.0.1")
        return f"{self.scheme}://{host}:{self.port()}/?token={self.token()}"

    def _ssl_ctx(self):
        if not self.api.config_get("https", False):
            return None
        d = self.api.data_dir
        cert, key = d / "cert.pem", d / "key.pem"
        ip = lan_ip()
        if not (cert.exists() and key.exists()) or self.api.config_get("cert_ip") != ip:
            make_cert(cert, key, ip)
            self.api.config_set("cert_ip", ip)
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(str(cert), str(key))
        return ctx

    def start(self):
        self.stop()
        self.stopped.clear()
        host = "0.0.0.0" if self.api.config_get("lan", False) else "127.0.0.1"
        port = self.port()
        try:
            ctx = self._ssl_ctx()
        except ImportError:
            ctx = None
            self.api.log("HTTPS выключен: нет пакета cryptography (py -3.12 -m pip install cryptography)")
        except Exception as e:
            ctx = None
            self.api.log(f"HTTPS выключен: не удалось сделать сертификат: {e}")
        try:
            httpd = QuietServer((host, port), make_handler(self))
        except OSError as e:
            self.httpd = None
            self.err = f"не удалось занять порт {port}: {e}"
            self.api.log(self.err)
            return False
        if ctx:
            httpd.socket = ctx.wrap_socket(httpd.socket, server_side=True, do_handshake_on_connect=False)
        self.scheme = "https" if ctx else "http"
        self.httpd = httpd
        threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.3}, daemon=True).start()
        self.err = ""
        self.api.log(f"веб-чат работает: {self.url()}")
        return True

    def stop(self):
        self.stopped.set()
        if self.httpd:
            h, self.httpd = self.httpd, None
            h.shutdown()
            h.server_close()


def make_handler(chat):
    class H(BaseHTTPRequestHandler):
        server_version = "NightlordWebChat/2.0"

        def log_message(self, *a):
            pass

        def setup(self):
            self.request.settimeout(30)
            if isinstance(self.request, ssl.SSLSocket):
                self.request.do_handshake()
            super().setup()

        # --- доступ ---
        def _given_token(self):
            q = parse_qs(urlparse(self.path).query)
            got = self.headers.get("X-Token") or (q.get("token") or [""])[0]
            if not got:
                c = SimpleCookie()
                try:
                    c.load(self.headers.get("Cookie", ""))
                except Exception:
                    pass
                got = c[COOKIE].value if COOKIE in c else ""
            return got

        def _ok(self):
            tok = chat.token()
            return bool(tok) and secrets.compare_digest(self._given_token().encode(), tok.encode())

        # --- ответы ---
        def _send(self, code, body=b"", ctype="application/json; charset=utf-8", extra=None):
            data = body.encode("utf-8") if isinstance(body, str) else body
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Frame-Options", "SAMEORIGIN")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            if data:
                self.wfile.write(data)

        def _json(self, code, obj):
            self._send(code, json.dumps(obj, ensure_ascii=False))

        def _body(self, limit):
            n = int(self.headers.get("Content-Length") or 0)
            if n > limit:
                raise OverflowError
            data, left = [], n
            while left > 0:
                chunk = self.rfile.read(min(left, 1 << 16))
                if not chunk:
                    break
                data.append(chunk)
                left -= len(chunk)
            return b"".join(data)

        def _file(self, path, ctype):
            try:
                data = path.read_bytes()
            except OSError:
                return self._json(404, {"error": "нет файла"})
            self._send(200, data, ctype)

        # --- GET ---
        def do_GET(self):
            u = urlparse(self.path)
            path = unquote(u.path)
            try:
                if not self._ok():
                    if path == "/":
                        return self._send(401, DENIED_HTML, "text/html; charset=utf-8")
                    return self._json(401, {"error": "нужен токен"})
                if path == "/":
                    secure = "; Secure" if chat.scheme == "https" else ""
                    cookie = f"{COOKIE}={chat.token()}; Path=/; Max-Age=2592000; HttpOnly; SameSite=Strict{secure}"
                    return self._send(200, PAGE_HTML, "text/html; charset=utf-8", {"Set-Cookie": cookie})
                if path == "/api/info":
                    return self._json(200, {"avatar": bool(chat.api.config_get("avatar", True)) and chat.av() is not None,
                                            "voice_in": chat.h is not None and hasattr(chat.h, "_stt"),
                                            "voice_out": bool(chat.api.config_get("voice_out", True)),
                                            "https": chat.scheme == "https"})
                if path == "/api/poll":
                    q = parse_qs(u.query)
                    try:
                        rev = int((q.get("rev") or ["0"])[0])
                    except ValueError:
                        rev = 0
                    return self._json(200, chat.since(rev, (q.get("epoch") or [""])[0]))
                if path.startswith("/api/audio/"):
                    name = path[len("/api/audio/"):]
                    if not FILE_RE.match(name):
                        return self._json(404, {"error": "нет файла"})
                    return self._file(chat.audio_dir() / name, "audio/wav")
                if path.startswith("/api/lip/"):
                    mid = path[len("/api/lip/"):]
                    if not mid.isdigit():
                        return self._json(404, {"error": "нет файла"})
                    return self._file(chat.audio_dir() / f"{mid}-bot.json", "application/json")
                # --- 3D-аватар агента (страница и её файлы) ---
                av = chat.av()
                if path == "/avatar":
                    if av is None or chat.M is None:
                        return self._send(200, NO_AVATAR_HTML, "text/html; charset=utf-8")
                    chat.av_cfg()
                    return self._send(200, av.page(), "text/html; charset=utf-8")
                if path == "/events":
                    return self._events()
                if av is not None and chat.M is not None and hasattr(chat.M, "AVATAR3D_DIR"):
                    if path.startswith("/model/"):
                        return av._file(self, chat.M.AVATAR3D_DIR, path[7:], (".vrm", ".glb"))
                    if path.startswith("/lib/"):
                        return av._file(self, chat.M.AVATAR3D_DIR / "lib", path[5:], (".js", ".mjs", ".json", ".wasm"))
                self._json(404, {"error": "нет такого адреса"})
            except (BrokenPipeError, ConnectionError, OSError):
                pass

        def _events(self):
            """Поток событий для аватара: настройки + то, что делает агент на ПК (если 3D-аватар агента включён)."""
            q = queue.Queue(maxsize=400)
            av = chat.av()
            sub = False

            def subscribe():
                nonlocal sub
                if av is not None and getattr(av, "srv", None) is not None and not sub:
                    with av.lock:
                        av.clients.append(q)
                    sub = True

            def unsubscribe():
                nonlocal sub
                if sub and av is not None:
                    with av.lock:
                        if q in av.clients:
                            av.clients.remove(q)
                sub = False
            try:
                self.request.settimeout(None)
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.end_headers()
                cfg = chat.av_cfg()
                cfg.pop("bubble", None)                     # реплики и так видны в чате; облачко — по желанию
                cfg["bubble"] = bool(chat.api.config_get("bubble", False))
                self.wfile.write(b"data: " + json.dumps({"t": "config", "cfg": cfg}).encode("utf-8") + b"\n\n")
                self.wfile.flush()
                idle = 0
                while not chat.stopped.is_set():
                    subscribe()
                    try:
                        item = q.get(timeout=5)
                    except queue.Empty:
                        idle += 1
                        if idle >= 3:
                            idle = 0
                            self.wfile.write(b": ping\n\n")
                            self.wfile.flush()
                        continue
                    if item is None:                        # 3D-аватар агента выключили
                        unsubscribe()
                        continue
                    try:
                        if json.loads(item).get("t") == "config":
                            continue                        # вид берём при открытии страницы
                    except ValueError:
                        continue
                    self.wfile.write(b"data: " + item.encode("utf-8") + b"\n\n")
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionError, OSError):
                pass
            finally:
                unsubscribe()

        # --- POST ---
        def do_POST(self):
            u = urlparse(self.path)
            path = u.path
            try:
                if not self._ok():
                    return self._json(401, {"error": "нужен токен"})
                if path == "/api/clear":
                    chat.clear()
                    return self._json(200, {"ok": True})
                if path == "/api/voice":
                    q = parse_qs(u.query)
                    try:
                        raw = self._body(MAX_VOICE_BYTES)
                    except OverflowError:
                        return self._json(413, {"error": "запись слишком большая"})
                    if not raw:
                        return self._json(400, {"error": "пустая запись"})
                    err = chat.send_voice(raw, (q.get("cid") or [""])[0][:40], (q.get("mode") or ["voice"])[0])
                    return self._json(409 if err else 200, {"error": err} if err else {"ok": True})
                try:
                    d = json.loads(self._body(64000) or b"{}")
                    d = d if isinstance(d, dict) else {}
                except (ValueError, OverflowError):
                    return self._json(400, {"error": "неверный запрос"})
                cid = str(d.get("cid", ""))[:40]
                if path == "/api/send":
                    text = str(d.get("text", "")).strip()
                    if not text:
                        return self._json(400, {"error": "пустое сообщение"})
                    err = chat.send_text(text[:MAX_TEXT], cid, str(d.get("mode", "voice")))
                    return self._json(409 if err else 200, {"error": err} if err else {"ok": True})
                if path == "/api/tts":
                    try:
                        mid = int(d.get("id"))
                    except (TypeError, ValueError):
                        return self._json(400, {"error": "нет id"})
                    if not chat.api.config_get("voice_out", True):
                        return self._json(403, {"error": "голосовые ответы выключены в настройках плагина"})
                    chat.voice_for(mid, cid)
                    return self._json(200, {"ok": True})
                self._json(404, {"error": "нет такого адреса"})
            except (BrokenPipeError, ConnectionError, OSError):
                pass
    return H


def register(api):
    if not api.config_get("token"):
        api.config_set("token", secrets.token_urlsafe(18))
    if api.config_get("port", DEFAULT_PORT) == OLD_PORT:     # прошлая версия стояла на порту 3D-аватара
        api.config_set("port", DEFAULT_PORT)
    chat = Chat(api)

    def on_user(text):
        if chat.pending or api.config_get("mirror", True):
            chat.add("user", text, "web" if chat.pending else "voice")

    def on_reply(text):
        if chat.pending or api.config_get("mirror", True):
            chat.add("assistant", text)
    api.on("user_text", on_user)
    api.on("reply", on_reply)

    def open_cmd(text, m):
        if not chat.httpd:
            return "Веб-чат не запущен. Посмотрите настройки на вкладке «Плагины»."
        webbrowser.open(chat.url(local=True))
        return "Открываю веб-чат."
    api.add_command(r"(?:открой|покажи|запусти) (?:веб ?чат|чат в браузере)", open_cmd, "открой веб-чат")

    chat.start()
    api.on_unload(chat.stop)

    def build(f):
        pad = {"padx": 6, "pady": 2}
        status = ttk.Label(f, text="")
        link = ttk.Label(f, text="", foreground="#3a6ff0", cursor="hand2")
        crypto = ttk.Label(f, text="", foreground="#777")

        def refresh():
            if chat.httpd:
                status.config(text=f"Сервер работает ({chat.scheme.upper()})", foreground="#2a8a2a")
                link.config(text=chat.url())
            else:
                status.config(text="Сервер не запущен" + (f": {chat.err}" if chat.err else ""), foreground="#c03030")
                link.config(text="")
            crypto.config(text="" if has_cryptography() else
                          "для HTTPS нужен пакет: py -3.12 -m pip install cryptography")

        def restart():
            chat.start()
            refresh()

        def new_token():
            if not messagebox.askyesno("Веб-чат", "Сделать новую ссылку? Старая (и открытые по ней страницы) "
                                                  "перестанет работать."):
                return
            api.config_set("token", secrets.token_urlsafe(18))
            refresh()

        def copy():
            if chat.httpd:
                f.clipboard_clear()
                f.clipboard_append(chat.url())
                messagebox.showinfo("Веб-чат", "Ссылка скопирована. Отправьте её себе на телефон.")

        def open_page():
            if chat.httpd:
                webbrowser.open(chat.url(local=True))

        r = 0
        ttk.Label(f, text="Состояние:").grid(row=r, column=0, sticky="w", **pad)
        status.grid(row=r, column=1, columnspan=3, sticky="w", **pad)
        r += 1
        ttk.Label(f, text="Ссылка:").grid(row=r, column=0, sticky="w", **pad)
        link.grid(row=r, column=1, columnspan=3, sticky="w", **pad)
        link.bind("<Button-1>", lambda e: open_page())
        r += 1
        bar = ttk.Frame(f)
        bar.grid(row=r, column=0, columnspan=4, sticky="w", **pad)
        ttk.Button(bar, text="Открыть в браузере", command=open_page).pack(side="left", padx=(0, 6))
        ttk.Button(bar, text="Скопировать ссылку", command=copy).pack(side="left", padx=(0, 6))
        ttk.Button(bar, text="Новая ссылка (токен)", command=new_token).pack(side="left", padx=(0, 6))
        r += 1
        ttk.Label(f, text="Порт:").grid(row=r, column=0, sticky="w", **pad)
        ttk.Entry(f, textvariable=api.var("port", DEFAULT_PORT), width=8).grid(row=r, column=1, sticky="w", **pad)
        r += 1
        ttk.Checkbutton(f, text="Доступ из локальной сети (телефон, планшет в том же Wi-Fi)",
                        variable=api.var("lan", False)).grid(row=r, column=0, columnspan=4, sticky="w", **pad)
        r += 1
        ttk.Checkbutton(f, text="HTTPS — нужно, чтобы на телефоне голосовое записывалось прямо на странице",
                        variable=api.var("https", False)).grid(row=r, column=0, columnspan=4, sticky="w", **pad)
        r += 1
        crypto.grid(row=r, column=0, columnspan=4, sticky="w", padx=26, pady=0)
        r += 1
        ttk.Button(f, text="Применить (перезапустить сервер)", command=restart).grid(
            row=r, column=0, columnspan=2, sticky="w", **pad)
        r += 1
        ttk.Separator(f).grid(row=r, column=0, columnspan=4, sticky="ew", pady=4)
        r += 1
        ttk.Checkbutton(f, text="Голосовые ответы: агент озвучивает ответ через Voicebox и присылает голосовое на страницу",
                        variable=api.var("voice_out", True)).grid(row=r, column=0, columnspan=4, sticky="w", **pad)
        r += 1
        ttk.Checkbutton(f, text="Показывать 3D-аватар на странице (модель и вид — на вкладке «Аватар»)",
                        variable=api.var("avatar", True)).grid(row=r, column=0, columnspan=4, sticky="w", **pad)
        r += 1
        ttk.Checkbutton(f, text="…с облачком реплик над аватаром",
                        variable=api.var("bubble", False)).grid(row=r, column=0, columnspan=4, sticky="w", padx=26, pady=2)
        r += 1
        ttk.Checkbutton(f, text="Показывать на странице и голосовую переписку с ПК",
                        variable=api.var("mirror", True)).grid(row=r, column=0, columnspan=4, sticky="w", **pad)
        r += 1
        ttk.Checkbutton(f, text="Озвучивать на ПК ответы на сообщения из веб-чата",
                        variable=api.var("speak", False)).grid(row=r, column=0, columnspan=4, sticky="w", **pad)
        r += 1
        ttk.Checkbutton(f, text="Разрешать из веб-чата действия на ПК, инструменты и горячие команды "
                                "(иначе только разговор)",
                        variable=api.var("allow_actions", False)).grid(row=r, column=0, columnspan=4, sticky="w", **pad)
        r += 1
        ttk.Label(f, text=("Страница открывается только по ссылке с токеном. Порт, сеть и HTTPS применяются кнопкой "
                           "«Применить». Браузер даёт записывать голос прямо на странице только на этом ПК или по HTTPS; "
                           "без HTTPS на телефоне кнопка 🎤 откроет диктофон телефона. Сертификат самоподписанный: "
                           "браузер предупредит — нажмите «Дополнительно» → «Перейти на сайт». Аватару нужен интернет "
                           "(библиотека three.js), если она не лежит локально в avatar3d/lib. Не открывайте порт "
                           "в интернет на роутере."),
                  foreground="#777", wraplength=780, justify="left").grid(row=r, column=0, columnspan=4, sticky="w", **pad)
        refresh()
    api.add_settings(build)


DENIED_HTML = """<!doctype html><html lang="ru"><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Nightlord — нет доступа</title>
<body style="background:#0f1115;color:#e6e8ee;font:16px system-ui,sans-serif;padding:32px">
<h2>Нет доступа</h2><p>Откройте ссылку с токеном: агент → вкладка «Плагины» → «Веб-чат» → «Скопировать ссылку».</p>
</body></html>"""

NO_AVATAR_HTML = """<!doctype html><html lang="ru"><meta charset="utf-8">
<body style="margin:0;display:grid;place-items:center;height:100vh;color:#8a90a0;font:14px system-ui,sans-serif">
Аватар недоступен в этой версии агента</body></html>"""

PAGE_HTML = r"""<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="theme-color" content="#14171d">
<title>Nightlord агент</title>
<style>
  :root { --bg:#0f1115; --panel:#14171d; --stage:#12151b; --line:#252a35; --me:#2b4a7a; --bot:#20242f;
          --text:#e6e8ee; --muted:#8a90a0; --accent:#6c8cff; --rec:#ff5468; --err:#ff8a8a; }
  * { box-sizing:border-box; }
  html, body { height:100%; }
  body { margin:0; display:flex; flex-direction:column; height:100dvh; background:var(--bg); color:var(--text);
         font:15px/1.5 system-ui, -apple-system, "Segoe UI", sans-serif; overflow:hidden; }
  header { display:flex; align-items:center; gap:8px; padding:8px 12px; background:var(--panel);
           border-bottom:1px solid var(--line); }
  header h1 { font-size:16px; margin:0; flex:1; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
  #conn { width:9px; height:9px; border-radius:50%; background:#3c3; flex:none; }
  #conn.off { background:#c33; }
  button { background:var(--accent); color:#fff; border:0; border-radius:10px; padding:8px 14px; font:inherit;
           cursor:pointer; }
  button.ghost { background:transparent; color:var(--muted); border:1px solid #333847; padding:5px 10px; }
  button:disabled { opacity:.45; cursor:default; }
  select { background:transparent; color:var(--muted); border:1px solid #333847; border-radius:10px; padding:5px 6px;
           font:inherit; font-size:13px; }
  select option { background:var(--panel); color:var(--text); }

  main { flex:1; display:flex; min-height:0; }
  #stage { position:relative; flex:0 0 40%; max-width:560px; background:radial-gradient(ellipse at 50% 70%, #1d2433, var(--stage) 70%);
           border-right:1px solid var(--line); }
  #stage iframe { position:absolute; inset:0; width:100%; height:100%; border:0; background:transparent; }
  body.noav #stage { display:none; }
  #chat { flex:1; display:flex; flex-direction:column; min-width:0; min-height:0; }
  @media (max-width: 760px) {
    main { flex-direction:column; }
    #stage { flex:0 0 32vh; max-width:none; border-right:0; border-bottom:1px solid var(--line); }
  }

  #log { flex:1; overflow-y:auto; padding:14px; display:flex; flex-direction:column; gap:8px; }
  .row { display:flex; flex-direction:column; max-width:min(720px, 88%); }
  .row.me { align-self:flex-end; align-items:flex-end; }
  .row.bot { align-self:flex-start; }
  .row.sys { align-self:center; max-width:92%; }
  .msg { padding:9px 13px; border-radius:14px; white-space:pre-wrap; overflow-wrap:anywhere; }
  .me .msg { background:var(--me); border-bottom-right-radius:4px; }
  .bot .msg { background:var(--bot); border-bottom-left-radius:4px; }
  .sys .msg { background:none; color:var(--err); font-size:13px; padding:2px; text-align:center; }
  .meta { color:var(--muted); font-size:11px; margin:2px 4px 0; display:flex; gap:6px; align-items:center; }
  .note { color:var(--muted); font-size:12px; font-style:italic; }
  .terr { color:var(--err); font-size:12px; }
  .voice { display:flex; align-items:center; gap:9px; min-width:190px; }
  .voice + .vtext { margin-top:6px; }
  .play { width:34px; height:34px; border-radius:50%; padding:0; flex:none; display:grid; place-items:center;
          font-size:13px; background:#ffffff26; }
  .bot .play { background:var(--accent); }
  .bar { flex:1; height:4px; background:#ffffff2a; border-radius:2px; overflow:hidden; }
  .fill { height:100%; width:0; background:#fff; }
  .dur { font-size:12px; color:#c9cede; font-variant-numeric:tabular-nums; }
  .ttsbtn { background:none; border:0; color:var(--muted); padding:0 2px; font-size:12px; cursor:pointer; }
  .ttsbtn:hover { color:var(--text); }
  #typing { align-self:flex-start; color:var(--muted); font-size:13px; padding:4px 2px; }
  #typing[hidden] { display:none; }
  .empty { margin:auto; color:var(--muted); text-align:center; padding:0 20px; }

  form { display:flex; gap:8px; align-items:flex-end; padding:10px 12px calc(10px + env(safe-area-inset-bottom));
         background:var(--panel); border-top:1px solid var(--line); }
  textarea { flex:1; resize:none; max-height:150px; min-height:40px; background:var(--bg); color:var(--text);
             border:1px solid #333847; border-radius:12px; padding:8px 12px; font:inherit; outline:none; }
  textarea:focus { border-color:var(--accent); }
  .round { width:42px; height:42px; border-radius:50%; padding:0; flex:none; display:grid; place-items:center; font-size:18px; }
  #mic { background:#2a2f3c; }
  #mic.rec { background:var(--rec); animation:pulse 1.2s ease-in-out infinite; }
  @keyframes pulse { 50% { box-shadow:0 0 0 7px #ff546833; } }
  #recbar { flex:1; display:flex; align-items:center; gap:10px; min-height:40px; padding:0 4px; }
  #recbar[hidden], textarea[hidden], #send[hidden] { display:none; }
  .recdot { width:10px; height:10px; border-radius:50%; background:var(--rec); animation:blink 1s steps(1) infinite; }
  @keyframes blink { 50% { opacity:.2; } }
  #rectime { font-variant-numeric:tabular-nums; }
  .rechint { color:var(--muted); font-size:13px; flex:1; }
</style>
</head>
<body>
<header>
  <span id="conn" title="связь с агентом"></span>
  <h1>Nightlord агент</h1>
  <select id="vmode" title="Когда агент отвечает голосовым">
    <option value="voice">🔊 на голосовые</option>
    <option value="always">🔊 всегда</option>
    <option value="never">🔇 текстом</option>
  </select>
  <button class="ghost" id="avbtn" type="button" title="Показать или скрыть аватар">👤</button>
  <button class="ghost" id="clear" type="button" title="Очистить историю">🗑</button>
</header>
<main>
  <section id="stage"><iframe id="av" title="Аватар агента"></iframe></section>
  <section id="chat">
    <div id="log"><div class="empty" id="empty">Напишите агенту или запишите голосовое 🎤</div><div id="typing" hidden>Агент думает…</div></div>
    <form id="f">
      <div id="recbar" hidden><span class="recdot"></span><span id="rectime">0:00</span>
        <span class="rechint">Запись… нажмите ⏹, чтобы отправить</span>
        <button type="button" class="ghost" id="reccancel">Отмена</button></div>
      <textarea id="t" rows="1" placeholder="Сообщение…"></textarea>
      <button type="button" class="round" id="mic" title="Голосовое сообщение">🎤</button>
      <button class="round" id="send" title="Отправить">➤</button>
    </form>
    <input type="file" id="filein" accept="audio/*" capture hidden>
  </section>
</main>
<script>
const $ = id => document.getElementById(id);
const qs = new URLSearchParams(location.search);
const token = qs.get("token") || "";
if (token) history.replaceState(null, "", location.pathname);       // токен дальше живёт в cookie
const H = token ? {"X-Token": token} : {};
const cid = Math.random().toString(36).slice(2, 12);
const log = $("log"), typing = $("typing"), t = $("t"), send = $("send"), f = $("f"), conn = $("conn"),
      empty = $("empty"), mic = $("mic"), recbar = $("recbar"), vmode = $("vmode"), fr = $("av");
function lsGet(k, d) { try { const v = localStorage.getItem(k); return v === null ? d : v; } catch (e) { return d; } }
function lsSet(k, v) { try { localStorage.setItem(k, v); } catch (e) {} }

let info = {avatar: false, voice_in: true, voice_out: true};
let epoch = "", rev = 0, busy = false, first = true;
const known = {};          // id -> сообщение
const blobs = {};          // имя файла -> blob: URL

vmode.value = lsGet("nl_vmode", "voice");
vmode.onchange = () => lsSet("nl_vmode", vmode.value);

// ---------- аватар ----------
function avatar(msg) {
  try { const w = fr.contentWindow; if (w && w.__brain) w.__brain.handle(msg, w.performance.now() / 1000); } catch (e) {}
}
function avState(s) { avatar({t: "state", state: s}); }
function setAvatarVisible(on) {
  document.body.classList.toggle("noav", !on);
  if (on && !fr.src) fr.src = "/avatar?bg=transparent";
  lsSet("nl_avatar", on ? "1" : "0");
}
$("avbtn").onclick = () => setAvatarVisible(document.body.classList.contains("noav"));

// ---------- плеер ----------
const player = new Audio();
let cur = null;                 // {id, lip, dur}
function fmt(s) { s = Math.max(0, Math.round(s || 0)); return Math.floor(s / 60) + ":" + String(s % 60).padStart(2, "0"); }
function playUI() {
  for (const b of log.querySelectorAll(".play")) {
    const on = cur && Number(b.dataset.id) === cur.id && !player.paused;
    b.textContent = on ? "⏸" : "▶";
  }
}
player.addEventListener("timeupdate", () => {
  if (!cur) return;
  const row = log.querySelector(`.row[data-id="${cur.id}"]`);
  if (!row) return;
  const d = player.duration || cur.dur || 1;
  const fill = row.querySelector(".fill"), dur = row.querySelector(".dur");
  if (fill) fill.style.width = Math.min(100, player.currentTime / d * 100) + "%";
  if (dur) dur.textContent = fmt(player.currentTime) + " / " + fmt(d);
});
player.addEventListener("pause", () => { playUI(); if (cur && cur.bot) avState(busy ? "thinking" : "idle"); });
player.addEventListener("ended", () => {
  const row = cur && log.querySelector(`.row[data-id="${cur.id}"]`);
  if (row) { const fl = row.querySelector(".fill"); if (fl) fl.style.width = "0"; }
  playUI();
});
function speakFrom(offset) {
  if (!cur || !cur.bot) return;
  const m = known[cur.id] || {};
  const lip = cur.lip || {}, i = Math.floor(offset / 0.04);
  avatar({t: "speak", env: lip.env ? lip.env.slice(i) : null, vis: lip.vis ? lip.vis.slice(i) : null,
          dur: Math.max(.3, (player.duration || m.dur || 1) - offset), emotion: m.emo || "neutral", text: m.text || ""});
}
async function togglePlay(id) {
  const m = known[id];
  if (!m || !m.audio) return;
  if (cur && cur.id === id) {
    if (!player.paused) { player.pause(); return; }
    try { await player.play(); speakFrom(player.currentTime); playUI(); } catch (e) {}
    return;
  }
  try {
    let url = blobs[m.audio];
    if (!url) {
      const r = await fetch("/api/audio/" + m.audio, {headers: H});
      if (!r.ok) throw new Error("HTTP " + r.status);
      url = blobs[m.audio] = URL.createObjectURL(await r.blob());
    }
    let lip = null;
    if (m.role === "assistant") {
      try { const r = await fetch("/api/lip/" + id, {headers: H}); if (r.ok) lip = await r.json(); } catch (e) {}
    }
    player.pause();
    cur = {id, lip, dur: m.dur, bot: m.role === "assistant"};
    player.src = url;
    await player.play();
    speakFrom(0);
    playUI();
  } catch (e) {
    if (e && e.name === "NotAllowedError") return;      // браузер не дал играть без нажатия — кнопка ▶ на месте
    sys("Не удалось воспроизвести: " + (e.message || e));
  }
}

// ---------- сообщения ----------
function nearBottom() { return log.scrollHeight - log.scrollTop - log.clientHeight < 90; }
function sys(text) { place({id: "s" + Date.now() + Math.random(), role: "system", text}); }

function voiceEl(m) {
  const v = document.createElement("div");
  v.className = "voice";
  const b = document.createElement("button");
  b.type = "button"; b.className = "play"; b.dataset.id = m.id; b.textContent = "▶";
  b.onclick = () => togglePlay(m.id);
  const bar = document.createElement("div"); bar.className = "bar";
  const fill = document.createElement("div"); fill.className = "fill"; bar.appendChild(fill);
  const d = document.createElement("span"); d.className = "dur"; d.textContent = fmt(m.dur);
  v.append(b, bar, d);
  return v;
}

function build(m) {
  const row = document.createElement("div");
  row.className = "row " + (m.role === "user" ? "me" : m.role === "assistant" ? "bot" : "sys");
  row.dataset.id = m.id;
  const box = document.createElement("div");
  box.className = "msg";
  if (m.audio) box.appendChild(voiceEl(m));
  const txt = document.createElement("div");
  txt.className = "vtext";
  if (m.text) txt.textContent = m.text;
  else if (m.stt) { txt.className = "vtext note"; txt.textContent = "распознаю…"; }
  if (txt.textContent) box.appendChild(txt);
  row.appendChild(box);
  if (m.role !== "system") {
    const meta = document.createElement("div");
    meta.className = "meta";
    const tm = new Date((m.t || Date.now() / 1000) * 1000).toLocaleTimeString([], {hour: "2-digit", minute: "2-digit"});
    meta.append((m.src === "voice" ? "🎤 с ПК · " : "") + tm);
    if (m.role === "assistant") {
      if (m.tts === "pending") {
        const n = document.createElement("span"); n.className = "note"; n.textContent = "· записываю голосовое…"; meta.append(n);
      } else if (!m.audio && info.voice_out) {
        const b = document.createElement("button");
        b.type = "button"; b.className = "ttsbtn"; b.title = "Прислать голосом"; b.textContent = "🔊 голосом";
        b.onclick = () => askTts(m.id);
        meta.append(b);
      }
      if (m.tts_err) { const e = document.createElement("span"); e.className = "terr"; e.textContent = "· " + m.tts_err; meta.append(e); }
    }
    row.appendChild(meta);
  }
  return row;
}

function place(m) {
  const stick = nearBottom();
  empty.hidden = true;
  const el = build(m), old = log.querySelector(`.row[data-id="${m.id}"]`);
  if (old) old.replaceWith(el); else log.insertBefore(el, typing);
  if (stick) log.scrollTop = log.scrollHeight;
}

function upsert(m) {
  const prev = known[m.id];
  known[m.id] = m;
  place(m);
  playUI();
  if (!first && m.role === "assistant" && m.audio && !(prev && prev.audio) && m.for === cid && vmode.value !== "never")
    togglePlay(m.id);
}

function clearUI() {
  player.pause(); cur = null;
  for (const el of [...log.querySelectorAll(".row")]) el.remove();
  for (const k of Object.keys(known)) delete known[k];
  empty.hidden = false;
}

function setBusy(b) {
  if (b === busy) return;
  busy = b;
  typing.hidden = !b;
  send.disabled = b; mic.disabled = b && !rec;
  if (b) { avState("thinking"); if (nearBottom()) log.scrollTop = log.scrollHeight; }
  else if (player.paused) avState("idle");
}

async function poll() {
  let delay = 1500;
  try {
    const r = await fetch(`/api/poll?rev=${rev}&epoch=${epoch}`, {headers: H, cache: "no-store"});
    if (r.status === 401) {
      conn.className = "off";
      document.body.innerHTML = "<p style='padding:24px'>Ссылка устарела. Скопируйте новую на вкладке «Плагины» → «Веб-чат».</p>";
      return;
    }
    const d = await r.json();
    if (d.epoch !== epoch) { if (epoch) clearUI(); epoch = d.epoch; }
    for (const m of d.msgs) upsert(m);
    rev = d.rev;
    first = false;
    setBusy(d.busy);
    conn.className = "";
    if (d.busy || d.msgs.some(m => m.tts === "pending" || m.stt)) delay = 600;
  } catch (e) { conn.className = "off"; delay = 3000; }
  setTimeout(poll, delay);
}

async function post(url, body, headers) {
  const r = await fetch(url, {method: "POST", headers: {...H, ...headers}, body});
  const d = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(d.error || ("Ошибка " + r.status));
  return d;
}

async function askTts(id) {
  try { await post("/api/tts", JSON.stringify({id, cid}), {"Content-Type": "application/json"}); setTimeout(poll, 150); }
  catch (e) { sys(e.message); }
}

// ---------- текст ----------
t.addEventListener("input", () => { t.style.height = "auto"; t.style.height = t.scrollHeight + "px"; });
t.addEventListener("keydown", e => {
  if (e.key === "Enter" && !e.shiftKey && !e.isComposing && matchMedia("(pointer:fine)").matches) {
    e.preventDefault(); f.requestSubmit();
  }
});
f.addEventListener("submit", async e => {
  e.preventDefault();
  const text = t.value.trim();
  if (!text || busy) return;
  setBusy(true);
  try {
    await post("/api/send", JSON.stringify({text, cid, mode: vmode.value}), {"Content-Type": "application/json"});
    t.value = ""; t.style.height = "auto";
    setTimeout(poll, 150);
  } catch (err) { sys(err.message); setBusy(false); }
});

$("clear").onclick = async () => {
  if (!confirm("Очистить историю чата?")) return;
  try { await post("/api/clear", "", {}); clearUI(); } catch (e) { sys(e.message); }
};

// ---------- голосовые ----------
let rec = null;               // {stream, mr, chunks, t0, timer, cancel}
const MAX_SEC = 120;

function recUI(on) {
  recbar.hidden = !on; t.hidden = on; send.hidden = on;
  mic.classList.toggle("rec", on);
  mic.textContent = on ? "⏹" : "🎤";
  mic.title = on ? "Остановить и отправить" : "Голосовое сообщение";
}

async function startRec() {
  const can = navigator.mediaDevices && navigator.mediaDevices.getUserMedia && window.MediaRecorder;
  if (!can) {
    if (!window.isSecureContext && !lsGet("nl_hint", ""))  {
      sys("Браузер разрешает запись на странице только по HTTPS. Сейчас откроется диктофон телефона. " +
          "Чтобы записывать прямо здесь, включите HTTPS в настройках плагина «Веб-чат» на ПК.");
      lsSet("nl_hint", "1");
    }
    $("filein").click();
    return;
  }
  let stream;
  try {
    stream = await navigator.mediaDevices.getUserMedia({audio: {echoCancellation: true, noiseSuppression: true}});
  } catch (e) { sys("Нет доступа к микрофону: " + (e.message || e.name)); return; }
  player.pause();
  const mr = new MediaRecorder(stream);
  rec = {stream, mr, chunks: [], t0: Date.now(), cancel: false};
  mr.ondataavailable = e => { if (e.data && e.data.size) rec.chunks.push(e.data); };
  mr.onstop = async () => {
    const r = rec; rec = null;
    clearInterval(r.timer);
    r.stream.getTracks().forEach(tr => tr.stop());
    recUI(false);
    mic.disabled = busy;
    if (r.cancel) { avState(busy ? "thinking" : "idle"); return; }
    if (Date.now() - r.t0 < 500) { sys("Слишком коротко — нажмите 🎤, скажите фразу и нажмите ⏹."); avState("idle"); return; }
    await sendVoice(new Blob(r.chunks, {type: mr.mimeType || "audio/webm"}));
  };
  mr.start(250);
  rec.timer = setInterval(() => {
    const s = (Date.now() - rec.t0) / 1000;
    $("rectime").textContent = fmt(s);
    if (s >= MAX_SEC) mr.stop();
  }, 250);
  $("rectime").textContent = "0:00";
  recUI(true);
  avState("listening");
}

mic.onclick = () => {
  if (rec) { rec.mr.stop(); return; }
  if (!busy) startRec();
};
$("reccancel").onclick = () => { if (rec) { rec.cancel = true; rec.mr.stop(); } };
$("filein").onchange = async e => {
  const file = e.target.files && e.target.files[0];
  e.target.value = "";
  if (file) await sendVoice(file);
};

async function toWav16k(buf) {
  const AC = window.AudioContext || window.webkitAudioContext;
  const ac = new AC();
  let audio;
  try {
    audio = await new Promise((res, rej) => { const p = ac.decodeAudioData(buf, res, rej); if (p && p.then) p.then(res, rej); });
  } finally { try { ac.close(); } catch (e) {} }
  const sr = 16000, len = Math.max(1, Math.ceil(audio.duration * sr));
  const OAC = window.OfflineAudioContext || window.webkitOfflineAudioContext;
  const off = new OAC(1, len, sr);
  const src = off.createBufferSource();
  src.buffer = audio; src.connect(off.destination); src.start();
  const out = await off.startRendering();
  const ch = out.getChannelData(0), n = ch.length;
  const b = new ArrayBuffer(44 + n * 2), v = new DataView(b);
  const str = (o, s) => { for (let i = 0; i < s.length; i++) v.setUint8(o + i, s.charCodeAt(i)); };
  str(0, "RIFF"); v.setUint32(4, 36 + n * 2, true); str(8, "WAVE"); str(12, "fmt ");
  v.setUint32(16, 16, true); v.setUint16(20, 1, true); v.setUint16(22, 1, true); v.setUint32(24, sr, true);
  v.setUint32(28, sr * 2, true); v.setUint16(32, 2, true); v.setUint16(34, 16, true); str(36, "data");
  v.setUint32(40, n * 2, true);
  for (let i = 0; i < n; i++) { const s = Math.max(-1, Math.min(1, ch[i])); v.setInt16(44 + i * 2, s < 0 ? s * 0x8000 : s * 0x7fff, true); }
  return new Blob([b], {type: "audio/wav"});
}

async function sendVoice(blob) {
  if (busy) return;
  setBusy(true);
  let body = blob, type = blob.type || "application/octet-stream";
  try { body = await toWav16k(await blob.arrayBuffer()); type = "audio/wav"; }
  catch (e) { /* не смогли перевести в WAV в браузере — отправим как есть, агент попробует сам */ }
  try {
    await post(`/api/voice?cid=${cid}&mode=${vmode.value}`, body, {"Content-Type": type});
    setTimeout(poll, 150);
  } catch (e) { sys(e.message); setBusy(false); }
}

// ---------- старт ----------
fetch("/api/info", {headers: H}).then(r => r.json()).then(d => {
  info = d;
  if (!d.voice_in) mic.hidden = true;
  if (!d.voice_out) vmode.hidden = true;
  if (d.avatar) setAvatarVisible(lsGet("nl_avatar", "1") === "1");
  else { document.body.classList.add("noav"); $("avbtn").hidden = true; }
}).catch(() => { document.body.classList.add("noav"); }).finally(poll);
document.body.classList.add("noav");
if (matchMedia("(pointer:fine)").matches) { t.placeholder = "Сообщение… (Enter — отправить, Shift+Enter — новая строка)"; t.focus(); }
</script>
</body>
</html>
"""
