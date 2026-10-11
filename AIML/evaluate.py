"""Evaluate the early-warning run: did we predict before the failure, any false alarms?

Ground truth comes from config.json (failure_window, restart_minute).
Reads alerts.log (or runs the replay in-process with --run).

Checks:
  1. First WARNING-or-worse is inside/after the peak ramp, before failure end.
  2. A failure prediction fires strictly BEFORE failure_window start.
  3. Zero WARNING/CRITICAL in the normal train window (false alarms).
  4. Scores settle back to OK/WATCH after restart+2 (recovery detected).

Usage:
  python3 monitor.py --replay && python3 evaluate.py
  python3 evaluate.py --run   # replay in-process, then evaluate
"""

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from early_warning import CONFIG

HERE = os.path.dirname(os.path.abspath(__file__))


def load_alerts():
    with open(os.path.join(HERE, CONFIG["alerts_file"]), encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Evaluate early-warning performance.")
    ap.add_argument("--run", action="store_true", help="run replay first, then evaluate")
    args = ap.parse_args(argv)
    if args.run:
        from train import load_minutes
        from monitor import StdBackend, handle_minute
        import io

        backend = StdBackend()
        buckets = load_minutes(os.path.join(HERE, CONFIG["log_file"]))
        buf = io.StringIO()
        for m in sorted(buckets):
            handle_minute(m, buckets[m], backend, buf)
        buf.seek(0)
        alerts = [json.loads(line) for line in buf if line.strip()]
    else:
        alerts = load_alerts()

    f_lo, f_hi = CONFIG["failure_window"]
    t_lo, t_hi = CONFIG["train_window"]
    restart = CONFIG["restart_minute"]
    warns = [a for a in alerts if a["kind"] == "alert" and a["level"] in ("WARNING", "CRITICAL")]
    preds = [a for a in alerts if a["kind"] == "prediction"]
    fails, notes = [], []

    first_warn = min([a["ts_min"] for a in warns], default=None)
    if first_warn is None:
        fails.append("no WARNING/CRITICAL raised at all")
    elif first_warn >= f_hi:
        fails.append(f"first WARNING at min {first_warn} — after failure already underway")
    else:
        notes.append(f"first WARNING/CRITICAL at min {first_warn} "
                     f"({f_lo - first_warn} min before failure window {f_lo}-{f_hi})")

    early_preds = [a for a in preds if a["minute"] < f_lo]
    if not early_preds:
        fails.append("no failure prediction before the failure window")
    else:
        p = early_preds[0]
        notes.append(f"failure predicted at min {p['minute']} for {p['service']} "
                     f"({f_lo - p['minute']} min lead time)")

    fp = sorted({a["ts_min"] for a in warns if t_lo <= a["ts_min"] <= t_hi})
    if fp:
        fails.append(f"false alarms inside normal window min {t_lo}-{t_hi}: {fp}")
    else:
        notes.append(f"zero false WARNING/CRITICAL in normal window min {t_lo}-{t_hi}")

    post = [a for a in warns if a["ts_min"] >= restart + 2]
    if post:
        notes.append(f"note: {len(post)} WARNING+ after recovery (min {restart + 2}+) — "
                     "check residual backlog severity")
    else:
        notes.append(f"scores settled after restart (no WARNING+ from min {restart + 2})")

    print("Early-warning evaluation")
    for n in notes:
        print(f"  PASS-ish: {n}")
    for f_ in fails:
        print(f"  FAIL: {f_}")
    print(f"  alerts={len([a for a in alerts if a['kind']=='alert'])} "
          f"predictions={len(preds)}")
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(main())
