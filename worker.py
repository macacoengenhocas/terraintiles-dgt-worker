#!/usr/bin/env python3
import ipaddress
import json
import os
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

VERSION = "0.2.0"
MAX_BODY = 16 * 1024
MAX_READ = 128 * 1024
RATE_LIMIT = 6
RATE_WINDOW = 600
ALLOWED_HOST = "cdd.dgterritorio.gov.pt"
ALLOWED_PATH_PREFIX = "/dgt-be/"
TIFF_MAGIC = {
    bytes.fromhex("49492a00"),
    bytes.fromhex("4d4d002a"),
    bytes.fromhex("49492b00"),
    bytes.fromhex("4d4d002b"),
}
REQUEST_TIMES = deque()


def diagnostic_enabled():
    return os.getenv("TT_DIAGNOSTIC_ENABLED", "0").strip().lower() in {"1", "true", "yes", "on"}


def is_public_host(hostname):
    try:
        infos = socket.getaddrinfo(hostname, 443, type=socket.SOCK_STREAM)
    except OSError:
        return False
    if not infos:
        return False
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_multicast
            or ip.is_reserved
            or ip.is_unspecified
        ):
            return False
    return True


def validate_initial_href(href):
    parsed = urllib.parse.urlparse(href)
    if parsed.scheme != "https":
        raise ValueError("asset href must use https")
    if (parsed.hostname or "").lower() != ALLOWED_HOST:
        raise ValueError("asset host not allowed")
    if not parsed.path.startswith(ALLOWED_PATH_PREFIX):
        raise ValueError("asset path not allowed")
    if parsed.username or parsed.password:
        raise ValueError("userinfo in URL is not allowed")
    if parsed.port not in (None, 443):
        raise ValueError("non-standard port is not allowed")
    if not is_public_host(parsed.hostname):
        raise ValueError("asset host did not resolve to public IPs")
    return parsed.geturl()


def validate_redirect_url(url):
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "https":
        raise ValueError("redirect must use https")
    if not parsed.hostname:
        raise ValueError("redirect hostname missing")
    if parsed.username or parsed.password:
        raise ValueError("redirect userinfo not allowed")
    if parsed.port not in (None, 443):
        raise ValueError("redirect non-standard port not allowed")
    if not is_public_host(parsed.hostname):
        raise ValueError("redirect target is not public")
    return parsed.geturl()


class SafeRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        safe = validate_redirect_url(newurl)
        # Never forward CDD session cookies to a different host.
        old_host = (urllib.parse.urlparse(req.full_url).hostname or "").lower()
        new_host = (urllib.parse.urlparse(safe).hostname or "").lower()
        new_headers = dict(req.headers)
        if new_host != old_host:
            new_headers.pop("Cookie", None)
            new_headers.pop("cookie", None)
        return urllib.request.Request(
            safe,
            headers=new_headers,
            method=req.get_method(),
        )


def rate_limit_ok():
    now = time.monotonic()
    while REQUEST_TIMES and now - REQUEST_TIMES[0] > RATE_WINDOW:
        REQUEST_TIMES.popleft()
    if len(REQUEST_TIMES) >= RATE_LIMIT:
        return False
    REQUEST_TIMES.append(now)
    return True


def probe_asset(href, cookie):
    href = validate_initial_href(href)
    if not isinstance(cookie, str) or not cookie.strip():
        raise ValueError("cookie is required")
    if "\r" in cookie or "\n" in cookie:
        raise ValueError("invalid cookie header")

    headers = {
        "Cookie": cookie.strip(),
        "Range": f"bytes=0-{MAX_READ - 1}",
        "User-Agent": f"TerrainTiles-DGT-Worker/{VERSION}",
        "Accept": "application/octet-stream,*/*;q=0.8",
    }
    req = urllib.request.Request(href, headers=headers, method="GET")
    opener = urllib.request.build_opener(SafeRedirect())
    started = time.monotonic()
    try:
        with opener.open(req, timeout=30) as resp:
            data = resp.read(MAX_READ)
            final_url = resp.geturl()
            status = getattr(resp, "status", None) or resp.getcode()
            content_type = resp.headers.get("Content-Type", "")
            content_range = resp.headers.get("Content-Range")
            content_length = resp.headers.get("Content-Length")
    except urllib.error.HTTPError as e:
        body = e.read(min(MAX_READ, 4096))
        return {
            "ok": False,
            "stage": "http",
            "status": e.code,
            "reason": str(e.reason),
            "body_prefix_hex": body[:32].hex(),
            "elapsed_ms": round((time.monotonic() - started) * 1000),
        }
    except urllib.error.URLError as e:
        return {
            "ok": False,
            "stage": "network",
            "error": str(e.reason),
            "elapsed_ms": round((time.monotonic() - started) * 1000),
        }

    magic = data[:4]
    return {
        "ok": status in (200, 206) and magic in TIFF_MAGIC,
        "stage": "object",
        "status": status,
        "final_host": urllib.parse.urlparse(final_url).hostname,
        "content_type": content_type,
        "content_range": content_range,
        "content_length": content_length,
        "bytes_read": len(data),
        "magic_hex": magic.hex(),
        "tiff_magic": magic in TIFF_MAGIC,
        "elapsed_ms": round((time.monotonic() - started) * 1000),
    }


class Handler(BaseHTTPRequestHandler):
    server_version = f"TerrainTilesDGT/{VERSION}"

    def log_message(self, fmt, *args):
        # Deliberately logs request metadata only; never request bodies/cookies.
        print(f"{self.address_string()} - {fmt % args}")

    def send_json(self, status, payload):
        raw = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        if self.path == "/health":
            self.send_json(200, {
                "ok": True,
                "service": "terraintiles-dgt-worker",
                "version": VERSION,
                "diagnostic_enabled": diagnostic_enabled(),
            })
            return
        self.send_json(404, {"ok": False, "error": "not_found"})

    def do_POST(self):
        if self.path != "/dgt/probe":
            self.send_json(404, {"ok": False, "error": "not_found"})
            return
        if not diagnostic_enabled():
            self.send_json(403, {"ok": False, "error": "diagnostic_disabled"})
            return
        if not rate_limit_ok():
            self.send_json(429, {"ok": False, "error": "rate_limited"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self.send_json(400, {"ok": False, "error": "invalid_content_length"})
            return
        if length <= 0 or length > MAX_BODY:
            self.send_json(413, {"ok": False, "error": "invalid_body_size"})
            return
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            href = payload.get("href")
            cookie = payload.get("cookie")
            if not isinstance(href, str):
                raise ValueError("href is required")
            result = probe_asset(href, cookie)
            self.send_json(200, {"worker_version": VERSION, **result})
        except (ValueError, json.JSONDecodeError) as e:
            self.send_json(400, {"ok": False, "error": str(e)})
        except Exception as e:
            self.send_json(500, {"ok": False, "error": type(e).__name__})


def main():
    port = int(os.getenv("PORT", "10000"))
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    print(f"TerrainTiles DGT worker {VERSION} listening on :{port}")
    server.serve_forever()


if __name__ == "__main__":
    main()
