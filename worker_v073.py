#!/usr/bin/env python3
import html
import json
import os
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import worker_v071 as qa
import worker_v072 as visible

VERSION = qa.VERSION


def _frame_check():
    request = urllib.request.Request(
        qa.mcp.APP_ORIGIN + '/',
        method='HEAD',
        headers={'User-Agent': f'TerrainTiles-MCP-QA/{VERSION}'},
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            headers = response.headers
            status = response.status
    except urllib.error.HTTPError as exc:
        if exc.code not in (405, 501):
            return {'ok': False, 'error': f'HTTP {exc.code}'}
        request = urllib.request.Request(
            qa.mcp.APP_ORIGIN + '/',
            method='GET',
            headers={'User-Agent': f'TerrainTiles-MCP-QA/{VERSION}', 'Range': 'bytes=0-0'},
        )
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                headers = response.headers
                status = response.status
        except Exception as inner:
            return {'ok': False, 'error': f'{type(inner).__name__}: {str(inner)[:180]}'}
    except Exception as exc:
        return {'ok': False, 'error': f'{type(exc).__name__}: {str(exc)[:180]}'}

    xfo = (headers.get('X-Frame-Options') or '').strip()
    csp = (headers.get('Content-Security-Policy') or '').strip()
    xfo_lower = xfo.lower()
    blocked_by_xfo = xfo_lower in {'deny', 'sameorigin'} or xfo_lower.startswith('allow-from')
    frame_ancestors = ''
    for directive in csp.split(';'):
        token = directive.strip()
        if token.lower().startswith('frame-ancestors'):
            frame_ancestors = token
            break
    blocked_by_csp = False
    if frame_ancestors:
        value = frame_ancestors.lower()
        if "'none'" in value:
            blocked_by_csp = True
        elif value.strip() in {"frame-ancestors 'self'", 'frame-ancestors self'}:
            blocked_by_csp = True
    return {
        'ok': status in (200, 206) and not blocked_by_xfo and not blocked_by_csp,
        'http_status': status,
        'x_frame_options': xfo or None,
        'frame_ancestors': frame_ancestors or None,
        'content_security_policy_present': bool(csp),
        'blocked_by_x_frame_options': blocked_by_xfo,
        'blocked_by_frame_ancestors': blocked_by_csp,
    }


def _full_check(port):
    result = qa._roundtrip_check(port)
    frame = _frame_check()
    result['frame_embed'] = frame
    result['ok'] = bool(result.get('ok') and frame.get('ok'))
    return result


class Handler(visible.Handler):
    server_version = f'TerrainTiles/{VERSION}'

    def do_GET(self):
        path = self._path()
        if path == '/mcp/frame-check':
            result = _frame_check()
            self.send_mcp_json(200 if result.get('ok') else 503, result)
            return
        if path == '/mcp/check.html':
            result = _full_check(self.server.server_address[1])
            passed = bool(result.get('ok'))
            title = 'TerrainTiles MCP EMBED PASS' if passed else 'TerrainTiles MCP EMBED FAIL'
            rendered = html.escape(json.dumps(result, ensure_ascii=False, indent=2))
            description = html.escape('MCP POST roundtrips and embedded-app frame headers passed.' if passed else 'MCP or embedded-app frame validation failed.')
            markup = f'<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>{title}</title><meta name="description" content="{description}"></head><body><h1>{title}</h1><pre id="out">{rendered}</pre></body></html>'
            self.send_html(200 if passed else 503, markup)
            return
        super().do_GET()


def main():
    port = int(os.getenv('PORT', '10000'))
    public_key_ok = True
    try:
        qa.mcp.core._raw_public_key()
    except RuntimeError:
        public_key_ok = False
    server = ThreadingHTTPServer(('0.0.0.0', port), Handler)
    print(f'TerrainTiles worker+MCP {VERSION} listening on :{port}', flush=True)
    print('ED25519_PUBLIC_KEY_CONFIGURED', public_key_ok, flush=True)
    print('MCP_ENDPOINT', qa.mcp.MCP_ORIGIN + '/mcp', flush=True)
    print('MCP_FULL_SELF_CHECK', '/mcp/check.html', flush=True)
    server.serve_forever()


if __name__ == '__main__':
    main()
