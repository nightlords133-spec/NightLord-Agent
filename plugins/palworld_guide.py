# Плагин Nightlord агента: Palworld — помощник по игре (справочник палов, разведение, работа на базе, навыки, вики).
# Работает вместе со встроенным плагином «Palworld» (тот управляет сервером) и не мешает ему.
# База палов вшита в файл (данные PalCalc, взяты из файлов игры, MIT); кнопка «Обновить базу» скачивает свежую.
# Вики (локации, дроп, стихии, боссы) запрашивается через интернет — с прокси агента, если он настроен.
import base64
import difflib
import json
import re
import threading
import time
import zlib

PLUGIN = {"name": "Palworld помощник", "version": "1.0",
          "description": "Подсказки по игре Palworld: «кто получится от Анубиса и Ламболла», «как вывести Джетрагона», "
                         "«кто лучше всего для добычи», «что умеет Анубис», «что даёт пассивка Виртуоз», "
                         "«у меня есть Пенкинг», «путь до Анубиса из моих палов». Плюс вики и заметки по игре."}

WORK_RU = {"Kindling": "разжигание", "Watering": "полив", "Planting": "посадка", "GenerateElectricity": "электричество",
           "Handiwork": "ручной труд", "Gathering": "сбор", "Lumbering": "рубка леса", "Mining": "добыча",
           "MedicineProduction": "медицина", "Cooling": "охлаждение", "Transporting": "перенос", "Farming": "ферма"}
WORK_WORDS = {
    "Kindling": "разжиг огон огн плавил печ готов кухн костр kindl fire",
    "Watering": "полив вод water",
    "Planting": "посад посев сажа семен plant",
    "GenerateElectricity": "электр ток генерат energy electric",
    "Handiwork": "ручн крафт ремесл изготов верстак кузн handi craft",
    "Gathering": "сбор собира урожа gather",
    "Lumbering": "рубк лес дерев древес бревн lumber wood",
    "Mining": "добыч руд камн камен шахт копа карьер mining mine ore",
    "MedicineProduction": "медиц лекарст аптек medic",
    "Cooling": "охлаж холод мороз cool",
    "Transporting": "перенос транспорт таска носи переноск transport",
    "Farming": "ферм ранч животн farm ranch",
}
RAW_DB = "https://raw.githubusercontent.com/tylercamp/palcalc/main/PalCalc.Model/{}"
WIKIS = {"wiki.gg": "https://palworld.wiki.gg", "fandom": "https://palworld.fandom.com"}
UA = {"User-Agent": "NightlordAgent-PalworldGuide/1.0 (personal voice assistant)"}


def norm(s):
    s = (s or "").lower().replace("ё", "е")
    return re.sub(r"[^a-zа-я0-9]+", "", s)


