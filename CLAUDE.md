# Notes for coding agents

Read `docs/ARCHITECTURE.md` before changing structure. Key rules:

- New LLM feature = new folder `app/features/<kind>/` (`__init__.py`, `schema.py`, `system.j2`,
  `user.j2`, `view.html`) + one import line in `app/features/__init__.py`. No DB migration,
  no route changes. Bump `LLMTask.version` whenever a prompt or schema changes (invalidates cache).
- Features never import each other; cross-feature data is read from the `analyses` table.
- All LLM calls go through `app.llm.gateway.LLMGateway.run` (cache, budget, usage). Never call a
  provider SDK from features, services or web code.
- Anything slow (LLM, network) runs as a job (`@job_handler` in `app/services/`); web handlers
  only enqueue and redirect to `/jobs/<id>`.
- Web layer calls `app/services/*`; ownership checks live in services (`get_owned`, `get_for_user`).
- Model changes need an Alembic migration (`alembic revision --autogenerate`);
  `tests/test_migrations.py` fails otherwise.
- Resume/vacancy text in prompts must stay inside `<resume>`/`<vacancy>`/`<preferences>` tags
  (see `app/llm/prompts/_common.j2`).
- UI text and prompts are in Russian; code and comments in English.

- User-facing failures raise subclasses of `app.core.errors.UserError` (`NotFound`, `ValidationFailed`,
  `JobError`, `LLMError`, `SourceError`); set `retryable = True` only for transient ones.
- New source: set `trusted = False` if its text is written by arbitrary people (it must never
  become the shared canonical vacancy). Parse third-party items with `safe_map`.
- Bot messages use `parse_mode=HTML`: escape every dynamic value with `esc()` and shorten plain
  text with `clip()` *before* escaping.
- Migrations on SQLite run with foreign keys off (see `migrations/env.py`); new NOT NULL columns
  need a `server_default`.

Before committing run `ruff check app tests` and `pytest` (fake LLM, fake Telegram, no network).
`tests/test_architecture.py` fails on layering violations — fix the design, don't add exceptions.
`worksaas doctor [--llm]` checks config and live connectivity on a real machine.

## Review agents

`.claude/agents/` holds critic subagents. After a non-trivial change, run the relevant ones
(in parallel) and address their findings before committing:

| Change | Agents |
|---|---|
| any code | `code-reviewer` |
| routes, auth, sources, bot, uploads, config | `security-auditor` |
| new module/source/feature/provider | `architecture-guard` |
| prompts or output schemas | `prompt-critic` |
| templates or bot flows | `ux-reviewer` |
| features / bug fixes | `test-critic` |
| planning what to build next | `product-critic` |
