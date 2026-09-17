"""Журнал векторов: пишется, вытесняется, не мешает разговору.

Журнал — диагностика, а не работа. Поэтому проверяется не только «записалось»,
но и то, что его сбой не утаскивает за собой разметку говорящих, и что он не
растёт на диске бесконечно.
"""
from __future__ import annotations

import json

import numpy as np
import pytest

from vera_listener.speakers import journal
from vera_listener.speakers.embedder import EMBEDDING_DIM


def _vectors(count: int) -> list[np.ndarray]:
    rng = np.random.default_rng(0)
    return [rng.standard_normal(EMBEDDING_DIM).astype(np.float32)
            for _ in range(count)]


def _write(directory, count=3, names=("Виктор",), members=None, **over):
    vectors = _vectors(count)
    payload = {
        "keys": [float(i) for i in range(count)],
        "names": list(names),
        "members": members if members is not None else [list(range(count))],
        "counterpart": "Виктор",
        "app": "slack.exe",
    }
    payload.update(over)
    journal.write(directory, vectors, payload["keys"], payload["names"],
                  payload["members"], counterpart=payload["counterpart"],
                  app=payload["app"])
    return vectors


class TestWriting:
    def test_vectors_and_labels_land_together(self, tmp_path):
        vectors = _write(tmp_path)

        saved = list(tmp_path.glob("*.npy"))
        meta = list(tmp_path.glob("*.json"))
        assert len(saved) == 1 and len(meta) == 1
        assert np.load(saved[0]).shape == (len(vectors), EMBEDDING_DIM)

        body = json.loads(meta[0].read_text(encoding="utf-8"))
        assert body["counterpart"] == "Виктор"
        assert body["app"] == "slack.exe"
        assert [line["voice"] for line in body["lines"]] == ["Виктор"] * 3

    def test_each_line_keeps_its_own_voice(self, tmp_path):
        """Ради этого журнал и ведётся: какой вектор какому голосу приписали."""
        _write(tmp_path, count=4, names=("Собеседник 1", "Собеседник 2"),
               members=[[0, 2], [1, 3]])

        body = json.loads(next(tmp_path.glob("*.json")).read_text(encoding="utf-8"))
        assert [line["voice"] for line in body["lines"]] == [
            "Собеседник 1", "Собеседник 2", "Собеседник 1", "Собеседник 2"]

    def test_nothing_written_without_vectors(self, tmp_path):
        journal.write(tmp_path, [], [], [], [], counterpart=None, app=None)
        assert list(tmp_path.iterdir()) == []


class TestItNeverHurtsTheConversation:
    def test_unwritable_directory_does_not_raise(self, tmp_path):
        """Разметка уже готова; журнал не имеет права её утащить."""
        busy = tmp_path / "занято"
        busy.write_text("не каталог", encoding="utf-8")
        journal.write(busy, _vectors(2), [0.0, 1.0], ["Виктор"], [[0, 1]],
                      counterpart="Виктор", app="slack.exe")

    def test_broken_payload_does_not_raise(self, tmp_path):
        journal.write(tmp_path, _vectors(2), [0.0], ["Виктор"], [[0, 1]],
                      counterpart="Виктор", app=None)


class TestForgetting:
    def test_old_sessions_are_dropped(self, tmp_path, monkeypatch):
        """Диагностика не имеет права расти на чужом диске бесконечно."""
        monkeypatch.setattr(journal, "KEEP_SESSIONS", 3)
        for index in range(6):
            (tmp_path / f"2026091{index}T000000.npy").write_bytes(b"x")
            (tmp_path / f"2026091{index}T000000.json").write_text("{}", encoding="utf-8")

        journal._forget_old(tmp_path)

        assert sorted(p.stem for p in tmp_path.glob("*.npy")) == [
            "20260913T000000", "20260914T000000", "20260915T000000"]
        assert len(list(tmp_path.glob("*.json"))) == 3

    def test_half_written_session_is_also_forgotten(self, tmp_path, monkeypatch):
        """Разметка без векторов — след оборванной записи, и он тоже мусор.

        Старая чистка искала только `*.npy`, поэтому одинокий `.json` не
        попадал ни в «оставить», ни в «удалить» и жил вечно, унося с собой
        имена собеседников. Нашло ревью.
        """
        monkeypatch.setattr(journal, "KEEP_SESSIONS", 2)
        for index in range(4):
            (tmp_path / f"2026090{index}T000000.json").write_text("{}", encoding="utf-8")

        journal._forget_old(tmp_path)

        assert sorted(p.stem for p in tmp_path.glob("*.json")) == [
            "20260902T000000", "20260903T000000"]

    def test_writing_prunes_by_itself(self, tmp_path, monkeypatch):
        monkeypatch.setattr(journal, "KEEP_SESSIONS", 2)
        for index in range(4):
            (tmp_path / f"2026090{index}T000000.npy").write_bytes(b"x")
        _write(tmp_path)
        assert len(list(tmp_path.glob("*.npy"))) <= 2 + 1


@pytest.mark.parametrize("count", [1, 2, 25])
def test_size_stays_about_a_kilobyte_per_line(tmp_path, count):
    """Обещание в докстринге — 1 КБ на реплику. Оно должно быть правдой."""
    _write(tmp_path, count=count, members=[list(range(count))])
    total = sum(p.stat().st_size for p in tmp_path.iterdir())
    assert total < 1024 * count + 2048
