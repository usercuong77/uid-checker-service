# UID Checker Service (Python)

Canonical cross-system handover guide:
- `../huong-dan/HUONG_DAN_AI.md`
- Update this guide whenever code/config/infrastructure changes.

Service nay cung cap API check UID Facebook de Apps Script goi sang.
Ngoai ra service co them relay webhook ngan cho SePay, de tranh vuong gioi han do dai URL khi can forward sang Apps Script.
Service nay cung co the lam relay webhook Telegram cho bot chinh/bot con, de tranh loi 302/401/405 khi Telegram goi truc tiep Apps Script.

## 1) Cai dat

```bash
cd weblamquoccuong/Bot/uid-checker-service
python -m venv .venv
.venv\\Scripts\\activate
pip install -r requirements.txt
playwright install chromium
```

## 2) Chay local

```bash
set UID_CHECKER_API_KEY=your_secret_key
set UID_CHECKER_FB_COOKIES_JSON={"c_user":"1000xxxxxxxx","xs":"xx:xxxxxxxxxxxxxxxx"}
set UID_CHECKER_FB_COOKIES_POOL_JSON=[{"c_user":"1000xxxxxxxx","xs":"xx:xxxxxxxxxxxxxxxx"},{"c_user":"1000yyyyyyyy","xs":"yy:yyyyyyyyyyyyyyyy"}]
set SEPAY_RELAY_TARGET_URL=https://script.googleusercontent.com/macros/echo?user_content_key=...&lib=...
set SEPAY_RELAY_TIMEOUT=20
set TELEGRAM_RELAY_TARGET_URL=https://script.google.com/macros/s/DEPLOY_ID/exec
set TELEGRAM_RELAY_TIMEOUT=20
uvicorn app:app --host 0.0.0.0 --port 8080
```

Chay unit test:

```bash
cd weblamquoccuong/Bot/uid-checker-service
.venv\\Scripts\\python.exe -m unittest discover -s tests -v
```

Health check:

```bash
curl http://127.0.0.1:8080/health
```

Relay webhook SePay local:

```bash
curl -X POST http://127.0.0.1:8080/sepay-webhook ^
  -H "Content-Type: application/json" ^
  -H "Authorization: Apikey your_sepay_key" ^
  -d "{\"id\":123,\"transferType\":\"in\"}"
```

Relay webhook Telegram local:

```bash
curl -X POST "http://127.0.0.1:8080/telegram-webhook?bot=buff" ^
  -H "Content-Type: application/json" ^
  -d "{\"update_id\":1,\"message\":{\"message_id\":1,\"chat\":{\"id\":123},\"text\":\"/start\"}}"
```

Check UID:

```bash
curl -X POST http://127.0.0.1:8080/check ^
  -H "Content-Type: application/json" ^
  -H "X-Api-Key: your_secret_key" ^
  -d "{\"uid\":\"100041775009544\"}"
```

Check UID voi cookie trong request (optional):

```bash
curl -X POST http://127.0.0.1:8080/check ^
  -H "Content-Type: application/json" ^
  -H "X-Api-Key: your_secret_key" ^
  -d "{\"uid\":\"100041775009544\",\"cookies\":{\"c_user\":\"1000xxxxxxxx\",\"xs\":\"xx:xxxxxxxxxxxxxxxx\"}}"
```

Check UID voi cookie pool trong request (optional):

```bash
curl -X POST http://127.0.0.1:8080/check ^
  -H "Content-Type: application/json" ^
  -H "X-Api-Key: your_secret_key" ^
  -d "{\"uid\":\"100041775009544\",\"cookiesPool\":[{\"c_user\":\"1000xxxxxxxx\",\"xs\":\"xx:xxxxxxxxxxxxxxxx\"},{\"c_user\":\"1000yyyyyyyy\",\"xs\":\"yy:yyyyyyyyyyyyyyyy\"}]}"
```

Check TikTok LIVE:

```bash
curl -X POST http://127.0.0.1:8080/live-check ^
  -H "Content-Type: application/json" ^
  -H "X-Api-Key: your_secret_key" ^
  -d "{\"platform\":\"tiktok\",\"usernames\":[\"tiktok_user1\",\"tiktok_user2\"]}"
```

Check Instagram LIVE (co the truyen proxy pool):

```bash
curl -X POST http://127.0.0.1:8080/live-check ^
  -H "Content-Type: application/json" ^
  -H "X-Api-Key: your_secret_key" ^
  -d "{\"platform\":\"instagram\",\"usernames\":[\"ig_user1\",\"ig_user2\"],\"proxies\":[\"http://user:pass@host:port\"],\"concurrency\":25}"
```

## 3) Noi vao Apps Script

