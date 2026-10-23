# Инструмент Nightlord агента: погода (Open-Meteo, без ключа) и курсы валют (ЦБ РФ) + криптовалюты (CoinGecko).
import re
from datetime import date, datetime, timedelta

import requests

PLUGIN = {"name": "Погода и курсы", "version": "1.0", "group": "tools",
          "description": "«какая погода», «погода в Казани завтра», «погода на неделю», «курс доллара», «курс евро», «сколько стоит биткоин», "
                         "«переведи 100 долларов в рубли». Погода — Open-Meteo, валюты — курс ЦБ РФ, крипта — CoinGecko. Ключи не нужны."}

UA = {"User-Agent": "NightlordAgent-WeatherRates/1.0"}
WMO = {0: "ясно", 1: "в основном ясно", 2: "переменная облачность", 3: "пасмурно", 45: "туман", 48: "изморозь", 51: "слабая морось",
       53: "морось", 55: "сильная морось", 56: "ледяная морось", 57: "сильная ледяная морось", 61: "небольшой дождь", 63: "дождь",
       65: "сильный дождь", 66: "ледяной дождь", 67: "сильный ледяной дождь", 71: "небольшой снег", 73: "снег", 75: "сильный снег",
       77: "снежные зёрна", 80: "небольшие ливни", 81: "ливни", 82: "сильные ливни", 85: "снегопад", 86: "сильный снегопад",
       95: "гроза", 96: "гроза с градом", 99: "сильная гроза с градом"}
CUR_WORDS = {"USD": ("доллар", "бакс", "usd"), "EUR": ("евро", "eur"), "CNY": ("юан", "cny"), "GBP": ("фунт", "gbp"), "JPY": ("иен", "йен", "jpy"),
             "KZT": ("тенге", "kzt"), "TRY": ("лир", "try"), "BYN": ("белорусск", "byn"), "UAH": ("гривн", "uah"), "CHF": ("франк", "chf"),
             "AED": ("дирхам", "aed"), "PLN": ("злот", "pln"), "INR": ("рупи", "inr"), "RUB": ("рубл", "руб", "rub")}
CUR_NAME = {"USD": "доллар США", "EUR": "евро", "CNY": "юань", "GBP": "фунт стерлингов", "JPY": "иена", "KZT": "тенге", "TRY": "турецкая лира",
            "BYN": "белорусский рубль", "UAH": "гривна", "CHF": "швейцарский франк", "AED": "дирхам ОАЭ", "PLN": "злотый", "INR": "рупия", "RUB": "рубль"}
CRYPTO = {"bitcoin": ("биткоин", "bitcoin", "btc", "биток"), "ethereum": ("эфир", "ethereum", "eth"), "toncoin": ("тон", "toncoin", "ton"),
          "tether": ("usdt", "тезер", "tether"), "solana": ("солана", "solana", "sol"), "dogecoin": ("догикоин", "доги", "dogecoin", "doge")}
CRYPTO_SHORT = {"bitcoin": "BTC", "ethereum": "ETH", "toncoin": "TON", "tether": "USDT", "solana": "SOL", "dogecoin": "DOGE"}


def find_currency(word):
    w = str(word).lower().replace("ё", "е")
    for code, keys in CUR_WORDS.items():
        if any(w.startswith(k) for k in keys):
            return code
    return None


def find_crypto(word):
    w = str(word).lower().replace("ё", "е")
    for cid, keys in CRYPTO.items():
        if any(w == k or (len(k) > 4 and w.startswith(k)) for k in keys):
            return cid
    return None


def city_prepositional(c):
    return c


def fmt_t(x):
    return f"{x:+.0f}°".replace("+0°", "0°").replace("-", "−")


