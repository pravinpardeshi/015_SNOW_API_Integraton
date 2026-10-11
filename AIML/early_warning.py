"""Early-warning core: per-minute features, learned normal baseline, failure predictor.

Stdlib only (no sklearn/numpy) so the monitor runs under any interpreter.

Features per (minute, service): request count, error rate, p95 latency,
mean latency, mean cpu %, mean memory %, mean pool usage %.
Baseline: per (service, feature) median + MAD fitted on known-normal minutes.
Score: modified robust z = 0.6745 * (x - median) / MAD, minute score =
max |z| over features (two-sided: catches surges AND drops, e.g. restart).
Levels: OK < WATCH(2.5) < WARNING(4.0) < CRITICAL(6.0)  [config.json]
Prediction: a service scoring >= WARNING for PREDICT_STREAK consecutive
minutes with a rising score => "failure likely within ~5 min" (once per
episode; episode resets below WATCH). Pure trend rule on top of the learned
baseline — no labels needed at runtime.
"""

import json
import os
import statistics
from typing import Optional

HERE = os.path.dirname(os.path.abspath(__file__))

with open(os.path.join(HERE, "config.json"), encoding="utf-8") as f:
    CONFIG = json.load(f)

FEATURES = ["count", "err_rate", "p95", "avg_lat", "cpu", "mem", "pool"]

# MAD floors: (<relative-to-median>, <absolute>) per feature. A zero-MAD
# feature (e.g. usually-zero error rate) would otherwise turn a single blip
# into an astronomical z-score. Effective MAD = max(mad, rel*|median|, abs).
MAD_FLOORS = {
    "count": (0.05, 1.0),
    "err_rate": (0.0, 0.01),
    "p95": (0.15, 5.0),
    "avg_lat": (0.10, 2.0),
    "cpu": (0.0, 2.0),
    "mem": (0.0, 2.0),
    "pool": (0.0, 3.0),
}


def parse_line(line: str):
    try:
        r = json.loads(line)
    except ValueError:
        return None
    if "service" not in r or "min" not in r:
        return None
    return r


def minute_features(records: list[dict]) -> dict:
    """Aggregate one minute of log records -> {service: {feature: value}}."""
    per_svc: dict[str, list[dict]] = {}
    for r in records:
        if r.get("event") in ("oom_crash", "restart"):
            continue  # markers, not traffic
        per_svc.setdefault(r["service"], []).append(r)
    out = {}
    for svc, rows in per_svc.items():
        lats = sorted(x.get("latency_ms", 0) or 0 for x in rows)
        n = len(rows)
        errs = sum(1 for x in rows if (x.get("status") or 0) >= 500)
        p95 = lats[min(n - 1, int(n * 0.95))]

        def _avg(key: str) -> float:
            vals = [x[key] for x in rows if x.get(key) is not None]
            return sum(vals) / len(vals) if vals else 0.0

        out[svc] = {
            "count": float(n),
            "err_rate": errs / n if n else 0.0,
            "p95": float(p95),
            "avg_lat": sum(lats) / n if n else 0.0,
            "cpu": _avg("cpu_pct"),
            "mem": _avg("mem_pct"),
            "pool": _avg("pool_used"),
        }
    return out


class Baseline:
    """Learned normal pattern: median + MAD per (service, feature)."""

    def __init__(self):
        self.stats: dict[str, dict[str, list[float]]] = {}  # svc -> feat -> [median, mad]

    def fit(self, per_minute: list[dict]) -> "Baseline":
        by_svc_feat: dict[str, dict[str, list[float]]] = {}
        for feats in per_minute:
            for svc, fmap in feats.items():
                for feat in FEATURES:
                    by_svc_feat.setdefault(svc, {}).setdefault(feat, []).append(fmap[feat])
        for svc, fmap in by_svc_feat.items():
            self.stats[svc] = {}
            for feat, vals in fmap.items():
                med = statistics.median(vals)
                mad = statistics.median([abs(v - med) for v in vals]) or 1e-6
                self.stats[svc][feat] = [med, mad]
        return self

    def zscores(self, feats: dict) -> dict:
        """{service: ({feature: z}, score)} for one minute of features."""
        out = {}
        for svc, fmap in feats.items():
            ref = self.stats.get(svc)
            if not ref:
                continue
            zs = {}
            for feat in FEATURES:
                med, mad = ref[feat]
                rel, floor_abs = MAD_FLOORS[feat]
                if feat == "err_rate":
                    # Small-sample guard: a couple of errors in a low-traffic
                    # service must not look catastrophic. Floor scales with
                    # 1.5 errors per typical minute for that service.
                    cnt_med = ref["count"][0]
                    floor_abs = max(floor_abs, 1.5 / max(cnt_med, 1.0))
                mad_eff = max(mad, rel * abs(med), floor_abs)
                zs[feat] = 0.6745 * (fmap[feat] - med) / mad_eff
            out[svc] = (zs, max(abs(v) for v in zs.values()))
        return out

    def save(self, path: str) -> None:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(self.stats, f, indent=2)

    @classmethod
    def load(cls, path: str) -> "Baseline":
        b = cls()
        with open(path, encoding="utf-8") as f:
            b.stats = json.load(f)
        return b


def level_for(score: float) -> str:
    th = CONFIG["thresholds"]
    if score >= th["critical"]:
        return "CRITICAL"
    if score >= th["warning"]:
        return "WARNING"
    if score >= th["watch"]:
        return "WATCH"
    return "OK"


class Predictor:
    """Streak-based failure forecast on top of anomaly scores.

    Tracks consecutive WARNING+ minutes per service; fires once when the
    streak reaches PREDICT_STREAK with a rising score. Returns a prediction
    dict or None.
    """

    def __init__(self):
        self.streak: dict[str, int] = {}
        self.prev: dict[str, float] = {}
        self.fired: dict[str, bool] = {}

    def update(self, minute: int, scores: dict) -> Optional[dict]:
        th = CONFIG["thresholds"]
        streak_n = CONFIG["predict_streak"]
        for svc, (_zs, score) in scores.items():
            if score >= th["warning"]:
                rising = score >= self.prev.get(svc, 0.0)
                self.streak[svc] = self.streak.get(svc, 0) + 1
                if rising and self.streak[svc] >= streak_n and not self.fired.get(svc):
                    self.fired[svc] = True
                    return {
                        "minute": minute, "service": svc, "score": round(score, 2),
                        "streak": self.streak[svc],
                        "message": (
                            f"PREDICTED FAILURE: {svc} anomalous for "
                            f"{self.streak[svc]} min and rising "
                            f"(score {score:.1f}) — failure likely within ~5 min"
                        ),
                    }
            elif score < th["watch"]:
                self.streak[svc] = 0
                self.fired[svc] = False
            self.prev[svc] = score
        return None
