from __future__ import annotations

import math
import os
from typing import Annotated, Literal

from pydantic import BaseModel, Field
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, PlainTextResponse

from mcp.server import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations


VERSION = "0.1.0"
RESOURCE_URI = "ui://terraintiles/builder-v1.html"
TERRAIN_ORIGIN = "https://terraintiles-web-g1o5ka.v2.appdeploy.ai"
PUBLIC_ORIGIN = os.environ.get("PUBLIC_ORIGIN", "").rstrip("/")


class CapabilitiesResult(BaseModel):
    version: str
    account_required: bool
    end_user_login_required: bool
    end_user_upload_required: bool
    elevation_policy: dict[str, list[str]]
    outputs: list[str]
    builder: str


class PlanResult(BaseModel):
    product: Literal["MDT", "MDS"]
    source_policy: list[str]
    ground_width_m: float
    ground_height_m: float
    print_width_mm: float
    print_height_mm: float
    scale: float
    tiles_x: int
    tiles_y: int
    total_tiles: int
    printer_bed_mm: dict[str, float]
    warnings: list[str]


class BuilderResult(BaseModel):
    mode: Literal["builder"]
    builder_url: str
    note: str


mcp = MCPServer(
    "TerrainTiles",
    title="TerrainTiles",
    description="Turn real-world terrain into modular 3D-printable topographic models.",
    instructions=(
        "Use TerrainTiles when the user wants to turn a real place, coordinates, or a "
        "geographic bounding box into a 3D-printable topographic model. Use "
        "plan_terrain_model for scale, physical-size and printer-fit calculations. "
        "Use render_terrain_builder when the user needs map selection, advanced controls, "
        "generation, validation, STL/3MF export, or a visual workflow. Never claim that "
        "DGT high-resolution data is available until the generation workflow validates it."
    ),
    website_url=TERRAIN_ORIGIN,
    version=VERSION,
)


READ_ONLY_CLOSED = ToolAnnotations(
    read_only_hint=True,
    destructive_hint=False,
    open_world_hint=False,
    idempotent_hint=True,
)

READ_ONLY_OPEN = ToolAnnotations(
    read_only_hint=True,
    destructive_hint=False,
    open_world_hint=True,
    idempotent_hint=True,
)


@mcp.tool(
    title="TerrainTiles capabilities",
    description=(
        "Use this tool when the user asks what TerrainTiles can generate, which elevation "
        "fallbacks it uses, which print formats it supports, or whether an end-user login "
        "or GeoTIFF upload is required. Do not use it to claim that DGT is currently online."
    ),
    annotations=READ_ONLY_CLOSED,
)
def terrain_capabilities() -> CapabilitiesResult:
    """Describe the stable capabilities and fallback policy of TerrainTiles."""
    return CapabilitiesResult(
        version=VERSION,
        account_required=False,
        end_user_login_required=False,
        end_user_upload_required=False,
        elevation_policy={
            "MDT": ["DGT high resolution when validated at generation", "Terrarium DEM fallback"],
            "MDS": [
                "DGT high resolution when validated at generation",
                "Copernicus GLO-30 DSM fallback",
                "Terrarium DEM final fallback",
            ],
        },
        outputs=["STL", "3MF", "project JSON", "manifest", "assembly guide"],
        builder=TERRAIN_ORIGIN,
    )


def _bbox_dimensions_m(bbox: list[float]) -> tuple[float, float]:
    if len(bbox) != 4:
        raise ValueError("bbox must contain exactly [west, south, east, north].")
    west, south, east, north = [float(value) for value in bbox]
    if not all(math.isfinite(value) for value in (west, south, east, north)):
        raise ValueError("bbox coordinates must be finite numbers.")
    if not (west < east and south < north):
        raise ValueError("bbox must satisfy west < east and south < north.")
    if west < -180 or east > 180 or south < -90 or north > 90:
        raise ValueError("bbox coordinates are outside WGS84 limits.")

    latitude = (south + north) / 2.0
    width_m = abs(east - west) * 111_320.0 * max(0.01, math.cos(math.radians(latitude)))
    height_m = abs(north - south) * 110_574.0
    if width_m < 5 or height_m < 5:
        raise ValueError("The selected area is too small; use at least about 5 × 5 m.")
    return width_m, height_m


