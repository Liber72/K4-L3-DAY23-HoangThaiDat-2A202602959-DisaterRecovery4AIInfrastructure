# Postmortem — DR Drill Lab 23 (TEMPLATE)

Theo đúng template §4 "Sau Failover: Blameless Postmortem". Blameless: câu hỏi là
"hệ thống/process nào cho phép chuyện này", không phải "ai làm sai".

## 1. Timeline (mọi dòng phải có evidence path:line)

| ISO time | Sự kiện | Evidence |
|---|---|---|
| 2026-10-09T15:22:25.0 | outage bắt đầu | chaos/chaos-events.jsonl |
| 2026-10-09T15:22:25.5 | user đầu tiên bị ảnh hưởng | reports/drill-2-withdr.jsonl |
| 2026-10-09T15:22:39.4 | health check alert | reports/health-events.jsonl |
| 2026-10-09T15:22:53.4 | operator confirm cutover | reports/failover-events.jsonl |
| 2026-10-09T15:22:57.8 | resolved (request đầu tiên OK từ region phụ) | reports/drill-2-withdr.jsonl |

## 2. RTO/RPO đo được vs mục tiêu — gap ở bước nào?

- RTO mục tiêu: 300s · đo được: 32.8s · gap: Đạt chỉ tiêu
- RPO mục tiêu: 300s · đo được: 10.0s (5 doc bị mất) · gap: Đạt chỉ tiêu
- **Bước tốn nhiều giây nhất:** Chờ đợi Health Check phát hiện (14.4 giây) — vì cấu hình `interval_s` = 5 và `threshold` = 3 nên bắt buộc phải đợi tối thiểu 15 giây mới dám ra quyết định failover (chống flapping).

## 3. Root cause (5 whys)

Không phải "vì tôi chạy chaos script". Câu hỏi: *nếu đây là outage thật, bước nào
trong runbook của tôi sẽ thất bại?*
Hệ thống đã có cơ chế tự động chuyển vùng (Failover) bằng DNS và phục hồi dữ liệu từ bản Snapshot. Các bước khôi phục đã diễn ra thành công tốt đẹp theo đúng quy trình Runbook mà không bị gián đoạn. Thời gian RTO 32.8s chủ yếu đến từ chiến lược chờ (wait) an toàn để chống báo động giả.
## 4. Action items (có owner + deadline)

| # | Action | Owner | Deadline | Giảm RTO/RPO bao nhiêu giây |
|---|---|---|---|---|
| 1 | | | | |
| 2 | | | | |

## 5. Ba câu hỏi bắt buộc trả lời

1. `interval × threshold` của bạn là bao nhiêu giây? Nó chiếm bao nhiêu % RTO?
2. Nếu hạ interval xuống 1s, RTO giảm mấy giây — và bạn trả giá gì (§4 flapping)?
3. Nếu outage kéo dài 6 giờ và region chính mất dữ liệu vĩnh viễn, `docs_lost` của
   bạn có nghĩa gì với khách hàng?
