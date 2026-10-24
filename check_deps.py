#!/usr/bin/env python3
"""Проверка зависимостей для voice_agent.py и установка недостающих.

Запуск:   py -3.12 check_deps.py            (спросит, что установить)
          py -3.12 check_deps.py --yes      (поставить всё недостающее без вопросов)
          py -3.12 check_deps.py --check    (только проверить, ничего не ставить)
          py -3.12 check_deps.py --required (при --yes ставить только обязательное)
Показывает также, чего не хватает каждому плагину, и расходятся ли файлы в plugins/ со встроенными версиями.
Использует только стандартную библиотеку. Ставит пакеты тем же Python, которым запущен.
"""
import argparse
import importlib.metadata as md
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.request

# (pip-пакет, модуль(и) для импорта — достаточно любого, зачем нужен, обязательный, где работает, для каких плагинов)
WIN, ANY = "win", "any"
PACKAGES = [
    ("numpy", ("numpy",), "обработка звука", True, ANY, ""),
    ("requests", ("requests",), "связь с LM Studio, Voicebox и интернетом", True, ANY, "почти все плагины"),
    ("psutil", ("psutil",), "мониторинг CPU/RAM", True, ANY, "game_mode"),
    ("sounddevice", ("sounddevice",), "микрофон и воспроизведение", True, ANY, ""),
    ("soundfile", ("soundfile",), "чтение/запись звука (в т. ч. голосовые Telegram в формате Opus)", True, ANY, "telegram, web_chat"),
    ("faster-whisper", ("faster_whisper",), "распознавание речи", True, ANY, ""),
    ("pillow", ("PIL",), "скриншоты и спрайты аватара (экран, 2D-аватар)", False, ANY, "dating_check, telegram (скриншоты), match3, screen_vision"),
    ("pynput", ("pynput",), "горячая клавиша, управление мышью и клавиатурой", False, ANY, ""),
    ("nvidia-ml-py", ("pynvml",), "мониторинг видеокарты NVIDIA", False, ANY, ""),
    ("sv-ttk", ("sv_ttk",), "современная тема окна (светлая/тёмная)", False, ANY, ""),
    ("pystray", ("pystray",), "значок в системном трее (сворачивание в трей)", False, ANY, ""),
    ("pypdf", ("pypdf",), "чтение PDF-файлов (папка файлов агента, поиск по документам)", False, ANY, "doc_search"),
    ("pysocks", ("socks",), "SOCKS5-прокси для интернет-запросов агента (Telegram, поиск)", False, ANY, "vpn_proxy (ручной режим), telegram"),
    ("cryptography", ("cryptography",), "HTTPS для веб-чата (самоподписанный сертификат)", False, ANY, "web_chat"),
    ("winrt-runtime", ("winrt.runtime", "winsdk"), "системный медиа-центр Windows (основа)", False, WIN, "media_player"),
    ("winrt-Windows.Media.Control", ("winrt.windows.media.control", "winsdk"), "«что играет», управление плеером", False, WIN, "media_player"),
    ("winrt-Windows.Foundation", ("winrt.windows.foundation", "winsdk"), "типы WinRT для плеера", False, WIN, "media_player"),
    ("winrt-Windows.Media.Ocr", ("winrt.windows.media.ocr", "winsdk"), "встроенное распознавание текста Windows (чтение экрана)", False, WIN, "screen_ocr"),
    ("winrt-Windows.Graphics.Imaging", ("winrt.windows.graphics.imaging", "winsdk"), "картинки для распознавания текста", False, WIN, "screen_ocr"),
    ("winrt-Windows.Storage.Streams", ("winrt.windows.storage.streams", "winsdk"), "потоки для распознавания текста", False, WIN, "screen_ocr"),
    ("winrt-Windows.Globalization", ("winrt.windows.globalization", "winsdk"), "языки распознавания текста", False, WIN, "screen_ocr"),
    ("uiautomation", ("uiautomation",), "дерево элементов окон Windows (инструмент «Зрение»)", False, WIN, "screen_vision"),
    ("yt-dlp", ("yt_dlp",), "субтитры и звук видео для пересказа (YouTube, VK Видео, Rutube)", False, ANY, "video_summary"),
    ("pyinstaller", ("PyInstaller",), "сборка агента в exe (py build_exe.py)", False, ANY, ""),
    ("nvidia-cublas-cu12", ("nvidia.cublas",), "Whisper на GPU (CUDA 12), только Windows/Linux с NVIDIA", False, ANY, ""),
    ("nvidia-cudnn-cu12==9.*", ("nvidia.cudnn",), "Whisper на GPU (CUDA 12), только Windows/Linux с NVIDIA", False, ANY, ""),
]

