"""ML backend: IsolationForest anomaly detection + RandomForest failure prediction.

Needs sklearn + numpy + joblib (system python3; NOT the project venv).

Features: the same 7 per-minute stats as the stdlib baseline, plus temporal
context — delta vs previous minute and rolling mean over the last 3 minutes
(21 numbers), plus a 6-way service one-hot for the global RF (27 total).

IsolationForest: one model per service, trained ONLY on normal minutes.
Anomaly score = -decision_function (higher = weirder). Alert levels are
calibrated per service from training scores (WATCH=p95, WARNING=p99,
CRITICAL=max) and stored in ml_meta.json.

RandomForest: one global classifier. Label = 1 when the known failure starts
within HORIZON minutes (pre-failure minutes only); failure/restart minutes
are excluded from training; everything else is 0. class_weight balanced.
At runtime predict_proba on the live window; >= proba_threshold fires once
per episode (resets below half threshold). No labels needed at runtime.
"""

import json
import os

import joblib
import numpy as np
from sklearn.ensemble import IsolationForest, RandomForestClassifier
from sklearn.preprocessing import StandardScaler
from typing import Optional

HERE = os.path.dirname(os.path.abspath(__file__))

with open(os.path.join(HERE, "config.json"), encoding="utf-8") as f:
    CONFIG = json.load(f)

BASE_FEATURES = ["count", "err_rate", "p95", "avg_lat", "cpu", "mem", "pool"]
SERVICES = CONFIG["services"]
ONE_HOT = {svc: [1.0 if s == svc else 0.0 for s in SERVICES] for svc in SERVICES}
WIN = 3  # rolling window for temporal context


def window_features(history: list[dict]) -> list[float]:
    """history: oldest..newest per-minute feature dicts (up to WIN)."""
    cur = history[-1]
    prev = history[-2] if len(history) >= 2 else cur
    base = [cur[f] for f in BASE_FEATURES]
    delta = [cur[f] - prev[f] for f in BASE_FEATURES]
    k = min(WIN, len(history))
    roll = [sum(h[f] for h in history[-k:]) / k for f in BASE_FEATURES]
    return base + delta + roll


def models_dir() -> str:
    return os.path.join(HERE, "models")


def train_isolation_forests(train_feats: dict[str, list[list[float]]]) -> dict:
    """train_feats: service -> list of 7-dim normal-minute vectors."""
    cont = CONFIG["ml"]["contamination"]
    cal = {}
    for svc, rows in train_feats.items():
        X = np.array(rows, dtype=float)
        scaler = StandardScaler().fit(X)
        Xs = scaler.transform(X)
        clf = IsolationForest(n_estimators=200, contamination=cont,
                              random_state=42).fit(Xs)
        scores = -clf.decision_function(Xs)
        train_max = float(scores.max())
        cal[svc] = {
            "watch": float(np.quantile(scores, 0.95)),
            "warning": train_max,
            "critical": train_max * 1.5,
        }
        joblib.dump({"scaler": scaler, "model": clf},
                    os.path.join(models_dir(), f"if_{svc}.joblib"))
    return cal


def train_failure_forest(X: list[list[float]], y: list[int]) -> dict:
    X = np.array(X, dtype=float)
    y = np.array(y, dtype=int)
    clf = RandomForestClassifier(n_estimators=300, class_weight="balanced",
                                 oob_score=True, random_state=42, n_jobs=-1)
    clf.fit(X, y)
    joblib.dump(clf, os.path.join(models_dir(), "rf_failure.joblib"))
    pred = clf.predict(X)
    tp = int(((pred == 1) & (y == 1)).sum())
    fp = int(((pred == 1) & (y == 0)).sum())
    fn = int(((pred == 0) & (y == 1)).sum())
    return {
        "oob_score": round(float(clf.oob_score_), 4),
        "train_precision": round(tp / max(1, tp + fp), 4),
        "train_recall": round(tp / max(1, tp + fn), 4),
        "positives": int(y.sum()), "samples": len(y),
    }


def save_meta(calibration: dict, rf_report: dict) -> None:
    meta = {
        "calibration": calibration,
        "rf": rf_report,
        "horizon": CONFIG["ml"]["horizon"],
        "proba_threshold": CONFIG["ml"]["proba_threshold"],
        "contamination": CONFIG["ml"]["contamination"],
        "base_features": BASE_FEATURES,
        "window": WIN,
    }
    with open(os.path.join(models_dir(), "ml_meta.json"), "w", encoding="utf-8") as f:
        json.dump(meta, f, indent=2)


class MLBackend:
    """Runtime scorer/predictor backed by the trained sklearn models."""

    def __init__(self):
        self.models: dict[str, dict] = {}
        for svc in SERVICES:
            self.models[svc] = joblib.load(os.path.join(models_dir(), f"if_{svc}.joblib"))
        self.rf: RandomForestClassifier = joblib.load(
            os.path.join(models_dir(), "rf_failure.joblib"))
        with open(os.path.join(models_dir(), "ml_meta.json"), encoding="utf-8") as f:
            self.meta = json.load(f)
        self.cal = self.meta["calibration"]
        self.thr = self.meta["proba_threshold"]
        self.history: dict[str, list[dict]] = {s: [] for s in SERVICES}
        self.fired: dict[str, bool] = {}

    def score_minute(self, feats: dict) -> dict:
        """-> {service: ({feature: approx_z}, score)} shaped like Baseline."""
        out = {}
        for svc, fmap in feats.items():
            pack = self.models.get(svc)
            if not pack:
                continue
            x = np.array([[fmap[f] for f in BASE_FEATURES]], dtype=float)
            xs = pack["scaler"].transform(x)
            score = float(-pack["model"].decision_function(xs)[0])
            out[svc] = ({"if_score": round(score, 3)}, score)
        return out

    def level_for(self, svc: str, score: float) -> str:
        cal = self.cal.get(svc, {})
        if score >= cal.get("critical", float("inf")):
            return "CRITICAL"
        if score >= cal.get("warning", float("inf")):
            return "WARNING"
        if score >= cal.get("watch", float("inf")):
            return "WATCH"
        return "OK"

    def predict(self, minute: int, feats: dict) -> Optional[dict]:
        for svc, fmap in feats.items():
            self.history.setdefault(svc, []).append(fmap)
        for svc, fmap in feats.items():
            hist = self.history.get(svc, [])
            if len(hist) < 2:
                continue
            x = np.array([window_features(hist) + ONE_HOT[svc]], dtype=float)
            proba = float(self.rf.predict_proba(x)[0][1])
            if proba >= self.thr and not self.fired.get(svc):
                self.fired[svc] = True
                return {
                    "minute": minute, "service": svc, "score": round(proba, 3),
                    "message": (
                        f"PREDICTED FAILURE (ML): {svc} failure probability "
                        f"{proba:.2f} — failure likely within "
                        f"~{self.meta['horizon']} min"
                    ),
                }
            if proba < self.thr / 2:
                self.fired[svc] = False
        return None
