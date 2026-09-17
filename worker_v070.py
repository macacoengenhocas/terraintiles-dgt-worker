#!/usr/bin/env python3
import html
import json
import math
import os
import re
import threading
import time
import urllib.parse
import urllib.request
from http.server import ThreadingHTTPServer

import worker_v065 as production

core = production.core
VERSION = '0.7.0'
MCP_APP_VERSION = '0.1.0'
MCP_MODERN_VERSION = '2026-07-28'
MCP_LEGACY_VERSION = '2025-11-25'
MCP_WIDGET_URI = 'ui://terraintiles/builder.html'
APP_ORIGIN = 'https://terraintiles-web-g1o5ka.v2.appdeploy.ai'
MCP_ORIGIN = 'https://terraintiles-dgt-worker-frankfurt.onrender.com'
MCP_MAX_BODY = 64 * 1024
MCP_RATE_WINDOW = 60
MCP_RATE_MAX = 60

production.VERSION = VERSION
core.VERSION = VERSION
core.core.VERSION = VERSION

_mcp_rate_lock = threading.Lock()
_mcp_rate_events = {}


def _mcp_allowed(ip):
    now = time.monotonic()
    cutoff = now - MCP_RATE_WINDOW
    with _mcp_rate_lock:
        events = [seen for seen in _mcp_rate_events.get(ip, []) if seen >= cutoff]
        if len(events) >= MCP_RATE_MAX:
            _mcp_rate_events[ip] = events
            return False
        events.append(now)
        _mcp_rate_events[ip] = events
        return True


def _valid_bbox(values):
    if not isinstance(values, (list, tuple)) or len(values) != 4:
        return False
    try:
        west, south, east, north = [float(value) for value in values]
    except (TypeError, ValueError):
        return False
    return (
        all(math.isfinite(value) for value in (west, south, east, north))
        and -180 <= west < east <= 180
        and -90 <= south < north <= 90
    )


def _parse_bbox(value):
    if not isinstance(value, dict):
        return None
    values = [value.get('west'), value.get('south'), value.get('east'), value.get('north')]
    if not _valid_bbox(values):
        return None
    return [float(item) for item in values]


def _dimensions(bbox, scale):
    west, south, east, north = bbox
    radius = 6371008.8
    mid_lat = math.radians((south + north) / 2.0)
    width_m = abs(math.radians(east - west)) * radius * math.cos(mid_lat)
    height_m = abs(math.radians(north - south)) * radius
    return {
        'ground_width_m': round(width_m, 3),
        'ground_height_m': round(height_m, 3),
        'model_width_mm': round(width_m * 1000.0 / scale, 3),
        'model_height_mm': round(height_m * 1000.0 / scale, 3),
        'area_km2': round(width_m * height_m / 1_000_000.0, 6),
    }


def _mainland_portugal_candidate(bbox):
    west, south, east, north = bbox
    return west >= -9.7 and east <= -6.0 and south >= 36.8 and north <= 42.3 and _dimensions(bbox, 25000)['area_km2'] <= 200


def _source_plan(bbox, product):
    if _mainland_portugal_candidate(bbox):
        fallback = ['copernicus-glo30', 'terrarium'] if product == 'MDS' else ['terrarium']
        return {
            'provider': 'dgt-cdd',
            'product': product,
            'resolution_m': 0.5,
            'availability': 'validated_at_generation',
            'status': 'candidate',
            'fallback': fallback,
            'note': 'DGT is preferred in mainland Portugal but live availability is checked only during generation.',
        }
    if product == 'MDS':
        return {
            'provider': 'copernicus-glo30',
            'product': 'DSM',
            'resolution_m': 30,
            'status': 'fallback',
            'fallback': ['terrarium'],
        }
    return {
        'provider': 'terrarium',
        'product': 'DEM',
        'resolution_m': None,
        'status': 'global',
        'fallback': [],
    }


