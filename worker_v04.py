#!/usr/bin/env python3
import html.parser
import http.cookiejar
import ipaddress
import json
import os
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

VERSION = '0.4.0'
MAX_READ = 128 * 1024
DGT_MAIN = 'https://cdd.dgterritorio.gov.pt'
DGT_STAC = f'{DGT_MAIN}/dgt-be/v1/search'
DGT_AUTH = 'https://auth.cdd.dgterritorio.gov.pt/realms/dgterritorio/protocol/openid-connect/auth'
DGT_REDIRECT = f'{DGT_MAIN}/auth/callback'
DGT_CLIENT_ID = 'aai-oidc-dgt'
DGT_USER_AGENT = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
ALLOWED_ASSET_HOST = 'cdd.dgterritorio.gov.pt'
ALLOWED_ASSET_PREFIX = '/dgt-be/'
TIFF_MAGIC = {
    bytes.fromhex('49492a00'),
    bytes.fromhex('4d4d002a'),
    bytes.fromhex('49492b00'),
    bytes.fromhex('4d4d002b'),
}

DGT_SESSION_HEADERS = {
    'User-Agent': DGT_USER_AGENT,
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
    'Accept-Language': 'pt-PT,pt;q=0.9,en;q=0.8',
    'Upgrade-Insecure-Requests': '1',
    'Sec-Fetch-Dest': 'document',
    'Sec-Fetch-Mode': 'navigate',
    'Sec-Fetch-Site': 'same-origin',
}


def env_enabled(name):
    return os.getenv(name, '0').strip().lower() in {'1', 'true', 'yes', 'on'}


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


def validate_https_public(url, *, asset=False):
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != 'https' or not parsed.hostname:
        raise ValueError('URL must use HTTPS')
    if parsed.username or parsed.password:
        raise ValueError('URL userinfo is not allowed')
    if parsed.port not in (None, 443):
        raise ValueError('non-standard HTTPS port is not allowed')
    if asset:
        if parsed.hostname.lower() != ALLOWED_ASSET_HOST:
            raise ValueError('asset host not allowed')
        if not parsed.path.startswith(ALLOWED_ASSET_PREFIX):
            raise ValueError('asset path not allowed')
    if not is_public_host(parsed.hostname):
        raise ValueError('target did not resolve to public IPs')
    return parsed


class SafeRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        validate_https_public(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class LoginFormParser(html.parser.HTMLParser):
    def __init__(self):
        super().__init__()
        self.in_form = False
        self.action = None
        self.hidden = {}

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag.lower() == 'form' and attrs.get('id') == 'kc-form-login':
            self.in_form = True
            self.action = attrs.get('action')
            return
        if self.in_form and tag.lower() == 'input' and attrs.get('type', 'text').lower() == 'hidden':
            name = attrs.get('name')
            if name:
                self.hidden[name] = attrs.get('value', '')

    def handle_endtag(self, tag):
        if tag.lower() == 'form' and self.in_form:
            self.in_form = False


def headers(extra=None):
    result = dict(DGT_SESSION_HEADERS)
    if extra:
        result.update(extra)
    return result


def opener_for(jar):
    return urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(jar),
        SafeRedirect(),
    )


def request(opener, url, *, method='GET', data=None, extra_headers=None, timeout=30):
    req = urllib.request.Request(
        url,
        data=data,
        headers=headers(extra_headers),
        method=method,
    )
    return opener.open(req, timeout=timeout)


def find_tiff_assets(data, collection):
    found = []
    seen = set()
    features = data.get('features', []) if isinstance(data, dict) else []
    for feature in features:
        if not isinstance(feature, dict) or feature.get('collection') != collection:
            continue
        assets = feature.get('assets') or {}
        if not isinstance(assets, dict):
            continue
        for asset in assets.values():
            if not isinstance(asset, dict):
                continue
            href = asset.get('href')
            mime = str(asset.get('type') or '').lower()
            if not isinstance(href, str) or href in seen:
                continue
            if 'tiff' not in mime and not urllib.parse.urlparse(href).path.lower().endswith(('.tif', '.tiff')):
                continue
            try:
                validate_https_public(href, asset=True)
            except ValueError:
                continue
            found.append(href)
            seen.add(href)
    return found


