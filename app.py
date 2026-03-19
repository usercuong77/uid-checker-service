import asyncio
import json
import os
import re
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, urlencode, urlparse

import aiohttp
from fastapi import FastAPI, Header, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

try:
    from fake_useragent import UserAgent
except Exception:
    UserAgent = None

try:
    from bs4 import BeautifulSoup
except Exception:
    BeautifulSoup = None

try:
    from TikTokLive import TikTokLiveClient
except Exception:
    TikTokLiveClient = None

# Playwright is intentionally optional. If not installed, IG live checks will
# fall back to lightweight HTTP parsing to keep Render builds simple.
try:
    from playwright.async_api import async_playwright
except Exception:
    async_playwright = None


APP_NAME = "uid-checker-service"
API_KEY = os.getenv("UID_CHECKER_API_KEY", "").strip()
HTTP_TIMEOUT_SECONDS = float(os.getenv("UID_CHECKER_TIMEOUT", "10"))
LIVE_CHECK_DEFAULT_CONCURRENCY = int(os.getenv("LIVE_CHECK_CONCURRENCY", "25"))
LIVE_CHECK_PAGE_TIMEOUT_MS = int(os.getenv("LIVE_CHECK_TIMEOUT_MS", "15000"))
UID_PROBE_UA_FILE = os.getenv("UID_PROBE_UA_FILE", "uid_probe_user_agents.txt").strip()
UID_PROBE_ACCEPT_LANGUAGE = os.getenv("UID_PROBE_ACCEPT_LANGUAGE", "en-US,en;q=0.9,vi;q=0.8").strip()
_UA = UserAgent() if UserAgent else None
FORWARDED_SEPAY_HEADERS = {
    "authorization",
    "content-type",
    "user-agent",
    "x-api-key",
}


DIE_KEYWORDS = [
    "this content isn't available at the moment",
    "this content isn't available right now",
    "this content isn't available",
    "this page isn't available",
    "page not found",
    "the link you followed may be broken",
    "couldn't find this page",
    "requested page could not be found",
    "this profile isn't available",
    "content unavailable",
    "we couldn't find this content",
    "noi dung nay hien khong kha dung",
    "trang nay hien khong co san",
    "khong tim thay trang",
]

AUTH_WALL_KEYWORDS = [
    "log in or sign up",
    "login or signup",
    "log into facebook",
    "log in to facebook",
    "email address or phone number",
    "email or phone",
    "password",
    "forgotten account",
    "create new account",
    "join facebook",
    "dang nhap hoac dang ky",
    "dang nhap facebook",
]

CHECKPOINT_STRONG_KEYWORDS = [
    "your account has been disabled",
    "this account has been disabled",
    "account disabled",
    "we disabled your account",
    "your account has been locked",
    "this account has been locked",
    "security checkpoint",
    "/checkpoint/",
    "tai khoan nay da bi vo hieu hoa",
    "tai khoan cua ban da bi vo hieu hoa",
    "bi khoa",
]

PROFILE_LIVE_MARKERS = [
    'profile_id":"',
    'entity_id":"',
    "fb://profile/",
    "fb://page/",
    "timeline",
    "friends",
    "message",
    "profile.php?id=",
]

PROFILE_NAME_BLOCKLIST = [
    "facebook",
    "log in",
    "login",
    "sign up",
    "dang nhap",
    "tao tai khoan",
    "create new account",
    "forgot password",
    "quen mat khau",
    "meta",
]

UID_SCRAPE_PATTERNS = [
    r'"userID"\s*:\s*"(\d{8,20})"',
    r'"profile_id"\s*:\s*(\d{8,20})',
    r'"entity_id"\s*:\s*"(\d{8,20})"',
    r'"actorID"\s*:\s*"(\d{8,20})"',
    r'"subject_id"\s*:\s*"(\d{8,20})"',
    r'profile\.php\?id=(\d{8,20})',
    r'fb://profile/(\d{8,20})',
]


class CheckRequest(BaseModel):
    uid: Optional[str] = Field(default=None)
    url: Optional[str] = Field(default=None)
    proxy: Optional[str] = Field(default=None)
    cookies: Optional[Dict[str, str]] = Field(default=None)
    cookiesPool: Optional[List[Dict[str, str]]] = Field(default=None)
    cookies_pool: Optional[List[Dict[str, str]]] = Field(default=None)


class LiveCheckRequest(BaseModel):
    platform: Optional[str] = Field(default=None)
    usernames: Optional[List[str]] = Field(default=None)
    proxy: Optional[str] = Field(default=None)
    proxies: Optional[List[str]] = Field(default=None)
    concurrency: Optional[int] = Field(default=None)


app = FastAPI(title=APP_NAME)


def get_sepay_relay_target_url() -> str:
    return str(os.getenv("SEPAY_RELAY_TARGET_URL", "")).strip()


def get_sepay_relay_timeout_seconds() -> float:
    raw = str(os.getenv("SEPAY_RELAY_TIMEOUT", "20")).strip()
    try:
        value = float(raw)
    except Exception:
        value = 20.0
    return max(1.0, value)


