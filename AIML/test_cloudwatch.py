"""Offline tests for the CloudWatch adapter (botocore Stubber — no AWS needed).

Covers: JSON + CLF message parsing, FilterLogEvents pagination, metric
overlay onto minute features, and the end-to-end fetch_minute_features
shape. Run:  python3 test_cloudwatch.py
"""

import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from botocore.stub import Stubber

from cloudwatch_source import CloudWatchSource, parse_log_message

FAILURES = []


def check(name: str, cond: bool, extra: str = "") -> None:
    print(("PASS " if cond else "FAIL ") + name + (f" [{extra}]" if extra and not cond else ""))
    if not cond:
        FAILURES.append(name)


def _logs_client(responses: list[dict]):
    import boto3

    client = boto3.client("logs", region_name="us-east-1",
                          aws_access_key_id="x", aws_secret_access_key="x")
    stub = Stubber(client)
    for resp in responses:
        stub.add_response("filter_log_events", resp)
    stub.activate()
    return client


def test_parse() -> None:
    r = parse_log_message('{"status": 500, "latency_ms": 812, "service": "orders"}', "x")
    check("parse json", r["status"] == 500 and r["latency_ms"] == 812 and r["service"] == "orders")
    r = parse_log_message('{"statusCode": 200, "responseTime": 34}', "auth")
    check("parse json alt keys", r["status"] == 200 and r["latency_ms"] == 34 and r["service"] == "auth")
    r = parse_log_message('10.0.0.1 - - [11/Oct/2026:09:01:02 +0000] "POST /orders HTTP/1.1" 503 512 188021', "orders")
    check("parse clf", r["status"] == 503 and abs(r["latency_ms"] - 188.021) < 0.01)
    r = parse_log_message("some unstructured line", "gateway")
    check("parse garbage", r["status"] is None and r["service"] == "gateway")


def _ev(ts_ms: int, msg: str) -> dict:
    return {"timestamp": ts_ms, "message": msg, "ingestionTime": ts_ms}


def test_pagination_and_features() -> None:
    base = int(datetime(2026, 10, 11, 9, 0, tzinfo=timezone.utc).timestamp() * 1000)
    page1 = {"events": [
        _ev(base + 1000, '{"status": 200, "latency_ms": 40}'),
        _ev(base + 2000, '{"status": 500, "latency_ms": 900}'),
    ], "nextToken": "tok"}
    page2 = {"events": [_ev(base + 3000, '{"status": 200, "latency_ms": 60}')]}
    logs = _logs_client([page1, page2])
    src = CloudWatchSource(log_groups={"orders": "/ecs/prod/orders"},
                           logs_client=logs, metrics_client=object())
    start = datetime(2026, 10, 11, 9, 0, tzinfo=timezone.utc)
    from datetime import timedelta

    recs = src.fetch_records(start, start + timedelta(seconds=60), minute=123)
    check("pagination collects", len(recs) == 3, f"got {len(recs)}")
    from early_warning import minute_features

    feats = minute_features(recs)
    o = feats["orders"]
    check("err_rate", abs(o["err_rate"] - 1 / 3) < 1e-9, str(o))
    check("logs-only keys", set(o) == {"err_rate", "p95", "avg_lat", "error_line_rate",
                                       "warn_rate", "exception_rate", "timeout_rate"}, str(o))


def test_metric_overlay() -> None:
    import boto3

    mw = boto3.client("cloudwatch", region_name="us-east-1",
                      aws_access_key_id="x", aws_secret_access_key="x")
    stub = Stubber(mw)
    ts = datetime(2026, 10, 11, 9, 0, 30, tzinfo=timezone.utc)
    stub.add_response("get_metric_data", {"MetricDataResults": [
        {"Id": "orders_cpu", "Label": "x", "Timestamps": [ts], "Values": [77.5],
         "StatusCode": "Complete"},
        {"Id": "orders_mem", "Label": "x", "Timestamps": [ts], "Values": [61.0],
         "StatusCode": "Complete"},
    ]})
    stub.activate()
    src = CloudWatchSource(log_groups={"orders": "/g"}, logs_client=object(),
                           metrics_client=mw)
    start = datetime(2026, 10, 11, 9, 0, tzinfo=timezone.utc)
    from datetime import timedelta

    got = src.fetch_metric_minute(start, start + timedelta(seconds=60))
    check("metric overlay", got.get(("orders", "cpu")) == 77.5
          and got.get(("orders", "mem")) == 61.0, str(got))


def test_end_to_end_shape() -> None:
    base = int(datetime(2026, 10, 11, 9, 0, tzinfo=timezone.utc).timestamp() * 1000)
    logs = _logs_client([{"events": [
        _ev(base + 1000, '{"status": 200, "latency_ms": 100}'),
        _ev(base + 2000, '{"status": 200, "latency_ms": 120}'),
    ]}])
    import boto3

    mw = boto3.client("cloudwatch", region_name="us-east-1",
                      aws_access_key_id="x", aws_secret_access_key="x")
    stub = Stubber(mw)
    stub.add_response("get_metric_data", {"MetricDataResults": []})
    stub.activate()
    src = CloudWatchSource(log_groups={"payments": "/ecs/prod/payments"},
                           logs_client=logs, metrics_client=mw)
    feats = src.fetch_minute_features(datetime(2026, 10, 11, 9, 0, tzinfo=timezone.utc))
    p = feats.get("payments", {})
    check("e2e shape", set(p) == {"err_rate", "p95", "avg_lat", "error_line_rate",
                                  "warn_rate", "exception_rate", "timeout_rate"}
          and p["err_rate"] == 0.0, str(p))


def main() -> int:
    test_parse()
    test_pagination_and_features()
    test_metric_overlay()
    test_end_to_end_shape()
    print(f"{len(FAILURES)} failures" if FAILURES else "all offline tests passed")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    raise SystemExit(main())