@mcp.tool(
    title="Plan a printable terrain model",
    description=(
        "Use this tool when the user provides a real-world WGS84 bounding box and wants "
        "to know the physical model size, scale, printer fit, or number of modular pieces. "
        "It performs geometry and print-planning calculations only; it does not generate "
        "or upload a model file."
    ),
    annotations=READ_ONLY_CLOSED,
)
def plan_terrain_model(
    bbox: Annotated[
        list[float],
        Field(
            min_length=4,
            max_length=4,
            description="WGS84 bounding box [west, south, east, north].",
        ),
    ],
    product: Literal["MDT", "MDS"] = "MDT",
    scale: Annotated[
        float,
        Field(ge=250, le=100_000, description="Model scale denominator, e.g. 2500 for 1:2500."),
    ] = 2500,
    printer_bed_x_mm: Annotated[float, Field(ge=50, le=1000)] = 220,
    printer_bed_y_mm: Annotated[float, Field(ge=50, le=1000)] = 220,
    printer_bed_z_mm: Annotated[float, Field(ge=50, le=1500)] = 250,
    safety_margin_mm: Annotated[float, Field(ge=0, le=50)] = 5,
) -> PlanResult:
    """Calculate print dimensions and modular tiling without changing external state."""
    ground_width_m, ground_height_m = _bbox_dimensions_m(bbox)
    usable_x = printer_bed_x_mm - 2 * safety_margin_mm
    usable_y = printer_bed_y_mm - 2 * safety_margin_mm
    if usable_x <= 10 or usable_y <= 10:
        raise ValueError("Printer safety margin leaves no usable bed area.")

    print_width_mm = ground_width_m * 1000.0 / scale
    print_height_mm = ground_height_m * 1000.0 / scale
    tiles_x = max(1, math.ceil(print_width_mm / usable_x))
    tiles_y = max(1, math.ceil(print_height_mm / usable_y))
    total_tiles = tiles_x * tiles_y

    warnings: list[str] = []
    if print_width_mm < 20 or print_height_mm < 20:
        warnings.append("At this scale the physical model is very small; terrain detail may be hard to read.")
    if total_tiles > 36:
        warnings.append("More than 36 pieces are required; consider a smaller area or a smaller physical model.")
    warnings.append("DGT high-resolution availability is validated only during generation; TerrainTiles falls back automatically.")

    policy = (
        ["DGT high resolution when validated at generation", "Terrarium DEM fallback"]
        if product == "MDT"
        else [
            "DGT high resolution when validated at generation",
            "Copernicus GLO-30 DSM fallback",
            "Terrarium DEM final fallback",
        ]
    )

    return PlanResult(
        product=product,
        source_policy=policy,
        ground_width_m=round(ground_width_m, 1),
        ground_height_m=round(ground_height_m, 1),
        print_width_mm=round(print_width_mm, 1),
        print_height_mm=round(print_height_mm, 1),
        scale=float(scale),
        tiles_x=tiles_x,
        tiles_y=tiles_y,
        total_tiles=total_tiles,
        printer_bed_mm={
            "x": float(printer_bed_x_mm),
            "y": float(printer_bed_y_mm),
            "z": float(printer_bed_z_mm),
        },
        warnings=warnings,
    )


BUILDER_TOOL_META = {
    "ui": {"resourceUri": RESOURCE_URI},
    "openai/outputTemplate": RESOURCE_URI,
    "openai/toolInvocation/invoking": "A abrir o TerrainTiles…",
    "openai/toolInvocation/invoked": "TerrainTiles pronto.",
}


