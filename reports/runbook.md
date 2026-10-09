# Runbook 1 trang — Region chính down

Runbook phải chạy được lúc 3h sáng bởi người KHÔNG viết nó. Mỗi bước: lệnh copy-paste
được + cách biết bước đó xong.

| # | Bước | Lệnh | Biết là xong khi | Ai làm |
|---|---|---|---|---|
| 1 | Xác nhận outage | `curl http://localhost:8001/healthz` | Báo lỗi timeout hoặc Connection Refused | on-call |
| 2 | Mở incident + bấm giờ RTO | Nhận cảnh báo từ hệ thống giám sát | Có log alert từ Health Checker ghi nhận lỗi | on-call |
| 3 | Restore state ở region phụ | `python dr/runbook.py --primary a --target b --backend fs` | Màn hình log báo step `2_restore_snapshot` OK | on-call |
| 4 | Scale pool warm→full | Chạy tự động trong lệnh runbook trên | `/readyz` của b trả 200 | script |
| 5 | DNS/LB cutover | Chạy tự động trong lệnh runbook trên | `curl localhost:8080/edge/state` cho `active_region=b` | script |
| 6 | Verify golden signals | Chạy tự động trong lệnh runbook trên | p95 < 200ms, error rate < 0.01 | script |
| 7 | Đo RTO + postmortem | `python tools/measure_rto.py --loadgen reports/drill-2-withdr.jsonl` | `rto_verdict` != null | on-call |

**Rollback (failover ngược):** điều kiện nào thì trả traffic về region A? Ai quyết định?
(§4 Anti-Patterns: full-auto không có circuit breaker → 2 region flap qua lại.)
Trả traffic về Region A khi và chỉ khi: Region A đã khôi phục hoàn toàn, pass health check bằng tay, và được đồng bộ dữ liệu (weights, vector) đầy đủ từ Region B. Quyết định Cutover ngược lại PHẢI do con người (Operator/Admin) phê duyệt để tránh hiện tượng flap liên tục giữa hai bên.
