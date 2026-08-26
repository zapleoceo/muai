# PyInstaller: слушатель как обычное приложение Windows, а не «pythonw.exe».
#
# Зачем: в Диспетчере задач процесс должен называться VeraListener, а на другом
# ноутбуке — запускаться без Python и без venv. Копируется папкой.
#
# onedir, НЕ onefile: onefile каждый запуск распаковывает ~400 МБ во временную
# папку — это секунды на старте и стабильный повод для антивируса. Плюс
# ctranslate2 и onnxruntime тащат свои DLL, которым onefile не идёт на пользу.
#
# collect_all по четырём пакетам обязателен: они несут не только код —
# pysilero_vad и faster_whisper везут ONNX-модели, ctranslate2 и onnxruntime
# нативные DLL. Без этого сборка собирается, а падает при первом кадре звука.
from PyInstaller.utils.hooks import collect_all

datas, binaries, hiddenimports = [], [], []
for package in ("pysilero_vad", "faster_whisper", "ctranslate2", "onnxruntime"):
    package_datas, package_binaries, package_hidden = collect_all(package)
    datas += package_datas
    binaries += package_binaries
    hiddenimports += package_hidden

# soundcard грузит WASAPI через cffi, pystray и pycaw — через comtypes: их
# бэкенды выбираются в рантайме, поэтому статический анализ их не находит.
hiddenimports += [
    "soundcard.mediafoundation",
    "pystray._win32",
    "comtypes.stream",
    "pycaw.pycaw",
]

a = Analysis(
    ["src/vera_listener/__main__.py"],
    pathex=["src"],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    excludes=["tkinter", "matplotlib", "pytest"],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz, a.scripts, [],
    exclude_binaries=True,
    name="VeraListener",
    debug=False,
    strip=False,
    upx=False,
    # console=False: слушателю нечего показывать, а окно консоли мигало бы при
    # каждом старте по триггеру планировщика.
    console=False,
    icon="packaging/VeraListener.ico",
    version="packaging/version.txt",
)
COLLECT(
    exe, a.binaries, a.datas,
    strip=False, upx=False, name="VeraListener",
)
