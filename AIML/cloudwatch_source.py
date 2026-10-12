"""CloudWatch adapter: pull logs + metrics, emit the standard minute features.

Same downstream pipeline (train / monitor / evaluate / UI scoring) works
unchanged — this module only translates CloudWatch into per-minute
{service: {count, err_rate, p95, avg_lat, cpu, mem, pool}} dicts.

Logs (per service -> log group, 1-minute windows, FilterLogEvents):
  message parsing, in order: JSON (status/statusCode, latency_ms/elapsed_ms/
  duration_ms/responseTime, service override) -> Common/Combined Log Format
  regex (status + trailing %D response-time microseconds) -> counted request
  with unknown status/latency (contributes to count only).
Metrics (GetMetricData, 60s period, aligned buckets):
  CPUUtilization + MemoryUtilization per service (default namespace AWS/ECS,
  dimensions ServiceName + ClusterName); optional custom pool metric
  (e.g. EMF PoolUsed). Missing metrics simply don't override log-derived
  resource averages (absent fields are skipped, never zero-filled).

Auth: standard boto3 chain (env AWS_ACCESS_KEY_ID/SECRET_ACCESS_KEY,
~/.aws, or instance/pod role). No keys in code or config.
"""

import json
import os
import re
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))

with open(os.path.join(HERE, "config.json"), encoding="utf-8") as f:
    _CONFIG = json.load(f)

CW_CFG = _CONFIG.get("cloudwatch", {})

# "METHOD /path PROTO" status bytes resp_time_us  (trailing %D)
CLF_RE = re.compile(r'"[A-Z]+ \S+ [^"]*" (\d{3}) (\d+|-) (\d+)')


def parse_log_message(message: str, default_service: str) -> dict:
    """One CloudWatch event message -> flat record fields."""
    rec = {"service": default_service, "status": None, "latency_ms": 0.0,
           "level": None, "cpu_pct": None, "mem_pct": None, "pool_used": None}
    if not message:
        return rec
    try:
        d = json.loads(message)
        if isinstance(d, dict):
            for k in ("status", "statusCode", "response_status", "code"):
                if d.get(k) is not None:
                    try:
                        rec["status"] = int(d[k])
                        break
                    except (TypeError, ValueError):
                        pass
            for k in ("latency_ms", "elapsed_ms", "duration_ms",
                      "responseTime", "response_time_ms"):
                if d.get(k) is not None:
                    try:
                        rec["latency_ms"] = float(d[k])
                        break
                    except (TypeError, ValueError):
                        pass
            for k in ("cpu_pct", "mem_pct", "pool_used"):
                if d.get(k) is not None:
                    try:
                        rec[k] = float(d[k])
                    except (TypeError, ValueError):
                        pass
            if d.get("service"):
                rec["service"] = str(d["service"])
            lvl = str(d.get("level", "") or "").upper()
            if lvl in ("DEBUG", "INFO", "WARN", "WARNING", "ERROR", "CRITICAL", "FATAL"):
                rec["level"] = "WARN" if lvl == "WARNING" else lvl
            return rec
    except ValueError:
        pass
    m = CLF_RE.search(message)
    if m:
        rec["status"] = int(m.group(1))
        rec["latency_ms"] = float(m.group(3)) / 1000.0
    return rec


def _to_record(event: dict, service: str, minute: int) -> dict:
    ts = datetime.fromtimestamp(event["timestamp"] / 1000.0, tz=timezone.utc)
    message = event.get("message", "")
    parsed = parse_log_message(message, service)
    level = parsed["level"] or ("ERROR" if (parsed["status"] or 0) >= 500 else "INFO")
    return {
        "ts": ts.strftime("%Y-%m-%dT%H:%M:%S.000"),
        "min": minute,
        "service": parsed["service"],
        "level": level,
        "endpoint": "-",
        "status": parsed["status"],
        "latency_ms": parsed["latency_ms"],
        "message": message[:2000],
        "cpu_pct": parsed["cpu_pct"],
        "mem_pct": parsed["mem_pct"],
        "pool_used": parsed["pool_used"],
    }


