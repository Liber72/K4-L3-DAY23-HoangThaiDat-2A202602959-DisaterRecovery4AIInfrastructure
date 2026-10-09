"""BƯỚC 3c — Tự động hoá runbook §4 "Runbook: Region Chính Down".

7 bước trên slide, mỗi bước 1 dòng log có ts. Log này CHÍNH LÀ timeline của postmortem.
  1 xac_nhan_outage          — probe cả 2 region, đừng tin 1 lần fail (dùng nhiều lần
                              hoặc gọi health_checker.probe nếu đã viết xong 3a)
  2 thong_bao_incident       — ts của dòng này là mốc "operator biết tin", LUÔN LUÔN
                              SAU t_outage trong chaos-events (không thể trùng — operator
                              không thể biết ngay giây outage xảy ra). Ghi cả 2 ts vào
                              log để postmortem tính được "độ trễ thông báo".
  3 scale_gpu_pool           — gọi HÀM `failover.failover(...)` MỘT LẦN DUY NHẤT. Hàm
                              đó tự làm đủ 5 bước con (verify/restore/scale/wait/cutover)
                              và tự ghi log riêng vào reports/failover-events.jsonl.
  4 verify_state_replica     — KHÔNG gọi lại failover — chỉ ĐỌC kết quả (vector count +
                              weights ở region phụ) từ dict mà bước 3 trả về, để log vào
                              runbook-run.jsonl cho postmortem đọc 1 chỗ duy nhất.
  5 dns_cutover              — cũng chỉ đọc lại: kết quả cutover có ok hay không.
  6 verify_golden_signals    — 10 request thật vào region phụ: p95 latency + error rate
  7 post_incident            — elapsed_s + lệnh đo RTO

BÁN TỰ ĐỘNG, KHÔNG FULL-AUTO (§4: "failover đầu tiên nên là bán tự động — alert +
1-click confirm — tránh flapping gây failover 2 chiều liên tục"). Mặc định phải hỏi
người vận hành confirm; --auto chỉ dùng trong CI/khi chấm điểm.

Chạy:  python dr/runbook.py --primary a --target b --backend fs
"""
import argparse
import json
import math
import pathlib
import sys
import time
from datetime import datetime, timezone

import httpx

sys.path.insert(0, ".")
from dr import failover as fo  # noqa: E402

LOG = pathlib.Path("reports/runbook-run.jsonl")
URL = {"a": "http://127.0.0.1:8001", "b": "http://127.0.0.1:8002"}


def step(n, name, **kw):
    """Append one timestamped runbook event and return it."""
    ts = time.time()
    iso = datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec="milliseconds")
    record = {
        "ts": ts,
        "iso": iso.replace("+00:00", "Z"),
        "step": n,
        "name": name,
        **kw,
    }
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")
    print(json.dumps(record, ensure_ascii=False))
    return record


def confirm(auto: bool, msg: str) -> bool:
    """Only skip the operator prompt when explicitly running with ``--auto``."""
    if auto:
        return True
    try:
        answer = input(f"{msg} [y/N]: ").strip().lower()
    except EOFError:
        return False
    return answer in {"y", "yes"}


def _jsonl(path: pathlib.Path) -> list[dict]:
    if not path.exists():
        return []
    records = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            records.append(value)
    return records


def _latest_outage(primary: str):
    kills = [event for event in _jsonl(pathlib.Path("chaos/chaos-events.jsonl"))
             if event.get("action") == "kill" and event.get("region") == primary]
    return kills[-1] if kills else None


def _wait_for_detection(primary: str, outage_ts, timeout: float = 60.0):
    """Wait for the checker event so failover cannot precede measured detection."""
    path = pathlib.Path("reports/health-events.jsonl")
    deadline = time.monotonic() + timeout
    while True:
        events = [event for event in _jsonl(path)
                  if event.get("event") == "state_change"
                  and event.get("region") == primary
                  and event.get("to") == "UNHEALTHY"
                  and (outage_ts is None or event.get("ts", 0) >= outage_ts)]
        if events:
            return events[0]
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None
        time.sleep(min(0.25, remaining))