def build_forward_url(base_url: str, query_string: str = "") -> str:
    target = str(base_url or "").strip()
    if not target:
        return ""

    query = str(query_string or "").lstrip("?").strip()
    if not query:
        return target

    separator = "&" if "?" in target else "?"
    return f"{target}{separator}{query}"


def normalize_sepay_api_key_value(raw: Any) -> str:
    value = str(raw or "").strip()
    if not value:
        return ""

    match = re.match(r"^apikey\s+(.+)$", value, flags=re.IGNORECASE)
    return str(match.group(1) if match else value).strip()


def augment_query_string_with_sepay_key(query_string: str = "", headers: Optional[Dict[str, str]] = None) -> str:
    params = parse_qs(str(query_string or "").lstrip("?"), keep_blank_values=True)
    if params.get("sepay_key") or params.get("api_key"):
        return urlencode(params, doseq=True)

    normalized_headers = headers or {}
    api_key = normalize_sepay_api_key_value(
        normalized_headers.get("Authorization")
        or normalized_headers.get("authorization")
        or normalized_headers.get("X-Api-Key")
        or normalized_headers.get("x-api-key")
    )
    if not api_key:
        return urlencode(params, doseq=True)

    params["sepay_key"] = [api_key]
    return urlencode(params, doseq=True)


def get_forwardable_sepay_headers(headers: Any) -> Dict[str, str]:
    out: Dict[str, str] = {}
    if not headers:
        return out

    items = headers.items() if hasattr(headers, "items") else []
    for key, value in items:
        header_name = str(key or "").strip()
        if not header_name or header_name.lower() not in FORWARDED_SEPAY_HEADERS:
            continue
        out[header_name] = str(value or "").strip()
    return out


async def forward_sepay_webhook(
    method: str,
    target_url: str,
    body: bytes,
    headers: Optional[Dict[str, str]] = None,
    query_string: str = "",
) -> Dict[str, Any]:
    safe_headers = headers or {}
    upstream_query = augment_query_string_with_sepay_key(query_string, safe_headers)
    upstream_url = build_forward_url(target_url, upstream_query)
    if not upstream_url:
        raise HTTPException(status_code=503, detail="sepay_relay_target_missing")

    timeout = aiohttp.ClientTimeout(total=get_sepay_relay_timeout_seconds())
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.request(
                (method or "POST").upper(),
                upstream_url,
                data=body,
                headers=safe_headers,
                allow_redirects=True,
            ) as resp:
                return {
                    "status_code": int(resp.status),
                    "body": await resp.read(),
                    "content_type": str(resp.headers.get("Content-Type", "application/json; charset=utf-8")),
                }
    except HTTPException:
        raise
    except asyncio.TimeoutError as err:
        raise HTTPException(status_code=504, detail=f"sepay_relay_timeout:{err}") from err
    except Exception as err:
        raise HTTPException(status_code=502, detail=f"sepay_relay_error:{err}") from err


def parse_cookie_json(raw: str) -> Dict[str, str]:
    value = str(raw or "").strip()
    if not value:
        return {}

    try:
        payload = json.loads(value)
    except Exception:
        return {}

    if not isinstance(payload, dict):
        return {}

    cleaned: Dict[str, str] = {}
    for key, val in payload.items():
        ck = str(key or "").strip()
        cv = str(val or "").strip()
        if ck and cv:
            cleaned[ck] = cv

    return cleaned


def normalize_social_username(raw: Any) -> str:
    value = str(raw or "").strip()
    if not value:
        return ""
    return value[1:] if value.startswith("@") else value


def normalize_url_input(raw: Any) -> str:
    value = str(raw or "").strip()
    if not value:
        return ""
    if value.startswith("http://") or value.startswith("https://"):
        return value
    return f"https://{value}"


def load_uid_probe_user_agents(file_path_raw: Optional[str] = None) -> List[str]:
    raw_path = str(file_path_raw or UID_PROBE_UA_FILE or "").strip()
    if not raw_path:
        return []

    file_path = raw_path
    if not os.path.isabs(file_path):
        file_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), file_path)

    try:
        with open(file_path, "r", encoding="utf-8") as handle:
            rows = handle.readlines()
    except Exception:
        return []

    out: List[str] = []
    seen = set()
    for row in rows:
        value = str(row or "").strip()
        if not value or value.startswith("#"):
            continue
        key = value.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(value)
    return out


def extract_tiktok_username(raw: Any) -> str:
    value = normalize_social_username(raw)
    if not value:
        return ""
    if "tiktok.com" in value.lower():
        try:
            parsed = urlparse(normalize_url_input(value))
            match = re.search(r"@([^/]+)", parsed.path or "")
            return str(match.group(1)).strip() if match else ""
        except Exception:
            match = re.search(r"tiktok\\.com/@([^/?#]+)", value, flags=re.IGNORECASE)
            return str(match.group(1)).strip() if match else ""
    return value


def extract_instagram_username(raw: Any) -> str:
    value = normalize_social_username(raw)
    if not value:
        return ""
    if "instagram.com" in value.lower():
        try:
            parsed = urlparse(normalize_url_input(value))
            path = str(parsed.path or "").strip("/")
            first = path.split("/")[0] if path else ""
            reserved = {
                "",
                "accounts",
                "explore",
                "reel",
                "reels",
                "p",
                "tv",
                "stories",
                "direct",
                "developer",
                "about",
                "legal",
            }
            return first if first and first.lower() not in reserved else ""
        except Exception:
            match = re.search(r"instagram\\.com/([^/?#]+)", value, flags=re.IGNORECASE)
            return str(match.group(1)).strip() if match else ""
    return value


