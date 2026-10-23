# Инструмент Nightlord агента: калькулятор, конвертер единиц и расчёты с датами. Всё считается локально и точно,
# без интернета: модель сама часто ошибается в арифметике, поэтому считает этот инструмент.
import ast
import math
import operator
import re
from datetime import date, datetime, timedelta

PLUGIN = {"name": "Калькулятор и конвертер", "version": "1.0", "group": "tools",
          "description": "Точный счёт и перевод величин без интернета: «посчитай 15 процентов от 2300», «сколько будет 12 умножить на 7», "
                         "«переведи 5 миль в километры», «сколько градусов по Фаренгейту в 20 Цельсия», «сколько дней до 1 января», "
                         "«какой день недели 5 марта», «через 45 дней будет». Корни, степени, функции, длина, масса, объём, площадь, "
                         "скорость, данные, время, температура, давление, мощность."}

# ---------------------------------------------------------------- калькулятор
BIN = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv,
       ast.FloorDiv: operator.floordiv, ast.Mod: operator.mod, ast.Pow: operator.pow}
FUNCS = {"sqrt": math.sqrt, "sin": math.sin, "cos": math.cos, "tan": math.tan, "asin": math.asin, "acos": math.acos,
         "atan": math.atan, "log": math.log, "log10": math.log10, "log2": math.log2, "ln": math.log, "exp": math.exp,
         "abs": abs, "round": round, "floor": math.floor, "ceil": math.ceil, "factorial": math.factorial,
         "radians": math.radians, "degrees": math.degrees, "min": min, "max": max, "cbrt": lambda x: math.copysign(abs(x) ** (1 / 3), x)}
CONST = {"pi": math.pi, "e": math.e, "tau": math.tau}
NUM = r"\d+(?:[.,]\d+)?"


class CalcError(Exception):
    pass


def to_expr(text):
    """Русская фраза или обычная запись -> выражение для безопасного вычисления."""
    t = str(text).lower().replace("ё", "е").strip().rstrip("?=.! ")
    t = re.sub(r"^(?:сколько будет|сколько получится|сколько|чему равно|чему равен|посчитай|вычисли|реши|calc)\s+", "", t)
    t = t.replace("×", "*").replace("÷", "/").replace("−", "-").replace("–", "-").replace("^", "**")
    t = re.sub(rf"({NUM})\s*(?:%|процент\w*)\s*от\s*({NUM})", r"(\1/100*\2)", t)
    t = re.sub(rf"({NUM})\s*(плюс|\+|минус|-)\s*({NUM})\s*(?:%|процент\w*)",
               lambda m: f"({m.group(1)}*(1{'+' if m.group(2) in ('плюс', '+') else '-'}{m.group(3)}/100))", t)
    for w, sym in (("умножить на", "*"), ("умножь на", "*"), ("помножить на", "*"), ("разделить на", "/"),
                   ("поделить на", "/"), ("делить на", "/"), ("деленное на", "/"), ("в степени", "**"),
                   ("в квадрате", "**2"), ("в кубе", "**3"), ("по модулю", "%"), ("плюс", "+"), ("минус", "-"),
                   ("умножить", "*"), ("разделить", "/")):
        t = t.replace(w, f" {sym} ")
    t = re.sub(r"корень\s+(?:квадратный\s+)?из\s*(\d+(?:[.,]\d+)?|\([^)]*\))", r"sqrt(\1)", t)
    t = re.sub(r"корень\s+кубический\s+из\s*(\d+(?:[.,]\d+)?|\([^)]*\))", r"cbrt(\1)", t)
    t = re.sub(r"факториал\s*(?:числа\s*)?(\d+)", r"factorial(\1)", t)
    t = re.sub(r"\bпи\b", "pi", t)
    t = re.sub(r"(?<=[\d)])\s*[xх]\s*(?=[\d(])", "*", t)
    t = re.sub(r"(?<=\d)\s*:\s*(?=\d)", "/", t)
    if not re.search(r"[a-z]+\(", t):
        t = re.sub(r"(\d),(\d)", r"\1.\2", t)
    t = re.sub(r"(?<=\d) (?=\d{3}(?!\d))", "", t)
    return re.sub(r"\s+", " ", t).strip()


