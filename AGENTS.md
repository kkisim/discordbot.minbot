# Repository Guidelines

## Project Structure & Module Organization
- Keep runtime code under `src/`; use `src/bot.py` (or `main.py`) as the single entry point and place reusable features in `src/cogs/` by domain (music, moderation, etc.).
- Store shared helpers in `src/utils/` (logging, embeds, config loaders) and long-lived constants in `src/settings.py`.
- Put tests in `tests/` mirroring the `src/` layout; integration-style cases can live under `tests/integration/`.
- Check example assets (sample config, starter intents) into `assets/` and keep secrets in `.env` only; provide sanitized examples via `.env.example`.

## Build, Test, and Development Commands
- Create an isolated environment: `python -m venv .venv` then `.\.venv\Scripts\activate` (Windows) or `source .venv/bin/activate` (Unix).
- Install dependencies: `pip install -r requirements.txt` (include dev extras like `pytest`, `black`, `ruff`).
- Run the bot locally: `python -m src.bot` (or `python src/bot.py`) after exporting `DISCORD_TOKEN` and required IDs.
- Format and lint: `python -m black .` and `python -m ruff check .` (use `python -m ruff format .` if configured).
- Test suite: `python -m pytest` for unit tests; add `-m integration` for network-dependent checks and keep them skipped by default in CI unless credentials are available.

## Coding Style & Naming Conventions
- Follow PEP 8 with 4-space indents, 88-character lines, and type hints on public functions; prefer dataclasses or TypedDicts for structured payloads.
- Use `snake_case` for functions/variables, `PascalCase` for classes/cogs, and prefix async task helpers with `async_` when clarity helps.
- Require docstrings on modules and public coroutines; include intent/side effects for commands that modify guild state.
- Prefer dependency injection (pass clients/config) instead of importing globals so cogs stay testable.

## Testing Guidelines
- Write `test_*.py` files that mirror module paths; name tests to describe behavior (`test_kick_command_rejects_self_target`).
- Mock Discord HTTP calls and gateways; isolate timeouts and sleeps with `asyncio` test utilities.
- Target ≥80% line coverage for new code; gate PRs with `python -m pytest --maxfail=1 --disable-warnings --cov=src`.
- For integration tests that hit Discord, guard with `@pytest.mark.integration` and document required env vars (`DISCORD_TOKEN`, `GUILD_ID`).

## Commit & Pull Request Guidelines
- Use small, focused commits following Conventional Commits (`feat: add ban command`, `fix: handle missing intents`); keep subject ≤72 chars.
- PRs should include: purpose/behavior summary, linked issues, commands/tests run, and screenshots or logs for user-facing changes.
- Avoid large mixed PRs; split refactors from feature work and include rollout or migration notes when altering config or permissions.

## Security & Configuration Tips
- Never commit secrets; load credentials from `.env` and check in a scrubbed `.env.example` showing keys like `DISCORD_TOKEN`, `APPLICATION_ID`, `GUILD_ID`.
- Restrict bot intents to the minimum required; document any privileged intent usage in `README` or `src/settings.py`.
- Rotate tokens after sharing logs, and ensure logging scrubs user IDs or tokens before writing to disk.
