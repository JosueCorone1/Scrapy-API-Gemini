"""Instagram transport: curl_cffi with browser-impersonated TLS. There is no
browser automation or cookie harvesting. Requests are anonymous by default;
an optional Cookie header from the user's own session can be supplied through
configuration. Some queries still target logged-out endpoints, so cookie-based
access depends on the Instagram surface used by each endpoint.

Four upstream surfaces, all from one anonymous session:

  * GRAPHQL  POST /api/graphql        (the logged-out web app's endpoint)
             POST /graphql/query      (the logged-in web app's endpoint)
    Persisted doc_ids (queries.py). TRAP: each doc_id only answers on ITS
    endpoint — a logged-out doc_id sent to /graphql/query returns
    `{"data":{"xig_user_by_username":null}}` with status ok; a logged-in
    doc_id on /graphql/query needs every relay-provider boolean it declares
    (queries.variables_for) or fails with missing_required_variable_value /
    "execution error". Both need an `lsd` token (x-fb-lsd + form field) and
    a csrftoken cookie echoed in x-csrftoken. Neither value is validated
    server-side: a random pair works, so a session needs NO bootstrap page.
    Login-gated queries answer 401 "Please wait a few minutes before you
    try again" (a gate, not a rate limit — it is immediate and cold on a
    fresh exit; do not build cooldowns on it) or 400 "Unauthorized logged
    out query".

  * MOBILE   https://i.instagram.com/api/v1/... with the Android app's
    User-Agent and app id, unsigned. Serves a user's timeline by USERNAME
    (feed/user/<username>/username/, 12 items, max_id paging — the numeric
    id form is flaky and rejects max_id with "useragent mismatch") and reels
    (POST clips/user/), with the counters the web queries hide (play_count,
    video_duration) and the full audio metadata. Gated paths answer 403
    login_required.

  * WWW API  POST /api/v1/clips/music/ (the audio page's own call): form
    audio_cluster_id + max_id + lsd, header x-ig-d: www, answer prefixed
    with `for (;;);`.

  * PAGES    GET /explore/locations/<id>/ etc.: server-rendered pages that
    embed Relay results in `<script type="application/json" data-sjs>`
    (ssr.py), used where the matching query is itself gated
    (PolarisExploreLocationsContainerQuery answers 401 when called).

BLOCK HANDLING: a 429 honors Retry-After (or the configured cooldown), and
subsequent requests in this process are stopped during that cooldown. A
403/429 is not retried on another identity or network route. A configured
INSTAGRAM_PROXY, if present, is a fixed route chosen by the operator.

Failure taxonomy (scraper_errors, mapped to HTTP by route_glue):
  InstagramUpstreamError   transport failure / 5xx                  — retryable
  InstagramExecutionError  GraphQL "execution error" with no data    — retryable
  InstagramGated           a login wall on a surface (401/redirect to
                           /accounts/login/) — callers may try a public
                           surface; otherwise 502
  InstagramBlocked         403 / 429 / HTML instead of JSON          — stop
  InstagramBadRequest      upstream 400                              — never retried
  InstagramNotFound        unknown user / post / tag / location      — never retried
"""
import json
import os
import secrets
import string
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from http.cookies import CookieError, SimpleCookie
from urllib.parse import urlparse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
try:  # the shared failure taxonomy of the scrapers service (route_glue maps it to HTTP)
    from scraper_errors import BadRequest, Blocked, NotFound, UpstreamError
except ImportError:  # standalone package: the same four bases, locally
    class UpstreamError(Exception):
        """Transport failure or 5xx — retryable."""

    class Blocked(UpstreamError):
        """Anti-bot challenge / 403 / 429 — stop the current request flow."""

    class BadRequest(Exception):
        """Upstream rejected the params — never retried."""

    class NotFound(Exception):
        """Entity / page does not exist — never retried."""

from .queries import QUERIES, variables_for

