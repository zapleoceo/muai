#!/usr/bin/env bash
# Деплой Vera 3: rsync → build → up → ПРОВЕРКА. Живёт в репозитории, потому что
# это код, а не настройка сервера: его надо ревьюить и уметь исправить пушем.
#
# На сервере `/usr/local/bin/vera3-deploy` — тонкая обёртка, прибитая к ключу
# CI через authorized_keys `command=`: она обновляет чекаут и зовёт ЭТОТ файл из
# свежего чекаута. Обёртка меняется раз в год, логика — этим репозиторием.
#
# Режимы:
#   deploy.sh                 полный деплой
#   deploy.sh --verify-only   только проверка, ничего не трогает
#
# ## Что проверяется и почему именно это
#
# До 2026-08-27 проверка была одна: отвечает ли `/healthz` у шлюза. Остальные
# шестнадцать контейнеров — media-worker, пять реплик триажа, пять ингесторов,
# дашборд, поиск, бот, прунер — могли лежать или крутиться на ПРОШЛОМ образе, а
# деплой всё равно возвращал ноль. Молчаливый деплой хуже упавшего: код на
# диске новый, в контейнере старый, и расхождение видно только если залезть
# внутрь контейнера руками.
#
# Поэтому после `up -d` проверяется каждый сервис:
#   1. контейнеры есть и в состоянии running (реплики — все);
#   2. образ контейнера СОВПАДАЕТ с текущим id своего тега. Если сборка сделала
#      новый образ, а контейнер остался на старом, тег уже указывает на новый —
#      сравнение это и ловит;
#   3. если у сервиса объявлен healthcheck — он не unhealthy;
#   4. шлюз отвечает на /healthz (как и раньше).
# Любое расхождение — ненулевой выход и печать виновных, а не «всё хорошо».
set -euo pipefail

CHECKOUT_DIR="${CHECKOUT_DIR:-/var/www/muai-checkout}"
TARGET_DIR="${TARGET_DIR:-/var/www/vera3}"
VERIFY_ONLY=0
[ "${1:-}" = "--verify-only" ] && VERIFY_ONLY=1

# Контейнер мог только что стартовать — даём ему успокоиться перед проверкой.
SETTLE_S="${SETTLE_S:-5}"
GATEWAY_TRIES="${GATEWAY_TRIES:-30}"


sync_tree() {
    echo "--- rsync vera3 → $TARGET_DIR ---"
    cd "$CHECKOUT_DIR"
    git log --oneline -1
    rsync -az --delete \
        --exclude='.env' \
        --exclude='infra/.env' \
        --exclude='*.session' \
        --exclude='__pycache__' \
        --exclude='*.pyc' \
        --exclude='.pytest_cache' \
        vera3/ "$TARGET_DIR/"
}


build_and_up() {
    cd "$TARGET_DIR/infra"
    echo "--- compose build ---"
    docker compose build --quiet
    echo "--- compose up ---"
    docker compose up -d --remove-orphans
}


# Возвращает 0, если все контейнеры сервисов живы и на свежих образах.
verify_containers() {
    cd "$TARGET_DIR/infra"
    local problems=0 seen=0
    local service name state ref actual target health

    while IFS=$'\t' read -r service name state image; do
        [ -z "$name" ] && continue
        seen=$((seen + 1))

        if [ "$state" != "running" ]; then
            echo "ПЛОХО $name ($service): состояние '$state', а не running"
            problems=$((problems + 1))
            continue
        fi

        # Ссылку берём из САМОГО контейнера (.Config.Image), а не из вывода
        # `compose ps`: тот показывает ссылку, только пока она разрешается в
        # этот образ, а как только тег уехал на новую сборку — печатает sha
        # контейнера. Сравнение sha с самим собой всегда совпадает, и проверка
        # молча пропускала ровно тот случай, ради которого написана (поймано
        # тестом: тег двигали на другой образ, а проверка отвечала «0 проблем»).
        ref="$(docker inspect -f '{{.Config.Image}}' "$name" 2>/dev/null || true)"
        actual="$(docker inspect -f '{{.Image}}' "$name" 2>/dev/null || true)"
        target="$(docker image inspect -f '{{.Id}}' "$ref" 2>/dev/null || true)"
        if [ -z "$ref" ]; then
            echo "ПЛОХО $name ($service): не удалось прочитать ссылку на образ"
            problems=$((problems + 1))
        elif [ -z "$target" ]; then
            echo "ПЛОХО $name ($service): образа '$ref' больше нет — контейнер на осиротевшем слое"
            problems=$((problems + 1))
        elif [ "$actual" != "$target" ]; then
            echo "ПЛОХО $name ($service): образ контейнера ${actual:7:19} ≠ текущий '$ref' = ${target:7:19}"
            echo "      сборка дала новый образ, а контейнер не пересоздан: код в нём ПРОШЛЫЙ"
            problems=$((problems + 1))
        fi

        health="$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{end}}' \
                  "$name" 2>/dev/null || true)"
        if [ "$health" = "unhealthy" ]; then
            echo "ПЛОХО $name ($service): healthcheck говорит unhealthy"
            problems=$((problems + 1))
        fi
    done < <(docker compose ps -a --format '{{.Service}}\t{{.Name}}\t{{.State}}\t{{.Image}}')

    if [ "$seen" -eq 0 ]; then
        echo "ПЛОХО: compose не показал ни одного контейнера"
        return 1
    fi
    echo "проверено контейнеров: $seen, проблем: $problems"
    [ "$problems" -eq 0 ]
}


