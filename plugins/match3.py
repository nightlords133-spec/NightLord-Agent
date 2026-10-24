# Плагин Nightlord агента: помощник для игр «3 в ряд» в браузере (и не только).
# Смотрит на экран, режет поле на клетки, определяет тип фишки по цвету, перебирает все обмены соседних клеток и называет лучший ход.
# По желанию сам делает ход мышью (по умолчанию выключено). Не читает память игры и не лезёт в браузер: только скриншот и мышь, как человек.
import colorsys
import math
import platform
import threading
import time

PLUGIN = {"name": "Три в ряд", "version": "1.0",
          "description": "Подсказывает лучший ход в играх «3 в ряд» по скриншоту: «лучший ход», «подскажи ход», «сделай ход», «играй автоматически 20 ходов», «стоп игру». "
                         "Сначала запомните поле: наведите мышь на левый верхний угол поля и скажите «запомни левый верхний угол», потом так же правый нижний. "
                         "Размер по умолчанию 8×8 («размер поля 9 на 7»). Нужен Pillow; автоход — только Windows."}

USER32 = None


def user32():
    global USER32
    if USER32 is None:
        import ctypes
        USER32 = ctypes.windll.user32
    return USER32


# ---------------------------------------------------------------- работа с экраном (подменяется в тестах)
def grab_region(bbox):
    from PIL import ImageGrab
    return ImageGrab.grab(bbox=bbox, all_screens=True).convert("RGB")


def get_cursor():
    if platform.system() != "Windows":
        raise RuntimeError("положение мыши читаю только в Windows: впишите координаты поля в настройках плагина")
    import ctypes
    from ctypes import wintypes
    p = wintypes.POINT()
    user32().GetCursorPos(ctypes.byref(p))
    return p.x, p.y


def click_at(x, y, hold=0.05):
    if platform.system() != "Windows":
        raise RuntimeError("клики мышью поддерживаются только в Windows")
    u = user32()
    u.SetCursorPos(int(x), int(y))
    time.sleep(0.03)
    u.mouse_event(0x0002, 0, 0, 0, 0)
    time.sleep(hold)
    u.mouse_event(0x0004, 0, 0, 0, 0)


def drag(x1, y1, x2, y2, steps=8, hold=0.03):
    if platform.system() != "Windows":
        raise RuntimeError("клики мышью поддерживаются только в Windows")
    u = user32()
    u.SetCursorPos(int(x1), int(y1))
    time.sleep(0.04)
    u.mouse_event(0x0002, 0, 0, 0, 0)
    for i in range(1, steps + 1):
        u.SetCursorPos(int(x1 + (x2 - x1) * i / steps), int(y1 + (y2 - y1) * i / steps))
        time.sleep(hold)
    u.mouse_event(0x0004, 0, 0, 0, 0)


# ---------------------------------------------------------------- распознавание поля
def cell_features(img, rows, cols, inner=0.5):
    """Средний цвет центральной части каждой клетки -> [[(r,g,b)]]."""
    from PIL import Image
    W, H = img.size
    cw, ch = W / cols, H / rows
    out = []
    for r in range(rows):
        row = []
        for c in range(cols):
            cx, cy = (c + 0.5) * cw, (r + 0.5) * ch
            hw, hh = cw * inner / 2, ch * inner / 2
            box = (int(cx - hw), int(cy - hh), max(int(cx + hw), int(cx - hw) + 1), max(int(cy + hh), int(cy - hh) + 1))
            px = img.crop(box).resize((1, 1), Image.BOX).getpixel((0, 0))
            row.append(tuple(px[:3]))
        out.append(row)
    return out


def dist(a, b):
    return math.sqrt(sum((x - y) ** 2 for x, y in zip(a, b)))


