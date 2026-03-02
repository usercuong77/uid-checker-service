import asyncio
import os
import re
from typing import Any, Dict, Optional, Tuple
from urllib.parse import parse_qs, urlparse

import aiohttp
from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

try:
    from fake_useragent import UserAgent
except Exception:
    UserAgent = None


APP_NAME = "uid-checker-service"
API_KEY = os.getenv("UID_CHECKER_API_KEY", "").strip()
HTTP_TIMEOUT_SECONDS = float(os.getenv("UID_CHECKER_TIMEOUT", "10"))
_UA = UserAgent() if UserAgent else None


DIE_KEYWORDS = [
    "this content isn't available right now",
    "this content isn't available",
    "the link you followed may be broken",
    "the page you requested cannot be displayed",
    "nội dung này hiện không khả dụng",
    "không tìm thấy trang",
]

CHECKPOINT_KEYWORDS = [
    "tài khoản này đã bị vô hiệu hóa",
    "tài khoản của bạn đã bị vô hiệu hóa",
    "disabled",
    "checkpoint",
    "bị khóa",
]

AUTH_WALL_KEYWORDS = [
    "log in or sign up",
    "log into facebook",
    "đăng nhập hoặc đăng ký",
    "join facebook",
]

# Keep checkpoint detection strict. Generic "checkpoint" is too noisy.
CHECKPOINT_STRONG_KEYWORDS = [
    "your account has been disabled",
    "this account has been disabled",
    "account disabled",
    "we disabled your account",
    "your account has been locked",
    "this account has been locked",
    "security checkpoint",
    "/checkpoint/",
]



class CheckRequest(BaseModel):
    uid: Optional[str] = Field(default=None)
    url: Optional[str] = Field(default=None)
    proxy: Optional[str] = Field(default=None)


app = FastAPI(title=APP_NAME)


def pick_user_agent() -> str:
    if _UA:
        try:
            return _UA.random
        except Exception:
            pass
    return "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36"


def normalize_uid(uid_raw: Optional[str]) -> str:
    uid = str(uid_raw or "").strip()
    return uid if re.fullmatch(r"\d{8,}", uid) else ""


def extract_uid_from_url(url_raw: Optional[str]) -> str:
    url = str(url_raw or "").strip()
    if not url:
        return ""
    if not re.match(r"^https?://", url, re.I):
        url = "https://" + url

    try:
        parsed = urlparse(url)
    except Exception:
        return ""

    host = (parsed.netloc or "").lower().replace("www.", "")
    if "facebook.com" not in host and "fb.com" not in host:
        return ""

    qs = parse_qs(parsed.query or "")
    profile_id = (qs.get("id", [""])[0] or "").strip()
    if re.fullmatch(r"\d{8,}", profile_id):
        return profile_id

    path = (parsed.path or "").strip("/")
    parts = [p for p in path.split("/") if p]
    if not parts:
        return ""

    if parts[0].lower() == "people" and len(parts) >= 3 and re.fullmatch(r"\d{8,}", parts[2]):
        return parts[2]

    if re.fullmatch(r"\d{8,}", parts[0]):
        return parts[0]

    return ""


def contains_any(text: str, keywords: list[str]) -> bool:
    low = text.lower()
    return any(k in low for k in keywords)


def has_checkpoint_signal(text: str) -> bool:
    low = text.lower()
    if contains_any(low, CHECKPOINT_STRONG_KEYWORDS):
        return True

    # "checkpoint" alone can appear in auth/login shells.
    return "checkpoint" in low and (
        "confirm your identity" in low
        or "identity confirmation" in low
        or "account restricted" in low
    )



async def probe_redirect(uid: str, session: aiohttp.ClientSession, headers: Dict[str, str], proxy: Optional[str]) -> Tuple[str, str]:
    source_url = f"https://www.facebook.com/{uid}"
    try:
        async with session.get(source_url, headers=headers, proxy=proxy, allow_redirects=False) as resp:
            code = resp.status
            location = str(resp.headers.get("Location", ""))
            if code in (404, 410):
                return "DIE", f"redirect_http_{code}"
            if code in (301, 302, 303, 307, 308):
                if location:
                    target = urlparse(location if re.match(r"^https?://", location, re.I) else f"https://www.facebook.com{location if location.startswith('/') else '/' + location}")
                    source_path = f"/{uid}"
                    target_path = (target.path or "/") + (target.query and f"?{target.query}" or "")
                    if target_path.lower() != source_path.lower():
                        return "LIVE", f"redirect_changed:{location}"
                    return "DIE", "redirect_same_path"
            # Facebook may return 200 with login shell for both live/die.
            return "UNKNOWN", f"redirect_http_{code}"
    except Exception as err:
        return "UNKNOWN", f"redirect_error:{err}"


