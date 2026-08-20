# /deploy

Ship `master` to production (Vera 3 on Hetzner).

## Preferred path — let CI do it

```bash
git push origin master
```

Push triggers `.github/workflows/deploy.yml`: **docs gate → test gate
(coverage 70%) → quality gate (ruff `E,F,W,I,B,UP,SIM,C4,RET`, vulture,
diff-cover 75%, docs name-sync) → deploy**. If any gate fails the deploy
is blocked. Watch the run before declaring success.

## Manual path — when CI is stuck

```bash
ssh hetzner-root /usr/local/bin/vera3-deploy
```

`vera3-deploy` takes **no arguments** and is not per-service. It always:

1. `git fetch + reset --hard origin/master` in `/var/www/muai-checkout`
2. `rsync -az --delete vera3/ → /var/www/vera3/` (preserves `.env`,
   `infra/.env`, `*.session`, pycache — replaces everything else)
3. `docker compose build --quiet && up -d --remove-orphans` in
   `/var/www/vera3/infra`
4. polls `vera3-gateway /healthz` for 60s; exits `11` if it never answers

So the manual path still deploys **whatever is on `origin/master`** — it
cannot ship uncommitted local work.

## Single service, without a full redeploy

```bash
ssh hetzner-root "cd /var/www/vera3/infra && docker compose up -d --build --no-deps gateway"
```

Valid services: `gateway`, `dashboard`, `brain-search`, `brain-triage`,
`bot-telegram`, `ingestor-gmail`, `ingestor-telegram`,
`ingestor-instagram`, `media-worker`, `postgres`, `prune`.

## Verify

```bash
ssh hetzner-root "cd /var/www/muai-checkout && git log --oneline -1"
```

Compare to local `git log --oneline -1`, then tail the service you
touched (see `/logs`).

## Rules

- Never `git push --force`, never `git reset --hard` on the server.
- Never `docker compose down -v` — it destroys the Postgres volume.
- On build failure: show the full error and stop. Do not auto-rollback.
- **Server-side edits are not durable.** The next `vera3-deploy` rsyncs
  `--delete` over `/var/www/vera3/`. Any live hotfix must be committed and
  pushed to `master` or it silently reverts. This already burned ~20 files
  in 2026-07.
