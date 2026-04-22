# .supercc

This directory is created automatically by `supercc` and contains the config for this project instance.

## Contents

- `config.yaml` — Bot credentials and configuration
- `skills/` — Private skills for this project
- `cron_jobs.json` — Cron job definitions

Note: sessions.db and memories.db live in ~/.supercc/ (home dir, shared across projects).
Other data (cron, logs, skills, media, pid) lives in {project}/.supercc/.

## Git Ignore

This directory is gitignored. It should never be committed.

