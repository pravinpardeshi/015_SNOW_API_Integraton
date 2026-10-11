"""Generate a 1-hour microservices log: normal -> peak-hour overload failure -> restart recovery.

Scenario (60 one-minute buckets, seeded RNG for reproducibility):
  min 00-34  normal baseline traffic (train window for the detector: min 05-29)
  min 35-44  peak-hour ramp: request rate x3, latencies climb, payments DB-pool
              saturates, error rate creeps 0.3% -> ~5%
  min 45-49  FAILURE: payments pool exhausted -> 70% 5xx + timeouts, orders
              cascades (~25% errors), gateway 503s, payments OOM-crash at min 49
  min 50     restart: payments restarting, backlog drain, still-degraded latency
  min 51-59  recovery: metrics settle back to baseline by ~min 54

Output: logs/microservices_1h.log (JSON lines, ~8 req/s baseline).
Each line: ts, min, service, level, endpoint, status, latency_ms,
cpu_pct, mem_pct, pool_used (+ event/msg for restart markers).

Usage:
  python3 generate_logs.py [--out logs/microservices_1h.log] [--seed 42]
"""

import argparse
import json
import os
import random
from datetime import datetime, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))

SERVICES = {
    # name: (base_rps, base_latency_ms, endpoint)
    "gateway":   (3.0, 25,  "POST /api/route"),
    "auth":      (1.2, 40,  "POST /auth/token"),
    "orders":    (1.5, 90,  "POST /orders"),
    "payments":  (1.0, 140, "POST /payments/charge"),
    "inventory": (0.8, 70,  "GET /inventory/check"),
    "notify":    (0.5, 120, "POST /notify/send"),
}

BASE_CPU = {"gateway": 38, "auth": 32, "orders": 42, "payments": 45,
            "inventory": 30, "notify": 28}
BASE_POOL = {"gateway": 30, "auth": 25, "orders": 35, "payments": 38,
             "inventory": 22, "notify": 20}

FAILURE_START = 45
RESTART_MIN = 50


def regime(m: int) -> str:
    if m < 35:
        return "normal"
    if m < FAILURE_START:
        return "peak"
    if m < RESTART_MIN:
        return "failure"
    if m == RESTART_MIN:
        return "restart"
    if m < 54:
        return "recovery"
    return "normal"


def peak_k(m: int) -> float:
    """0..1 ramp across the peak window (min 35-44)."""
    return min(1.0, max(0.0, (m - 34) / 10.0))


def draw_status(rng: random.Random, service: str, reg: str, m: int) -> int:
    if reg == "normal":
        p_err = 0.003
    elif reg == "peak":
        p_err = 0.003 + peak_k(m) * 0.05
    elif reg == "failure":
        p_err = {"payments": 0.70, "orders": 0.25, "gateway": 0.15}.get(service, 0.03)
    elif reg == "restart":
        p_err = 0.06
    else:  # recovery
        p_err = 0.02 if m < 52 else 0.005
    if rng.random() < p_err:
        return rng.choice([500, 500, 502, 503, 504] if service == "payments" and reg == "failure"
                          else [500, 502, 503])
    return 200


def draw_latency(rng: random.Random, service: str, base: float, reg: str, m: int, status: int) -> int:
    lat = base * rng.lognormvariate(0.0, 0.5)
    if reg == "peak":
        lat *= 1.0 + peak_k(m) * 3.0
    elif reg == "failure":
        if service == "payments":
            lat = rng.uniform(5000, 15000) if status >= 500 else base * 6
        elif service == "orders":
            lat *= 5.0
        else:
            lat *= 2.5
    elif reg == "restart":
        lat *= 3.0
    elif reg == "recovery":
        lat *= 1.8 if m < 52 else 1.2
    return max(1, int(lat))


