"""BƯỚC 3b — Failover/cutover sang region phụ.

5 bước, THỨ TỰ QUAN TRỌNG (§2 Kiến Trúc Tham Chiếu: DNS/LB, compute, state là 3 lớp riêng):
  1_verify_target    — /v1/state của region phụ: weights? vector count? pool_state?
  2_restore_snapshot — gọi state/snapshot.py get + state/snapshot.py rpo()
                       Log BẮT BUỘC: rpo_seconds, docs_lost, embed_model_version.
                       (§3: "backup index nhưng quên backup embedding model version
                        -> index không tương thích khi restore")
  3_scale_pool       — ghi "full" vào state/region-<t>/pool_state (warm -> full)
  4_wait_ready       — POLL /readyz tới khi 200. Region phụ có WARMUP_SECONDS —
                       đây là GPU pool warm-up của §4, nó nằm trong RTO của bạn.
  5_dns_cutover      — ghi region đích vào edge/active_region

BẪY: nếu bạn đổi edge/active_region TRƯỚC bước 4, user sẽ nhận 503 từ CẢ HAI region
và RTO của bạn dài hơn, không ngắn hơn. Nếu bước 4 timeout -> ABORT, KHÔNG cutover.

Mỗi bước ghi 1 dòng vào reports/failover-events.jsonl với ts + step.
Không có dòng 5_dns_cutover = tools/measure_rto.py không tìm được t_cutover = mất điểm.

Chạy:  python dr/failover.py --target b --backend fs
"""
import argparse
import json
import pathlib
import sys
import time
from datetime import datetime, timezone

import httpx

sys.path.insert(0, ".")
from state import snapshot  # noqa: E402

URL = {"a": "http://127.0.0.1:8001", "b": "http://127.0.0.1:8002"}
LOG = pathlib.Path("reports/failover-events.jsonl")
ACTIVE_FILE = pathlib.Path("edge/active_region")


def emit(**kw):
    """Append one timestamped event to the failover JSONL log and stdout."""
    ts = time.time()
    iso = datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec="milliseconds")
    event = {
        "ts": ts,
        "iso": iso.replace("+00:00", "Z"),
        **kw,
    }
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps(event, ensure_ascii=False) + "\n")
    print(json.dumps(event, ensure_ascii=False))
    return event


def state_of(region: str) -> dict:
    """Return the serving state for a region, or raise on an unreachable service."""
    response = httpx.get(f"{URL[region]}/v1/state", timeout=2.0)
    response.raise_for_status()
    return response.json()