SITE = "https://www.instagram.com"
MOBILE_SITE = "https://i.instagram.com"
GRAPHQL_URLS = {"api": SITE + "/api/graphql", "query": SITE + "/graphql/query"}
IMPERSONATE = "chrome"

# Constants of the instagram.com web app / Android app (same for every visitor).
WEB_APP_ID = "936619743392459"
WEB_ASBD_ID = "129477"
MOBILE_APP_ID = "567067343352427"
MOBILE_USER_AGENT = ("Instagram 275.0.0.27.98 Android (33/13; 420dpi; 1080x2400; samsung; SM-G991B; "
                     "o1s; exynos2100; en_US; 458229258)")

GRAPHQL_TIMEOUT = 30
MOBILE_TIMEOUT = 30
PAGE_TIMEOUT = 45          # explore pages are 1-2 MB
FANOUT_WORKERS = 4
_request_lock = threading.Lock()
_next_request_at = 0.0
_requests_blocked_until = 0.0

PAGE_HEADERS = {
    "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "accept-language": "en-US,en;q=0.9",
    "sec-fetch-dest": "document",
    "sec-fetch-mode": "navigate",
    "sec-fetch-site": "none",
    "upgrade-insecure-requests": "1",
}

_GATE_MARKERS = ("please wait a few minutes", "login_required", "unauthorized logged out", "checkpoint_required",
                 "challenge_required", "log in to continue", "please log in to your account")


class InstagramUpstreamError(UpstreamError):
    """Transport failure, 5xx or a GraphQL execution error — retryable."""


class InstagramExecutionError(InstagramUpstreamError):
    """GraphQL answered `data: null` with "execution error" — a transient
    backend failure, a stale doc_id / provider list, or (on paged queries)
    a cursor the server cannot decode."""


class InstagramFieldException(InstagramUpstreamError):
    """`data: null` with "A server error field_exception occured" — what
    /graphql/query answers for a shortcode that does not exist (or is
    geo-gated for this exit). Never retried; callers re-ask the logged-out
    endpoint, which distinguishes missing from gated."""


class InstagramGated(InstagramUpstreamError):
    """A login wall on this surface (401 "Please wait", 403 login_required,
    a redirect to /accounts/login/). Callers fall back to another surface."""


class InstagramBlocked(InstagramUpstreamError, Blocked):
    """403 / 429, or HTML where JSON was expected; stops the current scan."""


class InstagramRateLimited(InstagramBlocked):
    """HTTP 429 or a local cooldown; no further request is sent."""


class InstagramBadRequest(BadRequest):
    """Upstream 400 — never retried."""


class InstagramNotFound(NotFound):
    """The user / post / tag / location does not exist — never retried."""


# ---- sessions --------------------------------------------------------------------
# One curl session per worker thread (a curl handle must not be shared across
# threads), each with its own random csrftoken/lsd pair. The mobile calls use
# a separate session on the same thread. Optional user-provided cookies are
# scoped to Instagram's domain; no cookie is sent to any other host.
_local = threading.local()
_TOKEN_CHARS = string.ascii_letters + string.digits


def _token(n):
    return "".join(secrets.choice(_TOKEN_CHARS) for _ in range(n))


def _proxy_for_thread():
    """Use only the configured fixed proxy, if the operator supplied one."""
    proxy = config.instagram_proxy()
    return proxy is not None, proxy


def _new_session(proxy):
    from curl_cffi import requests as curl_requests
    sess = curl_requests.Session(impersonate=IMPERSONATE)
    if proxy:
        sess.proxies = {"http": proxy, "https": proxy}
    return sess


