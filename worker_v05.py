#!/usr/bin/env python3
import hashlib
import hmac
import json
import os
import shutil
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import worker_v041  # Applies safe redirect compatibility patch.
import worker_v04 as core

VERSION = '0.5.3'
MAX_BODY = 32 * 1024
CLOCK_SKEW_SECONDS = 120
NONCE_TTL_SECONDS = 300
SESSION_TTL_SECONDS = 15 * 60
AUTH_DOMAIN = b'TerrainTiles worker auth v1'

core.VERSION = VERSION

_nonce_lock = threading.Lock()
_seen_nonces = {}
_session_lock = threading.Lock()
_cached_opener = None
_cached_at = 0.0


def _raw_auth_secret():
    return os.getenv('TT_WORKER_AUTH_SECRET', '')


def _service_key():
    secret = _raw_auth_secret()
    if not secret:
        raise RuntimeError('WORKER_AUTH_NOT_CONFIGURED')
    return hmac.new(secret.encode('utf-8'), AUTH_DOMAIN, hashlib.sha256).digest()


def _clean_nonces(now):
    cutoff = now - NONCE_TTL_SECONDS
    expired = [nonce for nonce, seen_at in _seen_nonces.items() if seen_at < cutoff]
    for nonce in expired:
        _seen_nonces.pop(nonce, None)


def _verify_request(payload):
    href = payload.get('href')
    ts = payload.get('ts')
    nonce = payload.get('nonce')
    signature = payload.get('sig')
    if not isinstance(href, str) or not href or len(href) > 12000:
        raise ValueError('INVALID_HREF')
    if not isinstance(ts, int):
        raise ValueError('INVALID_TIMESTAMP')
    if not isinstance(nonce, str) or not (16 <= len(nonce) <= 128):
        raise ValueError('INVALID_NONCE')
    if not isinstance(signature, str) or len(signature) != 64:
        raise ValueError('INVALID_SIGNATURE')
    core.validate_https_public(href, asset=True)

    now = int(time.time())
    if abs(now - ts) > CLOCK_SKEW_SECONDS:
        raise PermissionError('STALE_REQUEST')
    canonical = f'v1\n{ts}\n{nonce}\n{href}'.encode('utf-8')
    expected = hmac.new(_service_key(), canonical, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, signature.lower()):
        raise PermissionError('BAD_SIGNATURE')

    with _nonce_lock:
        _clean_nonces(now)
        if nonce in _seen_nonces:
            raise PermissionError('REPLAYED_REQUEST')
        _seen_nonces[nonce] = now
    return href


def _reset_session():
    global _cached_opener, _cached_at
    with _session_lock:
        _cached_opener = None
        _cached_at = 0.0


def _active_opener(force=False):
    global _cached_opener, _cached_at
    now = time.monotonic()
    with _session_lock:
        if not force and _cached_opener is not None and now - _cached_at < SESSION_TTL_SECONDS:
            return _cached_opener
        username = os.getenv('DGT_CDD_USER', 'tiagoaraujo').strip()
        password = os.getenv('DGT_CDD_PASS', '')
        if not password:
            raise RuntimeError('DGT_CDD_PASS_MISSING')
        opener, _ = core.authenticate_and_search(
            username,
            password,
            [-9.145, 38.715, -9.14, 38.72],
            'MDT-50cm',
        )
        _cached_opener = opener
        _cached_at = time.monotonic()
        return opener


def _resolve_once(opener, href):
    req = urllib.request.Request(
        href,
        headers={
            'Range': 'bytes=0-3',
            'User-Agent': core.DGT_USER_AGENT,
            'Accept': 'application/octet-stream,*/*;q=0.8',
            'Accept-Language': 'pt-PT,pt;q=0.9,en;q=0.8',
        },
        method='GET',
    )
    with opener.open(req, timeout=30) as response:
        magic = response.read(4)
        status = getattr(response, 'status', None) or response.getcode()
        final_url = response.geturl()
        final = core.validate_https_public(final_url)
        if status not in (200, 206):
            raise RuntimeError(f'DGT_OBJECT_HTTP_{status}')
        if magic not in core.TIFF_MAGIC:
            raise RuntimeError('DGT_OBJECT_NOT_TIFF')
        if final.hostname == core.ALLOWED_ASSET_HOST:
            raise RuntimeError('DGT_OBJECT_REDIRECT_MISSING')
        return {
            'url': final_url,
            'status': status,
            'final_host': final.hostname,
            'tiff_magic': True,
        }