def cluster_colors(feats, thr=45.0, max_types=10):
    """Жадная кластеризация цветов клеток -> (сетка номеров типов, центры). Похожие цвета склеиваются."""
    cents, cnt = [], []
    flat = [(r, c, f) for r, row in enumerate(feats) for c, f in enumerate(row)]
    for _, _, f in flat:
        best, bd = None, 1e9
        for i, ce in enumerate(cents):
            d = dist(f, ce)
            if d < bd:
                best, bd = i, d
        if best is not None and bd < thr:
            n = cnt[best]
            cents[best] = tuple((ce * n + x) / (n + 1) for ce, x in zip(cents[best], f))
            cnt[best] += 1
        else:
            cents.append(tuple(float(x) for x in f))
            cnt.append(1)
    # переназначение по ближайшему центру и слияние почти одинаковых центров
    for _ in range(2):
        new = {}
        for _, _, f in flat:
            i = min(range(len(cents)), key=lambda k: dist(f, cents[k]))
            new.setdefault(i, []).append(f)
        cents = [tuple(sum(v[j] for v in vs) / len(vs) for j in range(3)) for i, vs in sorted(new.items())]
    merged = True
    while merged and len(cents) > 1:
        merged = False
        for i in range(len(cents)):
            for j in range(i + 1, len(cents)):
                if dist(cents[i], cents[j]) < thr * 0.55:
                    cents[i] = tuple((a + b) / 2 for a, b in zip(cents[i], cents[j]))
                    del cents[j]
                    merged = True
                    break
            if merged:
                break
    grid = [[min(range(len(cents)), key=lambda k: dist(f, cents[k])) for f in row] for row in feats]
    return grid, cents


def color_name(rgb):
    r, g, b = [x / 255 for x in rgb]
    h, s, v = colorsys.rgb_to_hsv(r, g, b)
    if v < 0.22:
        return "чёрный"
    if s < 0.18:
        return "белый" if v > 0.8 else "серый"
    h *= 360
    if h < 15 or h >= 345:
        return "красный"
    if h < 40:
        return "оранжевый" if v > 0.5 else "коричневый"
    if h < 70:
        return "жёлтый"
    if h < 160:
        return "зелёный"
    if h < 200:
        return "бирюзовый"
    if h < 255:
        return "синий"
    if h < 290:
        return "фиолетовый"
    return "розовый"


def type_names(cents):
    names, used = [], {}
    for ce in cents:
        n = color_name(ce)
        used[n] = used.get(n, 0) + 1
        names.append(n if used[n] == 1 else f"{n} {used[n]}")
    return names


# ---------------------------------------------------------------- логика игры
def find_runs(grid):
    """Все линии из 3+ одинаковых: [(клетки, длина, направление)]."""
    R, C = len(grid), len(grid[0])
    runs = []
    for r in range(R):
        c = 0
        while c < C:
            t = grid[r][c]
            e = c
            while e + 1 < C and t is not None and grid[r][e + 1] == t:
                e += 1
            if t is not None and e - c + 1 >= 3:
                runs.append(([(r, k) for k in range(c, e + 1)], e - c + 1, "h"))
            c = e + 1
    for c in range(C):
        r = 0
        while r < R:
            t = grid[r][c]
            e = r
            while e + 1 < R and t is not None and grid[e + 1][c] == t:
                e += 1
            if t is not None and e - r + 1 >= 3:
                runs.append(([(k, c) for k in range(r, e + 1)], e - r + 1, "v"))
            r = e + 1
    return runs


def run_score(runs):
    cells = set()
    for cs, _, _ in runs:
        cells.update(cs)
    pts = len(cells)
    for cs, ln, _ in runs:
        pts += 3 if ln == 4 else 8 if ln >= 5 else 0
    # пересечения линий (угол «Г», «Т») дают фигурную бомбу
    for i in range(len(runs)):
        for j in range(i + 1, len(runs)):
            if runs[i][2] != runs[j][2] and set(runs[i][0]) & set(runs[j][0]):
                pts += 4
    return pts, cells