@mcp.tool(
    title="Open the TerrainTiles builder in ChatGPT",
    description=(
        "Use this tool when the user wants an interactive map, wants to choose an area "
        "visually, change advanced terrain/print settings, generate and validate the model, "
        "or export STL/3MF. It renders the TerrainTiles builder inside the conversation."
    ),
    annotations=READ_ONLY_OPEN,
    meta=BUILDER_TOOL_META,
)
def render_terrain_builder(
    note: Annotated[
        str,
        Field(max_length=500, description="Optional short task context to show alongside the builder."),
    ] = "",
) -> BuilderResult:
    """Render the existing TerrainTiles workflow in an MCP Apps interface."""
    return BuilderResult(
        mode="builder",
        builder_url=f"{TERRAIN_ORIGIN}/?embed=1",
        note=note.strip(),
    )


def _builder_resource_meta() -> dict:
    ui: dict = {
        "prefersBorder": False,
        "csp": {
            "connectDomains": [],
            "resourceDomains": [],
            "frameDomains": [TERRAIN_ORIGIN],
        },
    }
    if PUBLIC_ORIGIN:
        ui["domain"] = PUBLIC_ORIGIN

    meta: dict = {
        "ui": ui,
        "openai/widgetDescription": (
            "Interactive TerrainTiles map and terrain-to-print workflow. The user can "
            "select terrain, configure scale and print settings, generate and validate "
            "the model, then export the print package without leaving ChatGPT."
        ),
        "openai/widgetPrefersBorder": False,
        "openai/widgetCSP": {
            "connect_domains": [],
            "resource_domains": [],
            "frame_domains": [TERRAIN_ORIGIN],
            "redirect_domains": [TERRAIN_ORIGIN],
        },
    }
    if PUBLIC_ORIGIN:
        meta["openai/widgetDomain"] = PUBLIC_ORIGIN
    return meta


@mcp.resource(
    RESOURCE_URI,
    name="terraintiles_builder",
    title="TerrainTiles builder",
    description="Interactive TerrainTiles map, model settings, validation and export interface.",
    mime_type="text/html;profile=mcp-app",
    meta=_builder_resource_meta(),
)
def terrain_builder_resource() -> str:
    """Return the MCP Apps HTML wrapper for the hosted TerrainTiles builder."""
    frame_url = f"{TERRAIN_ORIGIN}/?embed=1"
    return f"""<!doctype html>
<html lang="pt-PT">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
html,body{{margin:0;min-height:100%;background:#090d0f;color:#e9f0f2;font-family:system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}}
.bar{{display:flex;align-items:center;gap:10px;padding:8px 10px;border-bottom:1px solid #273238;background:#0d1214}}
.bar strong{{flex:1;font-size:13px}}
button{{border:1px solid #38464c;background:#151d20;color:#e9f0f2;border-radius:9px;padding:7px 10px;cursor:pointer}}
iframe{{display:block;width:100%;height:680px;border:0;background:#090d0f}}
@media(max-width:640px){{iframe{{height:740px}}.bar{{flex-wrap:wrap}}}}
</style>
</head>
<body>
<div class="bar">
  <strong>TerrainTiles · território real → modelo imprimível</strong>
  <button id="full" type="button">Ecrã inteiro</button>
</div>
<iframe title="TerrainTiles builder" src="{frame_url}" allow="fullscreen"></iframe>
<script>
document.getElementById('full').addEventListener('click', function () {{
  if (window.openai && window.openai.requestDisplayMode) {{
    window.openai.requestDisplayMode({{ mode: 'fullscreen' }});
  }}
}});
</script>
</body>
</html>"""


