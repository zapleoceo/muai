# Backup size notification noise

## State

- 2026-10-20, Asia/Saigon. Worktree D:/Projects/vera-backup-alert-20261020; branch codex/backup-alert-noise; base/live source 71a95dd9bfe3b9ca0400d6d6ce8be7976db774a1.
- Request: stop Telegram warnings based only on stored backup volume; keep actual fault/disk-pressure alerts and backup safety.
- Verified: cron runs deployed vera3/scripts/vera3-monitor.sh; source matches checkout. Root disk 73%, 21 GB free. backup_pending reports all dump/archive bytes, not a proven failed backup or NAS transfer.
- Evidence: old monitor reproduced unwanted backup_pending in all four scenarios. Patched monitor passes 4/4 isolated Bash behavior tests (4 GB on healthy disk silent; disk_warn, disk_critical, disk_runway preserved). Backups remain intact in each test. bash -n and targeted Ruff checks pass. Full repository suite not run; no deployment yet.
- Done: removed only size-based backup alert/recovery and its unused threshold, updated deploy-ops documentation, added regression tests.
- Next: awaiting explicit release authorization requested in chat; then apply the scoped monitor fix, verify live source and cron behavior. Do not release unrelated pending branches.
- KB: Vera search for backup monitor/NAS yielded no relevant operational record; canonical sources are deploy-ops.md, live cron and script.
- No backup deletion, retention changes, NAS confirmation changes or service deployment authorized by this patch.