def draw_resources(rng: random.Random, service: str, reg: str, m: int) -> tuple:
    cpu = BASE_CPU[service] + rng.uniform(-6, 6)
    pool = BASE_POOL[service] + rng.uniform(-5, 5)
    mem = 52 + rng.uniform(-4, 4)
    if reg == "peak":
        k = peak_k(m)
        cpu += k * 48
        mem += k * 18
        pool += k * (60 if service == "payments" else 25)
    elif reg == "failure":
        cpu = rng.uniform(88, 99)
        mem = rng.uniform(82, 96)
        pool = 100.0 if service == "payments" else rng.uniform(70, 90)
    elif reg == "restart":
        cpu, mem, pool = rng.uniform(55, 70), rng.uniform(60, 68), 45.0
    elif reg == "recovery":
        cpu += 15 if m < 52 else 3
        pool += 12 if m < 52 else 2
    return round(min(99.5, cpu), 1), round(min(98.0, mem), 1), round(min(100.0, pool), 1)


def rate_mult(service: str, reg: str, m: int) -> float:
    if reg == "normal":
        return 1.0
    if reg == "peak":
        return 1.0 + peak_k(m) * 2.0
    if reg == "failure":
        return 1.4 if service != "payments" else 0.9  # retries pile on; payments shedding
    if reg == "restart":
        return 0.35
    if reg == "recovery":
        return 1.7 if m < 52 else 1.1  # backlog drain, then normal
    return 1.0


def generate(seed: int = 42):
    rng = random.Random(seed)
    start = datetime(2026, 10, 11, 9, 0, 0)
    lines, counts = [], {}
    for m in range(60):
        reg = regime(m)
        for svc, (rps, base_lat, endpoint) in SERVICES.items():
            n = int(rps * 60 * rate_mult(svc, reg, m) * rng.uniform(0.9, 1.1))
            for _ in range(n):
                sec = rng.uniform(0, 60)
                ts = start + timedelta(minutes=m, seconds=sec)
                status = draw_status(rng, svc, reg, m)
                lat = draw_latency(rng, svc, base_lat, reg, m, status)
                cpu, mem, pool = draw_resources(rng, svc, reg, m)
                level = ("ERROR" if status >= 500
                         else "CRITICAL" if svc == "payments" and reg == "failure" and rng.random() < 0.1
                         else "WARN" if lat > base_lat * 3 or status == 503
                         else "INFO")
                lines.append({
                    "ts": ts.strftime("%Y-%m-%dT%H:%M:%S.") + f"{int(ts.microsecond / 1000):03d}",
                    "min": m, "service": svc, "level": level, "endpoint": endpoint,
                    "status": status, "latency_ms": lat,
                    "cpu_pct": cpu, "mem_pct": mem, "pool_used": pool,
                })
                counts[(reg, svc)] = counts.get((reg, svc), 0) + 1
        if m == 49:
            lines.append({"ts": (start + timedelta(minutes=49, seconds=50)).strftime("%Y-%m-%dT%H:%M:%S.000"),
                          "min": 49, "service": "payments", "level": "CRITICAL",
                          "event": "oom_crash", "status": 0, "latency_ms": 0,
                          "cpu_pct": 99.5, "mem_pct": 97.8, "pool_used": 100.0,
                          "msg": "payments OOMKilled (exit 137): heap exhausted holding pool waiters"})
        if m == RESTART_MIN:
            lines.append({"ts": (start + timedelta(minutes=50, seconds=5)).strftime("%Y-%m-%dT%H:%M:%S.000"),
                          "min": 50, "service": "payments", "level": "INFO",
                          "event": "restart", "status": 0, "latency_ms": 0,
                          "cpu_pct": 40.0, "mem_pct": 55.0, "pool_used": 10.0,
                          "msg": "payments restarted (pool reset, backlog draining)"})
    lines.sort(key=lambda r: r["ts"])
    return lines


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Generate 1-hour microservices log.")
    ap.add_argument("--out", default=os.path.join(HERE, "logs", "microservices_1h.log"))
    ap.add_argument("--seed", type=int, default=42)
    args = ap.parse_args(argv)
    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    lines = generate(seed=args.seed)
    with open(args.out, "w", encoding="utf-8") as f:
        for r in lines:
            f.write(json.dumps(r) + "\n")
    print(f"Wrote {len(lines)} lines -> {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
