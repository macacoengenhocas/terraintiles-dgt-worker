#!/usr/bin/env python3
import html
import json
import os
import urllib.request
from http.server import ThreadingHTTPServer

import worker_v070 as mcp

VERSION = mcp.VERSION
mcp.production.VERSION = VERSION
mcp.core.VERSION = VERSION
mcp.core.core.VERSION = VERSION


def _roundtrip_call(port, request_id, method, params=None):
    payload = json.dumps({
        'jsonrpc': '2.0',
        'id': request_id,
        'method': method,
        'params': params or {},
    }, separators=(',', ':')).encode('utf-8')
    request = urllib.request.Request(
        f'http://127.0.0.1:{port}/mcp',
        data=payload,
        method='POST',
        headers={
            'Content-Type': 'application/json',
            'Accept': 'application/json',
            'MCP-Protocol-Version': mcp.MCP_MODERN_VERSION,
        },
    )
    with urllib.request.urlopen(request, timeout=5) as response:
        body = json.loads(response.read(mcp.MCP_MAX_BODY).decode('utf-8'))
        return response.status, body, response.headers.get('MCP-Protocol-Version')


def _roundtrip_check(port):
    try:
        discover_status, discover, discover_protocol = _roundtrip_call(port, 1, 'server/discover')
        tools_status, tools, tools_protocol = _roundtrip_call(port, 2, 'tools/list')
        resources_status, resources, resources_protocol = _roundtrip_call(port, 3, 'resources/list')
        widget_status, widget, widget_protocol = _roundtrip_call(port, 4, 'resources/read', {'uri': mcp.MCP_WIDGET_URI})
        create_status, create, create_protocol = _roundtrip_call(port, 5, 'tools/call', {
            'name': 'create_printable_terrain_model',
            'arguments': {
                'bbox': {'west': -9.1437, 'south': 38.7208, 'east': -9.1388, 'north': 38.7247},
                'scale': 2500,
                'elevation': 'global_dem',
                'printer_bed_mm': {'x': 220, 'y': 220, 'z': 250},
            },
        })
        tool_names = [item.get('name') for item in tools.get('result', {}).get('tools', [])]
        resource_uris = [item.get('uri') for item in resources.get('result', {}).get('resources', [])]
        widget_contents = widget.get('result', {}).get('contents', [])
        create_result = create.get('result', {})
        structured = create_result.get('structuredContent', {}) if isinstance(create_result, dict) else {}
        launch_url = structured.get('launch_url') if isinstance(structured, dict) else None
        statuses = [discover_status, tools_status, resources_status, widget_status, create_status]
        protocols = [discover_protocol, tools_protocol, resources_protocol, widget_protocol, create_protocol]
        ok = (
            statuses == [200, 200, 200, 200, 200]
            and all(value == mcp.MCP_MODERN_VERSION for value in protocols)
            and bool(discover.get('result'))
            and 'create_printable_terrain_model' in tool_names
            and mcp.MCP_WIDGET_URI in resource_uris
            and bool(widget_contents)
            and widget_contents[0].get('mimeType') == 'text/html;profile=mcp-app'
            and isinstance(launch_url, str)
            and launch_url.startswith(mcp.APP_ORIGIN + '/?')
            and 'tt_embed=chatgpt' in launch_url
        )
        return {
            'ok': ok,
            'transport': 'real-http-post-loopback',
            'version': mcp.MCP_APP_VERSION,
            'worker_version': VERSION,
            'protocol': mcp.MCP_MODERN_VERSION,
            'http_statuses': statuses,
            'response_protocol_headers': protocols,
            'tools': tool_names,
            'resources': resource_uris,
            'widget_mime': widget_contents[0].get('mimeType') if widget_contents else None,
            'create_launch_url_valid': bool(isinstance(launch_url, str) and launch_url.startswith(mcp.APP_ORIGIN + '/?') and 'tt_embed=chatgpt' in launch_url),
            'create_launch_url': launch_url,
        }
    except Exception as exc:
        return {
            'ok': False,
            'transport': 'real-http-post-loopback',
            'version': mcp.MCP_APP_VERSION,
            'worker_version': VERSION,
            'error': f'{type(exc).__name__}: {str(exc)[:240]}',
        }


class Handler(mcp.Handler):
    server_version = f'TerrainTiles/{VERSION}'

    def do_GET(self):
        path = self._path()
        if path == '/mcp/http-check':
            result = _roundtrip_check(self.server.server_address[1])
            self.send_mcp_json(200 if result.get('ok') else 503, result)
            return
        if path == '/mcp/check.html':
            result = _roundtrip_check(self.server.server_address[1])
            rendered = html.escape(json.dumps(result, ensure_ascii=False, indent=2))
            markup = f'<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>TerrainTiles MCP Check</title><style>body{{margin:0;padding:24px;background:#0d1113;color:#f2f6f7;font:14px/1.5 ui-monospace,monospace}}pre{{white-space:pre-wrap;overflow-wrap:anywhere}}</style></head><body><pre id="out">{rendered}</pre></body></html>'
            self.send_html(200 if result.get('ok') else 503, markup)
            return
        super().do_GET()


def main():
    port = int(os.getenv('PORT', '10000'))
    public_key_ok = True
    try:
        mcp.core._raw_public_key()
    except RuntimeError:
        public_key_ok = False
    server = ThreadingHTTPServer(('0.0.0.0', port), Handler)
    print(f'TerrainTiles worker+MCP {VERSION} listening on :{port}', flush=True)
    print('ED25519_PUBLIC_KEY_CONFIGURED', public_key_ok, flush=True)
    print('MCP_ENDPOINT', mcp.MCP_ORIGIN + '/mcp', flush=True)
    print('MCP_HTTP_SELF_CHECK', '/mcp/http-check', flush=True)
    server.serve_forever()


if __name__ == '__main__':
    main()
