-- 021: учёт применённых миграций.
--
-- До этого применённость определялась только сверкой схемы руками: нумерация
-- скачет (014 дважды, 018/019 не существуют), а факта «что накатано» нигде не
-- было. Ревью 2026-08-20 уже дало ложный вывод «020 не применена» — проверяли
-- по имени объекта, которого миграция не создаёт.
--
-- Таблица заполняется вручную при накате (см. scripts/apply_migration.sh).
-- Бэкфил ниже отмечает всё, что подтверждено наличием объектов в проде.

CREATE TABLE IF NOT EXISTS schema_migrations (
    version     text PRIMARY KEY,
    applied_at  timestamptz NOT NULL DEFAULT now(),
    note        text
);

INSERT INTO schema_migrations (version, note) VALUES
    ('002_triage_started_at',        'backfill: подтверждено ix_events_processing_started'),
    ('003_nature_project',           'backfill: ix_events_project'),
    ('004_gmail_reauth_status',      'backfill: колонки gmail_accounts'),
    ('004_ready_subtype',            'backfill: ix_events_ready_subtype (номер 004 занят дважды)'),
    ('005_author_role',              'backfill: author_role в metadata'),
    ('006_retry_counter',            'backfill: ix_events_retry_due'),
    ('007_backfill_jobs',            'backfill: создавала backfill_jobs, снята 009'),
    ('008_drop_tokens_table',        'backfill: tokens отсутствует — применена'),
    ('009_app_control',              'backfill: app_control'),
    ('009_drop_backfill_jobs',       'backfill: backfill_jobs отсутствует (номер 009 занят дважды)'),
    ('010_project_membership',       'backfill: project_membership'),
    ('011_event_embeddings',         'backfill: event_embeddings'),
    ('012_usage_log_request_id',     'backfill: ix_usage_event'),
    ('013_relationships_event_fk',   'backfill: FK derived_from_event_id'),
    ('014_entity_avatars',           'backfill: entity_avatars'),
    ('014_events_pending_claim_index','backfill: ix_events_pending_claim (номер 014 занят дважды)'),
    ('015_events_tg_sender_idx',     'backfill: индекс по sender_id'),
    ('016_merge_suggestions',        'backfill: merge_suggestions'),
    ('017_relationships_dedupe_unique','backfill: uq_relationships_spo'),
    ('020_canonical_message_view',   'backfill: v_messages (018/019 не существуют)'),
    ('021_schema_migrations',        'эта миграция')
ON CONFLICT (version) DO NOTHING;