def _coordinate_candidate(query):
    match = re.match(r'^\s*(-?\d+(?:\.\d+)?)\s*[,; ]\s*(-?\d+(?:\.\d+)?)\s*$', query)
    if not match:
        return None
    lat, lon = float(match.group(1)), float(match.group(2))
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return None
    delta = 0.004
    return {
        'name': f'{lat:.6f}, {lon:.6f}',
        'lat': lat,
        'lon': lon,
        'bbox': {'west': lon - delta, 'south': lat - delta, 'east': lon + delta, 'north': lat + delta},
        'type': 'coordinates',
    }


def _geocode(query):
    text = str(query or '').strip()[:180]
    if len(text) < 2:
        raise ValueError('location_query_too_short')
    coordinate = _coordinate_candidate(text)
    if coordinate:
        return [coordinate]
    url = 'https://nominatim.openstreetmap.org/search?' + urllib.parse.urlencode({
        'format': 'jsonv2',
        'limit': '5',
        'q': text,
    })
    request = urllib.request.Request(url, headers={
        'User-Agent': f'TerrainTiles-ChatGPT/{VERSION}',
        'Accept': 'application/json',
        'Accept-Language': 'pt-PT,pt;q=0.9,en;q=0.7',
    })
    with urllib.request.urlopen(request, timeout=10) as response:
        raw = json.loads(response.read(1024 * 1024).decode('utf-8'))
    results = []
    for item in raw if isinstance(raw, list) else []:
        try:
            lat, lon = float(item.get('lat')), float(item.get('lon'))
        except (TypeError, ValueError):
            continue
        box = item.get('boundingbox')
        bbox = None
        if isinstance(box, list) and len(box) == 4:
            try:
                south, north, west, east = [float(value) for value in box]
                values = [west, south, east, north]
                if _valid_bbox(values):
                    bbox = {'west': west, 'south': south, 'east': east, 'north': north}
            except (TypeError, ValueError):
                bbox = None
        results.append({
            'name': str(item.get('display_name') or ''),
            'lat': lat,
            'lon': lon,
            'bbox': bbox,
            'type': str(item.get('type') or ''),
        })
    return results


def _tool_definitions():
    bbox_schema = {
        'type': 'object',
        'properties': {
            'west': {'type': 'number'},
            'south': {'type': 'number'},
            'east': {'type': 'number'},
            'north': {'type': 'number'},
        },
        'required': ['west', 'south', 'east', 'north'],
        'additionalProperties': False,
    }
    return [
        {
            'name': 'find_terrain_location',
            'title': 'Find a place for a terrain model',
            'description': 'Find a place, landmark, address or coordinates for a 3D-printable topographic model, terrain STL/3MF, relief map or maquete topografica. Returns candidate coordinates and bounding boxes.',
            'inputSchema': {
                'type': 'object',
                'properties': {
                    'query': {'type': 'string', 'minLength': 2, 'maxLength': 180},
                },
                'required': ['query'],
                'additionalProperties': False,
            },
            'annotations': {'readOnlyHint': True},
        },
        {
            'name': 'plan_terrain_model',
            'title': 'Plan a printable terrain model',
            'description': 'Check physical size, print scale and elevation-source strategy for a selected geographic bounding box before generating a terrain model.',
            'inputSchema': {
                'type': 'object',
                'properties': {
                    'bbox': bbox_schema,
                    'scale': {'type': 'integer', 'minimum': 100, 'maximum': 200000, 'default': 25000},
                    'product': {'type': 'string', 'enum': ['MDT', 'MDS'], 'default': 'MDT'},
                },
                'required': ['bbox'],
                'additionalProperties': False,
            },
            'annotations': {'readOnlyHint': True},
        },
        {
            'name': 'create_printable_terrain_model',
            'title': 'Create a 3D-printable terrain model',
            'description': 'Turn a real place or geographic area into a printable topographic model, physical terrain maquette, STL, 3MF or modular terrain tiles. Opens the interactive TerrainTiles builder inside ChatGPT, prefilled with place, bounds, scale, elevation mode and printer dimensions.',
            'inputSchema': {
                'type': 'object',
                'properties': {
                    'location': {'type': 'string', 'maxLength': 180},
                    'bbox': bbox_schema,
                    'scale': {'type': 'integer', 'minimum': 100, 'maximum': 200000, 'default': 25000},
                    'elevation': {'type': 'string', 'enum': ['auto_mdt', 'auto_mds', 'global_dem'], 'default': 'auto_mdt'},
                    'model_width_mm': {'type': 'number', 'minimum': 10, 'maximum': 2000},
                    'printer_bed_mm': {
                        'type': 'object',
                        'properties': {
                            'x': {'type': 'number', 'minimum': 20, 'maximum': 2000},
                            'y': {'type': 'number', 'minimum': 20, 'maximum': 2000},
                            'z': {'type': 'number', 'minimum': 20, 'maximum': 2000},
                        },
                        'required': ['x', 'y', 'z'],
                        'additionalProperties': False,
                    },
                    'engineering_mode': {'type': 'boolean', 'default': False},
                },
                'additionalProperties': False,
            },
            'annotations': {'readOnlyHint': True},
            '_meta': {
                'ui': {'resourceUri': MCP_WIDGET_URI},
                'openai/outputTemplate': MCP_WIDGET_URI,
                'openai/toolInvocation/invoking': 'A preparar a maquete...',
                'openai/toolInvocation/invoked': 'TerrainTiles pronto',
                'openai/widgetAccessible': True,
            },
        },
    ]


