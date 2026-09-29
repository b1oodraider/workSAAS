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

Run `pytest` before committing: it uses the fake LLM provider and needs no network.

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
