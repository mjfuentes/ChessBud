"""Summarize the trainer activity log (data/activity_log.jsonl).

Usage: .venv/bin/python tools/activity_report.py [--days 30]
"""

from __future__ import annotations

import argparse
import json
import time
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOG = ROOT / "data" / "activity_log.jsonl"


def load_events(days: float) -> list[dict]:
    if not LOG.exists():
        return []
    cutoff = time.time() - days * 86400
    events = []
    with open(LOG, encoding="utf-8") as fh:
        for line in fh:
            try:
                e = json.loads(line)
            except json.JSONDecodeError:
                continue
            if e.get("ts", 0) >= cutoff:
                events.append(e)
    return events


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days", type=float, default=30)
    args = parser.parse_args()
    events = load_events(args.days)
    if not events:
        print("No activity logged yet.")
        return

    by_kind = Counter(e["kind"] for e in events)
    print(f"Activity, last {args.days:.0f} days: " +
          ", ".join(f"{k}={n}" for k, n in by_kind.most_common()))

    moves = [e for e in events if e["kind"] == "move"]
    if moves:
        rejected = sum(1 for e in moves if e.get("rejected"))
        prep_known = [e for e in moves if e.get("prep") is not None and not e.get("rejected")]
        in_prep = sum(1 for e in prep_known if e["prep"])
        fixed = sum(1 for e in moves if e.get("fixed"))
        print(f"\nPRACTICE: {len(moves)} moves, {rejected} bounced "
              f"({100.0 * rejected / len(moves):.0f}%)")
        if prep_known:
            print(f"  prep adherence: {in_prep}/{len(prep_known)} "
                  f"({100.0 * in_prep / len(prep_known):.0f}%) of book-covered moves")
        print(f"  historical mistakes fixed in-game: {fixed}")

        # opening passes: per game, clean = no bounce before prep ran out / move 10
        starts = {e.get("game"): e for e in events if e["kind"] == "game_start" and e.get("game")}
        by_game: dict[str, list[dict]] = defaultdict(list)
        for e in moves:
            if e.get("game"):
                by_game[e["game"]].append(e)
        passes = fails = 0
        for game_id, game_moves in by_game.items():
            game_moves.sort(key=lambda e: e["ts"])
            bounces = 0
            orientation = starts.get(game_id, {}).get("orientation", "white")
            for e in game_moves:
                if e.get("rejected"):
                    bounces += 1
                    continue
                if e.get("source") == "engine" or e.get("ply", 0) >= 19:
                    cp = e.get("eval_cp", 0) or 0
                    user_cp = cp if orientation == "white" else -cp
                    if bounces == 0 and user_cp >= -100:
                        passes += 1
                    else:
                        fails += 1
                    break
        if passes or fails:
            print(f"  opening passes: {passes}/{passes + fails} games completed cleanly")

    attempts = [e for e in events if e["kind"] == "puzzle_attempt"]
    if attempts:
        firsts = [e for e in attempts if e.get("first_attempt")]
        correct_firsts = sum(1 for e in firsts if e["correct"])
        print(f"\nPUZZLES: {len(attempts)} attempts, {len(firsts)} first tries, "
              f"{correct_firsts} solved cold "
              f"({100.0 * correct_firsts / len(firsts):.0f}% first-try accuracy)"
              if firsts else f"\nPUZZLES: {len(attempts)} attempts")

        served_at: dict[str, float] = {}
        think_times = []
        for e in events:
            if e["kind"] == "puzzle_served":
                served_at[e["id"]] = e["ts"]
            elif e["kind"] == "puzzle_attempt" and e.get("first_attempt"):
                t0 = served_at.pop(e["id"], None)
                if t0 is not None and 0 < e["ts"] - t0 < 600:
                    think_times.append(e["ts"] - t0)
        if think_times:
            think_times.sort()
            median = think_times[len(think_times) // 2]
            print(f"  think time (first tries): median {median:.0f}s, "
                  f"max {think_times[-1]:.0f}s over {len(think_times)} puzzles")

    days_active = defaultdict(int)
    for e in events:
        days_active[time.strftime("%Y-%m-%d", time.localtime(e["ts"]))] += 1
    print("\nBY DAY: " + ", ".join(f"{d}: {n}" for d, n in sorted(days_active.items())))


if __name__ == "__main__":
    main()
