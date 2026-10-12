# Discord Nuôi Heo Plus 🐷

## Tính năng
- `/farm` mở bảng điều khiển bằng button.
- Menu chọn từng heo, hình trang trại và thanh trạng thái. Mỗi heo có màu, dấu lông và phụ kiện ổn định theo ID và phẩm chất.
- GIF hoạt hình ngay trong Discord khi cho ăn, mua heo, sinh sản; heo chuyển giai đoạn theo cân nặng sẽ có hoạt ảnh lớn lên.
- Heo con dưới 5 kg, thiếu niên từ 5 đến dưới 20 kg, trưởng thành từ 20 kg; hình ảnh được tạo bằng Pillow ngay lúc chơi, không cần tải bộ ảnh ngoài.
- Heo có phẩm chất: Thường, Hiếm, Sử thi, Huyền thoại; phẩm chất ảnh hưởng giá bán.
- Mua heo ngẫu nhiên phẩm chất/giới tính.
- Sinh sản: chọn một heo; bot tự tìm heo khác giới tính đủ điều kiện trong cùng trang trại. Tốn 150 xu, có cooldown 6 giờ mỗi heo, cần chỗ trống.
- Nhiệm vụ hằng ngày: cho ăn 3 lần, mua heo 1 lần, sinh sản 1 lần; mỗi nhiệm vụ thưởng 120 xu.
- Sự kiện luân phiên theo tuần UTC: lễ hội thức ăn (+20 xu khi cho ăn), tuần heo hiếm (tăng tỷ lệ hiếm), hội chợ trang trại (+15% giá bán).
- Database SQLite tự migrate các cột mới, giữ dữ liệu cũ.

## Cài đặt Discord
1. https://discord.com/developers/applications → New Application.
2. Tab Bot → tạo bot và lấy token. Không chia sẻ token.
3. OAuth2 → URL Generator: scopes `bot`, `applications.commands`; quyền `Send Messages`, `Embed Links`, `Read Message History`.
4. Mời bot vào server.
5. Lấy Server ID qua Developer Mode → chuột phải server → Copy Server ID.

## Local
Cần Python 3.11+:
```bash
python -m pip install -r requirements.txt
```
Windows PowerShell:
```powershell
$env:DISCORD_TOKEN="TOKEN_CUA_MAY"
$env:GUILD_ID="SERVER_ID"
python main.py
```
Không commit token.

## Render 24/7
1. Đẩy code lên GitHub private.
2. Render → New → Blueprint → chọn repo có `render.yaml`.
3. Nhập `DISCORD_TOKEN` và `GUILD_ID`.
4. Dùng worker không sleep và kiểm tra giá trước khi deploy.
5. Gắn Persistent Disk `/app/data`; `DB_PATH=/app/data/nuoi_heo.sqlite3`.
6. Deploy; kiểm tra log `Logged in as ...`; dùng `/farm`.

## Lưu ý
- Backup SQLite định kỳ; không xóa persistent disk nếu muốn giữ tiến trình.
- Chỉ một worker được ghi vào SQLite.
- Hoạt ảnh GIF được tạo tại thời điểm thao tác; đây là hoạt ảnh khung hình đơn giản, không phải video 3D.
- Mã đã được kiểm tra cú pháp Python; vẫn cần chạy thử với bot/token Discord thật để xác nhận triển khai.
- Sinh sản là gameplay giả lập; cần có heo khác giới tính, sức chứa trống, xu và cooldown.