def _launch_url(args):
    query = {'tt_embed': 'chatgpt'}
    location = str(args.get('location') or '').strip()[:180]
    if location:
        query['location'] = location
    bbox = _parse_bbox(args.get('bbox'))
    if bbox:
        query['bbox'] = ','.join(str(value) for value in bbox)
    try:
        scale = int(round(float(args.get('scale', 25000))))
    except (TypeError, ValueError):
        scale = 25000
    query['scale'] = str(max(100, min(200000, scale)))
    elevation = str(args.get('elevation') or 'auto_mdt')
    query['elevation'] = elevation if elevation in {'auto_mdt', 'auto_mds', 'global_dem'} else 'auto_mdt'
    try:
        width = float(args.get('model_width_mm'))
    except (TypeError, ValueError):
        width = 0
    if 10 <= width <= 2000:
        query['width_mm'] = str(width)
    bed = args.get('printer_bed_mm')
    if isinstance(bed, dict):
        for axis in ('x', 'y', 'z'):
            try:
                value = float(bed.get(axis))
            except (TypeError, ValueError):
                continue
            if 20 <= value <= 2000:
                query[f'bed_{axis}'] = str(value)
    if args.get('engineering_mode') is True:
        query['mode'] = 'engineering'
    return APP_ORIGIN + '/?' + urllib.parse.urlencode(query)


def _widget_html():
    return f'''<!doctype html><html lang="pt-PT"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><style>html,body{{margin:0;background:#0d1113;color:#f2f6f7;font:14px/1.4 system-ui,sans-serif}}*{{box-sizing:border-box}}.bar{{display:flex;align-items:center;gap:10px;padding:9px 11px;border-bottom:1px solid #2b3439;background:#111719}}.mark{{display:grid;place-items:center;width:28px;height:28px;border-radius:7px;background:#d8ff5a;color:#172000;font-weight:900}}.copy{{min-width:0;flex:1}}.copy b,.copy span{{display:block}}.copy span{{color:#93a0a6;font-size:11px}}.bar button{{border:1px solid #3a464d;border-radius:8px;background:#1a2024;color:#f2f6f7;padding:7px 9px;cursor:pointer}}iframe{{display:block;width:100%;height:680px;border:0;background:#0d1113}}</style></head><body><div class="bar"><div class="mark">TT</div><div class="copy"><b>TerrainTiles</b><span>territorio real -> modelo imprimivel, dentro do ChatGPT</span></div><button id="full">Ecrã completo</button></div><iframe id="terrain" title="TerrainTiles builder" allow="fullscreen; clipboard-write"></iframe><script>(()=>{{const frame=document.getElementById('terrain');const fallback='{APP_ORIGIN}/?tt_embed=chatgpt';function render(value){{const data=value&&typeof value==='object'?value:{{}};frame.src=typeof data.launch_url==='string'&&data.launch_url.startsWith('{APP_ORIGIN}/')?data.launch_url:fallback;}}render(window.openai?.toolOutput);window.addEventListener('openai:set_globals',event=>render(event.detail?.globals?.toolOutput||window.openai?.toolOutput),{{passive:true}});document.getElementById('full').onclick=()=>window.openai?.requestDisplayMode?.({{mode:'fullscreen'}});}})();</script></body></html>'''


