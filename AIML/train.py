"""Train the normal-pattern baseline from the stable window of the log.

File source (default): train_window minutes of logs/microservices_1h.log.
CloudWatch source: last --minutes complete minutes (assumes recent traffic
is normal — run this in a known-good window).

Usage:
  python3 train.py [--log logs/microservices_1h.log]
  python3 train.py --source cloudwatch [--minutes 30]
Writes models/baseline.json and prints the learned per-service medians.
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from early_warning import CONFIG, Baseline, minute_features, parse_line

HERE = os.path.dirname(os.path.abspath(__file__))


def load_minutes(log_path: str) -> dict[int, list[dict]]:
    buckets: dict[int, list[dict]] = {}
    with open(log_path, encoding="utf-8") as f:
        for line in f:
            r = parse_line(line)
            if r is None:
                continue
            buckets.setdefault(r["min"], []).append(r)
    return buckets


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Learn normal baseline from log.")
    ap.add_argument("--log", default=os.path.join(HERE, CONFIG["log_file"]))
    ap.add_argument("--source", default="file", choices=["file", "cloudwatch"])
    ap.add_argument("--minutes", type=int, default=30,
                    help="cloudwatch: how many recent complete minutes to learn from")
    args = ap.parse_args(argv)

    if args.source == "cloudwatch":
        from datetime import datetime, timedelta, timezone

        from cloudwatch_source import CloudWatchSource

        source = CloudWatchSource()
        if not source.log_groups:
            print("No log_groups mapped in config.json cloudwatch section.")
            return 1
        now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
        per_minute = []
        for back in range(args.minutes, 0, -1):
            start = now - timedelta(seconds=60 * (back + 1))
            try:
                feats = source.fetch_minute_features(start)
            except Exception as exc:
                print(f"skip {start.strftime('%H:%MZ')}: {str(exc)[:120]}")
                continue
            if feats:
                per_minute.append(feats)
        print(f"Learned from {len(per_minute)} recent CloudWatch minutes.")
    else:
        buckets = load_minutes(args.log)
        lo, hi = CONFIG["train_window"]
        per_minute = [minute_features(buckets[m]) for m in range(lo, hi + 1)
                      if m in buckets]
        print(f"Train window min {lo}-{hi} ({len(per_minute)} buckets).")
    if not per_minute:
        print("No training minutes found.")
        return 1
    model = Baseline().fit(per_minute)
    out = os.path.join(HERE, CONFIG["baseline_file"])
    os.makedirs(os.path.dirname(out), exist_ok=True)
    model.save(out)
    print(f"Trained on {len(per_minute)} buckets -> {out}")
    for svc in sorted(model.stats):
        meds = {f: round(model.stats[svc][f][0], 3) for f in
                ("err_rate", "p95", "warn_rate", "exception_rate", "timeout_rate")}
        print(f"  {svc:10s} medians: {meds}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
