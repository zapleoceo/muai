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
    local line service name state image actual target

    while IFS=$'\t' read -r service name state image; do
        [ -z "$name" ] && continue
        seen=$((seen + 1))

        if [ "$state" != "running" ]; then
            echo "ПЛОХО $name ($service): состояние '$state', а не running"
            problems=$((problems + 1))
            continue
        fi

        actual="$(docker inspect -f '{{.Image}}' "$name" 2>/dev/null || true)"
        target="$(docker image inspect -f '{{.Id}}' "$image" 2>/dev/null || true)"
        if [ -z "$target" ]; then
            echo "ПЛОХО $name ($service): образа '$image' больше нет — контейнер на осиротевшем слое"
            problems=$((problems + 1))
        elif [ "$actual" != "$target" ]; then
            echo "ПЛОХО $name ($service): образ контейнера ${actual:7:12} ≠ текущий тег ${target:7:12} ('$image')"
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
}

main