def normalize_live_usernames(usernames: Optional[List[Any]], platform: str) -> List[str]:
    items = usernames if isinstance(usernames, list) else []
    out: List[str] = []
    seen: Dict[str, bool] = {}
    for raw in items:
        username = extract_tiktok_username(raw) if platform == "tiktok" else extract_instagram_username(raw)
        normalized = normalize_social_username(username)
        if not normalized or normalized in seen:
            continue
        seen[normalized] = True
        out.append(normalized)
    return out


def pick_live_concurrency(value: Optional[int]) -> int:
    try:
        parsed = int(value or 0)
    except Exception:
        parsed = 0
    if parsed <= 0:
        parsed = LIVE_CHECK_DEFAULT_CONCURRENCY
    return max(1, min(60, parsed))


def normalize_proxy_pool(proxy: Optional[str], proxies: Optional[List[str]]) -> List[Optional[str]]:
    pool: List[Optional[str]] = []
    if proxy:
        pool.append(str(proxy).strip())
    if proxies:
        for item in proxies:
            val = str(item or "").strip()
            if val:
                pool.append(val)
    return pool


def attach_error_result(username: str, reason: str) -> Dict[str, Any]:
    return {"username": username, "is_live": False, "status": "unknown", "reason": reason}


async def check_tiktok_single(username: str) -> Dict[str, Any]:
    name = normalize_social_username(username)
    if not name:
        return attach_error_result(username, "empty_username")
    if TikTokLiveClient is None:
        return attach_error_result(name, "tiktoklive_missing")

    client = None
    try:
        client = TikTokLiveClient(unique_id=name)
        is_live = await client.is_live()
        if not is_live:
            return {"username": name, "is_live": False, "viewer": 0, "room_id": None, "status": "offline"}

        info = await client.get_room_info()
        viewer_count = int(info.get("viewer_count", 0)) if isinstance(info, dict) else 0
        return {
            "username": name,
            "is_live": True,
            "viewer": max(0, viewer_count),
            "room_id": getattr(client, "room_id", None),
            "status": "live",
        }
    except Exception as err:
        return attach_error_result(name, f"tiktok_error:{err}")
    finally:
        if client and hasattr(client, "close"):
            try:
                result = client.close()
                if asyncio.iscoroutine(result):
                    await result
            except Exception:
                pass


async def bulk_check_tiktok(usernames: List[str]) -> List[Dict[str, Any]]:
    tasks = [check_tiktok_single(name) for name in usernames]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    out: List[Dict[str, Any]] = []
    for item, name in zip(results, usernames):
        if isinstance(item, Exception):
            out.append(attach_error_result(name, f"tiktok_error:{item}"))
        else:
            out.append(item)
    return out


def parse_instagram_live_from_html(html: str) -> Optional[bool]:
    if not html:
        return None
    low = html.lower()
    if '"is_live":true' in low or '"is_live_broadcasting":true' in low:
        return True
    if '"is_live":false' in low or '"is_live_broadcasting":false' in low:
        return False
    return None


async def fetch_instagram_live_status(
    username: str,
    session: aiohttp.ClientSession,
    proxy: Optional[str],
) -> Dict[str, Any]:
    name = normalize_social_username(username)
    if not name:
        return attach_error_result(username, "empty_username")

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "application/json,text/html;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9,vi;q=0.8",
        "Referer": f"https://www.instagram.com/{name}/",
        "X-IG-App-ID": "936619743392459",
    }

    endpoints = [
        f"https://i.instagram.com/api/v1/users/web_profile_info/?username={name}",
        f"https://www.instagram.com/api/v1/users/web_profile_info/?username={name}",
        f"https://www.instagram.com/{name}/?__a=1&__d=dis",
    ]

    last_error = ""
    for url in endpoints:
        try:
            async with session.get(url, headers=headers, proxy=proxy, allow_redirects=True) as resp:
                text = await resp.text(errors="ignore")
                if resp.status != 200:
                    last_error = f"http_{resp.status}"
                    continue

                # Prefer JSON if possible.
                payload = None
                try:
                    payload = await resp.json(content_type=None)
                except Exception:
                    payload = None

                if isinstance(payload, dict):
                    data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
                    user = data.get("user") if isinstance(data, dict) else None
                    if isinstance(user, dict):
                        is_live = bool(
                            user.get("is_live")
                            or user.get("is_live_broadcasting")
                            or user.get("live_broadcasting")
                        )
                        return {
                            "username": name,
                            "is_live": is_live,
                            "status": "live" if is_live else "offline",
                        }

                html_live = parse_instagram_live_from_html(text)
                if html_live is not None:
                    return {
                        "username": name,
                        "is_live": bool(html_live),
                        "status": "live" if html_live else "offline",
                    }
        except Exception as err:
            last_error = f"ig_error:{err}"

    return attach_error_result(name, last_error or "ig_fetch_failed")