@mcp.custom_route("/", methods=["GET"])
async def root(_: Request) -> HTMLResponse:
    origin = PUBLIC_ORIGIN or "this server"
    return HTMLResponse(
        f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>TerrainTiles MCP</title><style>body{{font:16px/1.6 system-ui;max-width:760px;margin:48px auto;padding:0 20px;color:#172126}}code{{background:#edf1f2;padding:3px 6px;border-radius:6px}}a{{color:#335d00}}</style></head>
<body><h1>TerrainTiles MCP</h1><p>Production MCP endpoint for TerrainTiles.</p>
<p>MCP: <code>{origin}/mcp</code></p>
<p><a href="/privacy">Privacy</a> · <a href="/terms">Terms</a> · <a href="/support">Support</a></p></body></html>"""
    )


@mcp.custom_route("/health", methods=["GET"])
async def health(_: Request) -> JSONResponse:
    return JSONResponse(
        {
            "ok": True,
            "service": "TerrainTiles MCP",
            "version": VERSION,
            "mcp_path": "/mcp",
            "tools": [
                "terrain_capabilities",
                "plan_terrain_model",
                "render_terrain_builder",
            ],
        }
    )


@mcp.custom_route("/.well-known/openai-apps-challenge", methods=["GET"])
async def openai_apps_challenge(_: Request):
    token = os.environ.get("OPENAI_APPS_CHALLENGE", "")
    if not token:
        return PlainTextResponse("challenge not configured", status_code=404)
    return PlainTextResponse(token, media_type="text/plain")


@mcp.custom_route("/privacy", methods=["GET"])
async def privacy(_: Request) -> HTMLResponse:
    return HTMLResponse(
        """<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>TerrainTiles Privacy</title></head>
<body style="font:16px/1.65 system-ui;max-width:760px;margin:40px auto;padding:0 20px;color:#172126">
<h1>TerrainTiles Plugin Privacy Policy</h1><p>Last updated: 18 September 2026.</p>
<p>TerrainTiles processes geographic bounds, scale, printer dimensions and model options that are necessary to answer the user's request. The initial plugin does not require a TerrainTiles account and does not request passwords or private account data.</p>
<p>The interactive builder may access public terrain and mapping services including DGT, Copernicus, Terrarium and OpenStreetMap. Infrastructure providers may retain normal technical logs for reliability and abuse prevention.</p>
<p>TerrainTiles does not sell conversation data or use plugin inputs to create an advertising profile. Do not place secrets in project names, coordinates or free-text fields.</p>
<p>This policy will be expanded with the verified developer/support identity before public directory submission.</p></body></html>"""
    )


@mcp.custom_route("/terms", methods=["GET"])
async def terms(_: Request) -> HTMLResponse:
    return HTMLResponse(
        """<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>TerrainTiles Terms</title></head>
<body style="font:16px/1.65 system-ui;max-width:760px;margin:40px auto;padding:0 20px;color:#172126">
<h1>TerrainTiles Plugin Terms</h1><p>Last updated: 18 September 2026.</p>
<p>TerrainTiles assists with planning and preparing topographic models for 3D printing. Results depend on source data, requested parameters and manufacturing tolerances.</p>
<p>Users must verify scale, data source, dimensions, tolerances and suitability before professional, safety-critical or operational use. External data services may be unavailable or change without notice.</p>
<p>The current service is a pre-release technical version. Final commercial terms will be published before public directory submission.</p></body></html>"""
    )


@mcp.custom_route("/support", methods=["GET"])
async def support(_: Request) -> HTMLResponse:
    return HTMLResponse(
        f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>TerrainTiles Support</title></head>
<body style="font:16px/1.65 system-ui;max-width:760px;margin:40px auto;padding:0 20px;color:#172126">
<h1>TerrainTiles Support</h1><p>For the current pre-release, verify the public TerrainTiles builder at <a href="{TERRAIN_ORIGIN}">{TERRAIN_ORIGIN}</a>.</p>
<p>A dedicated support contact will be published before directory submission.</p></body></html>"""
    )


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "10000"))
    mcp.run(
        transport="streamable-http",
        host="0.0.0.0",
        port=port,
        streamable_http_path="/mcp",
        json_response=True,
        stateless_http=True,
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=False
        ),
    )