# что нужно каждому плагину: обязательные пакеты Python и подсказки (необязательные пакеты — в колонке выше)
PLUGIN_NEEDS = {
    "chrome": ("requests",), "cloud_llm": ("requests",), "dating_check": ("requests", "pillow"),
    "doc_search": ("pypdf",), "game_mode": ("psutil",), "home_assistant": ("requests",),
    "mail_calendar": ("requests",), "mcp": ("requests",), "media_player": ("winrt-runtime", "winrt-Windows.Media.Control",
                                                                         "winrt-Windows.Foundation"),
    "model_watch": ("requests",), "palworld": ("requests",), "palworld_guide": ("requests",), "search_api": ("requests",),
    "calc_convert": (), "file_tools": (), "net_check": ("requests",), "weather_rates": ("requests",),
    "screen_ocr": ("pillow", "winrt-Windows.Media.Ocr", "winrt-Windows.Graphics.Imaging", "winrt-Windows.Storage.Streams",
                   "winrt-Windows.Globalization"),
    "video_summary": ("requests", "yt-dlp"),
    "screen_vision": ("pillow", "uiautomation"),
    "match3": ("pillow",),
    "system_ctl": (), "telegram": ("requests",), "vk": ("requests",), "vpn_proxy": ("requests",),
    "web_chat": ("numpy",),
}

OK, BAD, WARN = "[ OK ]", "[ -- ]", "[ !! ]"
HERE = os.path.dirname(os.path.abspath(__file__))
IS_WIN = os.name == "nt"


def has_module(name):
    """name — модуль или кортеж модулей (достаточно любого). Родительские пакеты НЕ импортируются:
    find_spec("a.b.c") импортирует a и a.b, а у winrt-* это может зависнуть, поэтому ищем по файлам."""
    if isinstance(name, (tuple, list)):
        return any(has_module(n) for n in name)
    parts = str(name).split(".")
    try:
        spec = importlib.util.find_spec(parts[0])
    except (ImportError, ValueError, AttributeError):
        return False
    if spec is None:
        return False
    if len(parts) == 1:
        return True
    locs = list(spec.submodule_search_locations or [])
    for i, part in enumerate(parts[1:], 1):
        nxt, found = [], False
        for loc in locs:
            d = os.path.join(loc, part)
            if os.path.isdir(d):
                nxt.append(d)
                found = True
            elif i == len(parts) - 1 and any(os.path.isfile(os.path.join(loc, part + ext)) for ext in (".py", ".pyd", ".pyc")):
                return True
        if not found:
            return False
        locs = nxt
    return True


def version_of(pip_name):
    base = pip_name.split("==")[0].split(">=")[0]
    for cand in (base, "winsdk" if base.startswith("winrt") else base):
        try:
            return md.version(cand)
        except md.PackageNotFoundError:
            continue
    return ""


def http_ok(url, timeout=2):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return r.status < 500
    except Exception:
        return False


def ask(question, default=True):
    suffix = " [Д/н] " if default else " [д/Н] "
    try:
        a = input(question + suffix).strip().lower()
    except EOFError:
        return default
    if not a:
        return default
    return a in ("д", "да", "y", "yes", "1")


def pip_install(specs):
    cmd = [sys.executable, "-m", "pip", "install", "--upgrade-strategy", "only-if-needed"]
    if sys.version_info >= (3, 11) and sys.platform.startswith("linux"):
        cmd.append("--break-system-packages")
    cmd += specs
    print("\n>", " ".join(cmd))
    return subprocess.call(cmd) == 0


def applicable(row):
    return row[4] == ANY or (row[4] == WIN and IS_WIN)


def read_text(path):
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except OSError:
        return ""


def agent_info():
    """(версия агента, {плагин: версия встроенного}) — из voice_agent.py рядом со скриптом."""
    src = read_text(os.path.join(HERE, "voice_agent.py"))
    if not src:
        return None, {}
    m = re.search(r'^APP_VERSION\s*=\s*"([^"]+)"', src, re.M)
    bundled = {}
    start = src.find("BUNDLED_PLUGINS = {")
    if start >= 0:
        chunk = src[start:]
        for mm in re.finditer(r"^    '([\w]+)\.py': r\'\'\'", chunk, re.M):
            v = re.search(r'"version":\s*"([^"]+)"', chunk[mm.end():mm.end() + 4000])
            if v:
                bundled[mm.group(1)] = v.group(1)
    return (m.group(1) if m else "?"), bundled


def plugin_file_version(path):
    m = re.search(r'"version":\s*"([^"]+)"', read_text(path)[:6000])
    return m.group(1) if m else "?"