def convert_palcalc(d, br, date=""):
    """db.json + breeding.json из PalCalc -> компактная база плагина."""
    P = sorted(d["Pals"], key=lambda x: (x["Id"]["PalDexNo"], x["Id"]["IsVariant"], x["InternalName"]))
    idx = {p["InternalName"]: i for i, p in enumerate(P)}
    el_ru = {e["InternalName"]: (e.get("LocalizedNames") or {}).get("ru") or e["Name"] for e in d["Elements"]}
    skills, sk_idx = [], {}
    for s in d["ActiveSkills"]:
        ln = s.get("LocalizedNames") or {}
        sk_idx[s["InternalName"]] = len(skills)
        skills.append([s["Name"], ln.get("ru") or s["Name"], el_ru.get(s.get("ElementInternalName"), ""),
                       s.get("Power") or 0, s.get("CooldownSeconds") or 0, bool(s.get("HasSkillFruit"))])
    passives = []
    for s in d["PassiveSkills"]:
        if not s.get("IsStandardPassiveSkill"):
            continue
        ln, ld = s.get("LocalizedNames") or {}, s.get("LocalizedDescriptions") or {}
        passives.append([s["Name"], ln.get("ru") or s["Name"], s.get("Rank", 0),
                         (ld.get("ru") or s.get("Description") or "").strip()])
    gp = d.get("BreedingGenderProbability") or {}
    pals = []
    for p in P:
        ws = {k: v for k, v in (p.get("WorkSuitability") or {}).items() if v}
        atk = [[sk_idx[a["AttackInternalId"]], a["Level"]] for a in p.get("InternalAttackLeveling") or []
               if a["AttackInternalId"] in sk_idx]
        pals.append([p["InternalName"], p["Name"], (p.get("LocalizedNames") or {}).get("ru") or p["Name"],
                     p["Id"]["PalDexNo"], p["Id"]["IsVariant"], p["BreedingPower"], p.get("Rarity", 0),
                     p.get("Nocturnal", False), p.get("MinWildLevel", 0), p.get("MaxWildLevel", 0), p.get("Hp", 0),
                     p.get("Attack", 0), p.get("Defense", 0), p.get("FoodAmount", 0), ws, atk, p.get("Price", 0),
                     round((gp.get(p["InternalName"]) or {}).get("MALE", 0.5), 2), p.get("Size", "")])
    n = len(P)
    tri, gender = [-1] * (n * (n + 1) // 2), []
    for r in br:
        a, b, c = idx[r["Parent1InternalName"]], idx[r["Parent2InternalName"]], idx[r["ChildInternalName"]]
        i, j = min(a, b), max(a, b)
        key = i * n - i * (i - 1) // 2 + (j - i)
        if r["Parent1Gender"] != "WILDCARD":
            gender.append([a, r["Parent1Gender"], b, r["Parent2Gender"], c])
            if tri[key] != -1:
                continue
        tri[key] = c
    if -1 in tri:
        raise ValueError("в таблице разведения не хватает пар")
    return {"source": "PalCalc (github.com/tylercamp/palcalc, MIT), данные из файлов игры", "date": date,
            "pals": pals, "skills": skills, "passives": passives, "breed": tri, "gender": gender}


class PalDB:
    def __init__(self, data):
        self.d = data
        self.pals, self.skills, self.passives = data["pals"], data["skills"], data["passives"]
        self.n = len(self.pals)
        self.tri = data["breed"]
        self.gender = {}
        for a, ga, b, gb, c in data.get("gender", []):
            self.gender.setdefault((min(a, b), max(a, b)), []).append((a, ga, b, gb, c))
        self.names = {}
        for i, p in enumerate(self.pals):
            for nm in (p[0], p[1], p[2]):
                self.names.setdefault(norm(nm), i)
        for i, p in enumerate(self.pals):          # «Буши» для Bushi, «Джетрагон» для Jetragon и т. п. — как слышит распознавание
            self.names.setdefault(norm(translit(p[1])), i)
        self.sk_names = {}
        for i, s in enumerate(self.skills):
            for nm in s[:2]:
                self.sk_names.setdefault(norm(nm), ("a", i))
        for i, s in enumerate(self.passives):
            for nm in s[:2]:
                self.sk_names[norm(nm)] = ("p", i)       # у пассивок приоритет: их чаще спрашивают

    # ---- имена ----
    def find(self, name, cutoff=0.72):
        key = norm(name)
        if not key:
            return None
        if key in self.names:
            return self.names[key]
        m = difflib.get_close_matches(key, list(self.names), n=1, cutoff=cutoff)
        return self.names[m[0]] if m else None

    def label(self, i, both=True):
        p = self.pals[i]
        return f"{p[2]} ({p[1]})" if both and p[2] != p[1] else p[2]

    # ---- разведение ----
    def child(self, a, b):
        i, j = min(a, b), max(a, b)
        return self.tri[i * self.n - i * (i - 1) // 2 + (j - i)]

    def breed_text(self, a, b):
        g = self.gender.get((min(a, b), max(a, b)))
        if g:
            return "Зависит от пола: " + "; ".join(
                f"{self.label(x)} {'самка' if gx == 'FEMALE' else 'самец'} + {self.label(y)} "
                f"{'самка' if gy == 'FEMALE' else 'самец'} = {self.label(c)}" for x, gx, y, gy, c in g) + "."
        return f"{self.label(a)} + {self.label(b)} = {self.label(self.child(a, b))}."

    def parents(self, c, owned=()):
        owned = set(owned)
        out = []
        for a in range(self.n):
            for b in range(a, self.n):
                if self.child(a, b) == c or any(x[4] == c for x in self.gender.get((a, b), ())):
                    out.append(((a in owned) + (b in owned), a, b))
        out.sort(key=lambda x: (-x[0], a_easy(self, x[1]) + a_easy(self, x[2])))
        return out

    def path(self, target, owned, max_gen=5):
        """Кратчайшая цепочка разведения от своих палов до цели: [(родитель1, родитель2, ребёнок), ...] или None."""
        have = {o: None for o in owned}
        if target in have:
            return []
        for _ in range(max_gen):
            cur = list(have)
            new = {}
            for x in range(len(cur)):
                for y in range(x, len(cur)):
                    a, b = cur[x], cur[y]
                    for c in {self.child(a, b)} | {g[4] for g in self.gender.get((min(a, b), max(a, b)), ())}:
                        if c not in have and c not in new:
                            new[c] = (a, b)
            if not new:
                return None
            have.update(new)
            if target in have:
                steps, need = [], [target]
                while need:
                    c = need.pop()
                    if have.get(c) is None:
                        continue
                    a, b = have[c]
                    steps.append((a, b, c))
                    need += [a, b]
                seen, uniq = set(), []
                for s in reversed(steps):
                    if s[2] not in seen:
                        seen.add(s[2])
                        uniq.append(s)
                return uniq
        return None

    # ---- справка ----
    def info(self, i, short=False):
        p = self.pals[i]
        ws = sorted(p[14].items(), key=lambda kv: -kv[1])
        work = ", ".join(f"{WORK_RU.get(k, k)} {v}" for k, v in ws) or "не работает на базе"
        dex = f"№{p[3]}{'B' if p[4] else ''}" if p[3] < 10000 else "из коллаборации с Terraria"
        if short:
            return f"{self.label(i, False)}, {dex}. Работа: {work}." + (" Ночной." if p[7] else "")
        atk = "; ".join(f"ур.{lv} {self.skills[s][1]} ({self.skills[s][2]}, сила {self.skills[s][3]})"
                        for s, lv in p[15][:9])
        male = int(round(p[17] * 100))
        return (f"{self.label(i)}, {dex}, редкость {p[6]}, размер {p[18]}.\n"
                f"Работа на базе: {work}.\n"
                f"Характеристики: HP {p[10]}, атака {p[11]}, защита {p[12]}; ест {p[13]} ед.; "
                f"{'ночной (работает ночью)' if p[7] else 'дневной'}.\n"
                f"В дикой природе ур. {p[8]}–{p[9]}. Цена продажи {p[16]}. Пол: самцы {male}%, самки {100 - male}%.\n"
                f"Сила разведения {p[5]} (меньше — реже и сильнее).\n"
                f"Атаки: {atk}.\n"
                "Стихия, локации и дроп — через palworld_wiki.")

    def best_for(self, work, limit=6, owned=()):
        rows = [(p[14].get(work, 0), i) for i, p in enumerate(self.pals) if p[14].get(work, 0)]
        rows.sort(key=lambda x: (-x[0], -(x[1] in owned), a_easy(self, x[1])))
        return rows[:limit]

    def skill(self, name):
        key = norm(name)
        hit = self.sk_names.get(key)
        if not hit:
            m = difflib.get_close_matches(key, list(self.sk_names), n=1, cutoff=0.7)
            hit = self.sk_names[m[0]] if m else None
        if not hit:
            return None
        kind, i = hit
        if kind == "p":
            en, ru, rank, desc = self.passives[i]
            r = f"ранг {rank}" if rank >= 0 else f"негативная (ранг {rank})"
            return f"Пассивка «{ru}» ({en}), {r}: {desc or 'описания нет'}."
        en, ru, el, pw, cd, fruit = self.skills[i]
        who = [self.label(j, False) for j, p in enumerate(self.pals) if any(s == i for s, _ in p[15])][:6]
        return (f"Атака «{ru}» ({en}): стихия {el or '—'}, сила {pw}, перезарядка {cd:g} с."
                + (" Есть плод навыка." if fruit else "") + (f" Есть у: {', '.join(who)}." if who else ""))


TR = [("sh", "ш"), ("ch", "ч"), ("th", "т"), ("ph", "ф"), ("oo", "у"), ("ee", "и"), ("qu", "кв"), ("ck", "к"),
      ("x", "кс"), ("j", "дж"), ("y", "и"), ("a", "а"), ("b", "б"), ("c", "к"), ("d", "д"), ("e", "е"), ("f", "ф"),
      ("g", "г"), ("h", "х"), ("i", "и"), ("k", "к"), ("l", "л"), ("m", "м"), ("n", "н"), ("o", "о"), ("p", "п"),
      ("r", "р"), ("s", "с"), ("t", "т"), ("u", "у"), ("v", "в"), ("w", "в"), ("z", "з")]


def translit(s):
    s = s.lower()
    for a, b in TR:
        s = s.replace(a, b)
    return s


def a_easy(db, i):
    """Чем выше сила разведения — тем пал обычнее и проще достаётся (для сортировки)."""
    return -db.pals[i][5]


def register(api):
    api.var("voice", True)
    api.var("wiki", "wiki.gg")
    api.var("my_pals", "")
    state = {"db": None, "lock": threading.Lock()}
    db_file = api.data_dir / "db.json"
    notes_file = api.data_dir / "notes.json"

    def db():
        with state["lock"]:
            if state["db"] is None:
                data = None
                if db_file.exists():
                    try:
                        data = json.loads(db_file.read_text("utf-8"))
                    except (OSError, ValueError) as e:
                        api.log(f"скачанная база не читается ({e}), беру встроенную")
                if data is None:
                    data = json.loads(zlib.decompress(base64.b64decode(DB_B64)).decode("utf-8"))
                state["db"] = PalDB(data)
            return state["db"]

    # ---- мои палы ----
    def owned():
        d = db()
        out = []
        for nm in re.split(r"[,;\n]+", str(api.config_get("my_pals", ""))):
            i = d.find(nm, 0.8) if nm.strip() else None
            if i is not None and i not in out:
                out.append(i)
        return out

    def set_owned(ids):
        d = db()
        txt = ", ".join(d.pals[i][2] for i in ids)
        api.post(lambda: api.var("my_pals").set(txt))
        api.config_set("my_pals", txt)

    def split_names(text):
        return [x for x in re.split(r"\s*(?:,|;|\bи\b|\band\b)\s*", str(text)) if x.strip()]

    def my_pals(action, names=""):
        d = db()
        cur = owned()
        if action in ("add", "remove"):
            ok, bad = [], []
            for nm in split_names(names):
                i = d.find(nm)
                if i is None:
                    bad.append(nm)
                    continue
                if action == "add" and i not in cur:
                    cur.append(i)
                    ok.append(d.label(i, False))
                elif action == "remove" and i in cur:
                    cur.remove(i)
                    ok.append(d.label(i, False))
                else:
                    bad.append(d.label(i, False) + (" — уже есть" if action == "add" else " — не было в списке"))
            set_owned(cur)
            msg = (f"{'Добавил' if action == 'add' else 'Убрал'}: {', '.join(ok)}." if ok else "")
            return (msg + (f" Пропустил: {', '.join(bad)}." if bad else "")).strip() or "Никого не назвали."
        if action == "clear":
            set_owned([])
            return "Список ваших палов очищен."
        return ("Ваши палы: " + ", ".join(d.label(i, False) for i in cur) + ".") if cur else \
            "Список ваших палов пуст. Скажите, например: «у меня есть Анубис и Пенкинг»."

    # ---- ответы ----
    def need(name):
        i = db().find(name)
        if i is None:
            raise LookupError(f"Не знаю пала «{name}»")
        return i

    def breed(p1, p2):
        return db().breed_text(need(p1), need(p2))

    def parents(child, limit=8, only_owned=False):
        d = db()
        c = need(child)
        mine = owned()
        rows = d.parents(c, mine)
        if only_owned:
            rows = [r for r in rows if r[0] == 2]
        rows = [r for r in rows if not (r[1] == r[2] == c)] or rows
        if not rows:
            return f"Из ваших палов {d.label(c, False)} напрямую не вывести." if only_owned else \
                f"{d.label(c)} не выводится разведением."
        if all(r[1] == r[2] == c for r in rows):
            return f"{d.label(c)} выводится только от двух {d.label(c, False)} (самец + самка)."
        mark = {2: " — оба у вас есть", 1: " — один у вас есть", 0: ""}
        lines = [f"{d.label(a, False)} + {d.label(b, False)}{mark[s]}" for s, a, b in rows[:limit]]
        tail = f"\nВсего вариантов: {len(rows)}." if len(rows) > limit else ""
        return f"{d.label(c)} получается от:\n" + "\n".join(lines) + tail

    def path(target):
        d = db()
        t = need(target)
        mine = owned()
        if not mine:
            return "Сначала назовите своих палов: «у меня есть …» (или впишите их на вкладке «Плагины»)."
        if all(r[1] == r[2] == t for r in d.parents(t)):
            return f"{d.label(t, False)} выводится только от двух таких же — сначала поймайте самца и самку."
        steps = d.path(t, mine)
        if steps is None:
            return f"За 5 поколений из ваших палов {d.label(t, False)} не получить. Добавьте ещё палов."
        if not steps:
            return f"{d.label(t, False)} у вас уже есть."
        return f"Цель «{d.label(t, False)}», шагов разведения: {len(steps)}.\n" + "\n".join(
            f"{n}. {d.label(a, False)} + {d.label(b, False)} = {d.label(c, False)}"
            for n, (a, b, c) in enumerate(steps, 1)) + "\nДля каждого шага нужны самец и самка."

    def work_key(word):
        w = norm(word)
        for k, words in WORK_WORDS.items():
            if norm(k) == w or norm(WORK_RU[k]) == w or any(w.startswith(x) or x in w for x in words.split()):
                return k
        return None

    def best(work, limit=6):
        d = db()
        k = work_key(work)
        if not k:
            return "Не понял вид работы. Есть: " + ", ".join(WORK_RU.values()) + "."
        mine = set(owned())
        rows = d.best_for(k, int(limit or 6), mine)
        return f"Лучшие для работы «{WORK_RU[k]}»: " + ", ".join(
            f"{d.label(i, False)} {lv}{' (есть у вас)' if i in mine else ''}" for lv, i in rows) + "."

    def info(name, short=False):
        return db().info(need(name), short)

    def skill(name):
        return db().skill(name) or f"Не нашёл навык «{name}»."

    def wiki(query):
        import requests
        q = str(query).strip()
        if not q:
            return "Пустой запрос."
        i = db().find(q, 0.85)
        if i is not None:
            q = db().pals[i][1]                  # вики англоязычная — ищем по английскому имени
        order = [api.config_get("wiki", "wiki.gg")] + [k for k in WIKIS if k != api.config_get("wiki", "wiki.gg")]
        errors = []
        for key in order:
            base = WIKIS.get(key)
            if not base:
                continue
            try:
                s = requests.Session()
                s.headers.update(UA)
                px = api.proxies("web")
                if px:
                    s.proxies.update(px)
                r = s.get(f"{base}/api.php", params={"action": "opensearch", "search": q, "limit": 3,
                                                     "namespace": 0, "format": "json"}, timeout=12)
                r.raise_for_status()
                titles = (r.json() or [None, []])[1]
                if not titles:
                    errors.append(f"{key}: ничего не найдено")
                    continue
                title = titles[0]
                r = s.get(f"{base}/index.php", params={"title": title, "action": "raw"}, timeout=15)
                r.raise_for_status()
                text = wikitext_plain(r.text)
                if text.lower().startswith("#redirect") or text.lower().startswith("#перенаправление"):
                    m = re.search(r"\[\[([^\]|#]+)", r.text)
                    if m:
                        title = m.group(1)
                        r = s.get(f"{base}/index.php", params={"title": title, "action": "raw"}, timeout=15)
                        text = wikitext_plain(r.text)
                more = f"\nПохожие статьи: {', '.join(titles[1:])}" if len(titles) > 1 else ""
                return (f"Вики Palworld ({key}), статья «{title}» (на английском, перескажи по-русски):\n"
                        f"{text[:3000]}\nСсылка: {base}/wiki/{title.replace(' ', '_')}{more}")
            except Exception as e:
                errors.append(f"{key}: {api.explain_proxy_error(e) if hasattr(api, 'explain_proxy_error') else e}")
        return "Вики недоступна: " + "; ".join(str(e)[:120] for e in errors)

    def notes(action, text="", index=None):
        try:
            items = json.loads(notes_file.read_text("utf-8")) if notes_file.exists() else []
        except (OSError, ValueError):
            items = []
        if action == "add":
            if not str(text).strip():
                return "Что записать?"
            items.append({"text": str(text).strip(), "time": time.strftime("%Y-%m-%d %H:%M")})
            msg = f"Записал заметку по Palworld: {str(text).strip()}"
        elif action == "remove":
            try:
                k = int(index)
                msg = f"Удалил: {items.pop(k - 1)['text']}"
            except (TypeError, ValueError, IndexError):
                return "Нет заметки с таким номером."
        elif action == "clear":
            items, msg = [], "Заметки по Palworld очищены."
        else:
            return "\n".join(f"{n}. {x['text']}" for n, x in enumerate(items, 1)) if items else "Заметок по Palworld нет."
        api.data_dir.mkdir(parents=True, exist_ok=True)
        notes_file.write_text(json.dumps(items, ensure_ascii=False, indent=1), "utf-8")
        return msg

    def guard(fn):
        def run(a):
            try:
                return fn(a or {})
            except LookupError as e:
                return str(e).strip("'\"") + ". Уточните имя (можно по-английски)."
        return run

    # ---- инструменты для модели ----
    S = {"type": "string"}
    api.add_tool("pal_info", "Справка о пале Palworld: номер, работа на базе и её уровни, характеристики, атаки по "
                 "уровням, ночной ли, пол, сила разведения. Имя по-русски или по-английски.",
                 {"name": dict(S, description="Имя пала, например Анубис или Anubis")},
                 guard(lambda a: info(a.get("name", ""))), ["name"])
    api.add_tool("pal_breed", "Кто получится, если скрестить двух палов Palworld (точная таблица из файлов игры).",
                 {"parent1": S, "parent2": S}, guard(lambda a: breed(a.get("parent1", ""), a.get("parent2", ""))),
                 ["parent1", "parent2"])
    api.add_tool("pal_parents", "Какими парами родителей вывести нужного пала. Сначала пары из палов игрока. "
                 "only_owned=true — только из того, что у игрока есть. path=true — цепочка в несколько поколений "
                 "от палов игрока до цели.",
                 {"child": dict(S, description="Кого нужно вывести"), "only_owned": {"type": "boolean"},
                  "path": {"type": "boolean"}},
                 guard(lambda a: path(a.get("child", "")) if a.get("path") else
                       parents(a.get("child", ""), only_owned=bool(a.get("only_owned")))), ["child"])
    api.add_tool("pal_best_worker", "Лучшие палы Palworld для работы на базе: разжигание, полив, посадка, "
                 "электричество, ручной труд, сбор, рубка леса, добыча, медицина, охлаждение, перенос, ферма.",
                 {"work": dict(S, description="Вид работы по-русски"), "limit": {"type": "integer"}},
                 guard(lambda a: best(a.get("work", ""), a.get("limit") or 6)), ["work"])
    api.add_tool("pal_skill", "Что делает навык Palworld: пассивка (например Виртуоз, Legend) или атака "
                 "(сила, стихия, перезарядка, у кого есть).",
                 {"name": dict(S, description="Название навыка")}, guard(lambda a: skill(a.get("name", ""))), ["name"])
    api.add_tool("palworld_wiki", "Статья вики Palworld из интернета: где найти пала или ресурс, стихия, дроп, боссы, "
                 "башни, предметы, крафт, механики. Ответ на английском — перескажи кратко по-русски.",
                 {"query": dict(S, description="Что искать, лучше по-английски: Anubis, Pal Metal Ingot, Breeding")},
                 guard(lambda a: wiki(a.get("query", ""))), ["query"])
    api.add_tool("pal_my", "Список палов игрока в Palworld: add / remove (через запятую), list, clear.",
                 {"action": {"type": "string", "enum": ["add", "remove", "list", "clear"]}, "names": S},
                 guard(lambda a: my_pals(a.get("action", "list"), a.get("names", ""))), ["action"])
    api.add_tool("pal_notes", "Заметки игрока по Palworld (цели, где база, что скрафтить): add, list, remove по номеру, clear.",
                 {"action": {"type": "string", "enum": ["add", "list", "remove", "clear"]}, "text": S,
                  "index": {"type": "integer"}},
                 guard(lambda a: notes(a.get("action", "list"), a.get("text", ""), a.get("index"))), ["action"])

    # ---- голосовые команды (быстро, без модели) ----
    def two_pals(rest):
        """«анубис и ламболл» / «джетрагон с пенкингом» -> (i, j)."""
        d = db()
        words = rest.split()
        for k in range(1, len(words)):
            if words[k] not in ("и", "с", "со", "плюс", "+", "на"):
                continue
            ia, ib = find_spoken(" ".join(words[:k]), 0.7), find_spoken(" ".join(words[k + 1:]), 0.7)
            if ia is not None and ib is not None:
                return ia, ib
        return None

    def find_spoken(s, cutoff=0.75):
        """Имя из речи: «анубиса», «пенкингом», «джетрагона» — пробуем слово как есть и без падежного окончания."""
        d = db()
        words = s.strip().split()
        forms = {" ".join(words)}
        for w in range(len(words)):
            for end in ("ами", "ями", "ом", "ем", "ой", "ей", "ам", "ям", "ов", "а", "я", "у", "ю", "е", "ы", "и"):
                if words[w].endswith(end) and len(words[w]) - len(end) >= 3:
                    forms.add(" ".join(words[:w] + [words[w][:-len(end)]] + words[w + 1:]))
        for f in forms:
            if norm(f) in d.names:
                return d.names[norm(f)]
        best, score = None, cutoff
        for f in forms:
            for m in difflib.get_close_matches(norm(f), list(d.names), n=1, cutoff=score):
                r = difflib.SequenceMatcher(None, norm(f), m).ratio()
                if r >= score:
                    best, score = d.names[m], r
        return best

    def find_many(s):
        """«пенкинг буши и ламболл» (запятые распознавание уже убрало) -> [индексы] или None, если кого-то не узнали."""
        words = [w for w in s.split() if w not in ("и", "а", "еще", "также", "плюс", "пала", "палов")]
        d = db()
        out, k = [], 0
        while k < len(words):
            for ln in (3, 2, 1):
                if k + ln <= len(words):
                    i = find_spoken(" ".join(words[k:k + ln]), 0.8)
                    # кусок из нескольких слов засчитываем, только если и само имя из стольких слов («Йормунтайд Игнис»)
                    if i is not None and ln > 1 and not any(len(nm.split()) == ln for nm in d.pals[i][1:3]):
                        i = None
                    if i is not None:
                        out.append(i)
                        k += ln
                        break
            else:
                return None
        return out or None

    def command(text, m):
        if not api.config_get("voice", True):
            return None
        t = api.norm_ru(text)
        d = db()
        mm = re.search(r"(?:кто|что) (?:получится|выйдет|родится|будет)(?: если скрестить| от| у)? (.+)$", t) or \
            re.search(r"(?:скрести\w*|скрещ\w*|разведи|спарь|спаривани\w*)(?: палов| пала)? (.+)$", t)
        if mm:
            pr = two_pals(re.sub(r"^(?:палов|пала) ", "", mm.group(1)))
            if pr:
                return d.breed_text(*pr)
        mm = re.search(r"(?:как|чем|кем|от кого) (?:вывести|получить|развести|скрестить|вывод\w*) (.+)$", t) or \
            re.search(r"(?:от кого|из кого) (?:получается|выводится|рождается) (.+)$", t)
        if mm:
            i = find_spoken(re.sub(r" (?:разведением|в палворлд\w*|палворлд\w*)$", "", mm.group(1)))
            if i is not None:
                rows = [r for r in d.parents(i, owned()) if not (r[1] == r[2] == i)]
                if not rows:
                    return f"{d.label(i, False)} выводится только от двух таких же."
                top = "; ".join(f"{d.label(a, False)} и {d.label(b, False)}" for _, a, b in rows[:3])
                own = " Из ваших палов подходит первая пара." if rows[0][0] == 2 else ""
                return f"{d.label(i, False)}: {top}. Всего вариантов {len(rows)}.{own}"
        mm = re.search(r"(?:путь|цепочк\w*) (?:до|к|для) (.+)$", t)
        if mm:
            i = find_spoken(mm.group(1))
            if i is not None:
                return path(d.pals[i][1])
        mm = re.search(r"(?:кто|какой пал|какого пала|какие палы)\w* (?:лучше(?: всего)?|лучший|самый лучший|"
                       r"хорош\w*|подходит|подойдет)(?: всех)? (?:для|на|в) (.+)$", t)
        if mm and work_key(mm.group(1).split()[0]):
            lead = best(mm.group(1).split()[0], 4)
            return lead
        mm = re.search(r"(?:у меня есть|я поймал\w*|добавь в мои палы|запомни пала|запомни палов) (.+)$", t)
        if mm:
            ids = find_many(mm.group(1))
            if ids:
                return my_pals("add", ", ".join(d.pals[i][1] for i in ids))
        mm = re.search(r"(?:убери из моих палов|у меня больше нет|я продал\w*|удали из моих палов) (.+)$", t)
        if mm:
            ids = find_many(mm.group(1))
            if ids:
                return my_pals("remove", ", ".join(d.pals[i][1] for i in ids))
        if re.search(r"^(?:какие у меня палы|мои палы|список моих палов)$", t):
            return my_pals("list")
        mm = re.search(r"(?:что (?:умеет|может|за пал)|расскажи (?:про|о) (?:пала )?|какая работа у|кто такой) (.+)$", t)
        if mm:
            i = find_spoken(mm.group(1), 0.8)
            if i is not None:
                return d.info(i, short=True)
        mm = re.search(r"что (?:дает|делает|значит) (?:пассивк\w*|пассивн\w* навык|навык|умение|скилл?) (.+)$", t)
        if mm:
            r = d.skill(mm.group(1))
            if r:
                return r
        mm = re.search(r"(?:запиши|запомни) (?:в заметки )?(?:по )?палворлд\w* (.+)$", t)
        if mm:
            return notes("add", mm.group(1))
        if re.search(r"(?:заметки|мои заметки) (?:по )?палворлд\w*", t):
            return notes("list")
        return None

    api.add_command(r"(?:получится|выйдет|родится|скрест|скрещ|разведи|спар|вывести|получить|развести|выводится|"
                    r"получается|рождается|путь|цепочк|лучше|лучший|хорош|подходит|подойдет|у меня есть|поймал|"
                    r"убери из моих|больше нет|продал|удали из моих|"
                    r"мои палы|моих палов|запомни|запиши|заметки|умеет|может|за пал|расскажи|работа у|кто такой|"
                    r"пассив|навык|умение|скилл)", command,
                    "кто получится от X и Y / как вывести X / путь до X / кто лучше для добычи / что умеет X / "
                    "что даёт пассивка X / у меня есть X / мои палы / запиши палворлд … / заметки палворлд")

    # ---- обновление базы ----
    def update_db(done):
        import requests
        try:
            s = requests.Session()
            s.headers.update(UA)
            px = api.proxies("web")
            if px:
                s.proxies.update(px)
            raw = {}
            for f in ("db.json", "breeding.json"):
                r = s.get(RAW_DB.format(f), timeout=120)
                r.raise_for_status()
                raw[f] = json.loads(r.content.decode("utf-8-sig"))
            data = convert_palcalc(raw["db.json"], raw["breeding.json"]["Breeding"], time.strftime("%Y-%m-%d"))
            api.data_dir.mkdir(parents=True, exist_ok=True)
            db_file.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")), "utf-8")
            with state["lock"]:
                state["db"] = None
            msg = f"База обновлена: {len(data['pals'])} палов."
        except Exception as e:
            msg = f"Не удалось обновить базу: {str(e)[:200]}"
        api.log(msg)
        api.post(done, msg)

    def build(f):
        import tkinter as tk
        from tkinter import ttk, messagebox
        pad = {"padx": 6, "pady": 2}
        d = db()
        info_var = tk.StringVar(value=f"База: {d.n} палов, {len(d.skills)} атак, {len(d.passives)} пассивок; "
                                      f"данные от {d.d.get('date') or '?'} ({'скачанная' if db_file.exists() else 'встроенная'}).")
        ttk.Label(f, textvariable=info_var).grid(row=0, column=0, columnspan=4, sticky="w", **pad)
        ttk.Checkbutton(f, text="Голосовые команды без модели («кто получится от …», «как вывести …», «кто лучше для добычи»)",
                        variable=api.var("voice")).grid(row=1, column=0, columnspan=4, sticky="w", **pad)
        ttk.Label(f, text="Вики:").grid(row=2, column=0, sticky="w", **pad)
        ttk.Combobox(f, textvariable=api.var("wiki"), values=list(WIKIS), width=10, state="readonly").grid(
            row=2, column=1, sticky="w", **pad)
        ttk.Label(f, text="Мои палы (через запятую):").grid(row=3, column=0, sticky="w", **pad)
        ttk.Entry(f, textvariable=api.var("my_pals"), width=70).grid(row=3, column=1, columnspan=3, sticky="w", **pad)

        def upd():
            btn.state(["disabled"])
            info_var.set("Скачиваю базу (около 10 МБ)…")

            def done(msg):
                btn.state(["!disabled"])
                info_var.set(msg)
                (messagebox.showinfo if msg.startswith("База") else messagebox.showerror)("Palworld помощник", msg)
            threading.Thread(target=update_db, args=(done,), daemon=True).start()
        btn = ttk.Button(f, text="Обновить базу из интернета", command=upd)
        btn.grid(row=4, column=0, sticky="w", **pad)

        def reset():
            if db_file.exists() and messagebox.askyesno("Palworld помощник", "Вернуть встроенную базу?"):
                db_file.unlink()
                with state["lock"]:
                    state["db"] = None
                info_var.set("Встроенная база восстановлена.")
        ttk.Button(f, text="Встроенная база", command=reset).grid(row=4, column=1, sticky="w", **pad)
        ttk.Label(f, text=("Таблица разведения и характеристики — из файлов игры (проект PalCalc, лицензия MIT), поэтому "
                           "считаются точно, включая особые пары и пару, зависящую от пола. Стихии, локации и дроп "
                           "берутся с вики через интернет (с прокси агента, если он задан). Если после патча игры "
                           "появились новые палы — нажмите «Обновить базу». Инструменты для модели работают при галочке "
                           "«Разрешить модели вызывать инструменты»."),
                  foreground="#777", wraplength=780, justify="left").grid(row=5, column=0, columnspan=4, sticky="w", **pad)
    api.add_settings(build)


def wikitext_plain(wt):
    """Вики-разметка -> текст: поля инфобокса «ключ: значение» + очищенный текст статьи."""
    fields = []
    for m in re.finditer(r"^\|\s*([\w ]{2,30}?)\s*=\s*([^\n{}]{1,150})$", wt, re.M):
        v = re.sub(r"\[\[(?:[^|\]]*\|)?([^\]]*)\]\]", r"\1", m.group(2)).strip()
        if v and not v.startswith(("<", "File:")):
            fields.append(f"{m.group(1).strip()}: {v}")
    body = wt
    for _ in range(4):
        body = re.sub(r"\{\{[^{}]*\}\}", "", body)
    body = re.sub(r"\{\|.*?\|\}", "", body, flags=re.S)
    body = re.sub(r"<ref[^>]*/>|<ref[^>]*>.*?</ref>|<!--.*?-->|<[^>]+>", "", body, flags=re.S)
    body = re.sub(r"\[\[(?:File|Image|Category|Файл|Категория):[^\]]*\]\]", "", body)
    body = re.sub(r"\[\[(?:[^|\]]*\|)?([^\]]*)\]\]", r"\1", body)
    body = re.sub(r"\[https?://\S+ ([^\]]+)\]", r"\1", body)
    body = re.sub(r"'{2,}", "", body)
    body = re.sub(r"^=+\s*(.*?)\s*=+$", r"\n## \1", body, flags=re.M)
    body = re.sub(r"\n{3,}", "\n\n", body).strip()
    head = "\n".join(fields[:30])
    return (head + "\n\n" + body).strip() if head else body


# Сжатая база (zlib + base64). Не редактируйте вручную — пользуйтесь кнопкой «Обновить базу».
DB_B64 = "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"
