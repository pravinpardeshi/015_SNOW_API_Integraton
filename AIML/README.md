# Early-Warning System — microservices anomaly + failure prediction

Monitors service logs in real time, learns a **normal pattern** from stable
traffic, scores every minute for anomalies, and **predicts a failure minutes
before it happens**. Stdlib only — no ML packages needed.

## Scenario (synthetic, `logs/microservices_1h.log`, 34,841 lines, seed 42)

Six services (`gateway auth orders payments inventory notify`), one JSON
event per line: `ts min service level endpoint status latency_ms cpu_pct
mem_pct pool_used`.

| Minutes | Phase | What happens |
|---------|-------|--------------|
| 00–34 | normal | Baseline traffic, ~0.3% errors (train window: **05–29**) |
| 35–44 | peak hour | Load ramps ×3, latencies climb, **payments** DB pool saturates, errors → ~5% |
| 45–49 | **failure** | Pool exhausted → payments ~70% 5xx + timeouts, orders cascade, gateway 503s, OOM crash at :49 |
| 50 | restart | Payments restarts, pool reset, backlog drains (still degraded) |
| 51–59 | recovery | Metrics settle to baseline by ~min 54 |

## How detection works (`early_warning.py`)

- **Features/minute/service — logs-only:** error rate, p95 + mean latency,
  ERROR-line rate, WARN rate, exception-keyword rate
  (`Exception|Traceback|OOM|pool exhausted|…`), timeout rate
  (`Timeout|deadline exceeded` + 504s). No request counts (unreliable under
  sampled logs), no metrics API — every feature derives from log lines.
- **Learned baseline:** per-(service, feature) median + MAD from the train window → robust z-score per minute, with MAD floors so usually-zero rates can't explode into false alarms.
- **Levels:** OK < 2.5 WATCH < 4.0 WARNING < 6.0 CRITICAL (`config.json`).
- **Prediction:** a service at WARNING+ for 3 straight rising minutes ⇒ *"failure likely within ~5 min"* (once per episode). Trend rule over learned scores — no failure labels used at runtime.

## Run it (stdlib baseline — no packages needed)

```bash
python3 generate_logs.py            # rebuild the 1-hour log (seeded)
python3 train.py                    # learn baseline -> models/baseline.json
python3 monitor.py --replay         # score all 60 min -> alerts.log
python3 monitor.py --replay --speed 1  # same, 1 s per minute (real-time sim)
python3 monitor.py --live           # tail the log file as lines arrive
python3 evaluate.py                 # check prediction lead time + false alarms
```

## Machine-learning backend (IsolationForest + RandomForest)

Needs sklearn/numpy/joblib — present in system `python3`, **not** in the
project `venv`, so run the ML scripts with `python3` (LSTM was skipped
deliberately: 360 minute-samples is far too little for deep learning, and no
torch/TF is installed — tree ensembles dominate tabular data this small).

- **Anomaly detection — IsolationForest**, one model per service, trained
  only on normal minutes of 5 seeded logs (`logs/train_seed{N}.log`, test log
  is unseen seed 42). Score = −decision_function; alert levels calibrated per
  service from training scores (WARNING = train max, CRITICAL = 1.5× max).
- **Failure prediction — RandomForestClassifier** (300 trees,
  class_weight=balanced, OOB 1.0 on separable synthetic data), one global
  model on 27 features: 7 stats + 7 deltas + 7 rolling-means + 6 service
  one-hot. Label = 1 when the known failure starts within 10 min
  (pre-failure minutes only; failure/restart minutes excluded from training).
  Fires at probability ≥ 0.6, once per episode. No labels needed at runtime.

```bash
python3 ml_train.py                 # train IF + RF -> models/if_*.joblib, rf_failure.joblib
python3 ml_train.py --cw-logs logs/cw_seed1.jsonl logs/cw_seed2.jsonl  # train from CW files
python3 monitor.py --replay --ml    # score with ML backend -> alerts.log
python3 monitor.py --replay --ml --log logs/cw_seed42.jsonl --format cloudwatch
python3 monitor.py --live --ml      # tail with ML backend
python3 evaluate.py                 # same checks, works for both backends
```

## CloudWatch-format log files (synthetic)

`generate_logs.py --format cloudwatch` emits FilterLogEvents-shaped JSON
lines (`timestamp` ms, `message`, `logStreamName`, `ingestionTime`): JSON app
logs with trace ids (CLF + `%D` timing for ~25% of gateway lines), same
1-hour normal → peak → failure → restart → recovery scenario:

```bash
python3 generate_logs.py --format cloudwatch --seed 1 --out logs/cw_seed1.jsonl
```

`load_cw_file` parses them through the exact runtime path
(`parse_log_message`, incl. resource fields), with minutes relative to each
file's first event — so `ml_train.py --cw-logs` and
`monitor.py --replay --log … --format cloudwatch` train and predict on
CloudWatch-shaped data. Verified: CW-trained models on unseen `cw_seed42`
alert at min 35, predict at min 35 (10-min lead), zero false alarms; the
Early Warn UI scores from the same retrained models.

## CloudWatch source (AWS)

The whole pipeline is source-agnostic: `cloudwatch_source.py` translates
CloudWatch into the same per-minute features, so training, monitoring,
prediction and the UI scorer work unchanged.

- **Logs:** `FilterLogEvents` per mapped log group, 1-minute windows. Parses
  JSON events (`status`/`statusCode`, `latency_ms`/`elapsed_ms`/`duration_ms`/
  `responseTime`) then Common/Combined Log Format (status + trailing `%D`
  microsecond timing); unparseable lines count toward volume only.
- **Metrics:** `GetMetricData` CPU/Memory per service (`AWS/ECS` +
  `ServiceName`/`ClusterName`, 60 s average) overlaid on the features;
  optional custom pool metric. Missing metrics never zero-fill.
- **Auth:** standard boto3 chain (env vars, `~/.aws`, instance/pod role).
  Configure `config.json → cloudwatch` (region, service→log-group map,
  namespace, cluster). Data lands with ~1–3 min delay, so monitoring is
  near-real-time (65 s poll).

```bash
python3 train.py --source cloudwatch --minutes 30  # learn from recent good minutes
python3 monitor.py --source cloudwatch [--ml]      # poll forever (Ctrl+C stops)
python3 test_cloudwatch.py                         # 11 offline tests (Stubber, no AWS)
```

## Verified results (seed-42 log, logs-only features)

| Backend | First alert | Prediction (lead) | False alarms 05–29 | `evaluate.py` |
|---------|-------------|-------------------|--------------------|---------------|
| stdlib (robust-z + streak) | min 36 | min 38, 7 min | 0 | exit 0 |
| ML (IF + RF) | min 38 | min 35, **10 min** | 0 | exit 0 |

## Verified results (this log + thresholds)

- First WARNING/CRITICAL **min 36** — 9 min before the failure window.
- Failure **predicted min 38** (gateway, then all ramping services) — 7-min lead.
- **Zero** false WARNING/CRITICAL in the normal window (min 05–29).
- Failure minutes score 200–300 on payments latency; scores settle after restart (one residual WARNING at min 53 from backlog drain).
- `evaluate.py` exits 0 when all checks pass (numbers above are the stdlib
  backend; see the ML table for the IsolationForest/RandomForest path).

## Tuning

`config.json`: `thresholds`, `predict_streak`, `train_window`,
`failure_window`. Retrain after changing the log; no code changes needed.
Swap in a real log file by matching the JSON schema (`service`, `min` or
derivable timestamps, `status`, `latency_ms`, resource fields optional —
missing fields default to 0 and simply don't contribute).