def safe_eval(expr):
    expr = str(expr).strip()
    if not expr or len(expr) > 300:
        raise CalcError("пустое или слишком длинное выражение")
    try:
        tree = ast.parse(expr, mode="eval")
    except SyntaxError:
        raise CalcError("не смог разобрать выражение") from None
    if sum(1 for _ in ast.walk(tree)) > 200:
        raise CalcError("выражение слишком сложное")

    def ev(n):
        if isinstance(n, ast.Expression):
            return ev(n.body)
        if isinstance(n, ast.Constant) and isinstance(n.value, (int, float)) and not isinstance(n.value, bool):
            return n.value
        if isinstance(n, ast.BinOp) and type(n.op) in BIN:
            a, b = ev(n.left), ev(n.right)
            if isinstance(n.op, ast.Pow):
                if abs(b) > 1000 or (abs(a) > 1e6 and abs(b) > 50):
                    raise CalcError("слишком большая степень")
            return BIN[type(n.op)](a, b)
        if isinstance(n, ast.UnaryOp) and isinstance(n.op, (ast.UAdd, ast.USub)):
            v = ev(n.operand)
            return v if isinstance(n.op, ast.UAdd) else -v
        if isinstance(n, ast.Name) and n.id in CONST:
            return CONST[n.id]
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id in FUNCS and not n.keywords:
            args = [ev(a) for a in n.args]
            if n.func.id == "factorial" and (len(args) != 1 or args[0] > 170 or args[0] != int(args[0])):
                raise CalcError("факториал считаю для целых до 170")
            if n.func.id == "factorial":
                args = [int(args[0])]
            return FUNCS[n.func.id](*args)
        raise CalcError("в выражении есть то, что я не считаю")
    try:
        r = ev(tree)
    except ZeroDivisionError:
        raise CalcError("деление на ноль") from None
    except (ValueError, OverflowError, TypeError) as e:
        raise CalcError(f"так посчитать нельзя ({type(e).__name__})") from None
    if isinstance(r, complex):
        raise CalcError("результат не вещественный")
    return r


def fmt_num(x, comma=True, digits=10):
    if isinstance(x, float):
        if x != x or x in (float("inf"), float("-inf")):
            return "не число"
        if abs(x - round(x)) < 1e-9 * max(1, abs(x)) and abs(x) < 1e15:
            x = int(round(x))
    if isinstance(x, int):
        s = str(x) if abs(x) < 10 ** 15 else f"{x:.6e}"
    else:
        s = f"{x:.{digits}g}"
        if "e" in s:
            s = f"{x:.6g}"
    return s.replace(".", ",") if comma else s


