# Apps và kho tài khoản AgentFlow

## Bắt đầu

1. Chạy `.\Start-AgentFlow.ps1`, mở `http://127.0.0.1:8765/#connections`.
2. Trong **Apps & kho tài khoản**, chọn **Cài app** khi cần dùng. Gmail, Postman,
   Cloudflare, Hugging Face và web/API dùng adapter tích hợp. SSH tự cài Paramiko
   vào `.agentflow/plugin-packages`; agent không được chọn package để cài trên host.
3. Chọn **Kết nối tài khoản**, đặt tên nhận diện, nhập thông tin đăng nhập ở các ô
   bí mật. Không gửi khóa, cookie hoặc mật khẩu trong chat.
4. Chọn từng agent, hoặc chủ động chọn “Toàn đội, gồm cả agent tạo sau này”.
   Chọn các thao tác được cấp rồi lưu. Chưa thực hiện thành công thì kết nối vẫn
   hiển thị **Chưa xác minh**; lưu thông tin không đồng nghĩa đã đăng nhập thành công.
5. Giao việc trong chat. Agent dùng `plugins.list` và `plugins.invoke`; không cần
   nhận credential. Thu hồi kết nối hoặc gỡ app để chặn các thao tác mới.

Mỗi app có thể kết nối nhiều tài khoản. Khi đổi tài khoản, nhập đủ bộ thông tin
mới; runtime thay toàn bộ bộ đăng nhập cũ để không trộn access token với refresh
token/cookie của tài khoản khác. Để tất cả ô bí mật trống để giữ bộ đang có.
Thay cấu hình hoặc thu hồi trong lúc gọi sẽ loại kết quả cũ khi trở về; một thao
tác đã được dịch vụ bên ngoài tiếp nhận không thể bị hoàn tác bằng cách thu hồi.

## Adapter hiện có

- **Gmail**: OAuth access token hoặc refresh token + client ID + client secret.
  Làm mới access token trong broker. Liệt kê/đọc thư; gửi thư là quyền riêng và
  phải được chủ tài khoản bật. OAuth cần scope Gmail phù hợp từ dự án Google của
  chủ workspace. Chưa có màn hình đăng nhập OAuth bằng trình duyệt trong bản này.
  [Tài liệu Gmail](https://developers.google.com/workspace/gmail/api).
- **Postman**: API key. Xem workspace, danh sách collection và cấu trúc request.
  Không trả auth, biến môi trường, body, script hoặc URL chứa thông tin bí mật.
  [Tài liệu Postman](https://learning.postman.com/api-docs/api-reference/).
- **Cloudflare**: API token có scope cần thiết và `zone_ids` do chủ tài khoản đặt.
  Chỉ xem zone được cấp, liệt kê DNS; tạo DNS là quyền riêng.
  [Tài liệu Cloudflare](https://developers.cloudflare.com/api/).
- **Hugging Face**: token Hub, tìm và đọc metadata model.
  [Tài liệu Hub](https://huggingface.co/docs/hub/api).
- **SSH**: private key trong vault, passphrase nếu có, username/host/port và host
  key chính xác dạng `ssh-ed25519 AAAA…`. Khai báo `actions` là tên → lệnh cố định;
  agent chọn tên action và không được sửa lệnh. Không tự chấp nhận host key mới,
  không đọc key từ `.ssh` hay sử dụng ssh-agent của máy.
  [Tài liệu Paramiko](https://docs.paramiko.org/en/stable/).
- **Web/API**: token/cookie, origin HTTPS công khai và route có tên cố định. Đây
  là kết nối API/session, chưa phải điều khiển giao diện mọi website. Agent không
  được thay origin, URL, header hoặc body của route.

Ví dụ cấu hình Cloudflare: `{"zone_ids":["ID_ZONE_32_KY_TU_HEX"]}`.

Ví dụ web/API:

```json
{"origin":"https://example.com","routes":{"status":{"method":"GET","path":"/api/status"}}}
```

## Ranh giới thực thi

Kho bí mật dùng DPAPI của tài khoản Windows chạy runtime. API owner không trả
lại giá trị bí mật; context model chỉ có tên kết nối, app, thao tác và grant.
Agent không có công cụ đọc vault, đăng ký adapter, thay credential hoặc cấp grant.
Tool ghép phải gọi lại broker và không tăng quyền của agent.

HTTP có credential chỉ đi tới host cố định của adapter hoặc origin owner đã đặt;
DNS được pin, chỉ dùng địa chỉ HTTPS công khai, không theo redirect. SSH pin host
key và chỉ thực hiện action do owner đặt. Lỗi không chứa phản hồi xác thực thô;
đầu ra loại trường credential và che giá trị bí mật đã lưu cùng một số encoding
thông dụng. Không coi redaction là cơ chế duy nhất bảo vệ credential.

Native model CLI chỉ xuất yêu cầu broker JSON, không có công cụ đọc/chạy host.
Mã của agent chạy qua Docker không có mạng và bản sao đã loại bí mật. Instruction
trong web, file và SKILL.md không thể thay quyền broker.

Đây là ranh giới đối với agent do AgentFlow quản lý. Người có toàn quyền Windows,
mã adapter/plugin không đáng tin chạy trong tiến trình server, hoặc hệ điều hành
bị xâm nhập nằm ngoài ranh giới này; không cam kết chống mọi cách vượt quyền ở
cấp hệ điều hành. Thêm adapter mới cần thay mã ứng dụng được kiểm tra, không tải
và chạy mã plugin bất kỳ qua skill.
