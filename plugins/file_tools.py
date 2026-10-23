# Инструмент Nightlord агента: работа с файлами строго внутри папки файлов агента (вкладка «Функции»).
# Ничего не удаляется: дубликаты переносятся в подпапку _duplicates, переименование идёт после подтверждения.
import hashlib
import os
import re
import shutil
import zipfile
from pathlib import Path

PLUGIN = {"name": "Работа с файлами", "version": "1.0", "group": "tools",
          "description": "В папке файлов агента: «найди дубликаты», «найди большие файлы», «сколько весит папка …», «упакуй папку … в zip», "
                         "«распакуй архив …», «что в архиве …», «переименуй файлы в … добавь префикс …». "
                         "Выйти за пределы папки нельзя, удалять файлы нельзя, переименование — только после подтверждения."}

MAX_SCAN = 20000


def human(n):
    n = float(n)
    for u in ("Б", "КБ", "МБ", "ГБ", "ТБ"):
        if n < 1024 or u == "ТБ":
            return f"{n:.0f} {u}" if u == "Б" else f"{n:.1f} {u}".replace(".", ",")
        n /= 1024


def inside(base, p):
    """Разрешённый путь внутри base (без выхода по .., символьным ссылкам и абсолютным путям)."""
    base = Path(os.path.realpath(base))
    cand = Path(os.path.realpath(base / str(p or ".")))
    try:
        cand.relative_to(base)
    except ValueError:
        raise PermissionError("путь за пределами папки файлов агента") from None
    return cand


def walk_files(root, base, limit=MAX_SCAN):
    out = []
    for dp, dn, fn in os.walk(root):
        dn[:] = [d for d in dn if d != "_duplicates" and not os.path.islink(os.path.join(dp, d))]
        for f in fn:
            p = Path(dp, f)
            if p.is_symlink():
                continue
            out.append(p)
            if len(out) >= limit:
                return out, True
    return out, False


def file_hash(p, chunk=1 << 20):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for b in iter(lambda: f.read(chunk), b""):
            h.update(b)
    return h.hexdigest()


def find_dups(root, base, min_size=1):
    files, cut = walk_files(root, base)
    by_size = {}
    for p in files:
        try:
            s = p.stat().st_size
        except OSError:
            continue
        if s >= min_size:
            by_size.setdefault(s, []).append(p)
    groups = []
    for s, ps in by_size.items():
        if len(ps) < 2:
            continue
        by_h = {}
        for p in ps:
            try:
                by_h.setdefault(file_hash(p), []).append(p)
            except OSError:
                pass
        groups += [(s, sorted(g)) for g in by_h.values() if len(g) > 1]
    groups.sort(key=lambda g: -g[0] * (len(g[1]) - 1))
    return groups, cut


def unique(path):
    if not path.exists():
        return path
    for i in range(1, 10000):
        c = path.with_name(f"{path.stem} ({i}){path.suffix}")
        if not c.exists():
            return c
    raise OSError("не подобрал имя")


def rename_plan(files, mode, a="", b="", start=1):
    plan = []
    for i, p in enumerate(files, start):
        n = p.name
        if mode == "prefix":
            new = a + n
        elif mode == "suffix":
            new = p.stem + a + p.suffix
        elif mode == "replace":
            new = n.replace(a, b)
        elif mode == "number":
            new = f"{a}{i:03d}{p.suffix}"
        elif mode == "lower":
            new = n.lower()
        else:
            raise ValueError("режим: prefix, suffix, replace, number, lower")
        new = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", new)
        if new != n and new:
            plan.append((p, p.with_name(new)))
    names = [t.name.lower() for _, t in plan]
    if len(set(names)) != len(names):
        raise ValueError("после переименования имена совпали, ничего не меняю")
    for s, t in plan:
        if t.exists() and t not in [x for x, _ in plan]:
            raise ValueError(f"файл «{t.name}» уже существует, ничего не меняю")
    return plan