# ---------------------------------------------------------------- единицы
# (регулярка по нормализованному названию, категория, ключ)
UNIT_DEFS = [
    (r"(?:миллиметр\w*|мм)", "length", "мм"), (r"(?:сантиметр\w*|см)", "length", "см"), (r"(?:дециметр\w*|дм)", "length", "дм"),
    (r"(?:метр\w*|м)", "length", "м"), (r"(?:километр\w*|км)", "length", "км"), (r"(?:дюйм\w*|inch\w*|in)", "length", "дюйм"),
    (r"(?:фут\w*|ft|feet|foot)", "length", "фут"), (r"(?:ярд\w*|yd)", "length", "ярд"),
    (r"(?:морск\w+ мил\w+)", "length", "морская миля"), (r"(?:мил[яьиюей]\w*|mi|miles?)", "length", "миля"),
    (r"(?:миллиграмм\w*|мг)", "mass", "мг"), (r"(?:грамм\w*|г|гр)", "mass", "г"), (r"(?:килограмм\w*|кило|кг)", "mass", "кг"),
    (r"(?:тонн\w*|т)", "mass", "т"), (r"(?:центнер\w*)", "mass", "ц"), (r"(?:фунт\w*|lb|lbs|pounds?)", "mass", "фунт"),
    (r"(?:унци\w*|oz)", "mass", "унция"),
    (r"(?:миллилитр\w*|мл)", "volume", "мл"), (r"(?:литр\w*|л)", "volume", "л"), (r"(?:галлон\w*|gal)", "volume", "галлон"),
    (r"(?:пинт\w*)", "volume", "пинта"), (r"(?:стакан\w*)", "volume", "стакан"),
    (r"(?:чайн\w+ ложк\w+|ч ?л)", "volume", "чайная ложка"), (r"(?:столов\w+ ложк\w+|ст ?л)", "volume", "столовая ложка"),
    (r"(?:кубометр\w*|куб\w* метр\w*|м3|м³)", "volume", "м3"),
    (r"(?:кв\w* ?сантиметр\w*|см2|см²)", "area", "см2"), (r"(?:кв\w* ?метр\w*|м2|м²)", "area", "м2"),
    (r"(?:кв\w* ?километр\w*|км2|км²)", "area", "км2"), (r"(?:гектар\w*|га)", "area", "га"), (r"(?:сотк\w+|сотых)", "area", "сотка"),
    (r"(?:акр\w*|acre\w*)", "area", "акр"), (r"(?:кв\w* ?фут\w*)", "area", "фут2"),
    (r"(?:м/с|метр\w* в секунд\w*)", "speed", "м/с"), (r"(?:км/ч|километр\w* в час\w*)", "speed", "км/ч"),
    (r"(?:миль в час|mph|мили в час)", "speed", "миль/ч"), (r"(?:узл\w+)", "speed", "узел"),
    (r"(?:байт\w*)", "data", "байт"), (r"(?:килобайт\w*|кб|kb)", "data", "КБ"), (r"(?:мегабайт\w*|мб|mb)", "data", "МБ"),
    (r"(?:гигабайт\w*|гб|gb)", "data", "ГБ"), (r"(?:терабайт\w*|тб|tb)", "data", "ТБ"),
    (r"(?:килобит\w*|кбит\w*)", "data", "Кбит"), (r"(?:мегабит\w*|мбит\w*)", "data", "Мбит"), (r"(?:гигабит\w*|гбит\w*)", "data", "Гбит"),
    (r"(?:секунд\w*|сек|с)", "time", "сек"), (r"(?:минут\w*|мин)", "time", "мин"), (r"(?:час\w*|ч)", "time", "час"),
    (r"(?:сутк\w*|дн\w+|день|дней)", "time", "сутки"), (r"(?:недел\w*)", "time", "неделя"), (r"(?:месяц\w*|мес)", "time", "месяц"),
    (r"(?:год\w*|лет)", "time", "год"),
    (r"(?:градус\w* цельси\w*|цельси\w*|°c|c)", "temp", "C"), (r"(?:градус\w* (?:по )?фаренгейт\w*|фаренгейт\w*|°f|f)", "temp", "F"),
    (r"(?:кельвин\w*|k)", "temp", "K"),
    (r"(?:паскал\w*|па)", "pressure", "Па"), (r"(?:килопаскал\w*|кпа)", "pressure", "кПа"), (r"(?:бар\w*)", "pressure", "бар"),
    (r"(?:атмосфер\w*|атм)", "pressure", "атм"), (r"(?:мм рт\.? ?ст\.?|миллиметр\w* ртутного столба)", "pressure", "мм рт. ст."),
    (r"(?:psi|пси)", "pressure", "psi"),
    (r"(?:ватт\w*|вт)", "power", "Вт"), (r"(?:киловатт\w*|квт)", "power", "кВт"), (r"(?:лошадин\w+ сил\w*|л\.? ?с\.?)", "power", "л.с."),
]
FACT = {"length": {"мм": 0.001, "см": 0.01, "дм": 0.1, "м": 1, "км": 1000, "дюйм": 0.0254, "фут": 0.3048, "ярд": 0.9144,
                   "миля": 1609.344, "морская миля": 1852},
        "mass": {"мг": 1e-6, "г": 1e-3, "кг": 1, "т": 1000, "ц": 100, "фунт": 0.45359237, "унция": 0.028349523125},
        "volume": {"мл": 1e-3, "л": 1, "галлон": 3.785411784, "пинта": 0.473176473, "стакан": 0.25, "чайная ложка": 0.005,
                   "столовая ложка": 0.015, "м3": 1000},
        "area": {"см2": 1e-4, "м2": 1, "км2": 1e6, "га": 1e4, "сотка": 100, "акр": 4046.8564224, "фут2": 0.09290304},
        "speed": {"м/с": 1, "км/ч": 1 / 3.6, "миль/ч": 0.44704, "узел": 1852 / 3600},
        "data": {"байт": 1, "КБ": 1024, "МБ": 1024 ** 2, "ГБ": 1024 ** 3, "ТБ": 1024 ** 4, "Кбит": 125, "Мбит": 125000, "Гбит": 125000000},
        "time": {"сек": 1, "мин": 60, "час": 3600, "сутки": 86400, "неделя": 604800, "месяц": 2629746, "год": 31556952},
        "pressure": {"Па": 1, "кПа": 1000, "бар": 1e5, "атм": 101325, "мм рт. ст.": 133.322387415, "psi": 6894.757293168},
        "power": {"Вт": 1, "кВт": 1000, "л.с.": 735.49875}}