def authenticate_and_search(username, password, bbox, collection):
    jar = http.cookiejar.CookieJar()
    opener = opener_for(jar)

    with request(opener, DGT_MAIN) as response:
        response.read(1)

    auth_url = DGT_AUTH + '?' + urllib.parse.urlencode({
        'client_id': DGT_CLIENT_ID,
        'response_type': 'code',
        'redirect_uri': DGT_REDIRECT,
        'scope': 'openid profile email',
    })
    with request(opener, auth_url) as response:
        auth_html = response.read(1024 * 1024).decode('utf-8', 'replace')
        referer = response.geturl()

    parser = LoginFormParser()
    parser.feed(auth_html)
    if not parser.action:
        raise RuntimeError('DGT_LOGIN_FORM_CHANGED')

    login_url = urllib.parse.urljoin('https://auth.cdd.dgterritorio.gov.pt/', parser.action)
    parsed_login = urllib.parse.urlparse(login_url)
    if parsed_login.scheme != 'https' or parsed_login.hostname != 'auth.cdd.dgterritorio.gov.pt':
        raise RuntimeError('DGT_LOGIN_TARGET_INVALID')

    form = dict(parser.hidden)
    form['username'] = username
    form['password'] = password
    encoded = urllib.parse.urlencode(form).encode('utf-8')
    login_headers = {
        'Content-Type': 'application/x-www-form-urlencoded',
        'Origin': 'https://auth.cdd.dgterritorio.gov.pt',
        'Referer': referer,
    }
    with request(opener, login_url, method='POST', data=encoded, extra_headers=login_headers) as response:
        final_url = urllib.parse.urlparse(response.geturl())
        final_html = response.read(1024 * 1024).decode('utf-8', 'replace')

    if final_url.hostname != 'cdd.dgterritorio.gov.pt':
        if final_url.hostname == 'auth.cdd.dgterritorio.gov.pt' and 'kc-form-login' in final_html:
            raise RuntimeError('DGT_LOGIN_REJECTED')
        raise RuntimeError(f'DGT_LOGIN_UNEXPECTED_HOST_{final_url.hostname or "none"}')

    payload = json.dumps({
        'bbox': bbox,
        'limit': 1000,
        'collections': [collection],
    }).encode('utf-8')
    with request(
        opener,
        DGT_STAC,
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
    data = json.loads(raw.decode('utf-8'))
    return opener, find_tiff_assets(data, collection)


def probe_asset(opener, href):
    validate_https_public(href, asset=True)
    started = time.monotonic()
    req = urllib.request.Request(
        href,
        headers={
            'Range': f'bytes=0-{MAX_READ - 1}',
            'User-Agent': DGT_USER_AGENT,
            'Accept': 'application/octet-stream,*/*;q=0.8',
            'Accept-Language': 'pt-PT,pt;q=0.9,en;q=0.8',
        },
        method='GET',
    )
    try:
        with opener.open(req, timeout=30) as response:
            data = response.read(MAX_READ)
            status = getattr(response, 'status', None) or response.getcode()
            final_url = urllib.parse.urlparse(response.geturl())
            content_type = response.headers.get('Content-Type', '')
            content_range = response.headers.get('Content-Range')
            content_length = response.headers.get('Content-Length')
    except urllib.error.HTTPError as exc:
        body = exc.read(4096)
        return {
            'ok': False,
            'stage': 'asset_gate_http',
            'status': exc.code,
            'reason': str(exc.reason),
            'body_prefix_hex': body[:32].hex(),
            'elapsed_ms': round((time.monotonic() - started) * 1000),
        }
    except urllib.error.URLError as exc:
        return {
            'ok': False,
            'stage': 'asset_network',
            'error': str(exc.reason),
            'elapsed_ms': round((time.monotonic() - started) * 1000),
        }

    magic = data[:4]
    return {
        'ok': status in (200, 206) and magic in TIFF_MAGIC,
        'stage': 'asset_object',
        'status': status,
        'final_host': final_url.hostname,
        'content_type': content_type,
        'content_range': content_range,
        'content_length': content_length,
        'bytes_read': len(data),
        'magic_hex': magic.hex(),
        'tiff_magic': magic in TIFF_MAGIC,
        'elapsed_ms': round((time.monotonic() - started) * 1000),
    }


def self_test():
    if not env_enabled('TT_SELF_TEST_ON_START'):
        return
    username = os.getenv('DGT_CDD_USER', 'terraintiles@proton.me').strip()
    password = os.getenv('DGT_CDD_PASS', '')
    started = time.monotonic()
    if not password:
        print('TT_SELF_TEST ' + json.dumps({
            'ok': False,
            'stage': 'config',
            'error': 'DGT_CDD_PASS_missing',
        }, separators=(',', ':')), flush=True)
        return

    bbox = [-9.145, 38.715, -9.14, 38.72]
    collection = 'MDT-50cm'
    try:
        opener, assets = authenticate_and_search(username, password, bbox, collection)
        result = {
            'authenticated': True,
            'collection': collection,
            'asset_count': len(assets),
        }
        if assets:
            result.update(probe_asset(opener, assets[0]))
        else:
            result.update({
                'ok': False,
                'stage': 'search',
                'error': 'DGT_NO_RASTER_COVERAGE',
            })
        result['total_elapsed_ms'] = round((time.monotonic() - started) * 1000)
    except urllib.error.HTTPError as exc:
        body = exc.read(4096)
        result = {
            'ok': False,
            'stage': 'auth_or_stac_http',
            'status': exc.code,
            'reason': str(exc.reason),
            'body_prefix_hex': body[:32].hex(),
            'total_elapsed_ms': round((time.monotonic() - started) * 1000),
        }
    except Exception as exc:
        result = {
            'ok': False,
            'stage': 'auth_or_stac',
            'error': str(exc),
            'error_type': type(exc).__name__,
            'total_elapsed_ms': round((time.monotonic() - started) * 1000),
        }
    print('TT_SELF_TEST ' + json.dumps(result, separators=(',', ':')), flush=True)


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
                'self_test_on_start': env_enabled('TT_SELF_TEST_ON_START'),
                'dgt_secret_configured': bool(os.getenv('DGT_CDD_PASS')),
            })
            return
        self.send_json(404, {'ok': False, 'error': 'not_found'})

    def do_POST(self):
        self.send_json(404, {'ok': False, 'error': 'not_found'})


def main():
    port = int(os.getenv('PORT', '10000'))
    server = ThreadingHTTPServer(('0.0.0.0', port), Handler)
    print(f'TerrainTiles DGT worker {VERSION} listening on :{port}', flush=True)
    threading.Thread(target=self_test, daemon=True).start()
    server.serve_forever()


if __name__ == '__main__':
    main()
