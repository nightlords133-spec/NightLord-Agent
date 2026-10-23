# Инструмент Nightlord агента: чтение текста с экрана, активного окна или картинки. Распознаёт локально (ничего не уходит в сеть).
# Движки по порядку: встроенный распознаватель Windows (пакеты winrt) -> Tesseract (pytesseract + программа tesseract).
# Установка: py -3.12 -m pip install pillow winrt-runtime winrt-Windows.Media.Ocr winrt-Windows.Graphics.Imaging winrt-Windows.Storage.Streams winrt-Windows.Globalization winrt-Windows.Foundation
import asyncio
import io
import os
import platform
import re
from pathlib import Path

PLUGIN = {"name": "Чтение текста с экрана", "version": "1.0", "group": "tools",
          "description": "«прочитай экран», «что написано на экране», «прочитай активное окно», «прочитай картинку скриншот.png» (из папки файлов агента). "
                         "Распознавание локальное: встроенный OCR Windows (нужны пакеты winrt) или Tesseract. Скриншоты не сохраняются и никуда не отправляются."}

INSTALL = ("Нужен движок распознавания. Встроенный OCR Windows: py -3.12 -m pip install pillow winrt-runtime winrt-Windows.Media.Ocr "
           "winrt-Windows.Graphics.Imaging winrt-Windows.Storage.Streams winrt-Windows.Globalization winrt-Windows.Foundation. "
           "Либо Tesseract: программа tesseract (с языком rus) и pip install pytesseract pillow. Потом перезапустите агента.")
IMG_EXT = {".png", ".jpg", ".jpeg", ".bmp", ".gif", ".webp", ".tif", ".tiff"}


def grab(mode):
    """Снимок экрана (PIL.Image). mode: screen | window."""
    from PIL import ImageGrab
    if mode == "window" and platform.system() == "Windows":
        import ctypes
        from ctypes import wintypes
        u = ctypes.windll.user32
        h = u.GetForegroundWindow()
        r = wintypes.RECT()
        if h and u.GetWindowRect(h, ctypes.byref(r)) and r.right > r.left and r.bottom > r.top:
            return ImageGrab.grab(bbox=(max(r.left, 0), max(r.top, 0), r.right, r.bottom), all_screens=True)
    return ImageGrab.grab(all_screens=True)


def to_png(img):
    b = io.BytesIO()
    img.convert("RGB").save(b, "PNG")
    return b.getvalue()


def ocr_windows(png, lang="ru"):
    """Встроенный распознаватель Windows. Возвращает текст; ImportError — если пакетов нет."""
    from winrt.windows.globalization import Language
    from winrt.windows.graphics.imaging import BitmapDecoder
    from winrt.windows.media.ocr import OcrEngine
    from winrt.windows.storage.streams import DataWriter, InMemoryRandomAccessStream

    async def run():
        st = InMemoryRandomAccessStream()
        w = DataWriter(st)
        w.write_bytes(bytearray(png))
        await w.store_async()
        w.detach_stream()
        st.seek(0)
        dec = await BitmapDecoder.create_async(st)
        bmp = await dec.get_software_bitmap_async()
        eng = None
        if lang and lang != "auto":
            try:
                eng = OcrEngine.try_create_from_language(Language(lang))
            except Exception:
                eng = None
        eng = eng or OcrEngine.try_create_from_user_profile_languages()
        if eng is None:
            raise RuntimeError("в Windows не установлен языковой пакет с распознаванием текста (Параметры → Время и язык)")
        res = await eng.recognize_async(bmp)
        return res.text
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(run())
    finally:
        loop.close()


def ocr_tesseract(png, lang="rus+eng"):
    import pytesseract
    from PIL import Image
    return pytesseract.image_to_string(Image.open(io.BytesIO(png)), lang=lang)


def tidy(t):
    t = re.sub(r"[ \t]+", " ", str(t or ""))
    t = "\n".join(l.strip() for l in t.splitlines() if l.strip())
    return t.strip()


def recognize(png, engine="auto", lang="ru"):
    """-> (текст, название движка). RuntimeError с понятным текстом, если нечем распознавать."""
    errors = []
    if engine in ("auto", "windows"):
        try:
            return tidy(ocr_windows(png, lang)), "Windows OCR"
        except ImportError:
            errors.append("нет пакетов winrt")
        except Exception as e:
            errors.append(f"Windows OCR: {e}")
    if engine in ("auto", "tesseract"):
        try:
            return tidy(ocr_tesseract(png, "rus+eng" if lang in ("ru", "auto") else "eng+rus")), "Tesseract"
        except ImportError:
            errors.append("нет pytesseract")
        except Exception as e:
            errors.append(f"Tesseract: {e}")
    raise RuntimeError("; ".join(errors) + ". " + INSTALL)


