from fastapi import FastAPI, Header, HTTPException

from app_modules.api.controller import (
    CheckRequest,
    LatestPostRequest,
    RealtimeBulkRequest,
    check_input,
    health_payload,
    latest_post_input,
    realtime_check_bulk,
)
from app_modules.core.config import get_config


app = FastAPI(title="Clean Rebuild UID Checker", version="step3-minimal")


def require_api_key(x_api_key: str | None) -> None:
    config = get_config()
    if config.api_key and x_api_key != config.api_key:
        raise HTTPException(status_code=401, detail="invalid_api_key")


@app.get("/health")
def health() -> dict:
    return health_payload()


@app.post("/check")
def check(req: CheckRequest, x_api_key: str | None = Header(default=None)) -> dict:
    require_api_key(x_api_key)
    return check_input(req)


@app.post("/latest-post")
@app.post("/latest-post/")
@app.post("/checkpost")
def latest_post(req: LatestPostRequest, x_api_key: str | None = Header(default=None)) -> dict:
    require_api_key(x_api_key)
    return latest_post_input(req)


@app.post("/realtime/check-bulk")
@app.post("/realtime/check-bulk/")
def realtime_bulk(req: RealtimeBulkRequest, x_api_key: str | None = Header(default=None)) -> dict:
    require_api_key(x_api_key)
    return realtime_check_bulk(req)
