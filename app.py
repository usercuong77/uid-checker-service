import asyncio
import json
import os
import re
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, urlencode, urlparse

import aiohttp
from fastapi import FastAPI, Header, HTTPException, Request, Response
from pydantic import BaseModel, Field

try:
    from fake_useragent import UserAgent
except Exception:
    UserAgent = None

try:
    from bs4 import BeautifulSoup
except Exception:
    BeautifulSoup = None


APP_NAME = "uid-checker-service"
API_KEY = os.getenv("UID_CHECKER_API_KEY", "").strip()
HTTP_TIMEOUT_SECONDS = float(os.getenv("UID_CHECKER_TIMEOUT", "10"))
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


class CheckRequest(BaseModel):
    uid: Optional[str] = Field(default=None)
    url: Optional[str] = Field(default=None)
    proxy: Optional[str] = Field(default=None)
    cookies: Optional[Dict[str, str]] = Field(default=None)
    cookiesPool: Optional[List[Dict[str, str]]] = Field(default=None)
    cookies_pool: Optional[List[Dict[str, str]]] = Field(default=None)


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
