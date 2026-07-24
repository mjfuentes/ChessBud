# chesscoach

Personal blitz chess coaching workspace for chess.com user `prosekkopapi` (~1000–1100 blitz, 5+5).

## Layout

- `data/games.pgn` — chess.com game export (replace with fresh exports over time)
- `data/eco_*.tsv` — lichess ECO opening database (do not edit; re-download from lichess-org/chess-openings)
- `tools/` — analysis scripts, run with `.venv/bin/python tools/<script>.py`
- `tools/trainer_server.py` + `web/` — local opening trainer UI at http://localhost:8420 (scripted drill replies from `data/drills/*.json`, deviation warnings, Stockfish at ~1320 after book)
- `data/drills/*.json` — drill definitions: `lines` of SAN with per-black-move `notes`; add new openings by adding files here
- `tools/make_puzzles.py` — mines games for blunders (>=250cp) into `data/puzzles.json`; the trainer's "Blunder replay" mode serves them (re-run after each new PGN export)
- `tools/fetch_games.py --user <name>` — pulls a user's full chess.com archive into `data/users/<name>/games.pgn` (public API, no auth)
- `tools/prepare_openings.py --user <name>` — generates personalized opening drills into `data/drills/<name>/` (user's sound moves kept, engine corrections for leaks, real opponent branches, engine extension to ~14 plies); manifest at `data/users/<name>/openings.json`
- New-user pipeline (foundation, not yet automated in UI): fetch_games → prepare_openings → restart trainer with `--user <name>`
- `.claude/agents/chess-coach.md` — the coaching agent; use it for game analysis questions

## Environment

- Python venv at `.venv/` with `python-chess` installed. Always use `.venv/bin/python`, never system python.
- Stockfish installed via Homebrew (`stockfish` on PATH).

## Conventions

- Analysis scripts are read-only over `data/`; write any generated reports to `reports/`.
- Engine analysis: depth 12–16 (sufficient at this rating, keeps runs fast). Use `chess.engine.SimpleEngine` from python-chess, and always `engine.quit()` in a `finally` block.
- The user's username in PGNs is `prosekkopapi`; scripts should take `--user` with that default.