def _resource_definition():
    return {
        'uri': MCP_WIDGET_URI,
        'name': 'TerrainTiles interactive builder',
        'title': 'TerrainTiles - printable terrain builder',
        'description': 'Interactive map, scale, elevation, modularisation, 3D validation and STL/3MF export.',
        'mimeType': 'text/html;profile=mcp-app',
    }


def _rpc_ok(request_id, result):
    return {'jsonrpc': '2.0', 'id': request_id, 'result': result}


def _rpc_error(request_id, code, message, data=None):
    error = {'code': code, 'message': message}
    if data is not None:
        error['data'] = data
    return {'jsonrpc': '2.0', 'id': request_id, 'error': error}


def _tool_call(name, args):
    if not isinstance(args, dict):
        args = {}
    if name == 'find_terrain_location':
        try:
            results = _geocode(args.get('query'))
            return {
                'structuredContent': {'results': results},
                'content': [{'type': 'text', 'text': f'Encontrei {len(results)} localizacao(oes).' if results else 'Nao encontrei localizacoes correspondentes.'}],
            }
        except Exception as exc:
            return {'isError': True, 'content': [{'type': 'text', 'text': f'Pesquisa de local indisponivel: {type(exc).__name__}'}]}
    if name == 'plan_terrain_model':
        bbox = _parse_bbox(args.get('bbox'))
        if not bbox:
            return {'isError': True, 'content': [{'type': 'text', 'text': 'Bounding box invalida.'}]}
        try:
            scale = int(round(float(args.get('scale', 25000))))
        except (TypeError, ValueError):
            scale = 25000
        scale = max(100, min(200000, scale))
        product = 'MDS' if args.get('product') == 'MDS' else 'MDT'
        result = {
            'bbox': bbox,
            'scale': scale,
            'product': product,
            'source_plan': _source_plan(bbox, product),
            'dimensions': _dimensions(bbox, scale),
        }
        return {'structuredContent': result, 'content': [{'type': 'text', 'text': f'Plano TerrainTiles pronto a 1:{scale}.'}]}
    if name == 'create_printable_terrain_model':
        launch_url = _launch_url(args)
        result = {
            'launch_url': launch_url,
            'location': args.get('location') if isinstance(args.get('location'), str) else None,
            'bbox': _parse_bbox(args.get('bbox')),
            'scale': max(100, min(200000, int(round(float(args.get('scale', 25000)))))) if str(args.get('scale', '')).replace('.', '', 1).isdigit() else 25000,
            'elevation': args.get('elevation') if args.get('elevation') in {'auto_mdt', 'auto_mds', 'global_dem'} else 'auto_mdt',
            'embedded': True,
        }
        return {
            'structuredContent': result,
            'content': [{'type': 'text', 'text': 'Abri o TerrainTiles dentro do ChatGPT. Confirma a zona e gera o modelo; o Download produz o pacote STL/3MF.'}],
            '_meta': {'launch_url': launch_url},
        }
    return {'isError': True, 'content': [{'type': 'text', 'text': f'Ferramenta desconhecida: {name}'}]}


