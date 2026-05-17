# Render Service

Step 3 minimal service:

- `GET /health`
- `POST /check`

Request:

```json
{
  "input": "UID hoặc link Facebook",
  "mode": "all",
  "includeName": true
}
```

Response shape is stable and matches the rebuild contract:

```json
{
  "ok": true,
  "status": "LIVE|DIE|UNKNOWN",
  "confidence": "strong|weak",
  "uid": "",
  "username": "",
  "name": "",
  "canonicalUrl": "",
  "source": "",
  "reason": "",
  "httpCode": 0,
  "elapsedMs": 0,
  "probes": []
}
```

Mode behavior is documented in `99-docs/COMMAND_PARITY.md` and the step checkpoints.

## Local Facebook Cookie File

Real cookies are local/production secrets and must not be committed or written into docs.

For local testing, the cookie loader reads this ignored file by default:

`local_secrets/facebook_cookies.txt`

The expected format is shown in:

`local_secrets/facebook_cookies.example.txt`

Render should prefer environment variables such as `UID_CHECKER_FB_COOKIES_JSON` or `UID_CHECKER_FB_COOKIES_POOL_JSON`.

Run tests:

```powershell
python -m unittest discover -s tests -v
```

Run locally:

```powershell
uvicorn main:app --host 127.0.0.1 --port 8080
```

Production secrets must be read from environment variables, not hardcoded.
