"""
Сборка Nightlord агента в exe (Windows) через PyInstaller.

Запуск:   py -3.12 build_exe.py            — лёгкая сборка: ядро (Whisper на CPU, звук, интерфейс, плагины, поиск).
                                             Необязательное (CUDA, трей, PDF, SOCKS, тема, мониторинг GPU) в exe НЕ входит
                                             и докачивается во вкладке «Компоненты»
          py -3.12 build_exe.py --full     — положить в exe и необязательные пакеты, если они установлены у вас
          py -3.12 build_exe.py --with-gpu — положить в exe ещё и библиотеки CUDA из pip (+1–2 ГБ; обычно лучше докачка)
          py -3.12 build_exe.py --onefile  — один файл вместо папки (запускается медленнее, антивирусы ругаются чаще)

Результат: папка dist/NightlordAgent/ с NightlordAgent.exe. Её можно копировать целиком. Файлы настроек
(settings.json, memory.json, pronounce.json…) создаются рядом с NightlordAgent.exe.
Модели Whisper скачиваются при первом запуске в обычный кэш Hugging Face (в exe не входят).

Замечание: скрипт написан по документации PyInstaller и не проверялся на вашей системе. Если сборка
упала на «Hidden import» — добавьте модуль в список HIDDEN ниже и запустите снова.
"""
import importlib.util
import os
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
APP = HERE / "voice_agent.py"
NAME = "NightlordAgent"
HIDDEN = ["pynput.keyboard._win32", "pynput.mouse._win32", "sounddevice", "soundfile", "PIL.ImageGrab", "PIL.ImageTk", "sqlite3", "_sqlite3", "zipfile", "ctypes.wintypes"]
COLLECT_ALL = ["faster_whisper", "ctranslate2", "tokenizers", "onnxruntime"]
# необязательное: в лёгкую сборку не входит (докачивается во вкладке «Компоненты»), с --full входит, если установлено
EXTRA_HIDDEN = ["pystray._win32", "pynvml"]
EXTRA_COLLECT = ["sv_ttk"]
EXTRA_MODULES = ["pystray", "pypdf", "sv_ttk", "socks", "sockshandler", "pynvml", "nvidia"]
OPTIONAL = {"onnxruntime"}


# Библиотеки, которых агент не использует, но которые бывают установлены у вас и тянут сотни МБ в сборку.
HEAVY_UNUSED = ["torch", "torchvision", "torchaudio", "tensorflow", "keras", "matplotlib", "scipy", "pandas", "sklearn",
                "numba", "llvmlite", "cv2", "IPython", "notebook", "jupyter", "pytest", "sympy", "tkinter.test",
                "lib2to3", "pydoc_data",
                "setuptools", "pkg_resources", "distutils", "pip", "wheel", "pydoc", "doctest"]   # сборочный хлам, не нужен в exe


def report_sizes(out):
    """Показывает, что в сборке самое тяжёлое (папки первого и второго уровня)."""
    sizes = {}
    for p in out.rglob("*"):
        if p.is_file():
            rel = p.relative_to(out).parts
            for depth in (1, 2):
                if len(rel) > depth:
                    key = "/".join(rel[:depth])
                    sizes[key] = sizes.get(key, 0) + p.stat().st_size
    total = sum(p.stat().st_size for p in out.rglob("*") if p.is_file())
    print(f"\nРазмер сборки: {total / 1048576:.0f} МБ. Самое тяжёлое:")
    for k, v in sorted(sizes.items(), key=lambda x: -x[1])[:12]:
        print(f"  {v / 1048576:7.0f} МБ  {k}")


def have(mod):
    try:
        return importlib.util.find_spec(mod) is not None
    except (ImportError, ValueError):
        return False


def main():
    args = set(sys.argv[1:])
    if not APP.is_file():
        sys.exit(f"Не найден {APP}")
    if not have("PyInstaller"):
        print("PyInstaller не установлен. Ставлю…")
        subprocess.check_call([sys.executable, "-m", "pip", "install", "pyinstaller"])
    cmd = [sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--windowed", "--name", NAME,
           "--onefile" if "--onefile" in args else "--onedir"]
    ico = Path(__file__).with_name("nightlord.ico")
    if ico.exists():
        cmd += ["--icon", str(ico), "--add-data", str(ico) + os.pathsep + "."]
    full = "--full" in args or "--with-gpu" in args
    if full:
        COLLECT = COLLECT_ALL + EXTRA_COLLECT
        HID = HIDDEN + EXTRA_HIDDEN
    else:
        COLLECT, HID = COLLECT_ALL, HIDDEN
        for mod in EXTRA_MODULES:
            cmd += ["--exclude-module", mod]
    for mod in HEAVY_UNUSED:      # агенту не нужны (torch при необходимости докачивается во вкладке «Компоненты»)
        cmd += ["--exclude-module", mod]
    for mod in COLLECT:
        if have(mod):
            cmd += ["--collect-all", mod]
        elif mod not in OPTIONAL and mod not in EXTRA_COLLECT:
            print(f"Внимание: пакет {mod} не найден — в exe он не попадёт")
    for mod in HID:
        cmd += ["--hidden-import", mod]
    if "--with-gpu" in args:
        for mod in ("nvidia.cublas", "nvidia.cudnn", "nvidia.cuda_runtime"):
            if have(mod):
                cmd += ["--collect-binaries", mod]
            else:
                print(f"Нет {mod}: py -m pip install nvidia-cublas-cu12 nvidia-cudnn-cu12==9.*")
    cmd.append(str(APP))
    print("Команда:", " ".join(cmd))
    if subprocess.call(cmd, cwd=HERE) != 0:
        sys.exit("Сборка не удалась, см. сообщения выше.")
    out = HERE / "dist" / (NAME if "--onefile" not in args else f"{NAME}.exe")
    for fname in ("check_deps.py",):
        if (HERE / fname).is_file() and out.is_dir():
            shutil.copy2(HERE / fname, out / fname)
    print(f"\nГотово: {out}")
    if out.is_dir():
        report_sizes(out)


def _run():
    """Окно не закрывается само: ошибка и итог остаются на экране, копия пишется в build_log.txt."""
    import traceback
    log = HERE / "build_log.txt"
    code = 0
    try:
        print(f"Python {sys.version.split()[0]}, папка {HERE}")
        if sys.version_info < (3, 9):
            raise SystemExit("Нужен Python 3.9+ (лучше 3.12): запускайте  py -3.12 build_exe.py")
        main()
    except SystemExit as e:
        code = 0 if e.code in (None, 0) else 1
        if e.code not in (None, 0):
            print("\nОШИБКА:", e.code)
    except BaseException:
        code = 1
        print("\nОШИБКА СБОРКИ:")
        traceback.print_exc()
    try:
        log.write_text(f"код выхода {code}\n", encoding="utf-8")
    except OSError:
        pass
    print("\nГотово без ошибок." if code == 0 else "\nСборка не удалась. Скопируйте текст выше и пришлите.")
    try:
        input("\nНажмите Enter, чтобы закрыть окно…")
    except EOFError:
        pass
    return code


if __name__ == "__main__":
    sys.exit(_run())