async def check_instagram_single_http(
    username: str,
    session: aiohttp.ClientSession,
    semaphore: asyncio.Semaphore,
    proxy: Optional[str],
) -> Dict[str, Any]:
    name = normalize_social_username(username)
    if not name:
        return attach_error_result(username, "empty_username")
    async with semaphore:
        return await fetch_instagram_live_status(name, session, proxy)


async def bulk_check_instagram(usernames: List[str], proxies: List[Optional[str]], concurrency: int) -> List[Dict[str, Any]]:
    proxy_pool = proxies or [None]
    if not proxy_pool:
        proxy_pool = [None]
    semaphore = asyncio.Semaphore(concurrency)
    timeout_sec = max(5.0, LIVE_CHECK_PAGE_TIMEOUT_MS / 1000.0)
    timeout = aiohttp.ClientTimeout(total=timeout_sec)

    async with aiohttp.ClientSession(timeout=timeout) as session:
        tasks = []
        for idx, name in enumerate(usernames):
            proxy = proxy_pool[idx % len(proxy_pool)]
            tasks.append(check_instagram_single_http(name, session, semaphore, proxy))

        bucket_results = await asyncio.gather(*tasks, return_exceptions=True)
        out: List[Dict[str, Any]] = []
        for item, name in zip(bucket_results, usernames):
            if isinstance(item, Exception):
                out.append(attach_error_result(name, f"ig_error:{item}"))
            else:
                out.append(item)
        return out


def normalize_cookies(cookies: Optional[Dict[str, str]]) -> Dict[str, str]:
    if not isinstance(cookies, dict):
        return {}

    out: Dict[str, str] = {}
    for key, val in cookies.items():
        ck = str(key or "").strip()
        cv = str(val or "").strip()
        if ck and cv:
            out[ck] = cv
    return out


def parse_cookie_pool_json(raw: str) -> List[Dict[str, str]]:
    value = str(raw or "").strip()
    if not value:
        return []

    try:
        payload = json.loads(value)
    except Exception:
        return []

    if not isinstance(payload, list):
        return []

    pool: List[Dict[str, str]] = []
    for item in payload:
        cookies = normalize_cookies(item if isinstance(item, dict) else None)
        if cookies:
            pool.append(cookies)
    return pool


def normalize_cookie_pool(cookies_pool: Optional[List[Dict[str, str]]]) -> List[Dict[str, str]]:
    if not isinstance(cookies_pool, list):
        return []

    pool: List[Dict[str, str]] = []
    for item in cookies_pool:
        cookies = normalize_cookies(item if isinstance(item, dict) else None)
        if cookies:
            pool.append(cookies)
    return pool


def load_default_cookies() -> Dict[str, str]:
    # Primary: JSON in one env var (recommended).
    raw_json = os.getenv("UID_CHECKER_FB_COOKIES_JSON", "").strip() or os.getenv("FB_COOKIES_JSON", "").strip()
    cookies = parse_cookie_json(raw_json)

    # Secondary: allow direct c_user/xs env vars.
    c_user = os.getenv("UID_CHECKER_FB_C_USER", "").strip()
    xs = os.getenv("UID_CHECKER_FB_XS", "").strip()
    if c_user:
        cookies["c_user"] = c_user
    if xs:
        cookies["xs"] = xs

    return cookies


def load_default_cookie_pool() -> List[Dict[str, str]]:
    raw_json = os.getenv("UID_CHECKER_FB_COOKIES_POOL_JSON", "").strip() or os.getenv("FB_COOKIES_POOL_JSON", "").strip()
    return parse_cookie_pool_json(raw_json)


DEFAULT_FB_COOKIES = load_default_cookies()
DEFAULT_FB_COOKIE_POOL = load_default_cookie_pool()


def pick_user_agent() -> str:
    if _UA:
        try:
            return _UA.random
        except Exception:
            pass
    return (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/133.0.0.0 Safari/537.36"
    )


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


def extract_uid_from_html(html_raw: Any) -> str:
    html = str(html_raw or "")
    if not html:
        return ""

    normalized = (
        html.replace("\\/", "/")
        .replace("\\u002f", "/")
        .replace("\\u003a", ":")
        .replace("&quot;", '"')
    )

    for pattern in UID_SCRAPE_PATTERNS:
        match = re.search(pattern, normalized, flags=re.I)
        if not match:
            continue
        uid = str(match.group(1) if match.groups() else "").strip()
        if re.fullmatch(r"\d{8,20}", uid):
            return uid
    return ""


def build_facebook_probe_urls(url_raw: Any) -> List[str]:
    normalized = normalize_url_input(url_raw)
    if not normalized:
        return []

    urls: List[str] = [normalized]
    try:
        parsed = urlparse(normalized)
        host = (parsed.netloc or "").lower()
        if "facebook.com" in host or "fb.com" in host:
            path = parsed.path or "/"
            query = f"?{parsed.query}" if parsed.query else ""
            urls.append(f"https://m.facebook.com{path}{query}")
            urls.append(f"https://www.facebook.com{path}{query}")
    except Exception:
        pass

    out: List[str] = []
    seen = set()
    for item in urls:
        key = str(item or "").strip()
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(key)
    return out