def _apply_instagram_cookies(sess):
    """Load an optional Cookie header into this Instagram-only session."""
    raw = config.instagram_cookies()
    parsed = SimpleCookie()
    if raw:
        try:
            parsed.load(raw)
        except CookieError:
            raise ValueError("INSTAGRAM_COOKIES must be a Cookie header with name=value pairs") from None
        if not parsed:
            raise ValueError("INSTAGRAM_COOKIES must be a Cookie header with name=value pairs")

    values = {name: morsel.value for name, morsel in parsed.items()}
    csrf = values.pop("csrftoken", None)
    # When a Cookie header comes from a logged-in web session, GraphQL and
    # www_api must identify the actor as that session. Sending __user=0 with
    # a logged-in session can make Instagram answer the operation with an
    # opaque 1357004 error instead of its data.
    sess._ig_user_id = values.get("ds_user_id")
    sess._ig_csrf = csrf or _token(32)
    for name, value in values.items():
        sess.cookies.set(name, value, domain=".instagram.com", secure=True)
    sess.cookies.set("csrftoken", sess._ig_csrf, domain=".instagram.com", secure=True)


def _session():
    sess = getattr(_local, "session", None)
    via_proxy, proxy = _proxy_for_thread()
    if sess is not None and getattr(sess, "_ig_proxied", False) != via_proxy:
        _drop_session()
        sess = None
    if sess is None:
        sess = _new_session(proxy)
        sess._ig_proxied = via_proxy
        _apply_instagram_cookies(sess)
        sess._ig_lsd = "AV" + _token(9)
        _local.session = sess
    return sess


def _mobile_session():
    sess = getattr(_local, "mobile_session", None)
    via_proxy, proxy = _proxy_for_thread()
    if sess is not None and getattr(sess, "_ig_proxied", False) != via_proxy:
        _drop_session()
        sess = None
    if sess is None:
        sess = _new_session(proxy)
        sess._ig_proxied = via_proxy
        _apply_instagram_cookies(sess)
        _local.mobile_session = sess
    return sess


def _anonymous_session():
    """A cookie-free web session for public operations that gate cookie sessions."""
    sess = getattr(_local, "anonymous_session", None)
    via_proxy, proxy = _proxy_for_thread()
    if sess is not None and getattr(sess, "_ig_proxied", False) != via_proxy:
        _drop_session()
        sess = None
    if sess is None:
        sess = _new_session(proxy)
        sess._ig_proxied = via_proxy
        sess._ig_user_id = None
        sess._ig_csrf = _token(32)
        sess._ig_lsd = "AV" + _token(9)
        _local.anonymous_session = sess
    return sess


def _drop_session():
    for attr in ("session", "mobile_session", "anonymous_session"):
        sess = getattr(_local, attr, None)
        setattr(_local, attr, None)
        if sess is not None:
            try:
                sess.close()
            except Exception:
                pass


def dump_debug(name, text):
    """Write a raw response to $INSTAGRAM_DEBUG_DIR/<name>.txt."""
    dbg = os.environ.get("INSTAGRAM_DEBUG_DIR", "")
    if dbg and text:
        try:
            os.makedirs(dbg, exist_ok=True)
            with open(os.path.join(dbg, name + ".txt"), "w") as f:
                f.write(text)
        except OSError:
            pass


def _retrying(fn, no_retry=()):
    last = None
    for attempt in range(1, config.MAX_RETRIES + 1):
        try:
            return fn()
        except no_retry:
            raise
        except (InstagramBlocked, InstagramGated):
            raise
        except InstagramUpstreamError as exc:
            last = exc
            _drop_session()
        if attempt < config.MAX_RETRIES:
            time.sleep(config.RETRY_BACKOFF * attempt)
    raise last


def _retry_after_seconds(value):
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except (TypeError, ValueError):
        try:
            retry_at = parsedate_to_datetime(str(value))
        except (TypeError, ValueError, OverflowError):
            return None
        if retry_at.tzinfo is None:
            retry_at = retry_at.replace(tzinfo=timezone.utc)
        return max(0.0, (retry_at - datetime.now(timezone.utc)).total_seconds())


