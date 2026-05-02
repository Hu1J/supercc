# .supercc

This directory is created automatically by `supercc` and contains the config for this project instance.

## Contents

- `config.json` — Bot credentials and configuration（2026-05-02 起从 YAML 迁移）
- `model.json` — 模型配置（per-project 隔离）
- `skills/` — Private skills for this project
- `cron_jobs.json` — Cron job definitions

Note: sessions.db and memories.db live in ~/.supercc/ (home dir, shared across projects).
Other data (cron, logs, skills, media, pid) lives in {project}/.supercc/.

The `storage` section is no longer needed — sessions.db path is fixed to ~/.supercc/sessions.db.

## Git Ignore

This directory is gitignored. It should never be committed.

