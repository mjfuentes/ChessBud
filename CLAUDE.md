# chesscoach

Local blitz-chess coaching workspace: a chess.com archive, analysis scripts,
and a browser opening trainer (ChessBud). See README.md for the user-facing
overview.

## Layout

- `data/users/<user>/games.pgn` — the user's chess.com archive (from `tools/fetch_games.py`)
- `data/eco_*.tsv` — lichess ECO opening database (do not edit; re-download from lichess-org/chess-openings)
- `tools/` — analysis scripts, run with `.venv/bin/python tools/<script>.py`
- `tools/trainer_server.py` + `web/` — local opening trainer UI at http://localhost:8420 (scripted drill replies from `data/drills/`, deviation warnings, strength-limited Stockfish after book)
- `data/drills/*.json` — hand-written drills shared by every user: `lines` of SAN with per-move `notes`
- `data/drills/<user>/*.json` — generated drills from `tools/prepare_openings.py`; manifest at `data/users/<user>/openings.json`
- `tools/make_puzzles.py` — mines games for blunders (>=250cp) into `data/puzzles.json`; the trainer's "Blunder replay" mode serves them
- `tools/userarg.py` — shared `--user`/`--pgn` handling; every script imports it rather than hardcoding a username
- New-user pipeline: fetch_games → prepare_openings → make_puzzles → trainer_server
- `.claude/agents/chess-coach.md` — the coaching agent; use it for game analysis questions

## Environment

- Python venv at `.venv/` with `chess` (python-chess) installed. Always use `.venv/bin/python`, never system python.
- Stockfish on PATH.
- The active user comes from `--user` or the `CHESSCOACH_USER` environment variable. Never hardcode a username.

## Conventions

- Analysis scripts are read-only over `data/`; write any generated reports to `reports/` (ignored).
- Everything under `data/` except `eco_*.tsv` and top-level `drills/*.json` is per-user or runtime state and is git-ignored.
- Engine analysis: depth 12–16 (sufficient at club rating, keeps runs fast). Use `chess.engine.SimpleEngine` from python-chess, and always `engine.quit()` in a `finally` block.
