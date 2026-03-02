# UID Checker Service (Python)

Service nay cung cap API check UID Facebook de Apps Script goi sang.

## 1) Cai dat

```bash
cd weblamquoccuong/uid-checker-service
python -m venv .venv
.venv\\Scripts\\activate
pip install -r requirements.txt
```

## 2) Chay local

```bash
set UID_CHECKER_API_KEY=your_secret_key
uvicorn app:app --host 0.0.0.0 --port 8080
```

Health check:

```bash
curl http://127.0.0.1:8080/health
```

Check UID:

```bash
curl -X POST http://127.0.0.1:8080/check ^
  -H "Content-Type: application/json" ^
  -H "X-Api-Key: your_secret_key" ^
  -d "{\"uid\":\"100041775009544\"}"
```

## 3) Noi vao Apps Script

Mo `apps-script/case_manager_bot.gs`, trong `CONFIG`:

- `externalCheckerUrl`: URL public cua service Python (`https://.../check`)
- `externalCheckerApiKey`: key trung voi `UID_CHECKER_API_KEY`

Neu `externalCheckerUrl` de rong, bot se bo qua Python service va dung logic check noi bo.
