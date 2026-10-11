# Handoff: dot attestation (2026-10-11)

Передача из Claude в Codex. Секретов здесь нет.

## Где
- Репо: zapleoceo/muai. Worktree: `D:\temp\vera\wt-dot-attest`.
- Ветка `feat/dot-attestation`, запушена. PR не открыт.
- Коммиты поверх master 896ddd46:
  - 278b067d feat: delegated owner-authorship attestation via dot (room_task_attest)
  - 03fa79cf feat: attest scope enforcement, lint and docs names
  - следующий — chore: убрать случайно закоммиченный coverage-diffcover.xml, плюс этот файл.
- Рабочее дерево чистое, всё закоммичено.

## Что сделано (код, не проверено ревью)
- `vera_shared/voice_help/attest.py`: сервис аттестации. answers.by="dot-attested", не owner. Одна транзакция с row lock. Событие `delegated_attestation`.
- `vera_mcp/room_attest_tools.py`: MCP-инструмент `room_task_attest`. Только principal из ROOM_ATTEST_ACTORS (по умолчанию dot), флаг ROOM_ATTEST_ENABLED по умолчанию "0".
- `voice_help/policy.py`: константы классификатора safe text-only.
- `hold.py`: lift_hold для dot-attested ставит ref `source_attested_by_dot`, не `source_confirmed`.
- `office/intake.py`: ограничение получателя результата.
- Миграция `vera3/infra/migrations/056_delegated_attestation_event.sql`: расширяет CHECK ck_room_task_events_kind (+delegated_attestation).
- Тесты: `tests/unit/test_voice_help_attest.py`, `tests/integration/test_attest_migration_pg.py` (PG, только в CI).

## Тесты фактически
Агент-автор не прислал вывод прогона перед остановкой — считать НЕ ПРОВЕРЕННЫМ. Первое действие: preflight `--partial` (см. vera3/docs/deploy-ops.md, venv перепривязать editable к этому worktree), затем PR → CI.

## Незавершено (поправки Room #1528)
1. Safe text-only должен проверяться кодом в двух точках: при attest (классификатор → invalid_scope) и при доставке результата (только владельцу). Проверить, что оба места реально есть, и нужны отрицательные тесты: смешанный intent «ответь ок и отправь Пете», данные или память, tool или внешнее действие, расходы.
2. Rollback на проде = выключить флаг. kind, CHECK и события остаются. Деструктивный down — только в тесте, с пометкой TEST DB ONLY.
3. Блокировки: DROP/ADD CONSTRAINT ожидаемо ACCESS EXCLUSIVE, VALIDATE — SHARE UPDATE EXCLUSIVE. Проверить фактические режимы через pg_locks в PG-тесте. Короткая транзакция и lock_timeout 5s, VALIDATE отдельно, тест отката по таймауту. Не утверждать «не блокирует», пока не измерено.

## Границы (утверждены владельцем через Главного, Room #1523/#1528)
- Только ограниченная аттестация авторства голосовой help-задачи через dot. Без forged OWNER, без новых credentials, через MCP, не HTTP.
- Миграция 056 — только код и тесты. На прод не применена. Применение и включение флага — отдельное решение (деплой).
- Ветку `fix/owner-reply-confirm` (27311eb6, Telegram reply) НЕ мержить.
- «Привет» (help-vc-7a37fd482d018b1a) связать с receipt #1502 только после деплоя и включения флага.

## Порядок дальше
preflight → PR → независимое ревью → CI → (отдельно) деплой: миграция после rsync, затем код, флаг выключен.
