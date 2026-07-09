-- FK relationships.derived_from_event_id → events.id (ON DELETE SET NULL),
-- чтобы удаление события не оставляло висячих ссылок и аудит/rollback графа
-- (заявленная цель rel_extract) не ломался.
-- NOT VALID: не сканирует существующие строки под ACCESS EXCLUSIVE — старые
-- висячие ссылки (если есть) не блокируют миграцию, констрейнт действует на
-- новые/изменяемые строки. VALIDATE отдельным шагом при желании позже.
ALTER TABLE relationships
  ADD CONSTRAINT fk_relationships_event
  FOREIGN KEY (derived_from_event_id) REFERENCES events(id) ON DELETE SET NULL
  NOT VALID;