def _dispatch_mcp(body):
    if not isinstance(body, dict):
        return _rpc_error(None, -32600, 'Invalid Request')
    request_id = body.get('id') if isinstance(body.get('id'), (str, int)) or body.get('id') is None else None
    method = body.get('method') if isinstance(body.get('method'), str) else ''
    params = body.get('params') if isinstance(body.get('params'), dict) else {}
    if method == 'server/discover':
        return _rpc_ok(request_id, {
            'supportedVersions': [MCP_MODERN_VERSION, MCP_LEGACY_VERSION],
            'capabilities': {'tools': {}, 'resources': {}, 'extensions': {'io.modelcontextprotocol/ui': {}}},
            'instructions': 'TerrainTiles converts real geographic areas into 3D-printable terrain models. Use create_printable_terrain_model for STL/3MF, topographic reliefs, maquetes topograficas and modular terrain.',
            '_meta': {'io.modelcontextprotocol/serverInfo': {'name': 'TerrainTiles', 'version': MCP_APP_VERSION}},
        })
    if method == 'initialize':
        requested = params.get('protocolVersion') if isinstance(params.get('protocolVersion'), str) else MCP_LEGACY_VERSION
        negotiated = requested if requested in {MCP_LEGACY_VERSION, '2025-06-18', '2025-03-26'} else MCP_LEGACY_VERSION
        return _rpc_ok(request_id, {
            'protocolVersion': negotiated,
            'capabilities': {'tools': {}, 'resources': {}},
            'serverInfo': {'name': 'TerrainTiles', 'version': MCP_APP_VERSION},
            'instructions': 'TerrainTiles creates 3D-printable terrain models from real places.',
        })
    if method == 'tools/list':
        return _rpc_ok(request_id, {'tools': _tool_definitions()})
    if method == 'resources/list':
        return _rpc_ok(request_id, {'resources': [_resource_definition()]})
    if method == 'resources/read':
        if params.get('uri') != MCP_WIDGET_URI:
            return _rpc_error(request_id, -32002, 'Resource not found')
        resource = _resource_definition()
        resource.update({
            'text': _widget_html(),
            '_meta': {
                'ui': {
                    'prefersBorder': False,
                    'csp': {
                        'connectDomains': [APP_ORIGIN],
                        'resourceDomains': [APP_ORIGIN],
                        'frameDomains': [APP_ORIGIN],
                    },
                },
                'openai/widgetDescription': 'Interactive TerrainTiles map and printable-model builder embedded in ChatGPT.',
                'openai/widgetPrefersBorder': False,
                'openai/widgetCSP': {
                    'connect_domains': [APP_ORIGIN],
                    'resource_domains': [APP_ORIGIN],
                    'frame_domains': [APP_ORIGIN],
                },
            },
        })
        return _rpc_ok(request_id, {'contents': [resource]})
    if method == 'tools/call':
        name = params.get('name') if isinstance(params.get('name'), str) else ''
        arguments = params.get('arguments') if isinstance(params.get('arguments'), dict) else {}
        return _rpc_ok(request_id, _tool_call(name, arguments))
    if method == 'ping':
        return _rpc_ok(request_id, {})
    if method == 'notifications/initialized':
        return _rpc_ok(request_id, {})
    return _rpc_error(request_id, -32601, 'Method not found', {'method': method})


def _self_check():
    discover = _dispatch_mcp({'jsonrpc': '2.0', 'id': 1, 'method': 'server/discover', 'params': {}})
    tools = _dispatch_mcp({'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list', 'params': {}})
    resources = _dispatch_mcp({'jsonrpc': '2.0', 'id': 3, 'method': 'resources/list', 'params': {}})
    tool_names = [item.get('name') for item in tools.get('result', {}).get('tools', [])]
    resource_uris = [item.get('uri') for item in resources.get('result', {}).get('resources', [])]
    return {
        'ok': bool(discover.get('result') and 'create_printable_terrain_model' in tool_names and MCP_WIDGET_URI in resource_uris),
        'version': MCP_APP_VERSION,
        'protocols': discover.get('result', {}).get('supportedVersions', []),
        'tools': tool_names,
        'resources': resource_uris,
        'app_origin': APP_ORIGIN,
    }


