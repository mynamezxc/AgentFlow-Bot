# Đối chiếu Grok Bot → AgentFlow

Nghiên cứu ngày 18/10/2026 từ nguồn chính thức và 6 ảnh người dùng cung cấp:

- https://x.ai/news/designing-grok-bot
- https://cursor.com/docs/grok-bot/work
- https://cursor.com/help/grok-bot/routines
- https://x.ai/bot/guides/grok-bot-101

Grok Bot đặt đồng đội có danh tính lâu dài làm đơn vị chính. Chat là nơi giao
việc, nhận chuyển lời, thấy sản phẩm và ra quyết định. Coordinator xử lý vận
hành và đưa phán đoán cần thiết về chủ dự án. Các primitive gồm bots, chats,
prompts/skills/routines, tools và artifacts.

Ảnh minh họa: tạo đội bằng một yêu cầu, CEO chia việc rồi tự trao đổi với chuyên
gia; sự kiện Message from / Messaged trong transcript; báo rủi ro dựa trên
source và hỏi lựa chọn; thay Coder thành Project Manager giữa hội thoại; tài
liệu thành file thật và gửi đường dẫn.

## Quyết định triển khai

- Roster cạnh trái, preview tin cuối, trạng thái và câu hỏi.
- Chat mặc định, bảng quản trị hỗ trợ; một mục tiêu được tự điều phối.
- Giao việc/tạo agent/skill qua broker có kết quả thực, không chỉ lời hứa.
- Inbox, nhiều tin và callback; bỏ giới hạn hai lần chuyển lời.
- Kế hoạch nội bộ thực thi ngay; người dùng không chuyển task thủ công.
- Quyết định có ID và checkpoint; trả lời xong tự tiếp tục đúng nhánh.
- Skill được gán và dùng ngay, có lịch sử; vai trò agent có revision.
- Artifacts là file với metadata, xem được và tải được.
- Khác biệt theo yêu cầu: account/provider/model do chủ workspace cấu hình;
  agent mới kế thừa mặc định, chủ có thể override từng agent.

## Phần chưa tương đương

AgentFlow chạy trên máy local. Không có hạ tầng máy cloud, full computer/browser
và hệ sinh thái connector của Grok Bot. Broker có tệp, phối hợp, skills, routines,
memory, nguồn HTTPS, ảnh gateway và các channel đã triển khai. Có tool trong
danh sách không đồng nghĩa đã kết nối tài khoản hay chạy thành công.

## Kết nối Codex từ kiểm thử thật

Lượt đầu gặp xung đột vai trò native coding assistant: model tìm functions.exec
thay vì xuất action envelope. Sửa bằng worker instructions riêng qua
model_instructions_file, chỉ lấy final agent message và parse JSON envelope;
runtime thực thi tools. Đã kiểm chứng tự tạo agent, skill và thẻ câu hỏi với
tài khoản thật, không chỉ unit mock.

Tham khảo:
https://learn.chatgpt.com/docs/config-file/config-reference
https://learn.chatgpt.com/docs/non-interactive-mode
