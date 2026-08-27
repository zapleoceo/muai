"""Слушатель не имеет права греть процессор, пока никто не говорит.

Два дефекта, оба найдены замером на живом ноутбуке:

- OpenMP ждал АКТИВНО, а детектор речи зовут 62 раза в секунду — уснуть его
  воркеры не успевали никогда. 223% ядра против 5% с PASSIVE.
- `soundcard` ждёт звук опросом и на каждой итерации спрашивает у устройства
  период через COM. В профиле живого процесса это 48% времени, ещё 40% —
  сам цикл опроса.

Импорты проверяем в подпроцессе с PYTHONPATH: в venv пакет лежит КОПИЕЙ, и
тест, прочитавший установленную копию, проверял бы вчерашний код.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

SRC = str(Path(__file__).resolve().parents[1] / "src")

#: Заглушка soundcard: тесты идут и на Linux в CI, где библиотеки нет.
FAKE_SOUNDCARD = (
    "import sys, types\n"
    "fake = types.ModuleType('soundcard')\n"
    "fake.mediafoundation = types.ModuleType('soundcard.mediafoundation')\n"
    "sys.modules['soundcard'] = fake\n"
    "sys.modules['soundcard.mediafoundation'] = fake.mediafoundation\n"
)


def _run(code: str, env_extra: dict[str, str] | None = None) -> str:
    # UTF-8 обязателен: предупреждения по-русски, а консоль по умолчанию cp1252.
    env = {**os.environ, "PYTHONPATH": SRC, "PYTHONIOENCODING": "utf-8"}
    env.update(env_extra or {})
    done = subprocess.run([sys.executable, "-c", code], capture_output=True,
                          text=True, encoding="utf-8", env=env, timeout=60)
    assert done.returncode == 0, done.stderr
    return done.stdout.strip().splitlines()[-1]


class TestOpenMP:
    def test_import_sets_passive_wait(self):
        """Свежий процесс: импорт пакета обязан выставить переменную сам."""
        assert _run("import os, vera_listener; print(os.environ['OMP_WAIT_POLICY'])") \
            == "PASSIVE"

    def test_explicit_setting_wins(self):
        """Заданное снаружи не перетираем — иначе не отладить."""
        assert _run("import os, vera_listener; print(os.environ['OMP_WAIT_POLICY'])",
                    {"OMP_WAIT_POLICY": "ACTIVE"}) == "ACTIVE"

    def test_set_before_native_libraries_load(self):
        """Переменную читает vcomp140.dll при загрузке: после импорта уже поздно."""
        source = (Path(SRC) / "vera_listener" / "__init__.py").read_text(encoding="utf-8")
        assert "OMP_WAIT_POLICY" in source, "настройка обязана жить в __init__ пакета"


class TestPolling:
    def test_patch_survives_a_library_change(self):
        """Лезем во внутренности soundcard — падать от этого слушатель не вправе."""
        assert _run(FAKE_SOUNDCARD
                    + "import vera_listener.capture as c\n"
                      "c.tame_polling()\n"
                      "print('выжил')\n") == "выжил"

    def test_step_stays_in_the_safe_window(self):
        """Снизу — расход, сверху — целостность записи.

        Своё значение soundcard спит ~0.75 мс, то есть опрашивает 1300 раз в
        секунду на дорожку. А на периоде 40 мс проба дала 646 кадров с
        микрофона за 12 секунд вместо 373: библиотека решает, что карта молчит,
        и досыпает тишину — запись растягивается.
        """
        got = _run(FAKE_SOUNDCARD
                   + "from vera_listener.capture import POLL_STEP_S\n"
                     "print(POLL_STEP_S)\n")
        assert 0.01 <= float(got) <= 0.03

    def test_period_is_asked_once_per_client(self):
        """Кэш на клиент: период устройства не меняется, а COM-вызов не бесплатен."""
        got = _run(FAKE_SOUNDCARD + """
import soundcard as sc

calls = {"n": 0}


class Client:
    @property
    def deviceperiod(self):
        calls["n"] += 1
        return (0.01, 0.003)


sc.mediafoundation._AudioClient = Client
import vera_listener.capture as c

c.tame_polling()
one, two = Client(), Client()
for _ in range(50):
    one.deviceperiod
    two.deviceperiod
print(calls["n"])
""")
        assert got == "2", f"COM-вызовов должно быть по одному на клиент, а не {got}"


class TestSilenceShortcut:
    """Цифровую тишину нейросети не отдаём: она стоила 16% расхода."""

    def test_digital_silence_skips_the_network(self):
        got = _run(
            "import sys, types\n"
            "calls = {'n': 0}\n"
            "fake = types.ModuleType('pysilero_vad')\n"
            "class D:\n"
            "    def __call__(self, frame):\n"
            "        calls['n'] += 1\n"
            "        return 1.0\n"
            "    def reset(self): pass\n"
            "fake.SileroVoiceActivityDetector = D\n"
            "sys.modules['pysilero_vad'] = fake\n"
            "from vera_listener.vad import FRAME_BYTES, SpeechDetector\n"
            "d = SpeechDetector()\n"
            "print(d.is_speech(bytes(FRAME_BYTES)), calls['n'])\n")
        assert got == "False 0", got

    def test_audible_frame_still_reaches_the_network(self):
        got = _run(
            "import sys, types\n"
            "import numpy as np\n"
            "calls = {'n': 0}\n"
            "fake = types.ModuleType('pysilero_vad')\n"
            "class D:\n"
            "    def __call__(self, frame):\n"
            "        calls['n'] += 1\n"
            "        return 1.0\n"
            "    def reset(self): pass\n"
            "fake.SileroVoiceActivityDetector = D\n"
            "sys.modules['pysilero_vad'] = fake\n"
            "from vera_listener.vad import FRAME_SAMPLES, SpeechDetector\n"
            "d = SpeechDetector()\n"
            "loud = (np.ones(FRAME_SAMPLES, dtype=np.int16) * 4000).tobytes()\n"
            "print(d.is_speech(loud), calls['n'])\n")
        assert got == "True 1", got

    def test_quiet_room_is_not_cut_off(self):
        """Порог у самого нуля: тихую комнату глушить нельзя, только цифровой ноль."""
        got = _run(
            "import sys, types\n"
            "import numpy as np\n"
            "calls = {'n': 0}\n"
            "fake = types.ModuleType('pysilero_vad')\n"
            "class D:\n"
            "    def __call__(self, frame):\n"
            "        calls['n'] += 1\n"
            "        return 0.0\n"
            "    def reset(self): pass\n"
            "fake.SileroVoiceActivityDetector = D\n"
            "sys.modules['pysilero_vad'] = fake\n"
            "from vera_listener.vad import FRAME_SAMPLES, SpeechDetector\n"
            "d = SpeechDetector()\n"
            "hiss = (np.ones(FRAME_SAMPLES, dtype=np.int16) * 200).tobytes()\n"
            "d.is_speech(hiss)\n"
            "print(calls['n'])\n")
        assert got == "1", got