class Handler(core.Handler):
    server_version = f'TerrainTiles/{VERSION}'

    def _path(self):
        return urllib.parse.urlsplit(self.path).path

    def send_mcp_json(self, status, payload):
        raw = json.dumps(payload, separators=(',', ':'), ensure_ascii=False).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(raw)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Headers', 'content-type,accept,mcp-protocol-version,mcp-method,mcp-name')
        self.send_header('Access-Control-Allow-Methods', 'GET,POST,OPTIONS')
        self.send_header('MCP-Protocol-Version', MCP_MODERN_VERSION)
        self.end_headers()
        self.wfile.write(raw)

    def send_html(self, status, markup):
        raw = markup.encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('Content-Length', str(len(raw)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(raw)

    def do_OPTIONS(self):
        if self._path() == '/mcp':
            self.send_response(204)
            self.send_header('Access-Control-Allow-Origin', '*')
            self.send_header('Access-Control-Allow-Headers', 'content-type,accept,mcp-protocol-version,mcp-method,mcp-name')
            self.send_header('Access-Control-Allow-Methods', 'GET,POST,OPTIONS')
            self.send_header('Access-Control-Max-Age', '600')
            self.end_headers()
            return
        self.send_response(404)
        self.end_headers()

    def do_GET(self):
        path = self._path()
        if path == '/mcp':
            self.send_mcp_json(200, {
                'ok': True,
                'name': 'TerrainTiles',
                'version': MCP_APP_VERSION,
                'protocol_versions': [MCP_MODERN_VERSION, MCP_LEGACY_VERSION],
                'endpoint': MCP_ORIGIN + '/mcp',
                'tools': [tool['name'] for tool in _tool_definitions()],
                'widget': MCP_WIDGET_URI,
                'privacy': APP_ORIGIN + '/privacy.html',
                'terms': APP_ORIGIN + '/terms.html',
            })
            return
        if path == '/mcp/check':
            self.send_mcp_json(200, _self_check())
            return
        if path == '/mcp/check.html':
            markup = '''<!doctype html><html><head><meta charset="utf-8"><title>TerrainTiles MCP Check</title></head><body><pre id="out">RUNNING</pre><script>const out=document.getElementById('out');async function rpc(id,method,params={}){const r=await fetch('/mcp',{method:'POST',headers:{'content-type':'application/json','accept':'application/json'},body:JSON.stringify({jsonrpc:'2.0',id,method,params})});if(!r.ok)throw new Error('HTTP '+r.status);return r.json()}(async()=>{try{const discover=await rpc(1,'server/discover',{});const tools=await rpc(2,'tools/list',{});const resources=await rpc(3,'resources/list',{});const names=tools.result?.tools?.map(x=>x.name)||[];const uris=resources.result?.resources?.map(x=>x.uri)||[];out.textContent=JSON.stringify({ok:Boolean(discover.result&&names.includes('create_printable_terrain_model')&&uris.includes('ui://terraintiles/builder.html')),protocols:discover.result?.supportedVersions,tools:names,resources:uris},null,2)}catch(e){out.textContent=JSON.stringify({ok:false,error:String(e)},null,2)}})();</script></body></html>'''
            self.send_html(200, markup)
            return
        super().do_GET()

    def do_POST(self):
        if self._path() != '/mcp':
            super().do_POST()
            return
        if not _mcp_allowed(self.client_address[0]):
            self.send_mcp_json(429, _rpc_error(None, -32029, 'Rate limit exceeded'))
            return
        try:
            length = int(self.headers.get('Content-Length', '0'))
        except ValueError:
            self.send_mcp_json(400, _rpc_error(None, -32600, 'Invalid Content-Length'))
            return
        if length <= 0 or length > MCP_MAX_BODY:
            self.send_mcp_json(413, _rpc_error(None, -32600, 'Invalid request size'))
            return
        try:
            body = json.loads(self.rfile.read(length).decode('utf-8'))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self.send_mcp_json(400, _rpc_error(None, -32700, 'Parse error'))
            return
        if isinstance(body, dict) and body.get('method') == 'notifications/initialized' and 'id' not in body:
            self.send_response(204)
            self.send_header('Access-Control-Allow-Origin', '*')
            self.send_header('MCP-Protocol-Version', MCP_MODERN_VERSION)
            self.end_headers()
            return
        self.send_mcp_json(200, _dispatch_mcp(body))


def main():
    port = int(os.getenv('PORT', '10000'))
    public_key_ok = True
    try:
        core._raw_public_key()
    except RuntimeError:
        public_key_ok = False
    server = ThreadingHTTPServer(('0.0.0.0', port), Handler)
    print(f'TerrainTiles worker+MCP {VERSION} listening on :{port}', flush=True)
    print('ED25519_PUBLIC_KEY_CONFIGURED', public_key_ok, flush=True)
    print('MCP_ENDPOINT', MCP_ORIGIN + '/mcp', flush=True)
    server.serve_forever()


if __name__ == '__main__':
    main()