def paced_request(session, method, url, **kwargs):
    """Serialize requests and honor Retry-After after an HTTP 429."""
    global _next_request_at, _requests_blocked_until
    with _request_lock:
        now = time.monotonic()
        cooldown = _requests_blocked_until - now
        if cooldown > 0:
            raise InstagramRateLimited(
                f"HTTP 429 cooldown active for {cooldown:.0f}s; request not sent"
            )
        wait = _next_request_at - now
        if wait > 0:
            time.sleep(wait)
        try:
            response = getattr(session, method)(url, **kwargs)
        finally:
            _next_request_at = (
                time.monotonic() + config.INSTAGRAM_REQUEST_INTERVAL_SECONDS
            )
        if response.status_code == 429:
            delay = _retry_after_seconds(response.headers.get("Retry-After"))
            if delay is None:
                delay = config.INSTAGRAM_RATE_LIMIT_COOLDOWN_SECONDS
            _requests_blocked_until = time.monotonic() + delay
        return response

def _is_gate(text):
    low = (text or "")[:2000].lower()
    return any(marker in low for marker in _GATE_MARKERS)


def _parse_json(text, label):
    body = text or ""
    if body.startswith("for (;;);"):
        body = body[9:]
    if not body.lstrip().startswith("{"):
        dump_debug(f"nonjson_{label}", text)
        raise InstagramBlocked(f"{label} returned a non-JSON body")
    try:
        return json.loads(body)
    except ValueError:
        dump_debug(f"badjson_{label}", text)
        raise InstagramUpstreamError(f"could not parse JSON from {label}")


# ---- GraphQL -------------------------------------------------------------------------

def _is_execution_error(errors):
    return any("execution error" in str((e or {}).get("message", "")) for e in errors or [])


def _error_text(errors):
    return "; ".join(str((e or {}).get("message", "")) for e in errors or [] if isinstance(e, dict))[:300]


def graphql(name, retry_execution_errors=True, anonymous=False, **variables):
    """Run one persisted query -> its `data` object.

    A missing entity comes back as data with a null root (callers turn that
    into InstagramNotFound). `data: null` with errors is either a transient
    backend failure or a stale doc_id / provider list — retried, then
    surfaced as an upstream error naming the query so a re-harvest is
    obvious from the logs. Paged calls pass retry_execution_errors=False:
    an undecodable cursor fails the same way every time, so retrying only
    delays the 400."""
    query = QUERIES[name]
    url = GRAPHQL_URLS[query["endpoint"]]
    body = {
        "av": "0",
        "__d": "www",
        "__user": "0",
        "__a": "1",
        "__comet_req": "7",
        "lsd": None,
        "jazoest": "2100",
        "fb_api_caller_class": "RelayModern",
        "fb_api_req_friendly_name": name,
        "variables": json.dumps(variables_for(name, **variables), separators=(",", ":")),
        "server_timestamps": "true",
        "doc_id": query["doc_id"],
    }

    def once():
        sess = _anonymous_session() if anonymous else _session()
        body["lsd"] = sess._ig_lsd
        body["av"] = sess._ig_user_id or "0"
        body["__user"] = sess._ig_user_id or "0"
        headers = {
            "accept": "*/*",
            "accept-language": "en-US,en;q=0.9",
            "content-type": "application/x-www-form-urlencoded",
            "origin": SITE,
            "referer": SITE + "/",
            "sec-fetch-dest": "empty",
            "sec-fetch-mode": "cors",
            "sec-fetch-site": "same-origin",
            "x-asbd-id": WEB_ASBD_ID,
            "x-csrftoken": sess._ig_csrf,
            "x-fb-friendly-name": name,
            "x-fb-lsd": sess._ig_lsd,
            "x-ig-app-id": WEB_APP_ID,
        }
        try:
            resp = paced_request(sess, "post", url, data=body, headers=headers, timeout=GRAPHQL_TIMEOUT)
        except InstagramRateLimited:
            raise
        except Exception as e:
            raise InstagramUpstreamError(f"request failed: {type(e).__name__}: {e}")
        status = resp.status_code
        text = resp.text or ""
        if status in (401, 403, 429):
            dump_debug(f"blocked_{name}", text)
            if _is_gate(text):
                raise InstagramGated(f"{name} is login-gated (HTTP {status})")
            raise InstagramBlocked(f"HTTP {status} on {name}")
        if status >= 500:
            raise InstagramUpstreamError(f"HTTP {status} on {name}")
        payload = _parse_json(text, name)
        if status == 400:
            if _is_gate(text):
                raise InstagramGated(f"{name} is login-gated")
            raise InstagramBadRequest(f"instagram rejected {name}: {_error_text(payload.get('errors')) or 'HTTP 400'}")
        if status != 200:
            raise InstagramUpstreamError(f"HTTP {status} on {name}")
        auth_message = " ".join(str(payload.get(key) or "")
                                 for key in ("message", "errorSummary", "errorDescription"))
        if payload.get("error") == 1357001 or _is_gate(auth_message):
            raise InstagramGated(f"{name} requires an authenticated Instagram session")
        data = payload.get("data")
        if data is None:
            dump_debug(f"error_{name}", text)
            errors = payload.get("errors")
            if _is_execution_error(errors):
                raise InstagramExecutionError(f"{name} failed upstream (execution error)")
            if "field_exception" in _error_text(errors):
                raise InstagramFieldException(f"{name} answered field_exception")
            if _is_gate(_error_text(errors)):
                raise InstagramGated(f"{name} is login-gated")
            raise InstagramUpstreamError(f"{name} returned no data: {_error_text(errors) or 'empty'}")
        return data

    no_retry = (InstagramGated, InstagramFieldException) + (() if retry_execution_errors else (InstagramExecutionError,))
    return _retrying(once, no_retry=no_retry)


