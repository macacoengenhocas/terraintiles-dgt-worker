#!/usr/bin/env python3
import base64
import binascii
import json
import math
import os
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import worker_v041  # Applies safe redirect compatibility patch.
import worker_v04 as core

VERSION = '0.6.1'
MAX_BODY = 32 * 1024
CLOCK_SKEW_SECONDS = 120
NONCE_TTL_SECONDS = 300
SESSION_TTL_SECONDS = 15 * 60
RATE_WINDOW_SECONDS = 60
RATE_MAX_REQUESTS = 30
SPKI_PREFIX = bytes.fromhex('302a300506032b6570032100')
ALLOWED_COLLECTIONS = {'MDT-50cm', 'MDS-50cm', 'MDT-2m', 'MDS-2m'}

core.VERSION = VERSION

_nonce_lock = threading.Lock()
_seen_nonces = {}
_rate_lock = threading.Lock()
_rate_events = {}
_session_lock = threading.Lock()
_cached_opener = None
_cached_at = 0.0


def _raw_public_key():
    value = os.getenv('TT_WORKER_ED25519_RAW_PUB_HEX', '').strip().lower()
    if len(value) != 64:
        raise RuntimeError('WORKER_PUBLIC_KEY_NOT_CONFIGURED')
    try:
        raw = bytes.fromhex(value)
    except ValueError as exc:
        raise RuntimeError('WORKER_PUBLIC_KEY_INVALID') from exc
    if len(raw) != 32:
        raise RuntimeError('WORKER_PUBLIC_KEY_INVALID')
    return raw


def _clean_nonces(now):
    cutoff = now - NONCE_TTL_SECONDS
    expired = [nonce for nonce, seen_at in _seen_nonces.items() if seen_at < cutoff]
    for nonce in expired:
        _seen_nonces.pop(nonce, None)


def _allow_request(ip):
    now = time.monotonic()
    cutoff = now - RATE_WINDOW_SECONDS
    with _rate_lock:
        events = [seen for seen in _rate_events.get(ip, []) if seen >= cutoff]
        if len(events) >= RATE_MAX_REQUESTS:
            _rate_events[ip] = events
            return False
        events.append(now)
        _rate_events[ip] = events
        return True


