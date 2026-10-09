# RTO/RPO Evidence — Lab 23

Quy tắc duy nhất: mỗi con số ở đây phải trỏ được về **một dòng log thật**
(`đường/dẫn.jsonl:số_dòng`). `pytest tests/test_rto_evidence.py` sẽ mở từng file ra kiểm tra.
Con số không có evidence = trượt, bất kể các phần khác.

## 1. Drill 1 — không có DR (baseline)

| Chỉ số | Giá trị | Cách đo | Evidence |
|---|---|---|---|
| t_outage | `2026-10-09T14:57:44` | chaos kill | `chaos/chaos-events.jsonl:6` |
| Request fail đầu tiên | `+0.3s` | dòng `ok:false` đầu tiên sau t_outage | `reports/drill-1-nodr.jsonl:18` |
| Request thành công sau đó | không có | không có dòng `ok:true` nào sau t_outage | `reports/drill-1-nodr.jsonl:33` |
| RTO | `NO_RECOVERY` | `tools/measure_rto.py` | `reports/drill-1-nodr.jsonl:33` |

## 2. Drill 2 — có DR

| Mốc | +giây từ t_outage | Cách đo | Evidence |
|---|---|---|---|
| t_outage (mốc 0) | 0 | `action:kill` | `chaos/chaos-events.jsonl:7` |
| User thấy lỗi đầu tiên | 0.5s | dòng `ok:false` đầu | `reports/drill-2-withdr.jsonl:30` |
| Health check phát hiện | 14.4s | `to:UNHEALTHY, region:a` | `reports/health-events.jsonl:2` |
| Snapshot restore xong | 22.3s | `step:2_restore_snapshot` | `reports/failover-events.jsonl:2` |
| Region phụ ready | 28.5s | `step:4_wait_ready` | `reports/failover-events.jsonl:4` |
| DNS cutover | 28.5s | `step:5_dns_cutover` | `reports/failover-events.jsonl:5` |
| **RTO đo được** | 32.8s | dòng `ok:true` đầu sau lỗi | `reports/drill-2-withdr.jsonl:40` |

| Chỉ số | Đo được | Mục tiêu (slide §1) | Verdict |
|---|---|---|---|
| RTO — Inference API | `32.8s` | 300s (5 phút) | PASS |
| RPO — Vector DB | `10.0s` / `5` doc | 300s (5 phút) | PASS |

## 3. RTO của tôi gồm những gì (bắt buộc — đây là phần chấm điểm hiểu bài)

| Thành phần | Giây | Nó đến từ đâu | Giảm được bằng cách nào |
|---|---|---|---|
| Health-check detect floor | 15.0s | `interval_s × threshold` trong `reports/health-events.jsonl:2` | Giảm interval hoặc threshold (nhưng dễ gây flapping) |
| Snapshot restore | 0.0s | 2_restore → 3_scale | Dùng ổ mạng chia sẻ tốc độ cao thay vì copy file qua LAN |
| GPU pool warm-up | 6.19s | `waited_s` ở `4_wait_ready` | Tải weights sẵn lên VRAM thay vì tải từ đĩa khi có sự cố |
| DNS/LB TTL cache | 4.3s | t_recovered − t_cutover | Giảm TTL của DNS xuống thấp hơn nữa |