# ---- mobile app API --------------------------------------------------------------------

def mobile(path, *, params=None, data=None):
    """Request an Instagram mobile JSON endpoint. Login walls raise
    InstagramGated so callers may try another public surface; 403/429 blocks
    stop the current collection."""
    url = f"{MOBILE_SITE}/api/v1/{path.lstrip('/')}"
    label = path.strip("/").split("?")[0]

    def once():
        sess = _mobile_session()
        headers = {
            "user-agent": MOBILE_USER_AGENT,
            "accept": "*/*",
            "accept-language": "en-US",
            "x-ig-app-id": MOBILE_APP_ID,
        }
        try:
            if data is not None:
                resp = paced_request(sess, "post", url, data=data, params=params, headers=headers, timeout=MOBILE_TIMEOUT)
            else:
                resp = paced_request(sess, "get", url, params=params, headers=headers, timeout=MOBILE_TIMEOUT)
        except InstagramRateLimited:
            raise
        except Exception as e:
            raise InstagramUpstreamError(f"request failed: {type(e).__name__}: {e}")
        status = resp.status_code
        text = resp.text or ""
        if status == 404:
            raise InstagramNotFound(f"{label} not found")
        if status in (401, 403, 429):
            dump_debug(f"mobile_blocked_{label.replace('/', '_')}", text)
            if _is_gate(text):
                raise InstagramGated(f"{label} is login-gated")
            raise InstagramBlocked(f"HTTP {status} on {label}")
        if status >= 500:
            raise InstagramUpstreamError(f"HTTP {status} on {label}")
        payload = _parse_json(text, label)
        if status == 400:
            message = str(payload.get("message") or "")
            if _is_gate(message):
                raise InstagramGated(f"{label} requires Instagram verification")
            if "not found" in message.lower() or "invalid" in message.lower():
                raise InstagramNotFound(f"{label}: {message}")
            raise InstagramBadRequest(f"instagram rejected {label}: {message or 'HTTP 400'}")
        if status != 200:
            raise InstagramUpstreamError(f"HTTP {status} on {label}")
        if payload.get("status") == "fail":
            message = str(payload.get("message") or "")
            if _is_gate(message):
                raise InstagramGated(f"{label} requires Instagram verification")
            raise InstagramUpstreamError(f"{label} failed: {message or 'status fail'}")
        return payload

    return _retrying(once, no_retry=(InstagramGated, InstagramNotFound))