def failover(target: str, backend: str, wait: float) -> dict:
    """Restore a target region, wait for readiness, then change the edge pointer."""
    if target not in URL:
        raise ValueError("target must be 'a' or 'b'")
    if backend not in {"fs", "minio"}:
        raise ValueError("backend must be 'fs' or 'minio'")
    if wait < 0:
        raise ValueError("wait cannot be negative")

    primary = "b" if target == "a" else "a"
    events = []

    # 1. Inspect the target before touching its files.
    try:
        before = state_of(target)
        events.append(emit(step="1_verify_target", ok=True, target=target,
                           state=before))
    except (httpx.HTTPError, ValueError, OSError) as exc:
        message = f"target state unavailable: {type(exc).__name__}: {exc}"
        events.append(emit(step="1_verify_target", ok=False, target=target,
                           error=message))
        return {"ok": False, "target": target, "error": message, "steps": events}

    # 2. Restore the latest snapshot, then calculate data loss against the
    # primary's current database (the primary process may be paused, but its
    # local SQLite file remains readable).
    try:
        restored = snapshot.get(target, backend)
        primary_db = pathlib.Path(f"state/region-{primary}/vectors.sqlite")
        restored_db = pathlib.Path(f"state/region-{target}/vectors.sqlite")
        rpo = snapshot.rpo(primary_db, restored_db)
        restore_info = {
            **restored,
            "rpo_seconds": rpo.get("rpo_seconds"),
            "docs_lost": rpo.get("docs_lost"),
        }
        events.append(emit(step="2_restore_snapshot", ok=True, target=target,
                           rpo_seconds=restore_info["rpo_seconds"],
                           docs_lost=restore_info["docs_lost"],
                           embed_model_version=restored.get("embed_model_version"),
                           snapshot_at=restored.get("snapshot_at"),
                           latest_doc_ts=restored.get("latest_doc_ts")))
    except (Exception, SystemExit) as exc:
        message = f"snapshot restore failed: {type(exc).__name__}: {exc}"
        events.append(emit(step="2_restore_snapshot", ok=False, target=target,
                           error=message, rpo_seconds=None, docs_lost=None,
                           embed_model_version=None))
        return {"ok": False, "target": target, "error": message, "steps": events}

    # 3. Transition the running serving process from warm to full. The app's
    # readiness check starts its warm-up timer when it observes this change.
    pool_file = pathlib.Path(f"state/region-{target}/pool_state")
    try:
        pool_file.parent.mkdir(parents=True, exist_ok=True)
        pool_file.write_text("full\n", encoding="utf-8")
        events.append(emit(step="3_scale_pool", ok=True, target=target,
                           pool_state="full"))
    except OSError as exc:
        message = f"could not scale target pool: {type(exc).__name__}: {exc}"
        events.append(emit(step="3_scale_pool", ok=False, target=target,
                           error=message))
        return {"ok": False, "target": target, "error": message, "steps": events,
                "restore": restore_info}

    # 4. Keep polling until /readyz returns 200. A timeout must leave the edge
    # pointer untouched.
    deadline = time.monotonic() + wait
    wait_started = time.monotonic()
    ready_payload = None
    last_error = "target did not become ready before timeout"
    first_attempt = True
    while first_attempt or time.monotonic() < deadline:
        first_attempt = False
        remaining = max(0.0, deadline - time.monotonic())
        request_timeout = max(0.1, min(2.0, remaining))
        try:
            response = httpx.get(f"{URL[target]}/readyz", timeout=request_timeout)
            try:
                body = response.json()
            except (ValueError, TypeError):
                body = {}
            if (response.status_code == 200
                    and (not isinstance(body, dict) or body.get("ready", True))):
                ready_payload = body
                break
            reasons = body.get("reasons", []) if isinstance(body, dict) else []
            last_error = (f"readyz status={response.status_code}; "
                          f"reasons={','.join(str(x) for x in reasons)}")
        except httpx.RequestError as exc:
            last_error = f"readyz request error: {type(exc).__name__}: {exc}"

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        time.sleep(min(0.25, remaining))

    waited_s = round(time.monotonic() - wait_started, 2)
    if ready_payload is None:
        events.append(emit(step="4_wait_ready", ok=False, target=target,
                           waited_s=waited_s, error=last_error))
        return {"ok": False, "target": target, "error": last_error,
                "waited_s": waited_s, "steps": events, "restore": restore_info}

    vectors = ready_payload.get("vectors", {})
    target_state = {
        "region": target,
        "pool_state": ready_payload.get("pool_state", "full"),
        "weights": True,
        "count": vectors.get("count") if isinstance(vectors, dict) else None,
        "vectors": vectors,
        "ready": True,
    }
    events.append(emit(step="4_wait_ready", ok=True, target=target,
                       waited_s=waited_s, state=ready_payload))

    # 5. This is the only point at which the edge pointer may be changed.
    try:
        ACTIVE_FILE.parent.mkdir(parents=True, exist_ok=True)
        ACTIVE_FILE.write_text(target + "\n", encoding="utf-8")
        cutover = emit(step="5_dns_cutover", ok=True, target=target,
                       active_region=target)
    except OSError as exc:
        message = f"DNS cutover failed: {type(exc).__name__}: {exc}"
        events.append(emit(step="5_dns_cutover", ok=False, target=target,
                           error=message))
        return {"ok": False, "target": target, "error": message,
                "waited_s": waited_s, "steps": events, "restore": restore_info,
                "target_state": target_state}

    return {"ok": True, "target": target, "target_state": target_state,
            "restore": restore_info, "waited_s": waited_s, "cutover": cutover,
            "steps": events}


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--target", default="b", choices=["a", "b"])
    p.add_argument("--backend", default="fs", choices=["fs", "minio"])
    p.add_argument("--wait", type=float, default=60)
    a = p.parse_args()
    result = failover(a.target, a.backend, a.wait)
    print(json.dumps(result, indent=2, ensure_ascii=False))
    if not result.get("ok"):
        raise SystemExit(1)