def check_plugins(have, bundled):
    """Для каждого встроенного плагина: чего не хватает, и не расходится ли файл в plugins/ со встроенной версией."""
    print("\nПлагины (что нужно каждому):")
    pdir = os.path.join(HERE, "plugins")
    names = sorted(set(bundled) | set(PLUGIN_NEEDS))
    stale = []
    for n in names:
        miss = [p for p in PLUGIN_NEEDS.get(n, ()) if p not in have]
        ver = bundled.get(n, "")
        if n in ("media_player", "screen_ocr", "screen_vision") and not IS_WIN:
            note = "работает только в Windows"
            print(f"{WARN} {n:<16} {ver:<5} {note}")
            continue
        extra = ""
        f = os.path.join(pdir, n + ".py")
        if os.path.isfile(f) and ver:
            fv = plugin_file_version(f)
            if fv != ver:
                extra = f"  | файл plugins/{n}.py версии {fv}, встроена {ver}: удалите файл, если не правили его сами"
                stale.append(n)
        if miss:
            print(f"{BAD} {n:<16} {ver:<5} не хватает: {', '.join(miss)}{extra}")
        else:
            print(f"{OK} {n:<16} {ver:<5} готов{extra}")
    return stale


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--yes", "-y", action="store_true", help="ставить недостающее без вопросов")
    ap.add_argument("--check", action="store_true", help="только проверить")
    ap.add_argument("--required", action="store_true", help="с --yes: ставить только обязательное")
    ap.add_argument("--json", action="store_true", help="вывести состояние пакетов в JSON (для установщика с окном)")
    args = ap.parse_args()

    if args.json:
        import faulthandler
        faulthandler.dump_traceback_later(25, exit=False, file=sys.stderr)       # если зависли — в журнал установщика попадёт, где именно

        def step(t):
            sys.stderr.write(f"[шаг] {t}\n")
            sys.stderr.flush()
        step("tkinter")
        try:
            import tkinter  # noqa: F401
            has_tk = True
        except ImportError:
            has_tk = False
        step("чтение voice_agent.py")
        ver, bundled = agent_info()
        rows = []
        for row in PACKAGES:
            if not applicable(row):
                continue
            pip_name, mods, why, required, _, plug = row
            step(f"пакет {pip_name}")
            ok = has_module(mods)
            rows.append({"pip": pip_name, "required": required, "installed": ok, "version": version_of(pip_name) if ok else "",
                         "why": why, "plugins": plug,
                         "default": not pip_name.startswith(("nvidia-", "pyinstaller"))})
        faulthandler.cancel_dump_traceback_later()
        print(json.dumps({"python": sys.version.split()[0], "executable": sys.executable, "tkinter": has_tk,
                          "agent_version": ver, "packages": rows}, ensure_ascii=False))
        sys.stdout.flush()
        sys.stderr.flush()
        os._exit(0)             # не ждём фоновых потоков импортированных библиотек: из-за них процесс мог не завершаться

    print(f"Python {sys.version.split()[0]} ({sys.executable})")
    ver, bundled = agent_info()
    if ver:
        print(f"Агент: voice_agent.py, версия {ver}, встроенных плагинов: {len(bundled)}")
    else:
        print(f"{WARN} voice_agent.py рядом со скриптом не найден — проверка плагинов по версиям пропущена")
    problems = False

    if sys.version_info < (3, 9):
        print(f"{BAD} Нужен Python 3.9 или новее (рекомендуется 3.12)")
        problems = True
    else:
        print(f"{OK} Версия Python подходит")
        if sys.version_info >= (3, 13):
            print(f"{WARN} Python {sys.version_info.major}.{sys.version_info.minor}: часть пакетов (faster-whisper, nvidia-*, winsdk) "
                  "может не иметь сборок — если установка падает, используйте 3.12")

    try:
        import tkinter  # noqa: F401
        print(f"{OK} tkinter (окно программы)")
    except ImportError:
        print(f"{BAD} tkinter не найден — переустановите Python с python.org и отметьте «tcl/tk and IDLE»")
        problems = True

    if importlib.util.find_spec("pip") is None:
        print(f"{BAD} pip не найден: {sys.executable} -m ensurepip --upgrade")
        problems = True

    print("\nПакеты Python:")
    missing_req, missing_opt, have = [], [], set()
    for row in PACKAGES:
        pip_name, mods, why, required, _, plug = row
        if not applicable(row):
            continue
        extra = f"  [{plug}]" if plug else ""
        if has_module(mods):
            have.add(pip_name)
            v = version_of(pip_name)
            print(f"{OK} {pip_name:<28} {v:<10} {why}{extra}")
        else:
            tag = "обязательный" if required else "необязательный"
            print(f"{BAD} {pip_name:<28} {'нет':<10} {why} ({tag}){extra}")
            (missing_req if required else missing_opt).append(pip_name)

    stale = check_plugins(have, bundled)
    if stale:
        print("\nВерсии файлов в plugins/ расходятся со встроенными. Агент не перезаписывает файлы, которые вы правили, "
              "поэтому после обновления voice_agent.py удалите неизменённые файлы из plugins/ — он поставит свежие.")

    print("\nВнешние программы и службы (информационно):")
    print(("%s LM Studio: сервер отвечает на localhost:1234" % OK) if http_ok("http://localhost:1234/v1/models")
          else "%s LM Studio: не отвечает на localhost:1234 (запустите сервер: Developer → Start Server)" % WARN)
    print(("%s Voicebox: отвечает на localhost:17493" % OK) if http_ok("http://localhost:17493/health")
          else "%s Voicebox: не отвечает на localhost:17493 (запустите Voicebox)" % WARN)
    browser = shutil.which("msedge") or shutil.which("chrome") or shutil.which("google-chrome")
    if not browser and IS_WIN:
        for p in (r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
                  r"C:\Program Files\Google\Chrome\Application\chrome.exe",
                  r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"):
            if os.path.exists(p):
                browser = p
                break
    print(("%s Браузер для 3D-аватара и плагина Chrome: %s" % (OK, browser)) if browser
          else "%s Edge/Chrome не найден — 3D-аватар можно открыть вручную, плагин Chrome не заработает" % WARN)
    core_dir = os.path.join(HERE, "plugins", "data", "vpn_proxy", "core")
    found = [n for n in ("xray.exe", "xray", "sing-box.exe", "sing-box") if os.path.isfile(os.path.join(core_dir, n))]
    print(("%s Ядро для плагина VPN: %s" % (OK, found[0])) if found
          else "%s Ядро Xray/sing-box не скачано (нужно только плагину vpn_proxy: кнопка «Скачать» в его настройках)" % WARN)
    try:
        import sounddevice as sd
        devs = sd.query_devices()
        n_in = sum(1 for d in devs if d["max_input_channels"] > 0)
        n_out = sum(1 for d in devs if d["max_output_channels"] > 0)
        print(("%s Звук: микрофонов %d, устройств вывода %d" % (OK, n_in, n_out)) if n_in and n_out
              else "%s Звук: микрофонов %d, устройств вывода %d — проверьте настройки звука Windows" % (WARN, n_in, n_out))
    except Exception:
        pass

    if not missing_req and not missing_opt:
        print("\nВсе зависимости на месте." if not problems else "\nПакеты в порядке, но есть замечания выше.")
        return 1 if problems else 0

    print()
    if args.check:
        print("Не хватает: " + ", ".join(missing_req + missing_opt))
        print("Установить: запустите скрипт без --check.")
        return 1

    to_install = []
    if missing_req:
        if args.yes or ask("Установить обязательные пакеты (%s)?" % ", ".join(missing_req)):
            to_install += missing_req
    if missing_opt and not (args.yes and args.required):
        if args.yes or ask("Установить необязательные пакеты (%s)?" % ", ".join(missing_opt), default=True):
            to_install += missing_opt

    if not to_install:
        print("Ничего не устанавливаем.")
        return 1 if missing_req else 0

    failed = []
    # одним вызовом быстрее; при ошибке пробуем по одному, чтобы понять, какой пакет не встал
    if not pip_install(to_install):
        print("\nОбщая установка не удалась — пробую по одному…")
        for spec in to_install:
            if not pip_install([spec]):
                failed.append(spec)

    print("\nПовторная проверка:")
    still = [r[0] for r in PACKAGES if r[0] in to_install and not has_module(r[1])]
    for p in still:
        print(f"{BAD} {p} всё ещё не установлен")
    if not still:
        print(f"{OK} Всё выбранное установлено.")
    if failed:
        print("\nНе удалось поставить: " + ", ".join(failed))
        print("Подсказки: проверьте интернет; для sounddevice/soundfile на Windows нужен Python 64-бит;")
        print("nvidia-* нужны только для GPU — можно пропустить и выбрать Whisper на cpu;")
        print("winrt-* для плеера: если не ставятся, попробуйте pip install winsdk (Python 3.12 или старше).")
    return 1 if (still and any(PACKAGES_REQ(p) for p in still)) else 0


def PACKAGES_REQ(name):
    return any(r[0] == name and r[3] for r in PACKAGES)


if __name__ == "__main__":
    try:
        code = main()
    except KeyboardInterrupt:
        code = 130
    if IS_WIN and sys.stdin and sys.stdin.isatty() and "--yes" not in sys.argv and "-y" not in sys.argv:
        input("\nНажмите Enter, чтобы закрыть…")
    sys.exit(code)