def resolve_asset(href):
    last_error = None
    for attempt in range(2):
        try:
            opener = _active_opener(force=attempt > 0)
            return _resolve_once(opener, href)
        except urllib.error.HTTPError as exc:
            last_error = RuntimeError(f'DGT_GATE_HTTP_{exc.code}')
            if attempt == 0 and (exc.code in (401, 403) or exc.code >= 500):
                _reset_session()
                time.sleep(1)
                continue
            raise last_error
        except urllib.error.URLError as exc:
            last_error = RuntimeError(f'DGT_NETWORK_{type(exc.reason).__name__}')
            if attempt == 0:
                _reset_session()
                time.sleep(1)
                continue
            raise last_error
        except RuntimeError as exc:
            last_error = exc
            if attempt == 0 and str(exc).startswith(('DGT_SESSION_', 'DGT_LOGIN_')):
                _reset_session()
                time.sleep(1)
                continue
            raise
    raise last_error or RuntimeError('DGT_RESOLVE_FAILED')


class Handler(BaseHTTPRequestHandler):
    server_version = f'TerrainTilesDGT/{VERSION}'

    def log_message(self, fmt, *args):
        print(f'{self.address_string()} - {fmt % args}', flush=True)

    def send_json(self, status, payload):
        raw = json.dumps(payload, separators=(',', ':'), ensure_ascii=False).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(raw)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        if self.path == '/health':
            self.send_json(200, {
                'ok': True,
                'service': 'terraintiles-dgt-worker',
                'version': VERSION,
                'dgt_configured': bool(os.getenv('DGT_CDD_PASS')),
                'auth_configured': bool(_raw_auth_secret()),
                'openssl_available': bool(shutil.which('openssl')),
            })
            return
        self.send_json(404, {'ok': False, 'error': 'not_found'})

    def do_POST(self):
        if self.path != '/v1/resolve':
            self.send_json(404, {'ok': False, 'error': 'not_found'})
            return
        try:
            length = int(self.headers.get('Content-Length', '0'))
        except ValueError:
            self.send_json(400, {'ok': False, 'error': 'invalid_content_length'})
            return
        if length <= 0 or length > MAX_BODY:
            self.send_json(413, {'ok': False, 'error': 'invalid_body_size'})
            return
        try:
            payload = json.loads(self.rfile.read(length).decode('utf-8'))
            if not isinstance(payload, dict):
                raise ValueError('INVALID_BODY')
            href = _verify_request(payload)
            resolved = resolve_asset(href)
            self.send_json(200, {'ok': True, 'worker_version': VERSION, **resolved})
        except PermissionError as exc:
            self.send_json(401, {'ok': False, 'error': str(exc)})
        except (ValueError, json.JSONDecodeError) as exc:
            self.send_json(400, {'ok': False, 'error': str(exc)})
        except Exception as exc:
            code = str(exc) if isinstance(exc, RuntimeError) else type(exc).__name__
            print(f'WORKER_RESOLVE_ERROR {code}', flush=True)
            self.send_json(502, {'ok': False, 'error': code})


def main():
    port = int(os.getenv('PORT', '10000'))
    server = ThreadingHTTPServer(('0.0.0.0', port), Handler)
    print(f'TerrainTiles DGT worker {VERSION} listening on :{port}', flush=True)
    print(f'OPENSSL_AVAILABLE {bool(shutil.which("openssl"))}', flush=True)
    server.serve_forever()


if __name__ == '__main__':
    main()