def collapse(grid, cells):
    R, C = len(grid), len(grid[0])
    g = [row[:] for row in grid]
    for r, c in cells:
        g[r][c] = None
    for c in range(C):
        col = [g[r][c] for r in range(R) if g[r][c] is not None]
        col = [None] * (R - len(col)) + col
        for r in range(R):
            g[r][c] = col[r]
    return g


def move_value(grid, a, b, cascade_weight=0.7):
    """Очки за обмен клеток a и b (0, если ничего не собирается). Каскады считаются только по уже известным фишкам."""
    g = [row[:] for row in grid]
    (r1, c1), (r2, c2) = a, b
    if g[r1][c1] is None or g[r2][c2] is None or g[r1][c1] == g[r2][c2]:
        return 0, []
    g[r1][c1], g[r2][c2] = g[r2][c2], g[r1][c1]
    runs = find_runs(g)
    if not runs:
        return 0, []
    total, cells = run_score(runs)
    first = runs
    w = 1.0
    cur = g
    for _ in range(6):
        cur = collapse(cur, cells)
        runs = find_runs(cur)
        if not runs:
            break
        w *= cascade_weight
        pts, cells = run_score(runs)
        total += pts * w
    return total, first


def best_moves(grid, prefer="points", top=3):
    R, C = len(grid), len(grid[0])
    out = []
    for r in range(R):
        for c in range(C):
            for dr, dc in ((0, 1), (1, 0)):
                r2, c2 = r + dr, c + dc
                if r2 >= R or c2 >= C:
                    continue
                v, runs = move_value(grid, (r, c), (r2, c2))
                if v > 0:
                    # при равенстве очков: «сверху» — ходы выше (меньше случайных каскадов), «очки» — как есть
                    tie = -(r + r2) * 0.001 if prefer == "top" else (r + r2) * 0.001 if prefer == "bottom" else 0
                    out.append((v + tie, (r, c), (r2, c2), runs))
    out.sort(key=lambda m: -m[0])
    return out[:top]


def describe_move(m, names, grid):
    v, a, b, runs = m
    (r1, c1), (r2, c2) = a, b
    where = "вправо" if c2 > c1 else "вниз"
    ln = max((x[1] for x in runs), default=3)
    t = grid[r1][c1]
    tname = names[grid[r2][c2]] if grid[r1][c1] is not None else ""
    s = (f"Поменяй клетку в строке {r1 + 1}, столбце {c1 + 1} с соседней {where}"
         f" (строка {r2 + 1}, столбец {c2 + 1})")
    if ln >= 5:
        s += f": получится ряд из {ln}, это бонус"
    elif ln == 4:
        s += ": получится ряд из 4"
    else:
        s += ": соберётся три в ряд"
    return s + f" (оценка {v:.0f})."


def grid_text(grid, names=None):
    letters = "ABCDEFGHIJKLMNOP"
    return "\n".join(" ".join("." if t is None else letters[t % len(letters)] for t in row) for row in grid)


def annotate(img, rows, cols, move, path):
    from PIL import ImageDraw
    im = img.copy()
    d = ImageDraw.Draw(im)
    W, H = im.size
    cw, ch = W / cols, H / rows
    for i in range(1, cols):
        d.line([(i * cw, 0), (i * cw, H)], fill=(255, 255, 255), width=1)
    for i in range(1, rows):
        d.line([(0, i * ch), (W, i * ch)], fill=(255, 255, 255), width=1)
    if move:
        _, (r1, c1), (r2, c2), _ = move
        p1 = ((c1 + 0.5) * cw, (r1 + 0.5) * ch)
        p2 = ((c2 + 0.5) * cw, (r2 + 0.5) * ch)
        d.line([p1, p2], fill=(255, 0, 0), width=4)
        for p in (p1, p2):
            d.ellipse([p[0] - 8, p[1] - 8, p[0] + 8, p[1] + 8], outline=(255, 0, 0), width=3)
    im.save(path)


