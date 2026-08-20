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
"${PSQL[@]}" -tAc "INSERT INTO schema_migrations (version, note) VALUES ('$VERSION','applied via apply_migration.sh') ON CONFLICT DO NOTHING" >/dev/null
echo "готово: $VERSION"
