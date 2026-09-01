"""Пороги покрытия ПО ПАКЕТАМ, а не один процент на весь репозиторий.

Гейт `--cov-fail-under=70` считался по двум пакетам из двенадцати
(`vera_shared` и `gateway`), то есть описывал 38.8% продового кода. У
дашборда, триажа, поиска, бота и всех ингесторов пола не было вовсе:
регресс там ворота не останавливали.

Один общий процент это не чинит — сильный пакет маскирует слабый. Поэтому
у каждого свой пол, выставленный по факту на 2026-09-01 с небольшим
запасом вниз. Это храповик: поднимать цифры можно и нужно, опускать —
только осознанно, отдельной строкой в диффе.

Запуск (после pytest с --cov-report=xml):
    python scripts/check_coverage.py coverage.xml
"""
from __future__ import annotations

import sys
import xml.etree.ElementTree as ET
from collections import defaultdict
from pathlib import Path

#: пакет → минимальный процент покрытия строк.
#: Справа — факт на момент установки порога, чтобы был виден запас.
FLOORS: dict[str, int] = {
    "vera_shared":       88,   # 92.2 (был 88.5, пока tools стоял на нуле)
    "gateway":           85,   # 89.4
    "media-worker":      85,   # 91.0
    "brain-triage":      78,   # 81.8
    "ingestor-slack":    70,   # 75.1
    "brain-search":      65,   # 69.0
    "ingestor-trello":   62,   # 67.1
    "dashboard":         55,   # 59.5
    "bot-telegram":      42,   # 46.1
    "ingestor-gmail":    35,   # 38.8
    # Юзербот — самый низкий: почти весь модуль это работа с живым telethon,
    # которую юнит-тестом не покрыть. Поднимать через выделение чистой логики,
    # а не через моки на пол-файла.
    "ingestor-telegram": 15,   # 18.6
}

TOTAL_FLOOR = 70


def package_of(filename: str) -> str | None:
    """`shared/vera_shared/...` → vera_shared; `services/<pkg>/src/...` → <pkg>."""
    parts = Path(filename).parts
    if not parts:
        return None
    if parts[0] == "shared":
        return "vera_shared"
    if parts[0] == "services" and len(parts) > 1:
        return parts[1]
    return None


def collect(xml_path: Path) -> dict[str, tuple[int, int]]:
    """пакет → (покрытых строк, всего строк)."""
    root = ET.parse(xml_path).getroot()
    agg: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    for cls in root.iter("class"):
        pkg = package_of(cls.get("filename", ""))
        if pkg is None:
            continue
        for line in cls.iter("line"):
            agg[pkg][1] += 1
            if line.get("hits") != "0":
                agg[pkg][0] += 1
    return {k: (v[0], v[1]) for k, v in agg.items()}


def main(argv: list[str]) -> int:
    xml_path = Path(argv[1] if len(argv) > 1 else "coverage.xml")
    if not xml_path.exists():
        print(f"нет {xml_path} — прогони pytest с --cov-report=xml", file=sys.stderr)
        return 2

    got = collect(xml_path)
    failed: list[str] = []
    total_hit = total_all = 0

    print(f"{'пакет':22} {'покрыто':>14}  {'факт':>6}  {'пол':>5}")
    for pkg, floor in sorted(FLOORS.items()):
        hit, all_ = got.get(pkg, (0, 0))
        total_hit += hit
        total_all += all_
        if all_ == 0:
            # Пакет не попал в --cov: гейт бы молча ослаб именно так, как
            # это уже произошло с девятью пакетами.
            failed.append(f"{pkg}: нет данных, пакет не измеряется")
            print(f"{pkg:22} {'—':>14}  {'—':>6}  {floor:4}%  ← НЕ ИЗМЕРЯЕТСЯ")
            continue
        pct = 100 * hit / all_
        mark = "" if pct >= floor else "  ← НИЖЕ ПОЛА"
        print(f"{pkg:22} {hit:6}/{all_:<7} {pct:5.1f}%  {floor:4}%{mark}")
        if pct < floor:
            failed.append(f"{pkg}: {pct:.1f}% < {floor}%")

    total_pct = 100 * total_hit / max(total_all, 1)
    print(f"\n{'ИТОГО':22} {total_hit:6}/{total_all:<7} {total_pct:5.1f}%  {TOTAL_FLOOR:4}%")
    if total_pct < TOTAL_FLOOR:
        failed.append(f"итого: {total_pct:.1f}% < {TOTAL_FLOOR}%")

    if failed:
        print("\nПокрытие упало:", file=sys.stderr)
        for f in failed:
            print(f"  - {f}", file=sys.stderr)
        return 1
    print("\nвсе пороги пройдены")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
