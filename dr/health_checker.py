"""BƯỚC 3a — Health checker cho 2 region.

Yêu cầu (đọc §4 "Kiến Trúc Health-Check-Based Failover" + §2 "DNS Failover"):
  1. Poll /readyz của CẢ HAI region mỗi `interval` giây (mặc định 5s).
     Dùng /readyz, KHÔNG dùng /healthz. /healthz chỉ nói "process còn sống" —
     region có process sống nhưng vector DB rỗng thì vẫn không serve được.
  2. Chỉ đổi trạng thái sau `threshold` lần fail LIÊN TIẾP (mặc định 3).
     Một lần fail không phải outage. Đây là chống flapping (§4 Anti-Patterns).
  3. Ghi 1 dòng JSONL MỖI LẦN ĐỔI TRẠNG THÁI (không ghi mỗi lần poll — log sẽ ngập).
     Dòng bắt buộc có: ts, region, to (HEALTHY|UNHEALTHY), reason,
     interval_s, threshold. Thiếu interval_s/threshold thì tools/measure_rto.py
     không tính được detect floor -> mất điểm.

Chạy:  python dr/health_checker.py --interval 5 --threshold 3 --duration 300 \
              --out reports/health-events.jsonl

Ghi chú RTO: detection floor lý thuyết = interval × threshold. Ví dụ 5s × 3 = 15s.
Đây là thông tin để giải thích RTO trong báo cáo, không phải câu trả lời riêng cần nộp.
"""
import argparse
import json
import pathlib
import time
from datetime import datetime, timezone

import httpx

URL = {"a": "http://127.0.0.1:8001", "b": "http://127.0.0.1:8002"}


def probe(region: str, timeout: float) -> tuple[bool, str]:
    """Probe readiness, not liveness. Always bound the request with ``timeout``."""
    if region not in URL:
        return False, f"unknown_region={region}"
    try:
        response = httpx.get(f"{URL[region]}/readyz", timeout=timeout)
    except httpx.TimeoutException as exc:
        return False, f"timeout: {exc}"
    except httpx.RequestError as exc:
        return False, f"request_error={type(exc).__name__}: {exc}"

    if response.status_code == 200:
        return True, "ready"

    try:
        body = response.json()
        reasons = body.get("reasons", []) if isinstance(body, dict) else []
        detail = ",".join(str(reason) for reason in reasons)
    except (ValueError, TypeError):
        detail = ""
    suffix = f" reasons={detail}" if detail else ""
    return False, f"http_status={response.status_code}{suffix}"


def run(interval: float, timeout: float, threshold: int, duration: float, out: pathlib.Path):
    """Poll both regions and append one JSONL record for each state transition."""
    if interval <= 0:
        raise ValueError("interval must be greater than zero")
    if timeout <= 0:
        raise ValueError("timeout must be greater than zero")
    if threshold < 1:
        raise ValueError("threshold must be at least one")
    if duration < 0:
        raise ValueError("duration cannot be negative")

    out = pathlib.Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    # A newly started checker assumes healthy until probes prove otherwise. This
    # avoids writing a fake initial HEALTHY transition into the event stream.
    state = {region: "HEALTHY" for region in URL}
    failures = {region: 0 for region in URL}
    successes = {region: 0 for region in URL}
    transitions = []

    def emit(region: str, to: str, reason: str):
        ts = time.time()
        iso = datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec="milliseconds")
        event = {
            "ts": ts,
            "iso": iso.replace("+00:00", "Z"),
            "event": "state_change",
            "region": region,
            "to": to,
            "reason": reason,
            "interval_s": interval,
            "threshold": threshold,
            "consecutive_fails": failures[region],
            "consecutive_successes": successes[region],
        }
        with out.open("a", encoding="utf-8") as f:
            f.write(json.dumps(event, ensure_ascii=False) + "\n")
        print(json.dumps(event, ensure_ascii=False))
        transitions.append(event)

    deadline = time.monotonic() + duration
    while time.monotonic() < deadline:
        round_started = time.monotonic()
        for region in URL:
            if time.monotonic() >= deadline:
                break
            ready, reason = probe(region, timeout)
            if ready:
                failures[region] = 0
                successes[region] += 1
                if state[region] == "UNHEALTHY":
                    state[region] = "HEALTHY"
                    emit(region, "HEALTHY", reason)
            else:
                successes[region] = 0
                failures[region] += 1
                if state[region] == "HEALTHY" and failures[region] >= threshold:
                    state[region] = "UNHEALTHY"
                    emit(region, "UNHEALTHY", reason)

        # Keep a stable cadence even when a probe takes time. In particular, a
        # SIGSTOP/netblock should consume only its configured timeout.
        elapsed = time.monotonic() - round_started
        remaining = min(interval - elapsed, deadline - time.monotonic())
        if remaining > 0:
            time.sleep(remaining)

    return transitions


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--interval", type=float, default=5.0)
    p.add_argument("--timeout", type=float, default=2.0)
    p.add_argument("--threshold", type=int, default=3)
    p.add_argument("--duration", type=float, default=300)
    p.add_argument("--out", default="reports/health-events.jsonl")
    a = p.parse_args()
    run(a.interval, a.timeout, a.threshold, a.duration, pathlib.Path(a.out))