def load_cw_file(path: str) -> dict[int, list[dict]]:
    """CloudWatch-format file -> {relative_minute: [records]}.

    Accepts JSON-lines of FilterLogEvents-shaped events
    ({timestamp, message, logStreamName, ...}) or a whole-file
    {"events": [...]} export. Minutes are relative to the earliest event,
    so synthetic scenario windows (failure at rel-min 45) line up with
    config.json. Service defaults to the logStreamName prefix.
    """
    events = []
    with open(path, encoding="utf-8") as f:
        text = f.read().strip()
    if not text:
        return {}
    if text.startswith("{"):
        try:
            obj = json.loads(text)
            if isinstance(obj, dict) and isinstance(obj.get("events"), list):
                events = obj["events"]
        except ValueError:
            pass
    if not events:
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            if isinstance(ev, dict) and "timestamp" in ev:
                events.append(ev)
    if not events:
        return {}
    t0 = min(ev["timestamp"] for ev in events)
    buckets: dict[int, list[dict]] = {}
    for ev in events:
        rel = int((ev["timestamp"] - t0) // 60000)
        stream = str(ev.get("logStreamName", ""))
        default_svc = stream.split("/")[0] if "/" in stream else (stream or "?")
        buckets.setdefault(rel, []).append(_to_record(ev, default_svc, rel))
    return buckets


class CloudWatchSource:
    """Pulls one minute of logs + metrics. Inject boto3 clients for tests."""

    def __init__(self, region=None, log_groups=None, namespace=None,
                 cluster=None, pool_metric=None, logs_client=None,
                 metrics_client=None):
        self.region = region or CW_CFG.get("region") or "us-east-1"
        self.log_groups = log_groups or CW_CFG.get("log_groups", {})
        self.namespace = namespace or CW_CFG.get("metric_namespace", "AWS/ECS")
        self.cluster = cluster or CW_CFG.get("cluster", "")
        self.pool_metric = pool_metric or CW_CFG.get("pool_metric")
        self._logs = logs_client
        self._mw = metrics_client

    @property
    def logs(self):
        if self._logs is None:
            import boto3

            self._logs = boto3.client("logs", region_name=self.region)
        return self._logs

    @property
    def metrics(self):
        if self._mw is None:
            import boto3

            self._mw = boto3.client("cloudwatch", region_name=self.region)
        return self._mw

    # -- logs -----------------------------------------------------------
    def fetch_records(self, start, end, minute: int) -> list[dict]:
        """All log events in [start, end) across mapped groups -> records."""
        start_ms = int(start.timestamp() * 1000)
        end_ms = int(end.timestamp() * 1000)
        out = []
        for service, group in self.log_groups.items():
            token = None
            while True:
                kwargs = {"logGroupName": group, "startTime": start_ms,
                          "endTime": end_ms, "limit": 1000}
                if token:
                    kwargs["nextToken"] = token
                resp = self.logs.filter_log_events(**kwargs)
                for ev in resp.get("events", []):
                    out.append(_to_record(ev, service, minute))
                token = resp.get("nextToken")
                if not token:
                    break
        return out

    # -- metrics --------------------------------------------------------
    def _metric_queries(self, services: list[str]) -> list[dict]:
        dims = lambda svc: ([{"Name": "ServiceName", "Value": svc}] +
                            ([{"Name": "ClusterName", "Value": self.cluster}]
                             if self.cluster else []))
        queries = []
        for svc in services:
            for mid, name in (("cpu", "CPUUtilization"), ("mem", "MemoryUtilization")):
                queries.append({
                    "Id": f"{svc}_{mid}".lower().replace("-", "_"),
                    "MetricStat": {
                        "Metric": {"Namespace": self.namespace, "MetricName": name,
                                   "Dimensions": dims(svc)},
                        "Period": 60, "Stat": "Average",
                    },
                })
            if self.pool_metric:
                queries.append({
                    "Id": f"{svc}_pool".lower().replace("-", "_"),
                    "MetricStat": {
                        "Metric": {"Namespace": self.pool_metric["namespace"],
                                   "MetricName": self.pool_metric["name"],
                                   "Dimensions": dims(svc)},
                        "Period": 60, "Stat": "Average",
                    },
                })
        return queries

    def fetch_metric_minute(self, start, end) -> dict:
        """{(service, kind): value} for one minute window (may be sparse)."""
        queries = self._metric_queries(list(self.log_groups))
        if not queries:
            return {}
        resp = self.metrics.get_metric_data(
            MetricDataQueries=queries, StartTime=start, EndTime=end,
            ScanBy="TimestampAscending")
        wanted = int(start.timestamp() // 60)
        out = {}
        for qr in resp.get("MetricDataResults", []):
            label = qr.get("Id", "")
            for svc in self.log_groups:
                stem = svc.lower().replace("-", "_")
                for kind in ("cpu", "mem", "pool"):
                    if label == f"{stem}_{kind}":
                        for ts, val in zip(qr.get("Timestamps", []), qr.get("Values", [])):
                            ts_aware = ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)
                            if int(ts_aware.timestamp() // 60) == wanted:
                                out[(svc, kind)] = float(val)
        return out

    # -- combined -------------------------------------------------------
    def fetch_minute_features(self, minute_start) -> dict:
        """Full feature dict for the minute starting at minute_start (aware dt).

        Logs-only: features come purely from log events. Use
        fetch_metric_minute separately if you also want resource metrics
        (they are NOT mixed into the detection features).
        """
        from datetime import timedelta

        import early_warning as ew

        end = minute_start + timedelta(seconds=60)
        minute = int(minute_start.timestamp() // 60)
        records = self.fetch_records(minute_start, end, minute)
        return ew.minute_features(records)
