# Плагин Nightlord агента: поиск по вашим документам. Индекс (SQLite FTS5) строится локально из выбранных папок:
# txt, md, csv, log, json, html, docx, pdf (нужен pypdf — вкладка «Компоненты»). Индекс лежит в plugins/data/doc_search/.
# Голосом: «найди в документах договор аренды», «открой первый документ». Модель получает инструменты
# search_documents / read_document — но облачной модели фрагменты по умолчанию НЕ отдаются.
import html as html_lib
import os
import re
import sqlite3
import threading
import time
import tkinter as tk
import zipfile
from pathlib import Path
from tkinter import ttk, filedialog
from urllib.parse import urlparse

PLUGIN = {"name": "Поиск по документам", "version": "1.0",
          "description": "«найди в документах …», «открой первый документ», «обнови индекс документов». "
                         "Локальный индекс выбранных папок (txt, md, docx, pdf…); модель может искать и читать документы."}

EXT_GROUPS = {"text": {".txt", ".md", ".csv", ".log", ".json", ".ini", ".yaml", ".yml", ".rst"},
              "html": {".html", ".htm"}, "docx": {".docx"}, "pdf": {".pdf"},
              "code": {".py", ".js", ".ts", ".java", ".cs", ".cpp", ".c", ".h", ".go", ".rs", ".sql", ".bat", ".ps1"}}
SKIP_DIRS = {"node_modules", "__pycache__", "site-packages", "appdata", "$recycle.bin", "windows", "program files",
             "program files (x86)", "venv", ".git"}
STOP = set("и в во на с со по о об про что как это для из к у за от до или а но же ли бы не то там тут мне мой моих "
           "моем документ документах документы документе файл файлах файлы найди поищи ищи есть было".split())
ORD = {"первый": 1, "второй": 2, "третий": 3, "четвертый": 4, "пятый": 5}
MAX_BYTES = 30 * 1024 * 1024
CHUNK = 900


def open_path(p):
    os.startfile(p)          # только Windows; в тестах подменяется


def stem(w):
    n = len(w)
    return w if n <= 4 else w[:n - 1] if n == 5 else w[:n - 2] if n <= 7 else w[:n - 3]


def query_terms(q):
    ws = [w for w in re.findall(r"\w+", (q or "").lower().replace("ё", "е")) if len(w) >= 2 and w not in STOP]
    return [stem(w) for w in ws][:8]


def make_chunks(text):
    paras = [p.strip() for p in re.split(r"\n\s*\n|\r\n\s*\r\n", text) if p.strip()]
    out, cur = [], ""
    for p in paras:
        p = re.sub(r"[ \t]+", " ", p)
        while len(p) > CHUNK * 1.5:
            cut = p.rfind(" ", 0, CHUNK)
            cut = cut if cut > CHUNK // 2 else CHUNK
            if cur:
                out.append(cur)
                cur = ""
            out.append(p[:cut])
            p = p[cut:].strip()
        if len(cur) + len(p) + 1 > CHUNK and cur:
            out.append(cur)
            cur = p
        else:
            cur = (cur + "\n" + p).strip()
    if cur:
        out.append(cur)
    return out[:400]