CATEGORY_RU = {"length": "длина", "mass": "масса", "volume": "объём", "area": "площадь", "speed": "скорость", "data": "данные",
               "time": "время", "pressure": "давление", "power": "мощность", "temp": "температура"}


def parse_unit(s):
    t = re.sub(r"\s+", " ", str(s).lower().replace("ё", "е").strip().strip(".,?!"))
    t = re.sub(r"^(?:в|во|на|по)\s+", "", t)
    for rx, cat, key in UNIT_DEFS:
        if re.fullmatch(rx, t):
            return cat, key
    return None


def to_celsius(v, u):
    return v if u == "C" else (v - 32) * 5 / 9 if u == "F" else v - 273.15


def from_celsius(c, u):
    return c if u == "C" else c * 9 / 5 + 32 if u == "F" else c + 273.15


def convert(value, from_u, to_u):
    a, b = parse_unit(from_u), parse_unit(to_u)
    if not a:
        raise CalcError(f"не знаю единицу «{from_u}»")
    if not b:
        raise CalcError(f"не знаю единицу «{to_u}»")
    if a[0] != b[0]:
        raise CalcError(f"{from_u} ({CATEGORY_RU[a[0]]}) нельзя перевести в {to_u} ({CATEGORY_RU[b[0]]})")
    if a[0] == "temp":
        return from_celsius(to_celsius(value, a[1]), b[1]), a[1], b[1]
    return value * FACT[a[0]][a[1]] / FACT[b[0]][b[1]], a[1], b[1]


# ---------------------------------------------------------------- даты
MONTHS = {"январ": 1, "феврал": 2, "март": 3, "апрел": 4, "ма": 5, "июн": 6, "июл": 7, "август": 8, "сентябр": 9,
          "октябр": 10, "ноябр": 11, "декабр": 12}
WEEKDAYS = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]
MONTH_RU = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа", "сентября", "октября", "ноября", "декабря"]


