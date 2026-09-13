#!/usr/bin/env bash
# check_migrations.sh — сверка файлов миграций с учётом schema_migrations.
#
# Зачем: 13.09.2026 учёт на проде кончался на 025, а в репозитории лежали
# 026…030. Причём 026 и 027 были накатаны руками без записи, 028 и 029 не
# накатаны вовсе — код, который ждёт их индексов, уже неделю работал без них.
# Заметить было нечем: деплой миграции не катит, монитор их не смотрел.
#
# Печатает по строке на расхождение и возвращает 1, если оно есть. Ничего не
# меняет. Для незаписанной миграции дёшево проверяет объекты, которые она
# создаёт через IF NOT EXISTS (индекс, таблица, расширение, колонка), и
# сообщает, что нашлось, — но сама в учёт ничего не пишет: «объекты есть»
# ещё не значит «миграция выполнена целиком», это решает человек.
#
# Зовётся монитором (vera3-monitor.sh --check-migrations и проверка 12) и
# деплоем (deploy.sh, предупреждение без провала деплоя).
set -u

MIGRATIONS_DIR="${MIGRATIONS_DIR:-$(dirname "$0")/../infra/migrations}"

q() { docker exec vera3-postgres psql -U vera -d vera -tAc "$1" 2>/dev/null; }

# Выражения SQL «объект есть» по тексту миграции — по одному на строку.
# Комментарии срезаются, файл склеивается в одну строку: ALTER TABLE … ADD
# COLUMN в 030 разнесён на две строки.
probes() {
    local flat
    flat=$(sed 's/--.*$//' "$1" | tr '\n' ' ' | tr -s ' ')
    grep -oiE 'CREATE (UNIQUE )?(INDEX|TABLE) (CONCURRENTLY )?IF NOT EXISTS [a-z0-9_]+' <<< "$flat" \
        | awk '{print "to_regclass('\''public." tolower($NF) "'\'') IS NOT NULL"}'
    grep -oiE 'CREATE EXTENSION IF NOT EXISTS [a-z0-9_]+' <<< "$flat" \
        | awk '{print "EXISTS (SELECT 1 FROM pg_extension WHERE extname='\''" tolower($NF) "'\'')"}'
    grep -oiE 'ALTER TABLE [a-z0-9_]+ ADD COLUMN IF NOT EXISTS [a-z0-9_]+' <<< "$flat" \
        | awk '{print "EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='\''" tolower($3) "'\'' AND column_name='\''" tolower($NF) "'\'')"}'
}

describe_unrecorded() {
    local file="$1" version="$2" exprs total sum
    exprs=$(probes "$file")
    if [ -z "$exprs" ]; then
        echo "$version: не записана в учёт (объекты автоматически не проверяются)"
        return
    fi
    total=$(grep -c . <<< "$exprs")
    sum=$(q "SELECT $(sed 's/.*/(&)::int/' <<< "$exprs" | paste -sd '+' -)" | tr -d '[:space:]')
    if [ -z "$sum" ]; then
        echo "$version: не записана в учёт (проверка объектов не выполнилась)"
    elif [ "$sum" -eq "$total" ]; then
        echo "$version: объекты есть ($total из $total), но в учёт не записана"
    elif [ "$sum" -eq 0 ]; then
        echo "$version: не применена"
    else
        echo "$version: применена частично — объектов $sum из $total, в учёт не записана"
    fi
}

main() {
    local applied file version problems=0
    [ -d "$MIGRATIONS_DIR" ] || { echo "нет каталога миграций $MIGRATIONS_DIR"; return 1; }
    # Пустой ответ здесь — не «всё применено»: так выглядят лежащий postgres
    # и отсутствующая таблица учёта, и промолчать значит ослепнуть.
    if [ "$(q "SELECT to_regclass('public.schema_migrations') IS NOT NULL" | tr -d '[:space:]')" != "t" ]; then
        echo "учёт миграций не читается (postgres недоступен или нет schema_migrations)"
        return 1
    fi
    applied=$(q "SELECT version FROM schema_migrations")
    for file in "$MIGRATIONS_DIR"/*.sql; do
        [ -f "$file" ] || continue
        version=$(basename "$file" .sql)
        grep -qxF "$version" <<< "$applied" && continue
        describe_unrecorded "$file" "$version"
        problems=$(( problems + 1 ))
    done
    while read -r version; do
        [ -z "$version" ] && continue
        [ -f "$MIGRATIONS_DIR/$version.sql" ] && continue
        echo "$version: записана в учёт, но файла в репозитории нет"
        problems=$(( problems + 1 ))
    done <<< "$applied"
    [ "$problems" -eq 0 ]
}

main
