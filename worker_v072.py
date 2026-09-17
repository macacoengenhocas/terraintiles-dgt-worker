#!/usr/bin/env python3
import html
import json
import os
from http.server import ThreadingHTTPServer

import worker_v071 as qa

VERSION = qa.VERSION


class Handler(qa.Handler):
    server_version = f'TerrainTiles/{VERSION}'

    def do_GET(self):
        if self._path() == '/mcp/check.html':
            result = qa._roundtrip_check(self.server.server_address[1])
            passed = bool(result.get('ok'))
            title = 'TerrainTiles MCP PASS' if passed else 'TerrainTiles MCP FAIL'
            rendered = html.escape(json.dumps(result, ensure_ascii=False, indent=2))
            description = html.escape('Five real HTTP POST MCP roundtrips passed.' if passed else 'MCP HTTP roundtrip validation failed.')
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
    print('MCP_HTTP_SELF_CHECK', '/mcp/check.html', flush=True)
    server.serve_forever()


if __name__ == '__main__':
    main()
