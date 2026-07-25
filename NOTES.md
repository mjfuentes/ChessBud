# Ideas / roadmap

Design notes that aren't implemented yet. Keep short; delete once shipped.

## Progressive opening depth ("survive to the middlegame")

Right now every practice run is graded over a fixed **10 user moves**
(`user_moves == 10` in `handle_move`, `OPENING_CHECK_PLIES = 24` for the
mistake-capture window). One length for everyone, forever.

Instead, make the graded depth a **level** the user climbs:

| level | user moves | intent |
|---|---|---|
| Beginner | 3–4 | just the move order — do you know the first branch? |
| Intermediate | 10 | current behaviour — through the structural decisions |
| Advanced | 15 | out of the opening entirely, into the early middlegame |

The framing is "how long can you survive against the engine before the
middlegame". Past ~15 moves everyone at this rating gets destroyed anyway, so
15 is the natural ceiling — the goal is reaching a playable middlegame, not
outplaying Stockfish.

Open questions:

- **Per opening, or global?** Probably per opening — you can be advanced in the
  Italian and a beginner in the Pirc. The repertoire column on the home screen
  would show a level per line.
- **Promotion rule.** Something like: pass N runs clean at the current level and
  the next one unlocks. Needs to survive the drift-based verdict (a beginner run
  is 3 moves of book, so drift will almost always be ~0 — the level ladder may
  need its own pass rule at short depths).
- **Drill data.** Lines in `data/drills/` are ~14–20 plies, so advanced (15 user
  moves = 30 plies) will run past the book on most lines and hand off to the
  engine earlier than intended. `prepare_openings.py` would need to extend the
  engine tail for advanced levels.
- **Verdict copy** should name the level ("survived 10 of 10" reads better than
  "opening passed" once depth is variable).