def wind_dir(deg):
    return ["северный", "северо-восточный", "восточный", "юго-восточный", "южный", "юго-западный", "западный", "северо-западный"][int((deg + 22.5) % 360 // 45)]


def money(x):
    s = f"{x:,.2f}".replace(",", " ").replace(".", ",")
    return s[:-3] if s.endswith(",00") else s


def day_label(d, today):
    diff = (d - today).days
    return {0: "сегодня", 1: "завтра", 2: "послезавтра"}.get(diff) or ["пн", "вт", "ср", "чт", "пт", "сб", "вс"][d.weekday()] + f" {d.day}.{d.month:02d}"


def register(api):
    for k in ("weather_on", "rates_on", "crypto_on", "convert_on"):
        api.var(k, True)
    api.var("city", "Москва")
    api.var("days", 3)
    api.var("use_proxy", False)
    api.var("alert_on", False)

    def prox():
        return api.proxies("web") if api.config_get("use_proxy", False) else None

    def jget(url, **params):
        s = requests.Session()
        s.trust_env = False
        try:
            r = s.get(url, params=params or None, timeout=12, headers=UA, proxies=prox())
        except requests.RequestException as e:
            raise RuntimeError(api.explain_proxy_error(e)) from None
        if r.status_code >= 400:
            raise RuntimeError(f"сервис ответил кодом {r.status_code}")
        try:
            return r.json()
        except ValueError:
            raise RuntimeError("сервис вернул не JSON") from None

    def off(k, name):
        return None if api.config_get(k, True) else f"Функция «{name}» выключена в настройках инструмента."

    # ------------------------------------------------------------ погода
    def geocode(name):
        # «в Казани» -> Казань, «в Москве» -> Москва, «в Лондоне» -> Лондон: пробуем начальные формы по очереди
        cands = [name]
        if len(name) > 3:
            cands += [name[:-1] + "а", name[:-1] + "ь", name[:-1], name[:-2] + "ь", name[:-2] + "а", name[:-2] + "я", name[:-1] + "я", name[:-2]]
        seen = []
        for c in cands:
            if c in seen:
                continue
            seen.append(c)
            j = jget("https://geocoding-api.open-meteo.com/v1/search", name=c, count=1, language="ru", format="json")
            r = (j.get("results") or [None])[0]
            if r:
                return r["latitude"], r["longitude"], r.get("name") or c, r.get("country") or ""
            if len(seen) >= 6:
                break
        raise RuntimeError(f"город «{name}» не найден")

    def weather(args):
        if off("weather_on", "погода"):
            return off("weather_on", "погода")
        city = str(args.get("city") or api.config_get("city", "Москва")).strip()[:60] or "Москва"
        when = str(args.get("when") or "now").lower()
        try:
            lat, lon, nm, country = geocode(city)
            days = 7 if when == "week" else max(1, min(int(api.config_get("days", 3) or 3), 7)) if when == "days" else 2 if when in ("tomorrow", "today") else 1
            j = jget("https://api.open-meteo.com/v1/forecast", latitude=lat, longitude=lon, timezone="auto", forecast_days=max(days, 2),
                     wind_speed_unit="ms", current="temperature_2m,apparent_temperature,relative_humidity_2m,weather_code,wind_speed_10m,wind_direction_10m,precipitation",
                     daily="weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max,precipitation_sum,wind_speed_10m_max")
        except RuntimeError as e:
            return f"Погоду узнать не вышло: {e}."
        cur, dl = j.get("current") or {}, j.get("daily") or {}
        head = f"{nm}" + (f" ({country})" if country and country != "Россия" else "")
        today = date.today()
        try:
            today = datetime.strptime(dl["time"][0], "%Y-%m-%d").date()
        except (KeyError, ValueError, IndexError):
            pass

        def day_line(i):
            d = datetime.strptime(dl["time"][i], "%Y-%m-%d").date()
            s = f"{day_label(d, today)}: {WMO.get(dl['weather_code'][i], 'без описания')}, {fmt_t(dl['temperature_2m_min'][i])}…{fmt_t(dl['temperature_2m_max'][i])}"
            p = (dl.get("precipitation_probability_max") or [None] * 9)[i]
            if p is not None:
                s += f", осадки {p}%"
            w = (dl.get("wind_speed_10m_max") or [None] * 9)[i]
            return s + (f", ветер до {w:.0f} м/с" if w is not None else "")
        try:
            if when == "now":
                return (f"{head} сейчас: {WMO.get(cur['weather_code'], 'без описания')}, {fmt_t(cur['temperature_2m'])} "
                        f"(ощущается как {fmt_t(cur['apparent_temperature'])}), ветер {wind_dir(cur.get('wind_direction_10m', 0))} {cur['wind_speed_10m']:.0f} м/с, "
                        f"влажность {cur['relative_humidity_2m']}%.\nСегодня: {day_line(0).split(': ', 1)[1]}.")
            if when == "today":
                return f"{head}, " + day_line(0)
            if when == "tomorrow":
                return f"{head}, " + day_line(1)
            return f"{head}:\n" + "\n".join(day_line(i) for i in range(min(days, len(dl["time"]))))
        except (KeyError, IndexError, TypeError):
            return "Сервис погоды вернул неожиданный ответ."

    # ------------------------------------------------------------ курсы
    def cbr():
        j = jget("https://www.cbr-xml-daily.ru/daily_json.js")
        v = j.get("Valute") or {}
        out = {"RUB": (1.0, 1.0)}
        for code, d in v.items():
            try:
                out[code] = (d["Value"] / d["Nominal"], d["Previous"] / d["Nominal"])
            except (KeyError, ZeroDivisionError, TypeError):
                pass
        return out, str(j.get("Date", ""))[:10]

    def rate(args):
        if off("rates_on", "курсы валют"):
            return off("rates_on", "курсы валют")
        code = str(args.get("currency") or "USD").upper()
        code = code if code in CUR_WORDS else (find_currency(code) or code)
        try:
            rates, dt = cbr()
        except RuntimeError as e:
            return f"Курс узнать не вышло: {e}."
        if code not in rates:
            return f"Курса «{code}» у ЦБ нет."
        now, prev = rates[code]
        diff = now - prev
        return f"{(CUR_NAME.get(code, code)[:1].upper() + CUR_NAME.get(code, code)[1:])} ({code}): {money(now)} ₽ по курсу ЦБ" + (f" на {dt}" if dt else "") + \
            (f", {'+' if diff > 0 else '−'}{money(abs(diff))} к предыдущему" if abs(diff) > 1e-9 else "") + "."

    def rates_all(args):
        if off("rates_on", "курсы валют"):
            return off("rates_on", "курсы валют")
        try:
            rates, dt = cbr()
        except RuntimeError as e:
            return f"Курсы узнать не вышло: {e}."
        return f"Курс ЦБ на {dt}: " + ", ".join(f"{c} {money(rates[c][0])} ₽" for c in ("USD", "EUR", "CNY") if c in rates) + "."

    def crypto(args):
        if off("crypto_on", "криптовалюты"):
            return off("crypto_on", "криптовалюты")
        cid = find_crypto(str(args.get("coin") or "bitcoin")) or str(args.get("coin")).lower()
        try:
            j = jget("https://api.coingecko.com/api/v3/simple/price", ids=cid, vs_currencies="usd,rub", include_24hr_change="true")
        except RuntimeError as e:
            return f"Курс криптовалюты узнать не вышло: {e}."
        d = j.get(cid)
        if not d:
            return f"Не знаю монету «{cid}»."
        ch = d.get("usd_24h_change")
        return f"{CRYPTO_SHORT.get(cid, cid)}: ${money(d['usd'])}" + (f" ≈ {money(d['rub'])} ₽" if d.get("rub") else "") + \
            (f", за сутки {ch:+.1f}%".replace("-", "−").replace(".", ",") if ch is not None else "") + " (CoinGecko)."

    def conv(args):
        if off("convert_on", "перевод валют"):
            return off("convert_on", "перевод валют")
        try:
            amt = float(str(args.get("amount")).replace(",", ".").replace(" ", ""))
        except ValueError:
            return "Не понял сумму."
        a = find_currency(args.get("from_currency", "")) or str(args.get("from_currency", "")).upper()
        b = find_currency(args.get("to_currency", "")) or str(args.get("to_currency", "")).upper()
        try:
            rates, dt = cbr()
        except RuntimeError as e:
            return f"Курсы узнать не вышло: {e}."
        if a not in rates or b not in rates:
            return f"Нет курса для {a if a not in rates else b}."
        res = amt * rates[a][0] / rates[b][0]
        return f"{money(amt)} {a} ≈ {money(res)} {b} (курс ЦБ на {dt})."

    P_CITY = {"city": {"type": "string", "description": "город; пусто — город из настроек"},
              "when": {"type": "string", "description": "now | today | tomorrow | days | week"}}
    api.add_tool("get_weather", "Погода: сейчас, сегодня, завтра или на несколько дней (Open-Meteo).", P_CITY, weather)
    api.add_tool("get_currency_rate", "Официальный курс ЦБ РФ: USD, EUR, CNY, GBP, KZT, TRY и др.", {"currency": {"type": "string"}}, rate)
    api.add_tool("get_crypto_rate", "Курс криптовалюты (bitcoin, ethereum, toncoin, solana…) в долларах и рублях.", {"coin": {"type": "string"}}, crypto)
    api.add_tool("convert_currency", "Перевод суммы между валютами по курсу ЦБ РФ.",
                 {"amount": {"type": "number"}, "from_currency": {"type": "string"}, "to_currency": {"type": "string"}}, conv,
                 ["amount", "from_currency", "to_currency"])

    def command(text, m):
        t = api.norm_ru(text).strip().rstrip("?.!")
        mm = re.match(r"^(?:какая |что с |скажи |покажи )?погод\w*(?: на улице| сейчас)?(.*)$", t)
        if mm:
            rest = mm.group(1).strip()
            when = "now"
            for rx, w in ((r"\bна недел\w+", "week"), (r"\bна (\d) дн\w+|\bна (?:ближайшие|следующие) дни", "days"), (r"\bзавтра\b", "tomorrow"), (r"\bсегодня\b", "today")):
                if re.search(rx, rest):
                    when = w
                    rest = re.sub(rx, "", rest)
            city = re.sub(r"\b(?:сейчас|на улице|пожалуйста)\b", "", rest)
            city = re.sub(r"^\s*(?:в|во|для)\s+", "", city.strip()).strip()
            return weather({"city": city, "when": when})
        mm = re.match(r"^(?:какой )?курс (?:ц?б?\s*)?(?:валют\w*|рубля)?$", t)
        if mm and "курс" in t and len(t.split()) <= 3 and not re.search(r"доллар|евро|юан", t):
            return rates_all({})
        mm = re.match(r"^(?:какой |сколько сейчас )?курс (\S+)(?: сегодня| сейчас| цб)?$", t) or re.match(r"^сколько (?:сейчас )?стоит (\S+)$", t)
        if mm:
            w = mm.group(1)
            cid = find_crypto(w)
            if cid:
                return crypto({"coin": cid})
            c = find_currency(w)
            if c and c != "RUB":
                return rate({"currency": c})
        mm = re.match(r"^(?:переведи|конвертируй|сколько будет)\s+(\d+(?:[.,]\d+)?)\s*(\S+)\s+(?:в|на)\s+(\S+)$", t)
        if mm:
            a, b = find_currency(mm.group(2)), find_currency(mm.group(3))
            if a and b:
                return conv({"amount": mm.group(1), "from_currency": a, "to_currency": b})
        return None

    api.add_command(r"(?:какая погод|что с погод|скажи погод|покажи погод|погод|какой курс|курс |сколько (?:сейчас )?стоит|переведи|конвертируй|сколько будет)",
                    command, "погода [в Казани] [завтра|на неделю] / курс доллара / сколько стоит биткоин / переведи 100 долларов в рубли")

    def build(fr):
        from tkinter import ttk
        pad = {"padx": 6, "pady": 2}
        for i, (k, txt) in enumerate((("weather_on", "Погода (Open-Meteo)"), ("rates_on", "Курсы валют ЦБ РФ"),
                                      ("crypto_on", "Криптовалюты (CoinGecko)"), ("convert_on", "Перевод сумм между валютами"),
                                      ("use_proxy", "Ходить через веб-прокси агента (плагин VPN / прокси)"))):
            ttk.Checkbutton(fr, text=txt, variable=api.var(k)).grid(row=i, column=0, columnspan=3, sticky="w", **pad)
        ttk.Label(fr, text="Город по умолчанию:").grid(row=5, column=0, sticky="w", **pad)
        ttk.Entry(fr, textvariable=api.var("city"), width=28).grid(row=5, column=1, sticky="w", **pad)
        ttk.Label(fr, text="Дней в прогнозе:").grid(row=6, column=0, sticky="w", **pad)
        ttk.Spinbox(fr, from_=1, to=7, width=5, textvariable=api.var("days")).grid(row=6, column=1, sticky="w", **pad)
        ttk.Label(fr, text=("Запросы уходят на open-meteo.com, cbr-xml-daily.ru и coingecko.com; вашему названию города они увидят. Названия городов лучше "
                            "говорить в именительном падеже («погода Казань» тоже понимается). Это не инвестиционный совет: цены ориентировочные. "
                            "Форматы ответов взяты из документации сервисов, вживую в песочнице их проверить не удалось."),
                  foreground="#777", wraplength=720, justify="left").grid(row=7, column=0, columnspan=3, sticky="w", **pad)
    api.add_settings(build)