# ---------------------------------------------------------------- плагин
def register(api):
    api.var("x1", 0)
    api.var("y1", 0)
    api.var("x2", 0)
    api.var("y2", 0)
    api.var("rows", 8)
    api.var("cols", 8)
    api.var("threshold", 45)
    api.var("prefer", "points")        # points | top | bottom
    api.var("auto_click", False)       # разрешить «сделай ход» и «играй автоматически»
    api.var("click_mode", "click")     # click | drag
    api.var("move_delay", 1.6)         # пауза после хода (анимация), с
    api.var("max_auto", 20)
    api.var("save_debug", True)
    stop = threading.Event()
    auto = {"thread": None}

    def num(k, d, lo, hi, typ=int):
        try:
            return max(lo, min(typ(api.config_get(k, d) or d), hi))
        except (TypeError, ValueError):
            return d

    def region():
        x1, y1, x2, y2 = (num(k, 0, -20000, 20000) for k in ("x1", "y1", "x2", "y2"))
        if x2 - x1 < 60 or y2 - y1 < 60:
            raise RuntimeError("поле не задано: наведите мышь на левый верхний угол поля и скажите «запомни левый верхний угол», затем так же правый нижний")
        return x1, y1, x2, y2

    def dims():
        return num("rows", 8, 3, 15), num("cols", 8, 3, 15)

    def analyze(save=True):
        """-> (grid, names, moves, img, bbox)"""
        bbox = region()
        rows, cols = dims()
        try:
            img = grab_region(bbox)
        except ImportError:
            raise RuntimeError("нужен Pillow: py -3.12 -m pip install pillow") from None
        feats = cell_features(img, rows, cols)
        grid, cents = cluster_colors(feats, float(num("threshold", 45, 10, 120)))
        if len(cents) > 10:
            raise RuntimeError(f"нашёл {len(cents)} разных цветов: похоже, границы поля заданы неточно или в кадре анимация. Проверьте углы и размер поля")
        if len(cents) < 2:
            raise RuntimeError("все клетки одного цвета: проверьте, что рамка поля на игре, а не на пустом месте")
        names = type_names(cents)
        moves = best_moves(grid, str(api.config_get("prefer", "points")))
        if save and api.config_get("save_debug", True):
            try:
                d = api.data_dir
                d.mkdir(parents=True, exist_ok=True)
                annotate(img, rows, cols, moves[0] if moves else None, str(d / "last.png"))
            except Exception:
                pass
        return grid, names, moves, img, bbox

    def screen_xy(bbox, move):
        rows, cols = dims()
        x1, y1, x2, y2 = bbox
        cw, ch = (x2 - x1) / cols, (y2 - y1) / rows
        _, (r1, c1), (r2, c2), _ = move
        return ((x1 + (c1 + 0.5) * cw, y1 + (r1 + 0.5) * ch), (x1 + (c2 + 0.5) * cw, y1 + (r2 + 0.5) * ch))

    def do_move(bbox, move):
        (ax, ay), (bx, by) = screen_xy(bbox, move)
        if str(api.config_get("click_mode", "click")) == "drag":
            drag(ax, ay, bx, by)
        else:
            click_at(ax, ay)
            time.sleep(0.25)
            click_at(bx, by)

    # ------------------------------------------------------------ команды
    def hint(args):
        try:
            grid, names, moves, _, _ = analyze()
        except RuntimeError as e:
            return f"Не получилось: {e}."
        except Exception as e:
            return f"Не получилось посмотреть на поле: {e}."
        kinds = ", ".join(names)
        if not moves:
            return f"Ходов, которые собирают три в ряд, не вижу (типов фишек: {len(names)}: {kinds}). Возможно, поле перемешается, или в кадре анимация."
        out = describe_move(moves[0], names, grid)
        if len(moves) > 1:
            out += " Запасные: " + "; ".join(f"({m[1][0] + 1},{m[1][1] + 1})↔({m[2][0] + 1},{m[2][1] + 1})" for m in moves[1:])
        return out + f" Типов фишек: {len(names)}."

    def make_move(args):
        if not api.config_get("auto_click", False):
            return "Автоходы выключены. Включите «Делать ходы мышью» в настройках плагина «Три в ряд»."
        try:
            grid, names, moves, _, bbox = analyze()
            if not moves:
                return "Ходов не вижу."
            do_move(bbox, moves[0])
        except RuntimeError as e:
            return f"Не получилось: {e}."
        except Exception as e:
            return f"Не получилось сделать ход: {e}."
        _, (r1, c1), (r2, c2), _ = moves[0]
        return f"Сделал ход: поменял клетки (строка {r1 + 1}, столбец {c1 + 1}) и (строка {r2 + 1}, столбец {c2 + 1})."

    def autoplay(args):
        if not api.config_get("auto_click", False):
            return "Автоходы выключены. Включите «Делать ходы мышью» в настройках плагина «Три в ряд»."
        if auto["thread"] and auto["thread"].is_alive():
            return "Уже играю. Скажите «стоп игру», чтобы остановить."
        try:
            n = max(1, min(int(args.get("moves") or api.config_get("max_auto", 20)), 200))
        except (TypeError, ValueError):
            n = num("max_auto", 20, 1, 200)
        try:
            region()
        except RuntimeError as e:
            return f"Не получилось: {e}."
        stop.clear()

        def loop():
            done, same, reason = 0, 0, "готово"
            last = None
            try:
                while done < n and not stop.is_set():
                    try:
                        if platform.system() == "Windows" and get_cursor() == (0, 0):
                            reason = "мышь в левом верхнем углу экрана — аварийная остановка"
                            break
                    except RuntimeError:
                        pass
                    grid, names, moves, _, bbox = analyze(save=(done == 0))
                    key = str(grid)
                    same = same + 1 if key == last else 0
                    last = key
                    if same >= 2:
                        reason = "поле не меняется: возможно, окно игры потеряло фокус или ход не засчитался"
                        break
                    if not moves:
                        reason = "ходов нет"
                        break
                    do_move(bbox, moves[0])
                    done += 1
                    for _ in range(int(num("move_delay", 1.6, 0.3, 8, float) * 10)):
                        if stop.is_set():
                            break
                        time.sleep(0.1)
                else:
                    reason = "остановлен" if stop.is_set() else "сделал нужное число ходов"
            except Exception as e:
                reason = f"ошибка: {e}"
            api.say(f"Игра остановлена: {reason}. Ходов сделано: {done}.")
        t = threading.Thread(target=loop, daemon=True)
        auto["thread"] = t
        t.start()
        return f"Играю до {n} ходов. Чтобы остановить, скажите «стоп игру» или уведите мышь в левый верхний угол экрана."

    def stop_game(args):
        if auto["thread"] and auto["thread"].is_alive():
            stop.set()
            return "Останавливаю игру."
        return "Игра сейчас не идёт."

    def board(args):
        try:
            grid, names, moves, _, _ = analyze()
        except RuntimeError as e:
            return f"Не получилось: {e}."
        except Exception as e:
            return f"Не получилось: {e}."
        legend = ", ".join(f"{'ABCDEFGHIJKLMNOP'[i]} — {n}" for i, n in enumerate(names))
        return f"Поле {len(grid)}×{len(grid[0])}:\n{grid_text(grid)}\n{legend}"

    def set_corner(which):
        try:
            x, y = get_cursor()
        except RuntimeError as e:
            return f"Не получилось: {e}."
        a, b = ("x1", "y1") if which == "tl" else ("x2", "y2")
        api.config_set(a, x)
        api.config_set(b, y)
        return f"Запомнил {'левый верхний' if which == 'tl' else 'правый нижний'} угол: ({x}, {y})."

    def set_size(r, c):
        r, c = int(r), int(c)
        if not (3 <= r <= 15 and 3 <= c <= 15):
            return "Размер поля от 3 до 15 клеток по каждой стороне."
        api.config_set("rows", r)
        api.config_set("cols", c)
        return f"Размер поля: {r} строк × {c} столбцов."

    api.add_tool("match3_hint", "Посмотреть на поле игры «3 в ряд» на экране и назвать лучший ход (какие две соседние клетки поменять).", {}, hint)
    api.add_tool("match3_move", "Сделать лучший ход в игре «3 в ряд» мышью (нужно разрешение в настройках плагина).", {}, make_move)
    api.add_tool("match3_autoplay", "Играть в «3 в ряд» автоматически заданное число ходов (нужно разрешение в настройках плагина).",
                 {"moves": {"type": "integer"}}, autoplay)
    api.add_tool("match3_stop", "Остановить автоматическую игру в «3 в ряд».", {}, stop_game)
    api.add_tool("match3_board", "Показать, как плагин видит поле «3 в ряд» (буквы — типы фишек).", {}, board)

    import re

    def command(text, m):
        t = api.norm_ru(text).strip().rstrip("?.!")
        if re.match(r"^запомни левый верхний угол", t):
            return set_corner("tl")
        if re.match(r"^запомни правый нижний угол", t):
            return set_corner("br")
        mm = re.match(r"^размер поля (\d+) (?:на|х|x) (\d+)", t)
        if mm:
            return set_size(mm.group(1), mm.group(2))
        if re.match(r"^(?:стоп|останови|прекрати) игр\w*", t):
            return stop_game({})
        mm = re.match(r"^играй автоматически(?: (\d+) ход\w*)?", t)
        if mm:
            return autoplay({"moves": mm.group(1)})
        if re.match(r"^(?:сделай|сыграй) ход", t):
            return make_move({})
        if re.match(r"^(?:покажи|как ты видишь) поле", t):
            return board({})
        if re.match(r"^(?:лучший ход|подскажи ход|какой ход|что делать в три в ряд|помоги с игрой|подскажи в три в ряд)", t):
            return hint({})
        return None

    api.add_command(r"(?:запомни (?:левый верхний|правый нижний) угол|размер поля \d|(?:стоп|останови|прекрати) игр|играй автоматически|(?:сделай|сыграй) ход|"
                    r"(?:покажи|как ты видишь) поле|лучший ход|подскажи ход|какой ход|что делать в три в ряд|помоги с игрой|подскажи в три в ряд)",
                    command, "лучший ход / сделай ход / играй автоматически 20 ходов / стоп игру / запомни левый верхний угол")

    api.on_unload(stop.set)

    def build(fr):
        from tkinter import ttk
        pad = {"padx": 6, "pady": 2}
        ttk.Label(fr, text="Поле на экране (пиксели): левый верхний угол x, y и правый нижний x, y").grid(row=0, column=0, columnspan=6, sticky="w", **pad)
        for i, k in enumerate(("x1", "y1", "x2", "y2")):
            ttk.Entry(fr, textvariable=api.var(k), width=7).grid(row=1, column=i, sticky="w", **pad)

        def grab_corner(which):
            def work():
                time.sleep(3)
                try:
                    x, y = get_cursor()
                except RuntimeError as e:
                    api.log(str(e))
                    return
                kx, ky = ("x1", "y1") if which == "tl" else ("x2", "y2")
                api.post(lambda: (api.var(kx).set(x), api.var(ky).set(y)))
                api.log(f"угол {'левый верхний' if which == 'tl' else 'правый нижний'}: ({x}, {y})")
            threading.Thread(target=work, daemon=True).start()
        ttk.Button(fr, text="Левый верхний угол (через 3 с)", command=lambda: grab_corner("tl")).grid(row=1, column=4, sticky="w", **pad)
        ttk.Button(fr, text="Правый нижний (через 3 с)", command=lambda: grab_corner("br")).grid(row=1, column=5, sticky="w", **pad)
        ttk.Label(fr, text="Размер поля (строк × столбцов):").grid(row=2, column=0, columnspan=2, sticky="w", **pad)
        ttk.Spinbox(fr, from_=3, to=15, width=4, textvariable=api.var("rows")).grid(row=2, column=2, sticky="w", **pad)
        ttk.Spinbox(fr, from_=3, to=15, width=4, textvariable=api.var("cols")).grid(row=2, column=3, sticky="w", **pad)
        ttk.Label(fr, text="Чувствительность цвета (меньше — больше типов):").grid(row=3, column=0, columnspan=3, sticky="w", **pad)
        ttk.Spinbox(fr, from_=10, to=120, width=5, textvariable=api.var("threshold")).grid(row=3, column=3, sticky="w", **pad)
        ttk.Label(fr, text="Предпочитать ходы:").grid(row=4, column=0, sticky="w", **pad)
        pn = {"points": "по очкам", "top": "сверху", "bottom": "снизу"}
        cb = ttk.Combobox(fr, values=list(pn.values()), state="readonly", width=12)
        cb.set(pn.get(str(api.config_get("prefer", "points")), pn["points"]))
        cb.grid(row=4, column=1, columnspan=2, sticky="w", **pad)
        cb.bind("<<ComboboxSelected>>", lambda e: api.var("prefer").set(list(pn)[cb.current()]))
        ttk.Checkbutton(fr, text="Делать ходы мышью («сделай ход», «играй автоматически»). Мышь будет двигаться сама!", variable=api.var("auto_click")).grid(
            row=5, column=0, columnspan=6, sticky="w", **pad)
        ttk.Label(fr, text="Способ хода:").grid(row=6, column=0, sticky="w", **pad)
        cm = {"click": "два клика", "drag": "перетаскивание"}
        cb2 = ttk.Combobox(fr, values=list(cm.values()), state="readonly", width=16)
        cb2.set(cm.get(str(api.config_get("click_mode", "click")), cm["click"]))
        cb2.grid(row=6, column=1, columnspan=2, sticky="w", **pad)
        cb2.bind("<<ComboboxSelected>>", lambda e: api.var("click_mode").set(list(cm)[cb2.current()]))
        ttk.Label(fr, text="Пауза после хода, с / макс. ходов:").grid(row=7, column=0, columnspan=2, sticky="w", **pad)
        ttk.Entry(fr, textvariable=api.var("move_delay"), width=6).grid(row=7, column=2, sticky="w", **pad)
        ttk.Spinbox(fr, from_=1, to=200, width=5, textvariable=api.var("max_auto")).grid(row=7, column=3, sticky="w", **pad)
        ttk.Checkbutton(fr, text="Сохранять снимок с разметкой (plugins/data/match3/last.png) — чтобы проверить, как плагин видит поле",
                        variable=api.var("save_debug")).grid(row=8, column=0, columnspan=6, sticky="w", **pad)
        ttk.Label(fr, text=("Как настроить: откройте игру, наведите мышь на левый верхний угол поля и скажите «запомни левый верхний угол», то же для правого "
                            "нижнего, скажите «размер поля 8 на 8», затем «покажи поле» (буквы — типы фишек) и «лучший ход». Особые фишки (бомбы, полосатые) отдельно "
                            "не различаются. Если в Windows включён масштаб экрана выше 100%, координаты мыши и снимка могут расходиться. Играйте только там, где "
                            "боты не запрещены правилами. Аварийная остановка: уведите мышь в левый верхний угол экрана. Я проверял логику на искусственных "
                            "картинках, живую игру в браузере — нет."),
                  foreground="#777", wraplength=720, justify="left").grid(row=9, column=0, columnspan=6, sticky="w", **pad)
    api.add_settings(build)