def _verify_ed25519(message, signature):
    public_der = SPKI_PREFIX + _raw_public_key()
    paths = []
    try:
        for data in (public_der, message, signature):
            handle = tempfile.NamedTemporaryFile(prefix='tt_ed25519_', delete=False)
            handle.write(data)
            handle.flush()
            handle.close()
            paths.append(handle.name)
        result = subprocess.run(
            [
                'openssl', 'pkeyutl', '-verify', '-pubin', '-inkey', paths[0],
                '-keyform', 'DER', '-rawin', '-in', paths[1], '-sigfile', paths[2],
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=5,
            check=False,
        )
        return result.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError('WORKER_SIGNATURE_VERIFIER_UNAVAILABLE') from exc
    finally:
        for path in paths:
            try:
                os.unlink(path)
            except OSError:
                pass


def _verify_signature(payload, canonical):
    ts = payload.get('ts')
    nonce = payload.get('nonce')
    signature_text = payload.get('sig')
    algorithm = payload.get('alg')
    if algorithm != 'ed25519-v1':
        raise PermissionError('UNSUPPORTED_AUTH_SCHEME')
    if not isinstance(ts, int):
        raise ValueError('INVALID_TIMESTAMP')
    if not isinstance(nonce, str) or not (16 <= len(nonce) <= 128):
        raise ValueError('INVALID_NONCE')
    if not isinstance(signature_text, str) or not (80 <= len(signature_text) <= 100):
        raise ValueError('INVALID_SIGNATURE')
    now = int(time.time())
    if abs(now - ts) > CLOCK_SKEW_SECONDS:
        raise PermissionError('STALE_REQUEST')
    try:
        signature = base64.b64decode(signature_text, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError('INVALID_SIGNATURE') from exc
    if len(signature) != 64:
        raise ValueError('INVALID_SIGNATURE')
    if not _verify_ed25519(canonical.encode('utf-8'), signature):
        raise PermissionError('BAD_SIGNATURE')
    with _nonce_lock:
        _clean_nonces(now)
        if nonce in _seen_nonces:
            raise PermissionError('REPLAYED_REQUEST')
        _seen_nonces[nonce] = now


def _verify_resolve_request(payload):
    href = payload.get('href')
    ts = payload.get('ts')
    nonce = payload.get('nonce')
    if not isinstance(href, str) or not href or len(href) > 12000:
        raise ValueError('INVALID_HREF')
    core.validate_https_public(href, asset=True)
    _verify_signature(payload, f'v2\n{ts}\n{nonce}\n{href}')
    return href


def _verify_search_request(payload):
    bbox_token = payload.get('bbox')
    collection = payload.get('collection')
    ts = payload.get('ts')
    nonce = payload.get('nonce')
    if collection not in ALLOWED_COLLECTIONS:
        raise ValueError('INVALID_COLLECTION')
    if not isinstance(bbox_token, str) or len(bbox_token) > 120:
        raise ValueError('INVALID_BBOX')
    parts = bbox_token.split(',')
    if len(parts) != 4:
        raise ValueError('INVALID_BBOX')
    try:
        bbox = [float(value) for value in parts]
    except ValueError as exc:
        raise ValueError('INVALID_BBOX') from exc
    if any(not math.isfinite(value) for value in bbox):
        raise ValueError('INVALID_BBOX')
    west, south, east, north = bbox
    if not (west < east and south < north):
        raise ValueError('INVALID_BBOX')
    if west < -9.7 or east > -6.0 or south < 36.8 or north > 42.3:
        raise ValueError('BBOX_OUTSIDE_MAINLAND_PORTUGAL')
    _verify_signature(payload, f'search-v1\n{ts}\n{nonce}\n{collection}\n{bbox_token}')
    return bbox, collection


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


def _search_once(opener, bbox, collection):
    payload = json.dumps({
        'bbox': bbox,
        'limit': 1000,
        'collections': [collection],
    }).encode('utf-8')
    with core.request(
        opener,
        core.DGT_STAC,
        method='POST',
        data=payload,
        extra_headers={'Content-Type': 'application/json', 'Accept': 'application/json'},
    ) as response:
        content_type = response.headers.get('Content-Type', '').lower()
        raw = response.read(8 * 1024 * 1024)
        status = response.status
    if 'text/html' in content_type:
        raise RuntimeError('DGT_SESSION_NOT_VALID')
    if status != 200:
        raise RuntimeError(f'DGT_STAC_HTTP_{status}')
    return core.find_tiff_assets(json.loads(raw.decode('utf-8')), collection)


def search_assets(bbox, collection):
    last_error = None
    for attempt in range(2):
        try:
            return _search_once(_active_opener(force=attempt > 0), bbox, collection)
        except urllib.error.HTTPError as exc:
            last_error = RuntimeError(f'DGT_STAC_HTTP_{exc.code}')
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
    raise last_error or RuntimeError('DGT_SEARCH_FAILED')


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
            configured = True
            try:
                _raw_public_key()
            except RuntimeError:
                configured = False
            self.send_json(200, {
                'ok': True,
                'service': 'terraintiles-dgt-worker',
                'version': VERSION,
                'dgt_configured': bool(os.getenv('DGT_CDD_PASS')),
                'auth_scheme': 'ed25519-v1',
                'auth_configured': configured,
            })
            return
        self.send_json(404, {'ok': False, 'error': 'not_found'})

    def do_POST(self):
        if self.path not in {'/v1/resolve', '/v1/search'}:
            self.send_json(404, {'ok': False, 'error': 'not_found'})
            return
        if not _allow_request(self.client_address[0]):
            self.send_json(429, {'ok': False, 'error': 'rate_limited'})
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
            if self.path == '/v1/search':
                bbox, collection = _verify_search_request(payload)
                assets = search_assets(bbox, collection)
                self.send_json(200, {
                    'ok': True,
                    'worker_version': VERSION,
                    'collection': collection,
                    'assets': assets,
                })
                return
            href = _verify_resolve_request(payload)
            resolved = resolve_asset(href)
            self.send_json(200, {'ok': True, 'worker_version': VERSION, **resolved})
        except PermissionError as exc:
            self.send_json(401, {'ok': False, 'error': str(exc)})
        except (ValueError, json.JSONDecodeError) as exc:
            self.send_json(400, {'ok': False, 'error': str(exc)})
        except Exception as exc:
            code = str(exc) if isinstance(exc, RuntimeError) else type(exc).__name__
            print(f'WORKER_REQUEST_ERROR {code}', flush=True)
            self.send_json(502, {'ok': False, 'error': code})


def main():
    port = int(os.getenv('PORT', '10000'))
    public_key_ok = True
    try:
        _raw_public_key()
    except RuntimeError:
        public_key_ok = False
    server = ThreadingHTTPServer(('0.0.0.0', port), Handler)
    print(f'TerrainTiles DGT worker {VERSION} listening on :{port}', flush=True)
    print('ED25519_PUBLIC_KEY_CONFIGURED', public_key_ok, flush=True)
    server.serve_forever()


if __name__ == '__main__':
    main()