Mo `weblamquoccuong/Bot/apps-script/case_manager_bot.gs`, trong `CONFIG`:

- `externalCheckerUrl`: URL public cua service Python (`https://.../check`)
- `externalCheckerApiKey`: key trung voi `UID_CHECKER_API_KEY`

Neu `externalCheckerUrl` de rong, bot se bo qua Python service va dung logic check noi bo.

## 4) Cau hinh cookie tren Render (khuyen nghi)

Vao Render -> Service -> Environment:

- `UID_CHECKER_API_KEY`: key bao mat cho endpoint `/check`
- `UID_CHECKER_FB_COOKIES_JSON`: JSON cookie dang nhap Facebook, vi du:
  `{"c_user":"1000xxxxxxxx","xs":"xx:xxxxxxxxxxxxxxxx"}`
- `UID_CHECKER_FB_COOKIES_POOL_JSON`: danh sach nhieu cookie de du phong, vi du:
  `[{"c_user":"1000xxxxxxxx","xs":"xx:xxxxxxxxxxxxxxxx"},{"c_user":"1000yyyyyyyy","xs":"yy:yyyyyyyyyyyyyyyy"}]`
- `SEPAY_RELAY_TARGET_URL`: URL Apps Script day du can relay toi.
  - Co the la URL `/exec` neu Render follow redirect tot.
  - On dinh nhat la dan thang URL dich day du cua Apps Script neu ban da co.
- `SEPAY_RELAY_TIMEOUT`: timeout relay, mac dinh `20` giay.
- `TELEGRAM_RELAY_TARGET_URL`: URL Apps Script `/exec` ma Telegram relay se forward toi.
  - Khuyen nghi dung URL Web App `/exec` goc cua Apps Script.
  - Query string tu Telegram relay se duoc giu nguyen, vi du `?bot=buff` hoac `?bot=uid`.
- `TELEGRAM_RELAY_TIMEOUT`: timeout relay Telegram, mac dinh `20` giay.
- `LIVE_CHECK_CONCURRENCY`: gioi han song song cho LIVE check, mac dinh `25`.
- `LIVE_CHECK_TIMEOUT_MS`: timeout load trang cho LIVE check, mac dinh `15000`.

Ghi chu:

- Service se thu cookie theo thu tu: cookie tu request -> cookiesPool tu request -> cookie pool tren env -> cookie default tren env.
- De nhanh, service chi check 1 cookie dau tien. Chi fallback sang cookie tiep theo neu ket qua cho thay dau hieu cookie loi/auth wall/checkpoint.
- Cookie co the het han/checkpoint, can cap nhat dinh ky.
- Khong commit cookie vao GitHub.

## 5) Dung relay URL ngan cho SePay

Neu SePay khong cho dan URL Apps Script qua dai, dung URL ngan nay:

```text
https://<render-service>.onrender.com/sepay-webhook
```

Flow:

1. SePay goi `POST` vao URL ngan tren Render.
2. Relay giu nguyen `Authorization`, `Content-Type`, body JSON va query string.
3. Neu co `Authorization: Apikey ...`, `authorization: Apikey ...` hoac `X-Api-Key`, relay tu dong them `?sepay_key=...` vao URL upstream de Apps Script doc duoc API key on dinh.
4. Relay forward sang `SEPAY_RELAY_TARGET_URL`.
5. Apps Script xu ly webhook SePay nhu cu.

Luu y:

- Khong dung Bitly/TinyURL cho webhook thanh toan.
- Relay nay da duoc viet de xu ly `POST`, khong phai link click thong thuong.
- Neu Apps Script dang can query secret kieu `?sepay_key=...`, relay se giu nguyen query string khi forward.

## 6) Dung relay URL ngan cho Telegram webhook

Neu Telegram goi truc tiep Apps Script bi loi `302 Moved Temporarily`, `401 Unauthorized` hoac `405 Method Not Allowed`, dung URL ngan nay:

```text
https://<render-service>.onrender.com/telegram-webhook
```

Flow:

1. Telegram goi `POST` vao Render relay.
2. Relay giu nguyen body JSON update, query string va header quan trong.
3. Relay forward sang `TELEGRAM_RELAY_TARGET_URL` va follow redirect thay cho Telegram.
4. Apps Script nhan duoc update goc. Neu query co `?bot=buff` / `?bot=uid`, code Apps Script se route dung bot con.

Vi du URL webhook:

- Bot chinh: `https://<render-service>.onrender.com/telegram-webhook`
- Bot buff: `https://<render-service>.onrender.com/telegram-webhook?bot=buff`
- Bot UID: `https://<render-service>.onrender.com/telegram-webhook?bot=uid`
