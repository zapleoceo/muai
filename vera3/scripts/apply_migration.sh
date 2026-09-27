#!/usr/bin/env bash
# apply_migration.sh <файл.sql> — накатить миграцию и записать её в учёт.
#
# Зачем: миграции применяются руками, и до 2026-08-20 нигде не фиксировалось,
# что уже накатано. Этот скрипт отказывается применять дважды и сам пишет
# строку в schema_migrations, так что состояние всегда видно запросом.
set -euo pipefail

FILE="${1:?использование: apply_migration.sh vera3/infra/migrations/0XX_name.sql}"
[ -f "$FILE" ] || { echo "нет файла: $FILE" >&2; exit 1; }
VERSION="$(basename "$FILE" .sql)"
# Версия в учёте — это имя файла. Копия под временным именем (034.sql вместо
# 034_events_media_unrecognized_index.sql) записала бы в учёт версию, которой
# нет в репозитории, и монитор дважды в сутки поднимал бы тревогу о расхождении
# (26.09.2026). Поэтому имя обязано совпадать с файлом из infra/migrations.
REPO_MIGRATIONS="$(cd "$(dirname "$0")/../infra/migrations" && pwd)"
if [ ! -f "$REPO_MIGRATIONS/$VERSION.sql" ]; then
    echo "нет такой миграции в репозитории: $VERSION.sql — накатывай файл под его настоящим именем" >&2
    exit 1
fi
PSQL=(docker exec -i vera3-postgres psql -U vera -d vera)

if [ "$("${PSQL[@]}" -tAc "SELECT to_regclass('public.schema_migrations') IS NOT NULL")" != "t" ]; then
    echo "нет таблицы schema_migrations — сначала накати 021_schema_migrations.sql" >&2
    exit 1
fi
if [ "$("${PSQL[@]}" -tAc "SELECT EXISTS(SELECT 1 FROM schema_migrations WHERE version='$VERSION')")" = "t" ]; then
    echo "уже применена: $VERSION"
    exit 0
fi

echo "накатываю $VERSION…"
"${PSQL[@]}" -v ON_ERROR_STOP=1 < "$FILE"

# Невалидный индекс — не «применено». CREATE INDEX CONCURRENTLY, прерванный
# снаружи (OOM, Ctrl-C, обрыв ssh), оставляет INVALID-индекс; повторный накат
# с IF NOT EXISTS молча его пропускает, и без этой проверки миграция попала бы
# в учёт как успешная — с индексом, которым планировщик не пользуется.
INVALID="$("${PSQL[@]}" -tAc "SELECT string_agg(indexrelid::regclass::text, ', ') FROM pg_index WHERE NOT indisvalid")"
if [ -n "$INVALID" ]; then
    echo "в базе невалидные индексы: $INVALID — в учёт НЕ записываю." >&2
    echo "Удали их (DROP INDEX CONCURRENTLY …) и накати $VERSION заново." >&2
    exit 1
fi

"${PSQL[@]}" -tAc "INSERT INTO schema_migrations (version, note) VALUES ('$VERSION','applied via apply_migration.sh') ON CONFLICT DO NOTHING" >/dev/null
echo "готово: $VERSION"
