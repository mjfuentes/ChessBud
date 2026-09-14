# ChessBud

A local opening trainer that is built from *your* chess.com games.

It pulls your archive, works out which openings you actually reach and where
you leak, builds a drill book for each one (your sound moves kept, engine
corrections for the leaks, the real replies your opponents play, engine
extension past your games), and then lets you practise it on a board against
a scripted opponent that switches to a strength-limited Stockfish once you are
out of book. A second mode replays the blunders mined from your own games as
puzzles.

Everything runs on your machine. No accounts, no server, no telemetry. The
only network calls are to the public chess.com API to fetch games and ratings.

## What it does

- **Opening practice.** The opponent plays what your real opponents play. Off-book
  moves bounce back with a note; leaving your prep is graded by engine drift, and
  a run ends on an inaccuracy. Every move is named against the ECO book as it
  lands.
- **The tree.** The home screen draws your repertoire as a tree: branch length is
  how deep you have taken an opening, foliage is the lines you have cleared.
  Depth unlocks per opening as you clear lines (a "ladder"), so the book grows
  as you play instead of starting finished.
- **Blunder replay.** Positions where you lost 250cp or more, served as puzzles.
  Only the first attempt counts, and progress is persistent.
- **Coaching reports.** Command-line reports on openings, clock usage, and
  blunders by phase, for reading or for an LLM coaching agent.

## Requirements

- Python 3.11+
- [Stockfish](https://stockfishchess.org/) on your `PATH` (`brew install stockfish` on macOS)
- A chess.com username with some blitz games behind it

## Quick start

```bash
git clone git@github.com:mjfuentes/ChessBud.git && cd ChessBud
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt

export CHESSCOACH_USER=your_chesscom_name   # or pass --user to every script

.venv/bin/python tools/fetch_games.py        # data/users/<you>/games.pgn
.venv/bin/python tools/prepare_openings.py   # data/drills/<you>/*.json  (runs Stockfish, takes a while)
.venv/bin/python tools/make_puzzles.py       # data/puzzles.json         (runs Stockfish, takes a while)
.venv/bin/python tools/trainer_server.py     # http://localhost:8420
```

Re-run the three data steps whenever you want the trainer to catch up with
your recent games. Nothing you have cleared is lost: progress is keyed by
position, not by drill file.

## Tools

All scripts take `--user` (or read `CHESSCOACH_USER`) and are run from the repo root.

| Script | What it does |
| --- | --- |
| `tools/fetch_games.py` | Downloads a user's full chess.com archive to `data/users/<user>/games.pgn` |
| `tools/prepare_openings.py` | Builds per-opening drills from published theory plus your games, into `data/drills/<user>/` |
| `tools/make_puzzles.py` | Mines your games for blunders into `data/puzzles.json` |
| `tools/upgrade_puzzles.py` | Re-analyses the puzzle set with more engine time |
| `tools/trainer_server.py` | Serves the trainer UI and API on port 8420 |
| `tools/opening_report.py` | Score by opening family and colour |
| `tools/time_report.py` | Where your clock goes, from the `%clk` tags in chess.com PGNs |
| `tools/blunder_scan.py` | Engine scan of games for blunders, by phase and opening |
| `tools/activity_report.py` | Summary of your practice sessions |

## Data layout

```
data/
  eco_*.tsv              lichess ECO opening database (tracked)
  drills/*.json          hand-written drills shared by every user (tracked)
  drills/<user>/         generated drills                      (yours, ignored)
  users/<user>/          games.pgn and the openings manifest   (yours, ignored)
  puzzles*.json          mined puzzles                         (yours, ignored)
  ladder.json, puzzle_progress.json, activity_log.jsonl        (state, ignored)
```

A checkout serves one user at a time: puzzle and ladder state live in
`data/` next to that user's drills.

### Writing a drill by hand

`data/drills/exchange_french.json` is the shape: a name, an orientation, and
`lines` of SAN with `notes` keyed by the user's move number. Drop a file in
`data/drills/` and the trainer picks it up on the next start.

## Coaching with an LLM

`.claude/agents/chess-coach.md` is a Claude Code agent definition that knows
the tools above and how to ground advice in your own games. `CLAUDE.md`
holds the project conventions for it.

## Credits and licenses

This project is released under the GNU GPL v3 (see `LICENSE`), which is what
the vendored board library requires.

- [chessground](https://github.com/lichess-org/chessground) (GPL-3.0) is
  vendored in `web/vendor/` with a small local patch to arrow rendering.
- Piece set: *staunty* by sadsnake1, from the lichess assets (CC BY-NC-SA 4.0).
- Sounds: the lichess (lila) standard sound set; see lila's `COPYING.md`.
- Opening names: [lichess-org/chess-openings](https://github.com/lichess-org/chess-openings) (CC0).
- Leaf artwork on the home screen: adapted from an SVG Repo leaf icon.
- Stockfish is used as an external engine and is not distributed here.