# ---- www /api/v1 (the web app's non-GraphQL calls) --------------------------------------

def www_api(path, *, form, referer):
    """POST https://www.instagram.com/api/v1/<path> the way the web app does
    (form + lsd, x-ig-d: www) -> the JSON payload (the `for (;;);` guard
    stripped, `payload` unwrapped)."""
    url = f"{SITE}/api/v1/{path.lstrip('/')}"
    label = path.strip("/")

    def once():
        sess = _session()
        body = dict(form)
        actor_id = sess._ig_user_id or "0"
        body.update({"__d": "www", "__user": actor_id, "av": actor_id, "__a": "1", "__comet_req": "7", "lsd": sess._ig_lsd, "jazoest": "2100"})
        headers = {
            "accept": "*/*",
            "accept-language": "en-US,en;q=0.9",
            "content-type": "application/x-www-form-urlencoded",
            "origin": SITE,
            "referer": referer,
            "sec-fetch-dest": "empty",
            "sec-fetch-mode": "cors",
            "sec-fetch-site": "same-origin",
            "x-asbd-id": "359341",
            "x-csrftoken": sess._ig_csrf,
            "x-fb-lsd": sess._ig_lsd,
            "x-ig-d": "www",
        }
        try:
            resp = paced_request(sess, "post", url, data=body, headers=headers, timeout=GRAPHQL_TIMEOUT)
        except InstagramRateLimited:
            raise
        except Exception as e:
            raise InstagramUpstreamError(f"request failed: {type(e).__name__}: {e}")
        status = resp.status_code
        text = resp.text or ""
        if status == 404:
            raise InstagramNotFound(f"{label} not found")
        if status in (401, 403, 429):
            dump_debug(f"blocked_{label.replace('/', '_')}", text)
            if _is_gate(text):
                raise InstagramGated(f"{label} is login-gated (HTTP {status})")
            raise InstagramBlocked(f"HTTP {status} on {label}")
        if status >= 500:
            raise InstagramUpstreamError(f"HTTP {status} on {label}")
        payload = _parse_json(text, label)
        if status == 400:
            raise InstagramBadRequest(f"instagram rejected {label}: {payload.get('message') or 'HTTP 400'}")
        if status != 200:
            raise InstagramUpstreamError(f"HTTP {status} on {label}")
        inner = payload.get("payload")
        return inner if isinstance(inner, dict) else payload

    return _retrying(once, no_retry=(InstagramGated, InstagramNotFound))


def www_json(path, *, params, label, referer=None, session_auth=False):
    """GET https://www.instagram.com/<path> (a plain JSON endpoint such as
    oembed) -> the JSON object; a 404 is InstagramNotFound."""
    url = f"{SITE}/{path.lstrip('/')}"

    def once():
        sess = _session()
        headers = {
            "accept": "*/*",
            "accept-language": "en-US,en;q=0.9",
            "referer": referer or SITE + "/",
            "x-ig-app-id": WEB_APP_ID,
        }
        if session_auth:
            headers.update({
                "x-asbd-id": WEB_ASBD_ID,
                "x-csrftoken": sess._ig_csrf,
                "x-requested-with": "XMLHttpRequest",
            })
        try:
            resp = paced_request(sess, "get", url, params=params, headers=headers, timeout=GRAPHQL_TIMEOUT)
        except InstagramRateLimited:
            raise
        except Exception as e:
            raise InstagramUpstreamError(f"request failed: {type(e).__name__}: {e}")
        status = resp.status_code
        text = resp.text or ""
        if status == 404:
            raise InstagramNotFound(f"{label} not found")
        if status in (401, 403, 429):
            dump_debug(f"blocked_{label.replace(' ', '_')}", text)
            if _is_gate(text):
                raise InstagramGated(f"{label} is login-gated (HTTP {status})")
            raise InstagramBlocked(f"HTTP {status} on {label}")
        if status >= 500:
            raise InstagramUpstreamError(f"HTTP {status} on {label}")
        payload = _parse_json(text, label)
        if status == 400:
            raise InstagramBadRequest(f"instagram rejected {label}")
        if status != 200:
            raise InstagramUpstreamError(f"HTTP {status} on {label}")
        return payload

    return _retrying(once, no_retry=(InstagramGated, InstagramNotFound))