FALLBACK_UID_PROBE_USER_AGENTS = [
    "Mozilla/5.0",
    "facebookexternalhit/1.1 (+http://www.facebook.com/externalhit_uatext.php)",
    (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
]
FILE_UID_PROBE_USER_AGENTS = load_uid_probe_user_agents()


def build_uid_probe_header_candidates() -> List[Dict[str, str]]:
    accept_language = UID_PROBE_ACCEPT_LANGUAGE or "en-US,en;q=0.9,vi;q=0.8"
    user_agents = FILE_UID_PROBE_USER_AGENTS + FALLBACK_UID_PROBE_USER_AGENTS
    candidates: List[Dict[str, str]] = [
        {
            "User-Agent": ua,
            "Accept-Language": accept_language,
        }
        for ua in user_agents
    ]

    out: List[Dict[str, str]] = []
    seen = set()
    for item in candidates:
        key = f"{item.get('User-Agent', '').strip().lower()}|{item.get('Accept-Language', '').strip().lower()}"
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out


async def resolve_uid_from_facebook_url(url_raw: Any, proxy: Optional[str] = None) -> str:
    normalized = normalize_url_input(url_raw)
    direct_uid = extract_uid_from_url(normalized)
    if direct_uid:
        return direct_uid

    probe_urls = build_facebook_probe_urls(normalized)
    if not probe_urls:
        return ""

    header_candidates = build_uid_probe_header_candidates()

    timeout = aiohttp.ClientTimeout(total=max(5.0, HTTP_TIMEOUT_SECONDS))
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            for headers in header_candidates:
                for probe_url in probe_urls:
                    try:
                        async with session.get(
                            probe_url,
                            headers=headers,
                            proxy=proxy,
                            allow_redirects=True,
                        ) as resp:
                            body = await resp.text(errors="ignore")
                            uid_from_html = extract_uid_from_html(body)
                            if uid_from_html:
                                return uid_from_html

                            uid_from_final_url = extract_uid_from_url(str(resp.url))
                            if uid_from_final_url:
                                return uid_from_final_url
                    except Exception:
                        continue
    except Exception:
        return ""

    return ""


def contains_any(text: str, keywords: list[str]) -> bool:
    low = text.lower()
    return any(k in low for k in keywords)


def has_checkpoint_signal(text: str) -> bool:
    low = text.lower()
    if contains_any(low, CHECKPOINT_STRONG_KEYWORDS):
        return True

    return "checkpoint" in low and (
        "confirm your identity" in low
        or "identity confirmation" in low
        or "account restricted" in low
    )


def is_auth_wall(text: str, final_url: str = "") -> bool:
    low = text.lower()
    if contains_any(low, AUTH_WALL_KEYWORDS):
        return True

    url_low = (final_url or "").lower()
    return (
        "/login" in url_low
        or "/checkpoint" in url_low
        or "/recover" in url_low
        or "/security" in url_low
        or "/accounts" in url_low
    )


def is_valid_profile_name(raw_name: str) -> bool:
    name = re.sub(r"\s+", " ", str(raw_name or "")).strip()
    if len(name) < 2 or len(name) > 80:
        return False

    low = name.lower()
    if contains_any(low, PROFILE_NAME_BLOCKLIST):
        return False

    return bool(re.search(r"[A-Za-zÀ-ỹ]", name))


def extract_profile_name(html: str) -> str:
    if not html:
        return ""

    candidates: list[str] = []

    if BeautifulSoup:
        try:
            soup = BeautifulSoup(html, "html.parser")
            og_title = soup.find("meta", attrs={"property": "og:title"})
            if og_title and og_title.get("content"):
                candidates.append(str(og_title.get("content")))

            for tag_name in ["h1", "title", "strong"]:
                for node in soup.find_all(tag_name, limit=3):
                    text = node.get_text(" ", strip=True)
                    if text:
                        candidates.append(text)
        except Exception:
            pass
    else:
        title_match = re.search(r"<title[^>]*>(.*?)</title>", html, flags=re.I | re.S)
        if title_match:
            candidates.append(re.sub(r"\s+", " ", title_match.group(1)).strip())

    for candidate in candidates:
        clean = re.sub(r"\s+", " ", candidate).strip()
        if is_valid_profile_name(clean):
            return clean

    return ""


async def probe_redirect(
    uid: str,
    session: aiohttp.ClientSession,
    headers: Dict[str, str],
    proxy: Optional[str],
) -> Tuple[str, str]:
    source_url = f"https://www.facebook.com/{uid}"
    try:
        async with session.get(source_url, headers=headers, proxy=proxy, allow_redirects=False) as resp:
            code = resp.status
            location = str(resp.headers.get("Location", ""))

            if code in (404, 410):
                return "DIE", f"redirect_http_{code}"

            if code in (301, 302, 303, 307, 308) and location:
                target = urlparse(
                    location if re.match(r"^https?://", location, re.I)
                    else f"https://www.facebook.com{location if location.startswith('/') else '/' + location}"
                )
                source_path = f"/{uid}"
                target_path = (target.path or "/") + (target.query and f"?{target.query}" or "")
                target_path_low = target_path.lower()

                if is_auth_wall("", target_path_low):
                    return "UNKNOWN", f"redirect_auth_wall:{location}"

                if target_path_low != source_path.lower():
                    return "LIVE", f"redirect_changed:{location}"
                return "DIE", "redirect_same_path"

            return "UNKNOWN", f"redirect_http_{code}"
    except Exception as err:
        return "UNKNOWN", f"redirect_error:{err}"


async def probe_graph(
    uid: str,
    session: aiohttp.ClientSession,
    headers: Dict[str, str],
    proxy: Optional[str],
) -> Tuple[str, str]:
    url = f"https://graph.facebook.com/{uid}/picture?type=large&redirect=false"
    try:
        async with session.get(url, headers=headers, proxy=proxy, allow_redirects=True) as resp:
            code = resp.status
            if code in (404, 410):
                return "DIE", f"graph_http_{code}"

            try:
                payload: Dict[str, Any] = await resp.json(content_type=None)
            except Exception:
                payload = {}

            if isinstance(payload, dict) and payload.get("error"):
                err_obj = payload.get("error") or {}
                err_msg = str(err_obj.get("message", "")).lower()
                if "unsupported get request" in err_msg or "cannot be loaded due to missing permissions" in err_msg:
                    return "DIE", f"graph_error_die:{err_msg or 'unsupported_get_request'}"
                return "UNKNOWN", f"graph_error:{err_msg or 'unknown'}"

            data = payload.get("data", {}) if isinstance(payload, dict) else {}
            img_url = str(data.get("url", ""))
            img_url_low = img_url.lower()
            has_height = int(data.get("height", 0) or 0) > 0
            is_silhouette = data.get("is_silhouette")
            is_default_avatar = "static.xx.fbcdn.net/rsrc.php" in img_url_low

            if is_default_avatar:
                return "DIE", f"graph_default_avatar:{img_url or '-'}"

            if has_height and is_silhouette is False:
                return "LIVE", f"graph_not_silhouette:{img_url or '-'}"
            if has_height and is_silhouette is True:
                return "UNKNOWN", f"graph_silhouette:{img_url or '-'}"
            if has_height:
                return "LIVE", f"graph_has_height:{img_url or '-'}"

            return "UNKNOWN", f"graph_uncertain:{img_url or '-'}"
    except Exception as err:
        return "UNKNOWN", f"graph_error:{err}"


async def probe_public_page(
    source: str,
    url: str,
    session: aiohttp.ClientSession,
    headers: Dict[str, str],
    proxy: Optional[str],
) -> Dict[str, str]:
    try:
        async with session.get(url, headers=headers, proxy=proxy, allow_redirects=True) as resp:
            code = resp.status
            final_url = str(resp.url)
            html = await resp.text(errors="ignore")
            html_low = html.lower()

            if code in (404, 410):
                return {"status": "DIE", "reason": f"{source}_http_{code}", "name": "", "finalUrl": final_url}

            if contains_any(html_low, DIE_KEYWORDS):
                return {"status": "DIE", "reason": f"{source}_die_keyword", "name": "", "finalUrl": final_url}

            if has_checkpoint_signal(html_low):
                return {"status": "CHECKPOINT", "reason": f"{source}_checkpoint", "name": "", "finalUrl": final_url}

            if is_auth_wall(html_low, final_url):
                return {"status": "UNKNOWN", "reason": f"{source}_auth_wall", "name": "", "finalUrl": final_url}

            profile_name = extract_profile_name(html)
            if profile_name:
                return {
                    "status": "LIVE",
                    "reason": f"{source}_profile_name",
                    "name": profile_name,
                    "finalUrl": final_url,
                }

            if contains_any(html_low, PROFILE_LIVE_MARKERS):
                return {"status": "LIVE", "reason": f"{source}_profile_marker", "name": "", "finalUrl": final_url}

            return {"status": "UNKNOWN", "reason": f"{source}_uncertain", "name": "", "finalUrl": final_url}
    except asyncio.TimeoutError:
        return {"status": "UNKNOWN", "reason": f"{source}_timeout", "name": "", "finalUrl": ""}
    except Exception as err:
        return {"status": "UNKNOWN", "reason": f"{source}_error:{err}", "name": "", "finalUrl": ""}


def cookie_fingerprint(cookies: Dict[str, str]) -> str:
    if not cookies:
        return "__empty__"
    return "|".join(f"{key}={cookies[key]}" for key in sorted(cookies.keys()))


def build_cookie_candidates(
    cookies: Optional[Dict[str, str]],
    cookies_pool: Optional[List[Dict[str, str]]],
) -> List[Dict[str, Any]]:
    raw_candidates: List[Dict[str, Any]] = []

    request_cookies = normalize_cookies(cookies)
    if request_cookies:
        raw_candidates.append({"source": "request_cookie", "cookies": request_cookies})

    request_pool = normalize_cookie_pool(cookies_pool)
    for idx, pool_cookies in enumerate(request_pool):
        raw_candidates.append({"source": f"request_pool_{idx + 1}", "cookies": pool_cookies})

    for idx, pool_cookies in enumerate(DEFAULT_FB_COOKIE_POOL):
        raw_candidates.append({"source": f"env_pool_{idx + 1}", "cookies": dict(pool_cookies)})

    if DEFAULT_FB_COOKIES:
        raw_candidates.append({"source": "env_default", "cookies": dict(DEFAULT_FB_COOKIES)})

    candidates: List[Dict[str, Any]] = []
    seen = set()

    for item in raw_candidates:
        source = str(item.get("source") or "cookie")
        candidate_cookies = normalize_cookies(item.get("cookies"))
        if not candidate_cookies:
            continue
        fingerprint = cookie_fingerprint(candidate_cookies)
        if fingerprint in seen:
            continue
        seen.add(fingerprint)
        candidates.append({"source": source, "cookies": candidate_cookies})

    if not candidates:
        candidates.append({"source": "no_cookie", "cookies": {}})
    return candidates


def should_try_next_cookie(result: Dict[str, Any]) -> bool:
    status = str(result.get("status", "")).upper()
    if status == "CHECKPOINT":
        return True
    if status != "UNKNOWN":
        return False

    def has_cookie_failure_reason(reason_raw: Any) -> bool:
        reason = str(reason_raw or "").lower()
        if not reason:
            return False
        return (
            "auth_wall" in reason
            or "checkpoint" in reason
            or "login" in reason
            or "security" in reason
        )

    if has_cookie_failure_reason(result.get("reason")):
        return True

    signals = result.get("signals")
    if not isinstance(signals, dict):
        return False

    for key in ("redirect", "graph", "mbasic", "m", "touch"):
        signal = signals.get(key)
        if isinstance(signal, dict) and has_cookie_failure_reason(signal.get("reason")):
            return True

    return False


async def check_uid_once(
    uid: str,
    proxy: Optional[str] = None,
    session_cookies: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    timeout = aiohttp.ClientTimeout(total=HTTP_TIMEOUT_SECONDS)
    headers = {
        "User-Agent": pick_user_agent(),
        "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
        "Accept-Language": "vi-VN,vi;q=0.9,en-US;q=0.8,en;q=0.7",
        "Referer": "https://mbasic.facebook.com/",
    }

    mbasic_url = f"https://mbasic.facebook.com/profile.php?id={uid}"
    m_url = f"https://m.facebook.com/profile.php?id={uid}"
    touch_url = f"https://touch.facebook.com/profile.php?id={uid}"
    normalized_session_cookies = normalize_cookies(session_cookies)

    async with aiohttp.ClientSession(timeout=timeout, cookies=normalized_session_cookies) as session:
        redirect_task = probe_redirect(uid, session, headers, proxy)
        graph_task = probe_graph(uid, session, headers, proxy)
        mbasic_task = probe_public_page("mbasic", mbasic_url, session, headers, proxy)
        m_task = probe_public_page("m", m_url, session, headers, proxy)
        touch_task = probe_public_page("touch", touch_url, session, headers, proxy)

        redirect_signal, graph_signal, mbasic_signal, m_signal, touch_signal = await asyncio.gather(
            redirect_task,
            graph_task,
            mbasic_task,
            m_task,
            touch_task,
        )

    redirect_state, redirect_reason = redirect_signal
    graph_state, graph_reason = graph_signal
    public_signals = [mbasic_signal, m_signal, touch_signal]

    live_public = [item for item in public_signals if item.get("status") == "LIVE"]
    die_public = [item for item in public_signals if item.get("status") == "DIE"]
    checkpoint_public = [item for item in public_signals if item.get("status") == "CHECKPOINT"]

    status = "UNKNOWN"
    reason = "no_strong_signal"

    # Priority 1: strong DIE from public layers.
    if die_public:
        status = "DIE"
        reason = f"multi_public_die:{die_public[0].get('reason', '-')}"
    # Priority 2: graph default avatar.
    elif graph_state == "DIE" and graph_reason.startswith("graph_default_avatar:"):
        status = "DIE"
        reason = graph_reason
    # Priority 3: checkpoint when there is no reliable live signal.
    elif checkpoint_public and not live_public and graph_state != "LIVE":
        status = "CHECKPOINT"
        reason = f"multi_public_checkpoint:{checkpoint_public[0].get('reason', '-')}"
    # Priority 4: live from public profile extraction.
    elif live_public:
        status = "LIVE"
        reason = f"multi_public_live:{live_public[0].get('reason', '-')}"
    elif graph_state == "LIVE":
        status = "LIVE"
        reason = graph_reason
    elif redirect_state == "DIE" and graph_state == "DIE":
        status = "DIE"
        reason = f"{redirect_reason}|{graph_reason}"
    elif graph_state == "DIE":
        status = "DIE"
        reason = graph_reason
    elif redirect_state == "LIVE":
        # Redirect-only live is too weak, keep as UNKNOWN to avoid false LIVE.
        status = "UNKNOWN"
        reason = f"redirect_only:{redirect_reason}"

    return {
        "uid": uid,
        "status": status,
        "reason": reason,
        "httpCode": 200,
        "signals": {
            "redirect": {"status": redirect_state, "reason": redirect_reason},
            "graph": {"status": graph_state, "reason": graph_reason},
            "mbasic": mbasic_signal,
            "m": m_signal,
            "touch": touch_signal,
            "cookieMode": "on" if normalized_session_cookies else "off",
            "cookieCount": len(normalized_session_cookies),
        },
    }


async def check_uid(
    uid: str,
    proxy: Optional[str] = None,
    cookies: Optional[Dict[str, str]] = None,
    cookies_pool: Optional[List[Dict[str, str]]] = None,
) -> Dict[str, Any]:
    candidates = build_cookie_candidates(cookies, cookies_pool)
    attempts: List[Dict[str, Any]] = []
    final_result: Optional[Dict[str, Any]] = None

    for idx, candidate in enumerate(candidates):
        candidate_cookies = candidate.get("cookies") if isinstance(candidate, dict) else {}
        source = str(candidate.get("source") if isinstance(candidate, dict) else "") or f"cookie_{idx + 1}"

        current = await check_uid_once(uid=uid, proxy=proxy, session_cookies=candidate_cookies)
        current_signals = current.get("signals")
        if isinstance(current_signals, dict):
            current_signals["cookieSource"] = source
            current_signals["cookieAttempt"] = idx + 1
            current_signals["cookieCandidateTotal"] = len(candidates)

        attempts.append(
            {
                "attempt": idx + 1,
                "source": source,
                "status": str(current.get("status", "")),
                "reason": str(current.get("reason", "")),
                "cookieCount": len(normalize_cookies(candidate_cookies if isinstance(candidate_cookies, dict) else None)),
            }
        )
        final_result = current

        has_next = idx < (len(candidates) - 1)
        if not has_next:
            break
        if not should_try_next_cookie(current):
            break

    if not final_result:
        final_result = await check_uid_once(uid=uid, proxy=proxy, session_cookies={})

    final_signals = final_result.get("signals")
    if isinstance(final_signals, dict):
        final_signals["cookieFallbackUsed"] = len(attempts) > 1
        final_signals["cookieAttempts"] = attempts

    return final_result


def ensure_api_key(x_api_key: Optional[str]) -> None:
    if API_KEY and x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="invalid_api_key")


@app.get("/health")
async def health() -> Dict[str, Any]:
    return {
        "ok": True,
        "service": APP_NAME,
        "sepayRelayReady": bool(get_sepay_relay_target_url()),
    }


@app.get("/get-uid")
async def get_uid(url: Optional[str] = None, proxy: Optional[str] = None, x_api_key: Optional[str] = Header(default=None)) -> Any:
    ensure_api_key(x_api_key)

    fb_url = str(url or "").strip()
    if not fb_url:
        return JSONResponse(status_code=400, content={"success": False, "error": "Thiếu tham số url"})

    uid = await resolve_uid_from_facebook_url(fb_url, proxy)
    normalized_url = normalize_url_input(fb_url)
    if uid:
        return {"success": True, "uid": uid, "url": normalized_url}

    return JSONResponse(
        status_code=404,
        content={"success": False, "error": "Không tìm thấy UID", "url": normalized_url},
    )


@app.post("/get-uid")
async def get_uid_post(req: CheckRequest, x_api_key: Optional[str] = Header(default=None)) -> Any:
    ensure_api_key(x_api_key)

    fb_url = str(req.url or "").strip()
    if not fb_url:
        return JSONResponse(status_code=400, content={"success": False, "error": "Thiếu tham số url"})

    uid = await resolve_uid_from_facebook_url(fb_url, req.proxy)
    normalized_url = normalize_url_input(fb_url)
    if uid:
        return {"success": True, "uid": uid, "url": normalized_url}

    return JSONResponse(
        status_code=404,
        content={"success": False, "error": "Không tìm thấy UID", "url": normalized_url},
    )


@app.post("/sepay-webhook")
async def sepay_webhook_relay(request: Request) -> Response:
    upstream = await forward_sepay_webhook(
        "POST",
        get_sepay_relay_target_url(),
        await request.body(),
        get_forwardable_sepay_headers(request.headers),
        request.url.query,
    )
    return Response(
        content=upstream["body"],
        status_code=upstream["status_code"],
        headers={"Content-Type": upstream["content_type"]},
    )


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

    request_pool = req.cookiesPool or req.cookies_pool
    result = await check_uid(uid, req.proxy, req.cookies, request_pool)
    result["ok"] = True
    return result


@app.post("/live-check")
@app.post("/live-check/")
@app.post("/livecheck")
@app.post("/check-live")
async def live_check(req: LiveCheckRequest, x_api_key: Optional[str] = Header(default=None)) -> Dict[str, Any]:
    ensure_api_key(x_api_key)

    platform_raw = str(req.platform or "").strip().lower()
    if platform_raw in {"tt", "tiktok"}:
        platform = "tiktok"
    elif platform_raw in {"ig", "instagram"}:
        platform = "instagram"
    else:
        raise HTTPException(status_code=400, detail="unsupported_platform")

    usernames = normalize_live_usernames(req.usernames, platform)
    if not usernames:
        raise HTTPException(status_code=400, detail="empty_usernames")

    concurrency = pick_live_concurrency(req.concurrency)
    proxy_pool = normalize_proxy_pool(req.proxy, req.proxies)

    if platform == "tiktok":
        results = await bulk_check_tiktok(usernames)
    else:
        results = await bulk_check_instagram(usernames, proxy_pool, concurrency)

    return {
        "ok": True,
        "platform": platform,
        "total": len(results),
        "results": results,
    }
