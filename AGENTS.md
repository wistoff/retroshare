# AGENTS.md — Project orientation for AI coding agents

## Project summary

retroshare syncs ROM libraries to an R36S handheld running ArkOS over Wi-Fi. It has two components: a Docker app (runs on Unraid) that merges ROM folders from multiple Samba shares into a single unified share with a web UI, and Bash sync scripts (run on the R36S) that pull ROMs from that server to the handheld.

## Repository layout

```
docker/app/        Python server — merger, scraper, ROM identification, web UI
scripts/           Bash scripts deployed to the R36S handheld
docker-compose.yml Container orchestration
misc/coding-team/  Task briefs and working notes (gitignored)
```

## Tech stack

- **Python 3** — stdlib-first; only external dep: `watchdog`
- **Bash** — sync scripts; must work with mawk 1.3.3 and bash 5.0 on Ubuntu 19.10 arm64
- **Vanilla HTML/CSS/JS** — single-file SPA, no framework, no build step
- **Docker / Alpine 3.19**
- **Samba** — `smbd` in container, `smbclient` on handheld

## Key constraints

- **mawk pipe bug** — R36S runs ArkOS (Ubuntu 19.10 arm64) with mawk 1.3.3. Do NOT pipe `smbclient` output through `awk` directly; capture to a temp file first (mawk has pipe-buffering bugs that silently drop lines).
- **Filenames with spaces** — shell scripts use human-readable names (EmulationStation menu items).
- **ROM filename characters** — `&`, `,`, `'`, parentheses, and spaces are common; all quoting must handle these.
- **No linter/formatter configured.**
- **No type annotations** in Python.

## Testing

| What | Command |
|------|---------|
| Python unit tests | `python -m pytest docker/app/test_romident.py` |
| Docker build | `docker compose up -d --build` |
| Sync scripts | Deploy to R36S at `/roms2/tools/` via `scp`; run with `bash "/roms2/tools/Sync ROMs.sh"` |
| Server API | `http://<server-ip>:7868/api/status` |

## Conventions

- **Python** — `snake_case`, private helpers prefixed `_`, module docstrings
- **Bash** — `UPPER_SNAKE_CASE` for config vars, `lower_snake_case` for locals
- **No frameworks** — intentionally minimal