# ---- pages ------------------------------------------------------------------------------

def get_page(path, *, label):
    """GET one www.instagram.com page -> (html, final_url). A 404 is
    InstagramNotFound; a redirect to /accounts/login/ is a login wall
    (InstagramGated)."""
    url = SITE + path

    def once():
        sess = _session()
        try:
            resp = paced_request(sess, "get", url, headers=PAGE_HEADERS, timeout=PAGE_TIMEOUT, allow_redirects=True)
        except InstagramRateLimited:
            raise
        except Exception as e:
            raise InstagramUpstreamError(f"request failed: {type(e).__name__}: {e}")
        status = resp.status_code
        final = str(resp.url or url)
        if status == 404:
            raise InstagramNotFound(f"{label} not found")
        if status in (401, 403, 429):
            dump_debug("blocked_page", resp.text)
            raise InstagramBlocked(f"HTTP {status} on {path}")
        if status >= 500 or status != 200:
            raise InstagramUpstreamError(f"HTTP {status} on {path}")
        if urlparse(final).path.startswith("/accounts/login"):
            raise InstagramGated(f"{label} is behind a login wall")
        html = resp.text or ""
        if "<html" not in html[:3000].lower():
            dump_debug("nonhtml", html)
            raise InstagramBlocked(f"{path} returned a non-HTML body")
        return html, final

    return _retrying(once, no_retry=(InstagramGated, InstagramNotFound))


def get_redirect(path, *, label):
    """The Location a www.instagram.com path redirects to (share links),
    or None when it does not redirect."""
    url = SITE + path

    def once():
        sess = _session()
        try:
            resp = paced_request(sess, "get", url, headers=PAGE_HEADERS, timeout=PAGE_TIMEOUT, allow_redirects=False)
        except InstagramRateLimited:
            raise
        except Exception as e:
            raise InstagramUpstreamError(f"request failed: {type(e).__name__}: {e}")
        status = resp.status_code
        if status == 404:
            raise InstagramNotFound(f"{label} not found")
        if status in (401, 403, 429):
            raise InstagramBlocked(f"HTTP {status} on {path}")
        if status >= 500:
            raise InstagramUpstreamError(f"HTTP {status} on {path}")
        if status in (301, 302, 303, 307, 308):
            return resp.headers.get("location") or None
        return None

    return _retrying(once, no_retry=(InstagramNotFound,))


# ---- fan-out -------------------------------------------------------------------------

def run_parallel(fns):
    """Run zero-arg callables in parallel; results align with `fns`.
    Exceptions propagate from the first failing call."""
    if not fns:
        return []
    if len(fns) == 1:
        return [fns[0]()]
    with ThreadPoolExecutor(max_workers=min(FANOUT_WORKERS, len(fns))) as ex:
        futures = [ex.submit(fn) for fn in fns]
        return [f.result() for f in futures]


if __name__ == "__main__":
    # Smoke test: python -m instagram.fetch [username]
    who = sys.argv[1] if len(sys.argv) > 1 else "instagram"
    data = graphql("PolarisLoggedOutDesktopWWWProfileRootContentQuery", username=who)
    user = data.get("xig_user_by_username") or {}
    print("user:", user.get("username"), user.get("pk"), user.get("follower_count"))
