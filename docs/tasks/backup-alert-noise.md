# Backup size notification noise

## State

- 2026-10-20, Asia/Saigon. Worktree D:/Projects/vera-backup-alert-20261020; branch codex/backup-alert-noise; base/live source 71a95dd9bfe3b9ca0400d6d6ce8be7976db774a1.
- Request: stop Telegram warnings based only on stored backup volume; keep actual fault/disk-pressure alerts and backup safety.
- Verified: cron runs deployed vera3/scripts/vera3-monitor.sh; source matches checkout. Root disk 73%, 21 GB free. backup_pending reports all dump/archive bytes, not a proven failed backup or NAS transfer.
- Evidence: old monitor reproduced unwanted backup_pending in all four scenarios. Patched monitor passes 4/4 isolated Bash behavior tests (4 GB on healthy disk silent; disk_warn, disk_critical, disk_runway preserved). Backups remain intact in each test. bash -n and targeted Ruff checks pass. Full repository suite not run locally.
- Done: removed only size-based backup alert/recovery and its unused threshold, updated deploy-ops documentation, added regression tests.
- Release: user explicitly approved applying the monitor fix on 2026-10-20. Installed only the monitor script atomically, after checking old live SHA256 against base. Live/source SHA256: 6da8e63ddd59826fbe81410b10963ecb29a8f46166f917d94576164e602104bd. Cron still invokes that exact file every five minutes. No container or backup process restarted. Previous script retained for rollback outside backup storage at /var/lib/vera3-monitor/rollback/monitor-before-ee9571c7.sh.
- Next: open scoped PR and obtain normal CI/review/merge before another deployment; production hotfix is active but origin/master still has the original alert and a normal deploy would revert it. No unrelated branch merge or full service deployment performed.
- KB: Vera search for backup monitor/NAS yielded no relevant operational record; canonical sources are deploy-ops.md, live cron and script.
- No backup deletion, retention changes, NAS confirmation changes or service deployment authorized by this patch.
