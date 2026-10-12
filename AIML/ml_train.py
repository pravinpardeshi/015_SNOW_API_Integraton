"""Train the ML models on multi-seed synthetic logs (test on seed 42).

Default: generate internal-format logs in memory from --seeds.
With --cw-logs: train from CloudWatch-format files instead — parsed through
the exact runtime path (parse_log_message), so training and inference see
identical features. Minutes are relative to each file's first event; use
--failure-min to locate the failure window (default 45, matching the
synthetic scenario).

  IsolationForest: normal minutes (train_window) of every seed.
  RandomForest: windowed supervised labels — minute m is positive when the
  known failure starts within HORIZON minutes; failure/restart minutes are
  excluded (we predict *ahead*, not during).

Usage:
  python3 ml_train.py [--seeds 1 2 3 4 5]
  python3 ml_train.py --cw-logs logs/cw_seed1.jsonl logs/cw_seed2.jsonl [--failure-min 45]
Needs sklearn (system python3).
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from early_warning import CONFIG, minute_features
from ml_model import (ONE_HOT, WIN, BASE_FEATURES, models_dir, save_meta,
                      train_failure_forest, train_isolation_forests,
                      window_features)

HERE = os.path.dirname(os.path.abspath(__file__))


def seed_log_path(seed: int) -> str:
    return os.path.join(HERE, "logs", f"train_seed{seed}.log")


def build_seed_buckets(seed: int) -> dict[int, list[dict]]:
    import generate_logs
    import json

    lines = generate_logs.generate(seed=seed)
    buckets: dict[int, list[dict]] = {}
    for r in lines:
        buckets.setdefault(r["min"], []).append(r)
    return buckets


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Train IF + RF models.")
    ap.add_argument("--seeds", type=int, nargs="+", default=CONFIG["ml"]["train_seeds"])
    ap.add_argument("--cw-logs", nargs="*", default=[],
                    help="CloudWatch-format files to train from (instead of --seeds)")
    ap.add_argument("--failure-min", type=int, default=45,
                    help="relative failure-start minute inside --cw-logs files")
    args = ap.parse_args(argv)
    os.makedirs(models_dir(), exist_ok=True)

    lo, hi = CONFIG["train_window"]
    horizon = CONFIG["ml"]["horizon"]
    restart_min = CONFIG["restart_minute"]

    if_rows: dict[str, list[list[float]]] = {}
    X_rf, y_rf = [], []

    def ingest(feats_by_min: dict[int, dict], f_lo: int) -> None:
        for svc in CONFIG["services"]:
            series = [(m, feats_by_min[m][svc]) for m in sorted(feats_by_min)
                      if svc in feats_by_min[m]]
            for m, fmap in series:
                if lo <= m <= hi:
                    if_rows.setdefault(svc, []).append(
                        [fmap[f] for f in BASE_FEATURES])
            hist = []
            for m, fmap in series:
                hist.append(fmap)
                if f_lo <= m <= restart_min + (f_lo - CONFIG["failure_window"][0]):
                    continue  # never train on failure/restart minutes
                x = window_features(hist[-WIN:]) + ONE_HOT[svc]
                label = 1 if 0 < (f_lo - m) <= horizon else 0
                X_rf.append(x)
                y_rf.append(label)

    if args.cw_logs:
        from cloudwatch_source import load_cw_file

        for path in args.cw_logs:
            buckets = load_cw_file(path)
            print(f"{path}: {sum(len(v) for v in buckets.values())} events, "
                  f"{len(buckets)} minutes")
            ingest({m: minute_features(rows) for m, rows in buckets.items()},
                   args.failure_min)
    else:
        f_lo = CONFIG["failure_window"][0]
        for seed in args.seeds:
            buckets = build_seed_buckets(seed)
            # Persist the seed log for reproducibility.
            import json

            with open(seed_log_path(seed), "w", encoding="utf-8") as f:
                for m in sorted(buckets):
                    for r in buckets[m]:
                        f.write(json.dumps(r) + "\n")
            ingest({m: minute_features(rows) for m, rows in buckets.items()}, f_lo)
    print(f"IF train rows/service: { {s: len(v) for s, v in if_rows.items()} }")
    print(f"RF samples: {len(y_rf)} positives: {sum(y_rf)}")

    cal = train_isolation_forests(if_rows)
    print("IF calibration (watch/warning/critical per service):")
    for svc, th in cal.items():
        print(f"  {svc:10s} { {k: round(v, 3) for k, v in th.items()} }")
    report = train_failure_forest(X_rf, y_rf)
    print(f"RF train: {report}")
    save_meta(cal, report)
    print(f"Models -> {models_dir()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
