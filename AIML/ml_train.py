"""Train the ML models on multi-seed synthetic logs (test on seed 42).

  IsolationForest: normal minutes (train_window) of every seed.
  RandomForest: windowed supervised labels — minute m is positive when the
  known failure starts within HORIZON minutes; failure/restart minutes are
  excluded (we predict *ahead*, not during).

Usage:
  python3 ml_train.py [--seeds 1 2 3 4 5]
Needs sklearn (system python3).
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from early_warning import CONFIG, minute_features
from ml_model import (ONE_HOT, WIN, models_dir, save_meta, train_failure_forest,
                      train_isolation_forests, window_features)

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
    args = ap.parse_args(argv)
    os.makedirs(models_dir(), exist_ok=True)

    lo, hi = CONFIG["train_window"]
    f_lo = CONFIG["failure_window"][0]
    horizon = CONFIG["ml"]["horizon"]

    if_rows: dict[str, list[list[float]]] = {}
    X_rf, y_rf = [], []
    for seed in args.seeds:
        buckets = build_seed_buckets(seed)
        feats_by_min = {m: minute_features(rows) for m, rows in buckets.items()}
        # Persist the seed log for reproducibility.
        import json

        with open(seed_log_path(seed), "w", encoding="utf-8") as f:
            for m in sorted(buckets):
                for r in buckets[m]:
                    f.write(json.dumps(r) + "\n")
        for svc in CONFIG["services"]:
            series = [(m, feats_by_min[m][svc]) for m in sorted(feats_by_min)
                      if svc in feats_by_min[m]]
            for m, fmap in series:
                if lo <= m <= hi:
                    if_rows.setdefault(svc, []).append(
                        [fmap[f] for f in
                         ["count", "err_rate", "p95", "avg_lat", "cpu", "mem", "pool"]])
            hist = []
            for m, fmap in series:
                hist.append(fmap)
                if CONFIG["failure_window"][0] <= m <= CONFIG["restart_minute"]:
                    continue  # never train on failure/restart minutes
                x = window_features(hist[-WIN:]) + ONE_HOT[svc]
                label = 1 if 0 < (f_lo - m) <= horizon else 0
                X_rf.append(x)
                y_rf.append(label)
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
