"""Real-time monitor: score each minute, predict failure. Two backends:

  default (stdlib): robust-z baseline + streak predictor  -> any interpreter
  --ml             : IsolationForest + RandomForest (sklearn, system python3)

Modes:
  --replay            process the whole log minute-by-minute (default), with
                      --speed N inserting N seconds between minutes to simulate
                      real time (0 = as fast as possible).
  --live              tail the log file: score what exists, then keep watching
                      for appended lines (Ctrl+C to stop).

Every minute prints: min, worst service + score + level.
WATCH/WARNING/CRITICAL minutes and failure predictions are appended to
alerts.log as JSON lines.

Usage:
  python3 monitor.py --replay
  python3 monitor.py --replay --ml
  python3 monitor.py --replay --speed 1
  python3 monitor.py --live [--ml]
"""

import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from early_warning import CONFIG, Baseline, Predictor, level_for, minute_features, parse_line

HERE = os.path.dirname(os.path.abspath(__file__))


class StdBackend:
    """Original stdlib backend: Baseline z-scores + streak Predictor."""

    kind = "std"

    def __init__(self):
        self.model = Baseline.load(os.path.join(HERE, CONFIG["baseline_file"]))
        self.predictor = Predictor()

    def score_minute(self, feats: dict) -> dict:
        return self.model.zscores(feats)

    def level_for(self, svc: str, score: float) -> str:
        return level_for(score)

    def predict(self, minute: int, feats: dict):
        return self.predictor.update(minute, self.score_minute(feats))


class MLBackend:
    """sklearn backend: IsolationForest scores + RandomForest prediction."""

    kind = "ml"

    def __init__(self):
        from ml_model import MLBackend as _ML

        self.impl = _ML()

    def score_minute(self, feats: dict) -> dict:
        return self.impl.score_minute(feats)

    def level_for(self, svc: str, score: float) -> str:
        return self.impl.level_for(svc, score)

    def predict(self, minute: int, feats: dict):
        return self.impl.predict(minute, feats)


def open_alerts():
    path = os.path.join(HERE, CONFIG["alerts_file"])
    return open(path, "w", encoding="utf-8")


def handle_minute(m: int, records: list[dict], backend, alerts) -> dict:
    feats = minute_features(records)
    return handle_feats(m, feats, backend, alerts)


def handle_feats(m: int, feats: dict, backend, alerts, label: str = "") -> dict:
    scores = backend.score_minute(feats)
    worst = max(scores.items(), key=lambda kv: kv[1][1]) if scores else (None, ({}, 0.0))
    svc, (_detail, score) = worst
    level = backend.level_for(svc, score) if svc else "OK"
    top_feat = "-"
    if isinstance(_detail, dict) and _detail:
        top_feat = max(_detail, key=lambda k: abs(_detail[k]))
    tag = f"min {m:02d}" if not label else label
    print(f"{tag}  {svc or '-':10s} score {score:8.3f} {level:8s} (top: {top_feat})",
          flush=True)
    if level in ("WATCH", "WARNING", "CRITICAL"):
        alerts.write(json.dumps({"ts_min": m, "kind": "alert", "service": svc,
                                 "score": round(score, 3), "level": level,
                                 "top_feature": top_feat,
                                 "backend": backend.kind}) + "\n")
        alerts.flush()
    pred = backend.predict(m, feats)
    if pred:
        print(f"min {m:02d}  >>> {pred['message']}", flush=True)
        entry = {"ts_min": m, "kind": "prediction", "backend": backend.kind, **pred}
        alerts.write(json.dumps(entry) + "\n")
        alerts.flush()
    return {"minute": m, "service": svc, "score": score, "level": level}


def make_backend(use_ml: bool):
    return MLBackend() if use_ml else StdBackend()