async def probe_graph(uid: str, session: aiohttp.ClientSession, headers: Dict[str, str], proxy: Optional[str]) -> Tuple[str, str]:
    url = f"https://graph.facebook.com/{uid}/picture?redirect=false"
    try:
        async with session.get(url, headers=headers, proxy=proxy, allow_redirects=True) as resp:
            code = resp.status
            if code in (404, 410):
                return "DIE", f"graph_http_{code}"

            try:
                payload: Dict[str, Any] = await resp.json(content_type=None)
            except Exception:
                payload = {}
            data = payload.get("data", {}) if isinstance(payload, dict) else {}
            img_url = str(data.get("url", ""))
            has_height = int(data.get("height", 0) or 0) > 0
            is_default = "static.xx.fbcdn.net/rsrc.php" in img_url.lower()

            if not has_height and is_default:
                return "DIE", f"graph_default_avatar:{img_url or '-'}"
            if has_height:
                return "LIVE", f"graph_has_height:{img_url or '-'}"
            return "UNKNOWN", f"graph_uncertain:{img_url or '-'}"
    except Exception as err:
        return "UNKNOWN", f"graph_error:{err}"


async def probe_mbasic(uid: str, session: aiohttp.ClientSession, headers: Dict[str, str], proxy: Optional[str]) -> Tuple[str, str]:
    url = f"https://mbasic.facebook.com/profile.php?id={uid}"
    try:
        async with session.get(url, headers=headers, proxy=proxy, allow_redirects=True) as resp:
            html = await resp.text(errors="ignore")
            if contains_any(html, AUTH_WALL_KEYWORDS):
                return "UNKNOWN", "mbasic_auth_wall"
            if has_checkpoint_signal(html):
                return "CHECKPOINT", "mbasic_checkpoint_strong"
            if contains_any(html, DIE_KEYWORDS):
                return "DIE", "mbasic_die_keyword"

            profile_markers = [
                "profile.php?id=",
                "/photo.php",
                "timeline",
                "friends",
                "message",
            ]
            if contains_any(html, profile_markers):
                return "LIVE", "mbasic_profile_marker"
            return "UNKNOWN", "mbasic_uncertain"
    except Exception as err:
        return "UNKNOWN", f"mbasic_error:{err}"


async def check_uid(uid: str, proxy: Optional[str] = None) -> Dict[str, Any]:
    timeout = aiohttp.ClientTimeout(total=HTTP_TIMEOUT_SECONDS)
    headers = {
        "User-Agent": pick_user_agent(),
        "Accept": "text/html,application/xhtml+xml,application/json",
        "Accept-Language": "vi-VN,vi;q=0.9,en-US;q=0.8,en;q=0.7",
        "Referer": "https://mbasic.facebook.com/",
    }

    async with aiohttp.ClientSession(timeout=timeout) as session:
        redirect_task = probe_redirect(uid, session, headers, proxy)
        graph_task = probe_graph(uid, session, headers, proxy)
        mbasic_task = probe_mbasic(uid, session, headers, proxy)
        redirect_signal, graph_signal, mbasic_signal = await asyncio.gather(
            redirect_task, graph_task, mbasic_task
        )

    redirect_state, redirect_reason = redirect_signal
    graph_state, graph_reason = graph_signal
    mbasic_state, mbasic_reason = mbasic_signal

    status = "UNKNOWN"
    reason = "no_strong_signal"

    # Prioritize strong LIVE signals first to avoid false CHECKPOINT from mbasic login shells.
    if redirect_state == "LIVE":
        status = "LIVE"
        reason = redirect_reason
    elif graph_state == "LIVE":
        status = "LIVE"
        reason = graph_reason
    elif mbasic_state == "LIVE":
        status = "LIVE"
        reason = mbasic_reason
    elif mbasic_state == "CHECKPOINT":
        status = "CHECKPOINT"
        reason = mbasic_reason
    elif mbasic_state == "DIE":
        status = "DIE"
        reason = mbasic_reason
    elif redirect_state == "DIE" and graph_state == "DIE":
        status = "DIE"
        reason = f"{redirect_reason}|{graph_reason}"
    elif graph_state == "DIE" and mbasic_state != "LIVE":
        status = "DIE"
        reason = graph_reason

    return {
        "uid": uid,
        "status": status,
        "reason": reason,
        "httpCode": 200,
        "signals": {
            "redirect": {"status": redirect_state, "reason": redirect_reason},
            "graph": {"status": graph_state, "reason": graph_reason},
            "mbasic": {"status": mbasic_state, "reason": mbasic_reason},
        },
    }


def ensure_api_key(x_api_key: Optional[str]) -> None:
    if API_KEY and x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="invalid_api_key")


@app.get("/health")
async def health() -> Dict[str, Any]:
    return {"ok": True, "service": APP_NAME}


@app.post("/check")
async def check(req: CheckRequest, x_api_key: Optional[str] = Header(default=None)) -> Dict[str, Any]:
    ensure_api_key(x_api_key)

    uid = normalize_uid(req.uid) or extract_uid_from_url(req.url)
    if not uid:
        return {
            "uid": "",
            "status": "UNKNOWN",
            "reason": "invalid_uid",
            "httpCode": 0,
        }

    result = await check_uid(uid, req.proxy)
    result["ok"] = True
    return result
