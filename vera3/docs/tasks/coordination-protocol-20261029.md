# Протокол dot–Vera–Claude, 29.10.2026

## Стан

- Время: 2026-10-29 19:30, Asia/Saigon.
- Canonical task: main / vera-coordination-protocol-20261029, fencing_token 1;
  responsible dot; исполнитель — локальная сессия Codex codex-local-protocol-20261029.
- Worktree: C:/Users/work_zaporozhets/.codex/worktrees/vera-coordination-protocol/myAI;
  branch codex/coordination-protocol, base 29321e607de6868ea3312523697869f445a4f8e4.
- Scope: протокол общей координации и read-only task binding результата. Orchestra,
  платформенные инструкции, доступы/ключи, квоты, расписания исключены. Merge/deploy
  не разрешены этим поручением.
- Доказательства: 4 новых теста task binding сначала упали на отсутствующем аргументе,
  после изменения прошли. MCP-набор: 32 passed. Сценарный набор из протокола:
  205 passed, 2 прежних предупреждения pytest о sync-тестах с asyncio mark.
  Ruff, Vulture, git diff --check прошли. SQLite и транспортные заглушки;
  PostgreSQL/CI и живая доставка пока не проверены.
- Следующий шаг: независимое ревью, draft PR и CI.
- Блокер Claude: Room main #6110, message_id claude-5e015e1824e2,
  2026-10-29T09:32:17.668751: лимит исчерпан. Более поздние сверки не подтверждают
  восстановление квоты. Новых вызовов Claude 0; обхода/пробного запроса нет.

## Контекст з KB

- Источник поручения: Codex 01a18640-a5de-76b3-aa83-4b7f5fae3033;
  подтверждение владельца Sentinel_f3df33a895a88191ba7391c4f3e5c0f8,
  12:17:45: общая связка dot–Vera–Claude; Orchestra не входит.
- Координатор myAI: контекст, задачи и file locks отсутствуют при чтении.
  Room open tasks проверены: активная ORCH-633 отдельно, совпадающих протокольных
  задач/файлов не обнаружено. Корневые правки vera-listener не трогаются.
- Прочитаны common.md, Vera adapter, AGENTS.md, VERA.md, conventions,
  office-intake.md, agent-room.md, voice-commands.md и dispatcher/office/room код.
  Локальные .agents/skills содержат отчёты и эксплуатационные команды, для этой
  правки неприменимы. Общие Python/documentation helpers прочитаны выборочно;
  FastAPI и локальная документация Vera имеют приоритет над itstep/Flask defaults.
- Статья Anthropic прочитана по ссылке из поручения; перенос её подхода ограничен
  рабочим протоколом и exact-reference read, без изменения архитектуры агентов.
- Локальная memory registry: найдена прежняя авторизация DOT в пределах задачи;
  актуальную область задаёт текущее поручение, старые ID чатов не используются.

## Проверка границ

Новый аргумент read-only и opt-in: старые клиенты читают как раньше. Он отсекает
чужой task_id, но не удостоверяет автора/артефакт/внешнюю квитанцию.
Единственность runtime одного principal остаётся процедурной; lease/fencing,
idempotency, owner block, attestation и approvals не меняются.