def cmd_replay(args) -> int:
    from train import load_minutes

    backend = make_backend(args.ml)
    if args.format == "cloudwatch":
        from cloudwatch_source import load_cw_file

        buckets = load_cw_file(args.log)
        print(f"Replaying CloudWatch file {args.log} "
              f"({sum(len(v) for v in buckets.values())} events).")
    else:
        buckets = load_minutes(args.log or os.path.join(HERE, CONFIG["log_file"]))
    with open_alerts() as alerts:
        for m in sorted(buckets):
            handle_minute(m, buckets[m], backend, alerts)
            if args.speed > 0:
                time.sleep(args.speed)
    print(f"Replay [{backend.kind}] done -> {CONFIG['alerts_file']}")
    return 0


def cmd_live(args) -> int:
    backend = make_backend(args.ml)
    path = os.path.join(HERE, CONFIG["log_file"])
    buckets: dict[int, list[dict]] = {}
    done: set[int] = set()
    print(f"Tailing {path} [{backend.kind}] (Ctrl+C to stop)…")
    with open_alerts() as alerts:
        try:
            with open(path, encoding="utf-8") as f:
                while True:
                    pos = f.tell()
                    line = f.readline()
                    if not line:
                        # Score any complete minute, then wait for more data.
                        ready = [m for m in buckets if m not in done]
                        for m in sorted(ready):
                            handle_minute(m, buckets[m], backend, alerts)
                            done.add(m)
                        time.sleep(1.0)
                        f.seek(pos)
                        continue
                    r = parse_line(line)
                    if r is not None:
                        buckets.setdefault(r["min"], []).append(r)
        except KeyboardInterrupt:
            print("\nStopped.")
    return 0


def cmd_cloudwatch(args) -> int:
    """Poll CloudWatch minute-by-minute, forever (Ctrl+C to stop)."""
    from datetime import datetime, timedelta, timezone

    from cloudwatch_source import CW_CFG, CloudWatchSource

    backend = make_backend(args.ml)
    source = CloudWatchSource()
    if not source.log_groups:
        print("No log_groups mapped in config.json cloudwatch section.")
        return 1
    poll = CW_CFG.get("poll_interval", 65)
    print(f"Monitoring CloudWatch {source.region} "
          f"({len(source.log_groups)} groups) [{backend.kind}]…")
    done: set[int] = set()
    with open_alerts() as alerts:
        try:
            while True:
                now = datetime.now(timezone.utc)
                # Last *complete* minute (CloudWatch also lags ~1-3 min).
                start = now.replace(second=0, microsecond=0) - timedelta(seconds=60)
                epoch = int(start.timestamp() // 60)
                if epoch not in done:
                    try:
                        feats = source.fetch_minute_features(start)
                        if feats:
                            handle_feats(epoch, feats, backend, alerts,
                                         label=start.strftime("%H:%MZ"))
                            done.add(epoch)
                    except Exception as exc:  # transient AWS error: retry next poll
                        print(f"{start.strftime('%H:%MZ')}  AWS poll failed: "
                              f"{str(exc)[:150]}", flush=True)
                time.sleep(poll)
        except KeyboardInterrupt:
            print("\nStopped.")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Early-warning monitor.")
    ap.add_argument("--replay", action="store_true", help="score the whole log (default)")
    ap.add_argument("--live", action="store_true", help="tail the log file")
    ap.add_argument("--source", default="file", choices=["file", "cloudwatch"],
                    help="log source (default: file)")
    ap.add_argument("--ml", action="store_true", help="use IsolationForest + RandomForest backend")
    ap.add_argument("--log", default="", help="replay this file instead of the configured log")
    ap.add_argument("--format", default="internal", choices=["internal", "cloudwatch"],
                    help="format of --log (default: internal)")
    ap.add_argument("--speed", type=float, default=0.0, help="sec pause per minute in replay")
    args = ap.parse_args(argv)
    if args.source == "cloudwatch":
        return cmd_cloudwatch(args)
    if args.live:
        return cmd_live(args)
    return cmd_replay(args)


if __name__ == "__main__":
    raise SystemExit(main())