def register(api):
    for k in ("screen_on", "window_on", "file_on"):
        api.var(k, True)
    api.var("engine", "auto")
    api.var("lang", "ru")
    api.var("max_chars", 3000)
    api.var("read_aloud", True)

    def limit():
        try:
            return max(200, min(int(api.config_get("max_chars", 3000) or 3000), 20000))
        except (TypeError, ValueError):
            return 3000

    def result(png, what):
        try:
            text, eng = recognize(png, str(api.config_get("engine", "auto")), str(api.config_get("lang", "ru")))
        except RuntimeError as e:
            return f"Не прочитал ({what}): {e}"
        if not text:
            return f"Текста не нашёл ({what}, {eng})."
        cut = len(text) > limit()
        return f"Текст с {what} ({eng}):\n{text[:limit()]}" + ("\n…(дальше обрезано)" if cut else "")

    def screen(args):
        mode = "window" if str(args.get("target", "")).lower() == "window" else "screen"
        if not api.config_get("window_on" if mode == "window" else "screen_on", True):
            return "Эта функция выключена в настройках инструмента."
        try:
            img = grab(mode)
        except ImportError:
            return "Для снимка экрана нужен Pillow: py -3.12 -m pip install pillow."
        except Exception as e:
            return f"Снимок экрана не получился: {e}."
        return result(to_png(img), "активного окна" if mode == "window" else "экрана")

    def image(args):
        if not api.config_get("file_on", True):
            return "Чтение картинок выключено в настройках инструмента."
        name = str(args.get("file") or "").strip().strip("\"'")
        if not name:
            return "Какую картинку читать? Назовите файл из папки файлов агента."
        base = Path(os.path.realpath(api.files_dir()))
        try:
            p = Path(os.path.realpath(base / name))
            p.relative_to(base)
        except ValueError:
            return "Нельзя: файл за пределами папки файлов агента."
        if p.suffix.lower() not in IMG_EXT or not p.is_file():
            return f"Картинки «{name}» в папке файлов нет."
        if p.stat().st_size > 30 * 1024 * 1024:
            return "Файл слишком большой (лимит 30 МБ)."
        try:
            from PIL import Image
            with Image.open(p) as im:
                png = to_png(im)
        except ImportError:
            return "Нужен Pillow: py -3.12 -m pip install pillow."
        except Exception as e:
            return f"Не открыл картинку: {e}."
        return result(png, f"картинки {p.name}")

    api.add_tool("read_screen_text", "Прочитать (распознать) весь текст, видимый сейчас на экране ПК. target=window — только активное окно.",
                 {"target": {"type": "string", "description": "screen (по умолчанию) или window"}}, screen)
    api.add_tool("read_image_text", "Распознать текст на картинке из папки файлов агента.", {"file": {"type": "string"}}, image, ["file"])

    def command(text, m):
        t = api.norm_ru(text).strip().rstrip("?.!")
        if re.match(r"^(?:прочитай|распознай|считай) (?:мне )?(?:активное окно|окно|текущее окно)$", t):
            return screen({"target": "window"})
        if re.match(r"^(?:прочитай|распознай|считай) (?:мне )?(?:текст )?(?:с )?экран\w*$", t) or t in ("что написано на экране", "что на экране написано"):
            return screen({})
        mm = re.match(r"^(?:прочитай|распознай) (?:картинку|изображение|скриншот|фото) (.+)$", t)
        if mm:
            return image({"file": mm.group(1)})
        return None

    api.add_command(r"(?:прочитай|распознай|считай|что написано на экране|что на экране)", command,
                    "прочитай экран / прочитай активное окно / прочитай картинку имя.png")

    def build(fr):
        from tkinter import ttk
        pad = {"padx": 6, "pady": 2}
        for i, (k, txt) in enumerate((("screen_on", "Читать весь экран"), ("window_on", "Читать активное окно"),
                                      ("file_on", "Читать картинки из папки файлов агента"))):
            ttk.Checkbutton(fr, text=txt, variable=api.var(k)).grid(row=i, column=0, columnspan=3, sticky="w", **pad)
        ttk.Label(fr, text="Движок:").grid(row=3, column=0, sticky="w", **pad)
        names = {"auto": "Автоматически", "windows": "Только Windows OCR", "tesseract": "Только Tesseract"}
        cb = ttk.Combobox(fr, values=list(names.values()), state="readonly", width=24)
        cb.set(names.get(str(api.config_get("engine", "auto")), names["auto"]))
        cb.grid(row=3, column=1, sticky="w", **pad)
        cb.bind("<<ComboboxSelected>>", lambda e: api.var("engine").set(list(names)[cb.current()]))
        ttk.Label(fr, text="Язык (ru, en, auto):").grid(row=4, column=0, sticky="w", **pad)
        ttk.Entry(fr, textvariable=api.var("lang"), width=8).grid(row=4, column=1, sticky="w", **pad)
        ttk.Label(fr, text="Максимум знаков в ответе:").grid(row=5, column=0, sticky="w", **pad)
        ttk.Spinbox(fr, from_=200, to=20000, increment=500, width=8, textvariable=api.var("max_chars")).grid(row=5, column=1, sticky="w", **pad)
        ttk.Label(fr, text=("Распознанный текст попадает в диалог с моделью. Если на экране пароли или личная переписка, а модель облачная — "
                            "выключайте «Читать весь экран». " + INSTALL + " На реальном Windows я это не проверял: в песочнице проверена только логика."),
                  foreground="#777", wraplength=720, justify="left").grid(row=6, column=0, columnspan=3, sticky="w", **pad)
    api.add_settings(build)