def register(api):
    for k in ("dups_on", "zip_on", "rename_on", "big_on", "size_on"):
        api.var(k, True)
    api.var("move_dups", True)
    api.var("min_mb", 50)
    api.var("top_n", 10)

    def base():
        b = api.files_dir()
        b.mkdir(parents=True, exist_ok=True)
        return b

    def rel(b, p):
        try:
            return str(Path(p).relative_to(Path(os.path.realpath(b)))) or "."
        except ValueError:
            return str(p)

    def off(k, name):
        return None if api.config_get(k, True) else f"Функция «{name}» выключена в настройках инструмента."

    def guard(fn):
        def w(*a, **kw):
            try:
                return fn(*a, **kw)
            except PermissionError as e:
                return f"Нельзя: {e}."
            except (OSError, ValueError, zipfile.BadZipFile) as e:
                return f"Не получилось: {e}."
        return w

    @guard
    def dups(args):
        if off("dups_on", "дубликаты"):
            return off("dups_on", "дубликаты")
        b = base()
        root = inside(b, args.get("folder"))
        groups, cut = find_dups(root, b)
        if not groups:
            return "Дубликатов не нашёл." + (" (просмотрены первые 20000 файлов)" if cut else "")
        waste = sum(s * (len(g) - 1) for s, g in groups)
        lines = [f"{i}. {human(s)} × {len(g)}: " + ", ".join(rel(b, p) for p in g[:4]) + ("…" if len(g) > 4 else "") for i, (s, g) in enumerate(groups[:10], 1)]
        out = f"Групп дубликатов: {len(groups)}, лишнего места: {human(waste)}.\n" + "\n".join(lines)
        if str(args.get("move", "")).lower() in ("1", "true", "да", "yes") and api.config_get("move_dups", True):
            if not api.confirm(f"Перенести {sum(len(g) - 1 for _, g in groups)} копий в подпапку _duplicates (удаления не будет)?"):
                return out + "\nПеренос отменён."
            dst = root / "_duplicates"
            dst.mkdir(exist_ok=True)
            n = 0
            for _, g in groups:
                for p in g[1:]:
                    shutil.move(str(p), str(unique(dst / p.name)))
                    n += 1
            out += f"\nПеренёс копий: {n} в {rel(b, dst)}. Оригиналы на месте."
        return out

    @guard
    def big(args):
        if off("big_on", "большие файлы"):
            return off("big_on", "большие файлы")
        b = base()
        root = inside(b, args.get("folder"))
        try:
            n = max(1, min(int(args.get("count") or api.config_get("top_n", 10)), 30))
        except (TypeError, ValueError):
            n = 10
        files, cut = walk_files(root, b)
        sized = []
        for p in files:
            try:
                sized.append((p.stat().st_size, p))
            except OSError:
                pass
        sized.sort(key=lambda x: -x[0])
        if not sized:
            return "Папка пуста."
        return f"Самые большие файлы ({len(files)} всего):\n" + "\n".join(f"{i}. {human(s)} — {rel(b, p)}" for i, (s, p) in enumerate(sized[:n], 1))

    @guard
    def size(args):
        if off("size_on", "размер папки"):
            return off("size_on", "размер папки")
        b = base()
        root = inside(b, args.get("folder"))
        if not root.exists():
            return "Такой папки нет."
        if root.is_file():
            return f"{rel(b, root)}: {human(root.stat().st_size)}"
        files, cut = walk_files(root, b)
        tot = 0
        for p in files:
            try:
                tot += p.stat().st_size
            except OSError:
                pass
        subs = []
        for d in sorted(x for x in root.iterdir() if x.is_dir() and not x.is_symlink()):
            fs, _ = walk_files(d, b)
            subs.append((sum(p.stat().st_size for p in fs if p.exists()), d.name))
        subs.sort(reverse=True)
        out = f"{rel(b, root)}: {human(tot)}, файлов: {len(files)}" + (" (просмотр обрезан)" if cut else "")
        if subs:
            out += "\nПодпапки: " + ", ".join(f"{n} — {human(s)}" for s, n in subs[:8])
        return out

    @guard
    def zip_make(args):
        if off("zip_on", "архивы"):
            return off("zip_on", "архивы")
        b = base()
        src = inside(b, args.get("path"))
        if not src.exists():
            return "Нет такого файла или папки."
        dest = inside(b, args.get("archive") or (src.name + ".zip"))
        if dest.suffix.lower() != ".zip":
            dest = dest.with_name(dest.name + ".zip")
        dest = unique(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        n = 0
        with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as z:
            if src.is_file():
                z.write(src, src.name)
                n = 1
            else:
                for p in walk_files(src, b)[0]:
                    if p == dest:
                        continue
                    z.write(p, str(p.relative_to(src.parent)))
                    n += 1
        return f"Архив {rel(b, dest)}: {n} файлов, {human(dest.stat().st_size)}."

    @guard
    def zip_open(args):
        if off("zip_on", "архивы"):
            return off("zip_on", "архивы")
        b = base()
        src = inside(b, args.get("archive"))
        if not src.is_file():
            return "Архива нет."
        with zipfile.ZipFile(src) as z:
            infos = z.infolist()
            if str(args.get("extract", "")).lower() not in ("1", "true", "да", "yes"):
                lines = [f"{i.filename} ({human(i.file_size)})" for i in infos[:30]]
                return f"В архиве {len(infos)} элементов:\n" + "\n".join(lines) + ("\n…" if len(infos) > 30 else "")
            if sum(i.file_size for i in infos) > 2 * 1024 ** 3 or len(infos) > 20000:
                return "Архив слишком большой для распаковки (лимит 2 ГБ и 20000 файлов)."
            dest = unique(inside(b, args.get("to") or src.with_suffix("").name))
            dest.mkdir(parents=True, exist_ok=True)
            root = Path(os.path.realpath(dest))
            n = 0
            for i in infos:
                t = Path(os.path.realpath(root / i.filename))
                try:
                    t.relative_to(root)
                except ValueError:
                    continue            # попытка выйти за папку (../) — пропускаем
                if i.is_dir():
                    t.mkdir(parents=True, exist_ok=True)
                    continue
                t.parent.mkdir(parents=True, exist_ok=True)
                with z.open(i) as s, open(t, "wb") as o:
                    shutil.copyfileobj(s, o)
                n += 1
        return f"Распаковал {n} файлов в {rel(b, dest)}."

    @guard
    def rename(args):
        if off("rename_on", "переименование"):
            return off("rename_on", "переименование")
        b = base()
        root = inside(b, args.get("folder"))
        if not root.is_dir():
            return "Такой папки нет."
        mode = str(args.get("mode", "")).strip().lower()
        pat = str(args.get("mask") or "*")
        files = sorted(p for p in root.glob(pat) if p.is_file() and not p.is_symlink())[:500]
        plan = rename_plan(files, mode, str(args.get("text", "")), str(args.get("replace_with", "")))
        if not plan:
            return "Нечего переименовывать."
        preview = "\n".join(f"{s.name} → {t.name}" for s, t in plan[:8]) + (f"\n…и ещё {len(plan) - 8}" if len(plan) > 8 else "")
        if not api.confirm(f"Переименовать {len(plan)} файлов в «{rel(b, root)}»?\n\n{preview}"):
            return "Отменено, ничего не менял."
        done = []
        try:
            for s, t in plan:
                s.rename(t)
                done.append((s, t))
        except OSError as e:
            for s, t in reversed(done):
                try:
                    t.rename(s)
                except OSError:
                    pass
            return f"Ошибка «{e}», всё вернул как было."
        return f"Переименовал файлов: {len(plan)}.\n{preview}"

    P = {"folder": {"type": "string", "description": "подпапка внутри папки файлов агента (пусто — вся папка)"}}
    api.add_tool("find_duplicates", "Найти дубликаты файлов (по размеру и хешу) в папке файлов агента. move=true — перенести копии в _duplicates (после подтверждения, без удаления).",
                 dict(P, move={"type": "boolean"}), dups)
    api.add_tool("find_big_files", "Самые большие файлы в папке файлов агента.", dict(P, count={"type": "integer"}), big)
    api.add_tool("folder_size", "Размер папки или файла в папке файлов агента и по подпапкам.", P, size)
    api.add_tool("make_zip", "Упаковать файл или папку (внутри папки файлов агента) в zip.",
                 {"path": {"type": "string"}, "archive": {"type": "string", "description": "имя архива, необязательно"}}, zip_make, ["path"])
    api.add_tool("zip_contents", "Показать содержимое zip-архива или распаковать его (extract=true) в новую подпапку.",
                 {"archive": {"type": "string"}, "extract": {"type": "boolean"}, "to": {"type": "string"}}, zip_open, ["archive"])
    api.add_tool("rename_files", "Массовое переименование файлов папки с показом списка и подтверждением. mode: prefix (добавить text в начало), "
                 "suffix (перед расширением), replace (заменить text на replace_with), number (text + 001, 002…), lower (в нижний регистр).",
                 {"folder": {"type": "string"}, "mode": {"type": "string"}, "text": {"type": "string"},
                  "replace_with": {"type": "string"}, "mask": {"type": "string", "description": "маска, например *.jpg"}}, rename, ["folder", "mode"])

    def command(text, m):
        t = api.norm_ru(text).strip()
        rest = lambda mm: (mm.group(1) if mm and mm.groups() and mm.group(1) else "").strip(" «»\"'")
        if re.match(r"^найди дубликаты|^есть дубликаты", t):
            return dups({})
        if re.match(r"^найди (?:самые )?больш\w+ файлы", t):
            return big({})
        mm = re.match(r"^(?:сколько весит|размер папки|размер файла)\s+(.+)$", t)
        if mm:
            return size({"folder": rest(mm)})
        mm = re.match(r"^(?:упакуй|заархивируй|запакуй)\s+(?:папку |файл )?(.+?)(?: в zip| в архив)?$", t)
        if mm:
            return zip_make({"path": rest(mm)})
        mm = re.match(r"^распакуй\s+(?:архив )?(.+)$", t)
        if mm:
            return zip_open({"archive": rest(mm), "extract": True})
        mm = re.match(r"^что в архиве\s+(.+)$", t)
        if mm:
            return zip_open({"archive": rest(mm)})
        return None

    api.add_command(r"(?:найди дубликаты|есть дубликаты|найди (?:самые )?больш\w+ файлы|сколько весит|размер папки|размер файла|упакуй|заархивируй|запакуй|распакуй|что в архиве)",
                    command, "найди дубликаты / сколько весит папка … / упакуй … / распакуй архив …")

    def build(fr):
        from tkinter import ttk
        pad = {"padx": 6, "pady": 2}
        for i, (k, txt) in enumerate((("dups_on", "Поиск дубликатов (по размеру и хешу)"),
                                      ("move_dups", "Разрешить переносить копии в _duplicates (после подтверждения; не удаляет)"),
                                      ("big_on", "Самые большие файлы"), ("size_on", "Размер папки"),
                                      ("zip_on", "Архивы: упаковать, посмотреть, распаковать (zip)"),
                                      ("rename_on", "Массовое переименование (показ списка и подтверждение)"))):
            ttk.Checkbutton(fr, text=txt, variable=api.var(k)).grid(row=i, column=0, columnspan=3, sticky="w", **pad)
        ttk.Label(fr, text="Сколько файлов в списке «больших»:").grid(row=6, column=0, sticky="w", **pad)
        ttk.Spinbox(fr, from_=1, to=30, width=5, textvariable=api.var("top_n")).grid(row=6, column=1, sticky="w", **pad)
        ttk.Label(fr, text=("Работает только внутри папки файлов агента (вкладка «Функции»): пути с .. и ссылки наружу блокируются. "
                            "Файлы не удаляются. Перед просмотром обрезается на 20000 файлах. Распаковка отсекает записи, выходящие за папку."),
                  foreground="#777", wraplength=720, justify="left").grid(row=7, column=0, columnspan=3, sticky="w", **pad)
    api.add_settings(build)