def run(primary: str, target: str, backend: str, auto: bool) -> dict:
    """Confirm an outage, fail over once, verify service, and record the result."""
    if primary not in URL or target not in URL or primary == target:
        raise ValueError("primary and target must be different regions: 'a' and 'b'")
    if backend not in {"fs", "minio"}:
        raise ValueError("backend must be 'fs' or 'minio'")

    started_wall = time.time()
    outage = _latest_outage(primary)
    outage_ts = outage.get("ts") if outage else None

    # 1. Require three consecutive readiness failures on the primary and make
    # sure the other process is still alive before treating this as a failover.
    primary_failures = 0
    target_alive = False
    primary_reason = ""
    for attempt in range(3):
        try:
            response = httpx.get(f"{URL[primary]}/readyz", timeout=2.0)
            if response.status_code == 200:
                primary_failures = 0
                primary_reason = "primary /readyz returned 200"
            else:
                primary_failures += 1
                primary_reason = f"primary /readyz status={response.status_code}"
        except httpx.RequestError as exc:
            primary_failures += 1
            primary_reason = f"primary probe {type(exc).__name__}: {exc}"

        try:
            alive = httpx.get(f"{URL[target]}/healthz", timeout=1.5)
            target_alive = alive.status_code == 200
        except httpx.RequestError:
            target_alive = False

        if attempt < 2:
            time.sleep(0.25)

    outage_confirmed = primary_failures >= 3 and target_alive
    step(1, "xac_nhan_outage", ok=outage_confirmed, primary=primary,
         target=target, outage_ts=outage_ts, outage_iso=outage.get("iso") if outage else None,
         primary_consecutive_failures=primary_failures, primary_reason=primary_reason,
         target_alive=target_alive)

    incident_ts = time.time()
    incident = step(2, "thong_bao_incident", ok=outage_confirmed,
                    incident_ts=incident_ts,
                    outage_ts=outage_ts,
                    delay_from_outage_s=(round(incident_ts - outage_ts, 2)
                                         if outage_ts is not None else None),
                    primary=primary, target=target)

    def finish_aborted(reason: str, failover_result=None, step3_logged=False):
        if not step3_logged:
            step(3, "scale_gpu_pool", ok=False, skipped=failover_result is None,
                 reason=reason, failover_result=failover_result)
        step(4, "verify_state_replica", ok=False, skipped=True, reason=reason)
        step(5, "dns_cutover", ok=False, skipped=True, reason=reason)
        step(6, "verify_golden_signals", ok=False, skipped=True, reason=reason,
             request_count=0)
        end_ts = time.time()
        elapsed = round(end_ts - outage_ts, 2) if outage_ts is not None else round(
            end_ts - started_wall, 2)
        step(7, "post_incident", ok=False, elapsed_s=elapsed, reason=reason,
             measure_command=("python3 tools/measure_rto.py --loadgen "
                              "reports/drill-2-withdr.jsonl --target-rto 300"))
        return {"ok": False, "reason": reason, "incident": incident,
                "failover": failover_result, "elapsed_s": elapsed}

    if not outage_confirmed:
        return finish_aborted("outage not confirmed or target region is not alive")
    if not confirm(auto, f"Confirm failover from region-{primary} to region-{target}?"):
        return finish_aborted("operator declined failover")

    # Do not cut over before the health checker records the outage. This keeps
    # the automated drill timeline reproducible and avoids a false RTO result.
    detection = _wait_for_detection(primary, outage_ts, timeout=60.0)
    if detection is None:
        return finish_aborted("health checker did not record primary as UNHEALTHY")

    # 3. This call owns all five failover sub-steps and must happen exactly once.
    failover_result = fo.failover(target, backend, wait=60.0)
    step(3, "scale_gpu_pool", ok=bool(failover_result.get("ok")),
         failover_result=failover_result, detection_ts=detection.get("ts"))
    if not failover_result.get("ok"):
        return finish_aborted(failover_result.get("error", "failover failed"),
                              failover_result, step3_logged=True)

    # 4. Read the state returned by the one failover invocation; do not restore
    # or call failover again during verification.
    target_state = failover_result.get("target_state") or {}
    restore = failover_result.get("restore") or {}
    vector_count = target_state.get("count")
    replica_ok = (bool(target_state.get("weights")) and (vector_count or 0) > 0
                  and restore.get("rpo_seconds") is not None
                  and restore.get("docs_lost") is not None)
    step(4, "verify_state_replica", ok=replica_ok, target_state=target_state,
         rpo_seconds=restore.get("rpo_seconds"), docs_lost=restore.get("docs_lost"),
         embed_model_version=restore.get("embed_model_version"))

    # 5. The pointer was written by failover only after /readyz returned 200.
    cutover = failover_result.get("cutover") or {}
    step(5, "dns_cutover", ok=bool(cutover.get("ok")),
         active_region=cutover.get("active_region"), cutover_ts=cutover.get("ts"))

    # 6. Make ten real requests directly to the new region and record latency
    # and errors. The local lab has no published latency SLO, so report p95 and
    # error rate without inventing a pass threshold.
    latencies_ms = []
    errors = 0
    request_results = []
    for index in range(10):
        start = time.perf_counter()
        try:
            response = httpx.get(f"{URL[target]}/v1/infer", timeout=3.0)
            latency = (time.perf_counter() - start) * 1000
            ok = response.status_code == 200
            if not ok:
                errors += 1
            latencies_ms.append(latency)
            request_results.append({"seq": index + 1, "status": response.status_code,
                                     "ok": ok, "latency_ms": round(latency, 1)})
        except httpx.RequestError as exc:
            latency = (time.perf_counter() - start) * 1000
            errors += 1
            latencies_ms.append(latency)
            request_results.append({"seq": index + 1, "status": None, "ok": False,
                                     "error": type(exc).__name__,
                                     "latency_ms": round(latency, 1)})

    ordered = sorted(latencies_ms)
    p95_ms = round(ordered[max(0, math.ceil(0.95 * len(ordered)) - 1)], 1)
    error_rate = round(errors / 10, 3)
    golden_ok = errors == 0
    step(6, "verify_golden_signals", ok=golden_ok, target=target,
         request_count=10, errors=errors, error_rate=error_rate, p95_ms=p95_ms,
         latencies_ms=[round(value, 1) for value in latencies_ms],
         requests=request_results)

    # 7. Record elapsed wall-clock time and the command that derives RTO from
    # the load generator, chaos, health-check, and failover logs.
    end_ts = time.time()
    elapsed = round(end_ts - outage_ts, 2) if outage_ts is not None else round(
        end_ts - started_wall, 2)
    overall_ok = replica_ok and bool(cutover.get("ok")) and golden_ok
    measure_command = ("python3 tools/measure_rto.py --loadgen "
                       "reports/drill-2-withdr.jsonl --target-rto 300")
    step(7, "post_incident", ok=overall_ok, elapsed_s=elapsed,
         measure_command=measure_command, detection_ts=detection.get("ts"),
         cutover_ts=cutover.get("ts"))
    return {"ok": overall_ok, "primary": primary, "target": target,
            "incident": incident, "detection": detection,
            "failover": failover_result, "target_state": target_state,
            "golden_signals": {"request_count": 10, "errors": errors,
                               "error_rate": error_rate, "p95_ms": p95_ms},
            "elapsed_s": elapsed, "measure_command": measure_command}


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--primary", default="a")
    p.add_argument("--target", default="b")
    p.add_argument("--backend", default="fs", choices=["fs", "minio"])
    p.add_argument("--auto", action="store_true")
    a = p.parse_args()
    result = run(a.primary, a.target, a.backend, a.auto)
    print(json.dumps(result, indent=2, ensure_ascii=False))
    if not result.get("ok"):
        raise SystemExit(1)