# Сервис, объявленный в compose, но без контейнера вовсе, `ps -a` не покажет —
# ловим отдельно, сравнивая список сервисов с тем, что реально существует.
verify_nothing_missing() {
    cd "$TARGET_DIR/infra"
    local missing=0 svc
    for svc in $(docker compose config --services); do
        if [ -z "$(docker compose ps -aq "$svc" 2>/dev/null)" ]; then
            echo "ПЛОХО сервис $svc: контейнера нет вообще"
            missing=$((missing + 1))
        fi
    done
    [ "$missing" -eq 0 ]
}


verify_gateway() {
    local i
    for i in $(seq 1 "$GATEWAY_TRIES"); do
        if docker exec vera3-gateway python -c \
            "import urllib.request,sys;sys.exit(0 if urllib.request.urlopen('http://localhost:8000/healthz',timeout=5).status==200 else 1)" \
            2>/dev/null; then
            echo "шлюз ответил через $((i * 2))с"
            return 0
        fi
        sleep 2
    done
    echo "ПЛОХО: шлюз не ответил на /healthz"
    docker logs vera3-gateway --tail 30
    return 1
}


# Уборка за собой. Каждый деплой пересобирает 13 образов, и прежние
# остаются висячими: замер 2026-08-27 — 4.76 ГБ невостребованных образов
# (82% всех) плюс 3.74 ГБ кэша сборки при 8.5 ГБ свободного на диске.
# Дневной крон с фильтром until=72h тут не помогал по построению: наш же
# мусор моложе фильтра, а к 72 часам его накапливается ещё столько же.
# Поэтому собирает тот, кто насорил, и сразу.
#
# `image prune -f` без `-a` трогает ТОЛЬКО висячие (без тега) образы —
# чужие проекты на этой машине (aibroker, stepan2) держат свои под тегами
# и не задеваются. А вот `builder prune` чистит кэш BuildKit ХОСТА, общий
# на все проекты: фильтр until=24h тут единственная защита. Это в рамках
# уже принятой на машине практики — дневной крон делает host-wide
# `system prune -af --filter until=72h`, то есть то же самое, но реже.
collect_garbage() {
    local before after
    before=$(df -B1 --output=avail / | tail -1)
    docker image prune -f >/dev/null 2>&1 || true
    docker builder prune -f --filter until=24h >/dev/null 2>&1 || true
    after=$(df -B1 --output=avail / | tail -1)
    awk -v b="$before" -v a="$after" 'BEGIN {
        printf "уборка: освобождено %.2f ГБ, свободно %.2f ГБ\n",
               (a - b) / 1073741824, a / 1073741824 }'
}


main() {
    if [ "$VERIFY_ONLY" -eq 0 ]; then
        sync_tree
        build_and_up
        sleep "$SETTLE_S"
    fi

    echo "--- проверка ---"
    local failed=0
    verify_nothing_missing || failed=1
    verify_containers || failed=1
    verify_gateway || failed=1

    if [ "$failed" -ne 0 ]; then
        echo "ДЕПЛОЙ НЕ ПРИНЯТ: см. строки ПЛОХО выше"
        exit 12
    fi
    echo "деплой в порядке: все сервисы живы и на свежих образах"
    # Уборка только после настоящего деплоя: --verify-only обещает ничего
    # не трогать. Через if, а НЕ через `[ ... ] && collect_garbage`: такая
    # строка последней в main отдаёт наружу код упавшего теста, и в режиме
    # --verify-only скрипт возвращал 1 при полностью зелёной проверке —
    # поймано замером кода выхода до коммита.
    #
    # `|| true` — уборка best-effort: её сбой не имеет права переворачивать
    # вердикт успешного деплоя в FAILURE.
    if [ "$VERIFY_ONLY" -eq 0 ]; then
        collect_garbage || true
    fi
}

main