def extract(path):
    """Текст файла или исключение (с понятной причиной)."""
    ext = Path(path).suffix.lower()
    if ext == ".docx":
        with zipfile.ZipFile(path) as z:
            xml = z.read("word/document.xml").decode("utf-8", "ignore")
        xml = re.sub(r"</w:p>", "\n\n", xml)
        return html_lib.unescape(re.sub(r"<[^>]+>", "", xml))
    if ext == ".pdf":
        try:
            from pypdf import PdfReader
        except ImportError:
            raise ImportError("pypdf")
        return "\n\n".join((pg.extract_text() or "") for pg in PdfReader(str(path)).pages[:300])
    raw = Path(path).read_bytes()[:5_000_000]
    for enc in ("utf-8-sig", "cp1251"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    else:
        text = raw.decode("utf-8", "replace")
    if ext in EXT_GROUPS["html"]:
        text = re.sub(r"(?is)<(script|style).*?</\1>", " ", text)
        text = html_lib.unescape(re.sub(r"<[^>]+>", " ", text))
    return text


class Index:
    def __init__(self, db_path):
        self.db_path = str(db_path)
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        with self.conn() as c:
            c.execute("CREATE TABLE IF NOT EXISTS files(path TEXT PRIMARY KEY, mtime REAL, size INTEGER, chunks INTEGER)")
            c.execute("CREATE VIRTUAL TABLE IF NOT EXISTS chunks USING fts5(path UNINDEXED, n UNINDEXED, body, "
                      "tokenize='unicode61 remove_diacritics 2')")

    def conn(self):
        c = sqlite3.connect(self.db_path, timeout=30)
        return c

    def stats(self):
        c = self.conn()
        try:
            return c.execute("SELECT COUNT(*), COALESCE(SUM(chunks),0) FROM files").fetchone()
        finally:
            c.close()

    def clear(self):
        c = self.conn()
        try:
            c.execute("DELETE FROM files")
            c.execute("DELETE FROM chunks")
            c.commit()
        finally:
            c.close()

    def known(self, path):
        c = self.conn()
        try:
            return c.execute("SELECT 1 FROM files WHERE path=?", (path,)).fetchone() is not None
        finally:
            c.close()

    def update(self, folders, exts, stop, progress=None, max_files=20000):
        """Дочитывает изменённые файлы, выкидывает пропавшие. Возвращает словарь с итогами."""
        res = {"seen": 0, "added": 0, "skipped": 0, "removed": 0, "errors": 0, "need_pypdf": 0}
        c = self.conn()
        try:
            have = {r[0]: (r[1], r[2]) for r in c.execute("SELECT path, mtime, size FROM files")}
            seen = set()
            for folder in folders:
                for root, dirs, files in os.walk(folder):
                    dirs[:] = [d for d in dirs if not d.startswith(".") and d.lower() not in SKIP_DIRS]
                    for fn in files:
                        if stop.is_set():
                            c.commit()
                            return res
                        p = os.path.join(root, fn)
                        if Path(fn).suffix.lower() not in exts:
                            continue
                        try:
                            stt = os.stat(p)
                        except OSError:
                            continue
                        if stt.st_size > MAX_BYTES or stt.st_size == 0:
                            res["skipped"] += 1
                            continue
                        res["seen"] += 1
                        if res["seen"] > max_files:
                            c.commit()
                            return res
                        seen.add(p)
                        if have.get(p) == (stt.st_mtime, stt.st_size):
                            continue
                        try:
                            chunks = make_chunks(extract(p).replace("ё", "е").replace("Ё", "Е"))
                        except ImportError:
                            res["need_pypdf"] += 1
                            continue
                        except Exception:
                            res["errors"] += 1
                            continue
                        c.execute("DELETE FROM chunks WHERE path=?", (p,))
                        c.executemany("INSERT INTO chunks(path, n, body) VALUES (?,?,?)",
                                      [(p, i, t) for i, t in enumerate(chunks)])
                        c.execute("INSERT OR REPLACE INTO files VALUES (?,?,?,?)", (p, stt.st_mtime, stt.st_size, len(chunks)))
                        res["added"] += 1
                        if res["added"] % 25 == 0:
                            c.commit()
                        if progress:
                            progress(res)
            for p in set(have) - seen:
                if any(p.startswith(f) for f in folders):
                    c.execute("DELETE FROM chunks WHERE path=?", (p,))
                    c.execute("DELETE FROM files WHERE path=?", (p,))
                    res["removed"] += 1
            c.commit()
            return res
        finally:
            c.close()

    def search(self, query, limit=5):
        terms = query_terms(query)
        if not terms:
            return []
        c = self.conn()
        try:
            for joiner in (" AND ", " OR "):
                match = joiner.join(f'"{t}"*' for t in terms)
                try:
                    rows = c.execute("SELECT path, n, snippet(chunks, 2, '«', '»', ' … ', 28), bm25(chunks) FROM chunks "
                                     "WHERE chunks MATCH ? ORDER BY bm25(chunks) LIMIT 60", (match,)).fetchall()
                except sqlite3.OperationalError:
                    rows = []
                if rows:
                    break
            best, order = {}, []
            for path, n, snip, score in rows:
                if path not in best:
                    best[path] = (snip.replace("\n", " "), score)
                    order.append(path)
            return [(p, best[p][0]) for p in order[:limit]]
        finally:
            c.close()

    def text_of(self, path, limit):
        c = self.conn()
        try:
            rows = c.execute("SELECT body FROM chunks WHERE path=? ORDER BY CAST(n AS INTEGER)", (path,)).fetchall()
        finally:
            c.close()
        text = "\n\n".join(r[0] for r in rows)
        return text[:limit] + (f"\n…[обрезано, всего {len(text)} символов]" if len(text) > limit else "")


def is_cloud(url, bridge_port):
    """Локальный ли сервер модели. Мост «Облачных моделей» тоже на 127.0.0.1, поэтому сверяем его порт."""
    try:
        u = urlparse(url)
        host, port = (u.hostname or "").lower(), u.port
    except ValueError:
        return True
    if bridge_port and port == bridge_port:
        return True
    return not (host in ("localhost", "::1") or host.startswith("127.") or host.startswith("192.168.")
                or host.startswith("10.") or re.match(r"172\.(1[6-9]|2\d|3[01])\.", host))


def register(api):
    for k, d in (("text", True), ("html", True), ("docx", True), ("pdf", True), ("code", False), ("voice", True),
                 ("allow_open", True), ("allow_cloud", False), ("auto", True)):
        api.var(k, d)
    api.var("folders", "")
    api.var("every_min", 60)
    idx = {"db": None}
    st = {"busy": False, "stop": threading.Event(), "last": 0.0, "msg": "", "results": [], "label": None}
    lock = threading.Lock()

    def index():
        if idx["db"] is None:
            idx["db"] = Index(api.data_dir / "index.db")
        return idx["db"]

    def folders():
        return [f.strip() for f in re.split(r"[;\n]+", str(api.config_get("folders", ""))) if f.strip() and os.path.isdir(f.strip())]

    def exts():
        out = set()
        for g, s in EXT_GROUPS.items():
            if api.config_get(g, g != "code"):
                out |= s
        return out

    def set_msg(t):
        st["msg"] = t
        lab = st["label"]
        if lab is not None:
            api.post(lambda: lab.config(text=t))

    def reindex(wait=False):
        """Запустить (пере)индексацию в фоне. Возвращает False, если уже идёт или нет папок."""
        fl = folders()
        if not fl:
            set_msg("Папки не выбраны или не существуют.")
            return False
        if not lock.acquire(blocking=False):
            return False

        def work():
            try:
                st["busy"] = True
                st["stop"].clear()
                set_msg("Индексирую…")
                r = index().update(fl, exts(), st["stop"], progress=lambda r: set_msg(f"Индексирую… новых/изменённых: {r['added']}"))
                files, chunks = index().stats()
                extra = (f", не прочитано: {r['errors']}" if r["errors"] else "") + \
                        (f", PDF пропущено (нужен pypdf): {r['need_pypdf']}" if r["need_pypdf"] else "")
                set_msg(f"Готово: файлов в индексе {files} (новых/изменённых {r['added']}, удалено {r['removed']}){extra}.")
                api.log(st["msg"])
            except Exception as e:
                set_msg(f"Ошибка индексации: {str(e)[:150]}")
            finally:
                st["busy"] = False
                st["last"] = time.time()
                lock.release()
        t = threading.Thread(target=work, daemon=True)
        t.start()
        if wait:
            t.join()
        return True

    def cloud_blocked():
        if api.config_get("allow_cloud", False):
            return False
        url, _ = api.get_lm()
        port = None
        try:
            port = int(load_bridge_port())
        except (TypeError, ValueError):
            pass
        return is_cloud(url, port)

    def load_bridge_port():
        import json
        p = api.data_dir.parent / "cloud_llm" / "config.json"
        try:
            return json.loads(p.read_text(encoding="utf-8")).get("port", "11500") if p.exists() else None
        except (OSError, ValueError):
            return None

    def do_search(q, limit=5):
        if index().stats()[0] == 0:
            return None, "Индекс пуст. Выберите папки на вкладке «Плагины» и обновите индекс."
        res = index().search(q, limit)
        st["results"] = [p for p, _ in res]
        return res, ""

    def short(p):
        return Path(p).name

    def voice_search(q):
        res, err = do_search(q)
        if err:
            return err
        if not res:
            return f"В документах ничего не нашёл по запросу «{q}»."
        for i, (p, s) in enumerate(res, 1):
            api.log(f"{i}. {p}\n   {s}")
        def spoken(p):
            return "«" + Path(p).stem.replace("_", " ") + "»"
        rest = ", ".join(spoken(p) for p, _ in res[1:3])
        first = re.sub(r"[«»]", "", res[0][1])[:220]
        return (f"Нашёл документов: {len(res)}. Лучший — {spoken(res[0][0])}" + (f", ещё {rest}" if rest else "") +
                f". В лучшем: {first}")

    def command(text, m):
        if not api.config_get("voice", True):
            return None
        t = api.norm_ru(text)
        mm = (re.match(r"^(?:найди|поищи|ищи|посмотри) (?:у меня )?(?:в|по) (?:моих |своих )?(?:документам|документах|файлам|файлах|заметкам|заметках|папкам|папках)(?: про| о| об| по)? (.+)$", t)
              or re.match(r"^что (?:у меня )?(?:есть |написано )?в (?:моих )?(?:документах|файлах|заметках) (?:про|о|об) (.+)$", t))
        if mm:
            return voice_search(mm.group(1))
        if re.match(r"^(?:обнови|пересканируй|проиндексируй) (?:индекс |мои )?(?:документ\w*|файл\w*)$|^обнови индекс$", t):
            return "Обновляю индекс документов." if reindex() else (
                "Индекс уже обновляется." if st["busy"] else "Сначала выберите папки в настройках плагина.")
        mo = re.match(r"^открой (?:найденн\w+ |этот )?(?:(первый|второй|третий|четвертый|пятый|\d+)(?:-?й)? )?(?:документ|файл)$", t)
        if mo:
            if not api.config_get("allow_open", True):
                return "Открытие документов выключено в настройках плагина."
            if not st["results"]:
                return "Сначала найдите документ: «найди в документах …»."
            k = mo.group(1)
            n = ORD.get(k) or (int(k) if k and k.isdigit() else 1)
            if not 1 <= n <= len(st["results"]):
                return f"В последнем поиске только {len(st['results'])} документов."
            p = st["results"][n - 1]
            if not index().known(p) or not os.path.exists(p):
                return "Этого файла уже нет."
            open_path(p)
            return f"Открываю {short(p)}."
        return None

    def tool_search(args):
        if cloud_blocked():
            return "Поиск по документам недоступен: подключена облачная модель, а отдавать ей содержимое документов запрещено в настройках плагина."
        q = str(args.get("query", "")).strip()
        res, err = do_search(q, max(1, min(8, int(args.get("limit") or 5))))
        if err:
            return err
        if not res:
            return "Ничего не найдено."
        return "\n".join(f"[{i}] {p}\n{s}" for i, (p, s) in enumerate(res, 1))[:4000]

    def tool_read(args):
        if cloud_blocked():
            return "Чтение документов недоступно: подключена облачная модель (см. настройки плагина)."
        ref = str(args.get("path", "")).strip()
        p = None
        if ref.isdigit() and 1 <= int(ref) <= len(st["results"]):
            p = st["results"][int(ref) - 1]
        elif ref and index().known(ref):
            p = ref
        if not p:
            return "Такого документа нет в индексе. Укажите номер из последнего поиска или полный путь."
        return index().text_of(p, max(500, min(12000, int(args.get("max_chars") or 6000))))

    api.add_command(r"(?:документ|файл|заметк|папк|индекс)", command, "найди в документах …, открой первый документ, обнови индекс")
    api.add_tool("search_documents", "Ищет в локальном индексе документов пользователя (txt, md, docx, pdf…). "
                 "Возвращает пути и фрагменты. Для ответа о содержимом документа сначала вызови это.",
                 {"query": {"type": "string", "description": "Слова для поиска"},
                  "limit": {"type": "integer", "description": "Сколько документов вернуть (1-8)"}}, tool_search, ["query"])
    api.add_tool("read_document", "Читает текст документа из индекса: номер из последнего поиска или полный путь.",
                 {"path": {"type": "string", "description": "Номер результата (1, 2…) или путь"},
                  "max_chars": {"type": "integer", "description": "Сколько символов вернуть"}}, tool_read, ["path"])

    def tick():
        if st["busy"] or not api.config_get("auto", True) or not folders():
            return
        every = max(5, int(api.config_get("every_min", 60) or 60)) * 60
        if time.time() - st["last"] >= every:
            reindex()
    api.add_timer(60, tick)
    api.on_unload(lambda: st["stop"].set())

    def build(f):
        pad = {"padx": 6, "pady": 2}
        ttk.Label(f, text="Папки для индекса (через « ; »):").grid(row=0, column=0, columnspan=3, sticky="w", **pad)
        ttk.Entry(f, textvariable=api.var("folders"), width=72).grid(row=1, column=0, columnspan=3, sticky="w", **pad)

        def add():
            d = filedialog.askdirectory(title="Папка с документами")
            if d:
                cur = api.var("folders").get().strip().rstrip(";").strip()
                api.var("folders").set((cur + "; " if cur else "") + d)
        ttk.Button(f, text="Добавить папку…", command=add).grid(row=2, column=0, sticky="w", **pad)
        ttk.Button(f, text="Обновить индекс", command=lambda: reindex()).grid(row=2, column=1, sticky="w", **pad)

        def wipe():
            st["stop"].set()
            index().clear()
            set_msg("Индекс очищен.")
        ttk.Button(f, text="Очистить индекс", command=wipe).grid(row=2, column=2, sticky="w", **pad)
        rows = (("text", "Типы файлов: txt, md, csv, log, json, ini, yaml"), ("html", "html"), ("docx", "docx (Word)"),
                ("pdf", "pdf (нужен pypdf — вкладка «Компоненты»)"), ("code", "файлы кода (py, js, cs, sql…)"),
                ("voice", "Голосовые команды («найди в документах …»)"),
                ("allow_open", "Разрешить «открой первый документ»"),
                ("allow_cloud", "Отдавать фрагменты облачной модели (по умолчанию нет — документы остаются локальными)"),
                ("auto", "Обновлять индекс автоматически"))
        for i, (k, t) in enumerate(rows):
            ttk.Checkbutton(f, text=t, variable=api.var(k)).grid(row=3 + i, column=0, columnspan=3, sticky="w", **pad)
        ttk.Label(f, text="Автообновление раз в, мин:").grid(row=3 + len(rows), column=0, sticky="w", **pad)
        ttk.Spinbox(f, from_=5, to=1440, width=6, textvariable=api.var("every_min")).grid(row=3 + len(rows), column=1, sticky="w", **pad)
        st["label"] = ttk.Label(f, text=st["msg"] or "Индекс ещё не строился.")
        st["label"].grid(row=4 + len(rows), column=0, columnspan=3, sticky="w", **pad)
        ttk.Label(f, text=("Поиск понимает окончания слов приблизительно (по началу слова). Сканы PDF без текстового слоя не "
                           "читаются. Индекс содержит текст ваших документов — он лежит в plugins/data/doc_search/index.db."),
                  foreground="#777", wraplength=780, justify="left").grid(row=5 + len(rows), column=0, columnspan=3, sticky="w", **pad)
    api.add_settings(build)
