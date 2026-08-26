-- 023: снять мёртвую таблицу sources.
--
-- Её не читал и не писал ни один ингестор — ревизия 2026-08-26 подтвердила
-- грепом: единственные упоминания SourceRow были определение модели и реэкспорт.
-- Состояние обхода живёт в per-source таблицах (gmail_accounts,
-- telegram_sessions, instagram_sessions, trello_boards, slack_conversations),
-- секреты — в infra/.env либо в *_sessions под crypto. Вместе с моделью
-- удалены две мёртвые ABC источника (vera_shared/sources, vera_shared/connectors).
--
-- DROP выполняется ТОЛЬКО если таблица пуста. Если в ней вдруг есть строки —
-- миграция ничего не делает и громко об этом говорит: удалять непрочитанные
-- данные молча нельзя, надо сначала посмотреть, откуда они там.

BEGIN;

DO $$
DECLARE
    rows_left bigint;
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.tables
        WHERE table_schema = 'public' AND table_name = 'sources'
    ) THEN
        RAISE NOTICE '023: таблицы sources уже нет — пропускаю';
        RETURN;
    END IF;

    EXECUTE 'SELECT count(*) FROM sources' INTO rows_left;
    IF rows_left > 0 THEN
        RAISE WARNING '023: в sources % строк — НЕ удаляю, разберись сначала', rows_left;
        RETURN;
    END IF;

    EXECUTE 'DROP TABLE sources';
    RAISE NOTICE '023: пустая таблица sources удалена';
END $$;

INSERT INTO schema_migrations (version, note)
VALUES ('023_drop_sources_table', 'sources: мёртвая таблица, снята если пуста')
ON CONFLICT (version) DO NOTHING;

COMMIT;