def parse_date(text, today=None, future=True):
    """«1 января», «05.03.2027», «5 марта 2027», «завтра», «через 10 дней» -> date или None."""
    today = today or date.today()
    t = str(text).lower().replace("ё", "е").strip().rstrip("?.! ")
    if t in ("сегодня", "сейчас"):
        return today
    if t == "завтра":
        return today + timedelta(days=1)
    if t == "послезавтра":
        return today + timedelta(days=2)
    if t == "вчера":
        return today - timedelta(days=1)
    if t == "позавчера":
        return today - timedelta(days=2)
    m = re.fullmatch(r"через (\d+) (дн\w+|день|недел\w+|месяц\w*|год\w*|лет)", t)
    if m:
        n, u = int(m.group(1)), m.group(2)
        return add_span(today, n, u)
    m = re.fullmatch(r"(\d+) (дн\w+|день|недел\w+|месяц\w*|год\w*|лет) назад", t)
    if m:
        return add_span(today, -int(m.group(1)), m.group(2))
    m = re.fullmatch(r"(\d{1,2})[./-](\d{1,2})(?:[./-](\d{2,4}))?", t)
    if m:
        d, mo, y = int(m.group(1)), int(m.group(2)), m.group(3)
        return _mk(d, mo, y, today, future)
    m = re.fullmatch(r"(\d{1,2}) ([а-я]+)(?: (\d{4}))?(?: года| г\.?)?", t)
    if m:
        d, word, y = int(m.group(1)), m.group(2), m.group(3)
        for stem, mo in MONTHS.items():
            if word.startswith(stem) and (stem != "ма" or word in ("мая", "май")):
                return _mk(d, mo, y, today, future)
    return None


def _mk(d, mo, y, today, future):
    try:
        if y:
            y = int(y)
            y += 2000 if y < 100 else 0
            return date(y, mo, d)
        cand = date(today.year, mo, d)
        if future and cand < today:
            cand = date(today.year + 1, mo, d)
        elif not future and cand > today:
            cand = date(today.year - 1, mo, d)
        return cand
    except ValueError:
        return None


def add_span(d, n, unit):
    if unit.startswith("дн") or unit == "день":
        return d + timedelta(days=n)
    if unit.startswith("недел"):
        return d + timedelta(weeks=n)
    months = n * (12 if unit.startswith(("год", "лет")) else 1)
    mo = d.month - 1 + months
    y, mo = d.year + mo // 12, mo % 12 + 1
    for day in (d.day, 30, 29, 28):
        try:
            return date(y, mo, day)
        except ValueError:
            continue
    return d


def plural(n, one, few, many):
    n = abs(int(n)) % 100
    if 11 <= n <= 14:
        return many
    return one if n % 10 == 1 else few if 2 <= n % 10 <= 4 else many


def days_text(n):
    return f"{n} {plural(n, 'день', 'дня', 'дней')}"


def fmt_date(d):
    return f"{d.day} {MONTH_RU[d.month - 1]} {d.year}, {WEEKDAYS[d.weekday()]}"


def date_calc(q, today=None):
    """Ответ на вопрос о датах или None, если вопрос не распознан."""
    today = today or date.today()
    t = str(q).lower().replace("ё", "е").strip().rstrip("?.! ")
    m = re.match(r"^сколько (?:дней|суток) (?:осталось )?до (.+)$", t)
    if m:
        d = parse_date(m.group(1), today, future=True)
        if not d:
            return None
        n = (d - today).days
        return f"До {fmt_date(d)}: {days_text(n)}" + (f" (≈ {n // 7} нед.)" if n >= 14 else "") if n >= 0 else f"{fmt_date(d)} уже прошло {days_text(-n)} назад."
    m = re.match(r"^сколько (?:дней|суток|времени) прошло с (.+)$", t)
    if m:
        d = parse_date(m.group(1), today, future=False)
        if not d:
            return None
        n = (today - d).days
        y = today.year - d.year - ((today.month, today.day) < (d.month, d.day))
        return f"С {fmt_date(d)} прошло {days_text(n)}" + (f" (≈ {y} {plural(y, 'год', 'года', 'лет')})" if y >= 1 else "")
    m = re.match(r"^(?:какой |что за )?день недели(?: будет| был)?(?: у| в| на)? ?(.+)$", t)
    if m:
        d = parse_date(m.group(1), today, future=True)
        return None if not d else f"{fmt_date(d)} — {WEEKDAYS[d.weekday()]}."
    m = re.match(r"^(?:какая дата|какое число|сколько будет)?\s*(через .+|\d+ (?:дн\w+|день|недел\w+|месяц\w*|год\w*|лет) назад)(?: будет| была)?$", t)
    if m:
        d = parse_date(m.group(1), today)
        return None if not d else f"{m.group(1).capitalize()}: {fmt_date(d)}."
    m = re.match(r"^сколько (?:дней|суток) между (.+?) и (.+)$", t)
    if m:
        a = parse_date(m.group(1), today, future=True)
        b = parse_date(m.group(2), a, future=True) if a else None
        return None if not (a and b) else f"Между {fmt_date(a)} и {fmt_date(b)}: {days_text(abs((b - a).days))}."
    m = re.match(r"^(?:сколько мне лет|мой возраст),? (?:если )?(?:я )?(?:родился |родилась )?(?:в |с )?(.+)$", t)
    if m:
        d = parse_date(m.group(1), today, future=False)
        if not d or d.year == today.year and not re.search(r"\d{4}", m.group(1)):
            return None
        y = today.year - d.year - ((today.month, today.day) < (d.month, d.day))
        return f"{y} {plural(y, 'год', 'года', 'лет')}."
    return None


