# Ideas / roadmap

Design notes that aren't implemented yet. Keep short; delete once shipped.

## Follow-ups to the depth ladder (shipped — see tools/ladder.py)

- **The book is the ceiling.** Drill lines run 14–20 plies, so depth 10 (19
  plies) is supported by only a handful of lines per opening and depth 11+ by
  almost none. `deepest_supported()` caps each opening accordingly, which means
  hitting 1100 will not actually unlock depth 11 until `prepare_openings.py` is
  re-run with a longer engine tail (`TARGET_DEPTH`, currently 14).
- **Hints aren't attributable.** Hint events log `fen`/`san`/`mode` but no
  `game`, so a hint voids a pass only in the live client — reload or historical
  analysis and it is invisible. Needed before the ladder can be honest about
  hinted passes.
- **Verdict copy** could name the depth ("survived 6 of 6") now that the graded
  length varies per opening.
- **Demotion** is unimplemented: an opening never drops a rung, however badly it
  goes. Worth considering once there is data on whether ladders stall.

## Priority ordering (designed, not built)

Which opening to serve next should mix frequency with weakness: `√(share of
your real games) × (1 − depth/target)`. The square root keeps rare-but-real
defences (Pirc, 9 games) in rotation against common ones (Italian, 127) —
you do not choose which defence the opponent plays, so coverage matters more
than raw frequency.