# ---------------------------------------------------------------- плагин
def register(api):
    api.var("calc_on", True)
    api.var("units_on", True)
    api.var("dates_on", True)
    api.var("comma", True)       # в ответе десятичная запятая
    api.var("digits", 10)        # значащих цифр

    def digits():
        try:
            return max(3, min(int(api.config_get("digits", 10) or 10), 15))
        except (TypeError, ValueError):
            return 10

    def f(x):
        return fmt_num(x, bool(api.config_get("comma", True)), digits())

    def do_calc(expr):
        if not api.config_get("calc_on", True):
            return "Калькулятор выключен в настройках."
        try:
            e = to_expr(expr)
            res = f(safe_eval(e))
            shown = str(expr).strip().rstrip("?=. ")
            shown = re.sub(r"^(?:сколько будет|сколько получится|сколько|чему равно|чему равен|посчитай|вычисли|реши)\s+", "", shown, flags=re.I)
            if len(shown) > 70 or not shown or re.search(r"[*/^]", shown):
                shown = e.replace("**", " в степени ").replace("*", " умножить на ").replace("/", " разделить на ")
            return f"{shown} = {res}"
        except CalcError as ex:
            return f"Не посчитал: {ex}."

    def do_convert(value, from_u, to_u):
        if not api.config_get("units_on", True):
            return "Конвертер выключен в настройках."
        try:
            v = float(str(value).replace(",", ".").replace(" ", ""))
            res, a, b = convert(v, from_u, to_u)
        except ValueError:
            return "Не понял число."
        except CalcError as ex:
            return f"Не перевёл: {ex}."
        shown = {"C": "°C", "F": "°F", "K": "K"}
        return f"{f(v)} {str(from_u).strip() or a} = {f(float(f'{res:.6g}'))} {shown.get(b, b)}"

    def do_date(q):
        if not api.config_get("dates_on", True):
            return "Расчёты с датами выключены в настройках."
        r = date_calc(q)
        return r or "Не понял вопрос про даты. Примеры: «сколько дней до 1 января», «какой день недели 5 марта», «через 45 дней»."

    def command(text, m):
        t = api.norm_ru(text).strip()
        mm = re.match(rf"^(?:переведи|конвертируй|сколько(?: это)?(?: будет)?)\s+({NUM})\s*([^\d]+?)\s+(?:в|во|на|to)\s+([^\d]+)$", t)
        if mm and api.config_get("units_on", True):
            a, b = parse_unit(mm.group(2)), parse_unit(mm.group(3))
            if a and b:
                return do_convert(mm.group(1), mm.group(2), mm.group(3))
        mm = re.match(rf"^сколько (?:градус\w*)?\s*(?:по )?(цельси\w*|фаренгейт\w*|кельвин\w*) в ({NUM})\s*(?:градус\w*)?\s*(?:по )?(цельси\w*|фаренгейт\w*|кельвин\w*)$", t)
        if mm and api.config_get("units_on", True):
            return do_convert(mm.group(2), mm.group(3), mm.group(1))
        if re.match(r"^(?:сколько (?:дней|суток)|какой день недели|что за день недели|какая дата|какое число|сколько мне лет|мой возраст)", t) \
                or re.match(r"^(?:через \d+|\d+ \w+ назад)", t):
            r = date_calc(t) if api.config_get("dates_on", True) else None
            if r:
                return r
        mm = re.match(r"^(?:посчитай|вычисли|реши|сколько будет|сколько получится|чему равно|calc)\s+(.+)$", t)
        if mm and re.search(r"\d|корень|пи\b|факториал", mm.group(1)):
            return do_calc(mm.group(1))
        return None

    api.add_command(r"(?:посчитай|вычисли|реши|сколько|переведи|конвертируй|какой день недели|какая дата|какое число|через \d+|\d+ \w+ назад|мой возраст|чему равно|calc)",
                    command, "посчитай … / переведи 5 миль в километры / сколько дней до 1 января / какой день недели 5 марта")
    api.add_tool("calculate", "Точный калькулятор: выражение или русская фраза («15 процентов от 2300», «корень из 144», «12 умножить на 7», "
                 "«2 в степени 10»). Используй вместо счёта в уме.",
                 {"expression": {"type": "string", "description": "Выражение, например (3+4)*2 или 15% от 2300"}},
                 lambda a: do_calc(a.get("expression")), ["expression"])
    api.add_tool("convert_units", "Перевод величин: длина, масса, объём, площадь, скорость, данные, время, температура, давление, мощность.",
                 {"value": {"type": "number"}, "from_unit": {"type": "string", "description": "например мили, фунты, °F, Мбит"},
                  "to_unit": {"type": "string", "description": "например километры, кг, °C, МБ"}},
                 lambda a: do_convert(a.get("value"), a.get("from_unit", ""), a.get("to_unit", "")), ["value", "from_unit", "to_unit"])
    api.add_tool("date_calc", "Вопросы про даты: «сколько дней до 1 января», «какой день недели 5 марта 2027», «через 45 дней», "
                 "«сколько дней между 1 мая и 1 июня», «сколько мне лет, если родился 5.03.1990».",
                 {"question": {"type": "string"}}, lambda a: do_date(a.get("question", "")), ["question"])

    def build(fr):
        from tkinter import ttk
        pad = {"padx": 6, "pady": 2}
        for i, (k, txt) in enumerate((("calc_on", "Калькулятор: «посчитай 15 процентов от 2300», «корень из 144», «12 умножить на 7»"),
                                      ("units_on", "Конвертер: длина, масса, объём, площадь, скорость, данные, время, температура, давление, мощность"),
                                      ("dates_on", "Даты: «сколько дней до …», «какой день недели …», «через 45 дней», «сколько мне лет»"),
                                      ("comma", "В ответе десятичная запятая (3,5), а не точка"))):
            ttk.Checkbutton(fr, text=txt, variable=api.var(k)).grid(row=i, column=0, columnspan=3, sticky="w", **pad)
        ttk.Label(fr, text="Значащих цифр в ответе:").grid(row=4, column=0, sticky="w", **pad)
        ttk.Spinbox(fr, from_=3, to=15, width=5, textvariable=api.var("digits")).grid(row=4, column=1, sticky="w", **pad)
        ttk.Label(fr, text=("Всё считается на вашем ПК, без интернета и без запуска произвольного кода: разрешены числа, действия, "
                            "скобки и перечень математических функций. Для валют есть инструмент «Погода и курсы». "
                            "КБ/МБ/ГБ считаются по 1024, Мбит — по 1000/8. Стакан = 250 мл, чайная ложка = 5 мл, столовая = 15 мл."),
                  foreground="#777", wraplength=720, justify="left").grid(row=5, column=0, columnspan=3, sticky="w", **pad)
    api.add_settings(build)
