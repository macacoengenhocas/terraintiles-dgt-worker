from __future__ import annotations

import asyncio
import json
import os
import time
import urllib.request

from mcp import Client


MCP_URL = os.environ.get(
    "TERRAINTILES_MCP_URL",
    "https://terraintiles-chatgpt-plugin.onrender.com/mcp",
)
HEALTH_URL = MCP_URL.rsplit("/mcp", 1)[0] + "/health"
EXPECTED_SHA = os.environ.get("EXPECTED_GIT_SHA", "")


def wait_for_expected_deploy(timeout_seconds: int = 300) -> dict:
    deadline = time.time() + timeout_seconds
    last_error = ""
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(HEALTH_URL, timeout=10) as response:
                data = json.loads(response.read().decode("utf-8"))
            if not data.get("ok"):
                last_error = "health returned ok=false"
            elif EXPECTED_SHA and data.get("git_commit") != EXPECTED_SHA:
                last_error = (
                    f"waiting for git commit {EXPECTED_SHA}; "
                    f"live={data.get('git_commit') or 'unknown'}"
                )
            else:
                return data
        except Exception as exc:
            last_error = str(exc)
        time.sleep(5)
    raise RuntimeError(f"Render service did not reach expected deployment: {last_error}")


async def probe(mode: str) -> dict:
    async with Client(MCP_URL, mode=mode) as client:
        tools_result = await client.list_tools()
        tools = {tool.name: tool for tool in tools_result.tools}
        expected = {
            "terrain_capabilities",
            "plan_terrain_model",
            "render_terrain_builder",
        }
        assert set(tools) == expected, set(tools)

        for name, tool in tools.items():
            assert tool.annotations is not None, name
            assert tool.annotations.read_only_hint is True, name
            assert tool.annotations.destructive_hint is False, name
            assert tool.annotations.open_world_hint is not None, name

        plan = await client.call_tool(
            "plan_terrain_model",
            {
                "bbox": [-9.145, 38.720, -9.138, 38.726],
                "product": "MDT",
                "scale": 2500,
                "printer_bed_x_mm": 220,
                "printer_bed_y_mm": 220,
                "printer_bed_z_mm": 250,
                "safety_margin_mm": 5,
            },
        )
        assert plan.is_error is False
        assert plan.structured_content is not None
        assert plan.structured_content["total_tiles"] >= 1
        assert plan.structured_content["print_width_mm"] > 0

        capabilities = await client.call_tool("terrain_capabilities", {})
        assert capabilities.is_error is False
        assert capabilities.structured_content["account_required"] is False

        builder = await client.call_tool(
            "render_terrain_builder",
            {
                "note": "Lisbon prefill",
                "bbox": [-9.145, 38.720, -9.138, 38.726],
                "scale": 2500,
                "elevation": "auto_mds",
                "mode": "engineering",
                "printer_bed_x_mm": 220,
                "printer_bed_y_mm": 220,
                "printer_bed_z_mm": 250,
            },
        )
        assert builder.is_error is False
        builder_url = builder.structured_content["builder_url"]
        assert "tt_embed=chatgpt" in builder_url
        assert "scale=2500" in builder_url
        assert "elevation=auto_mds" in builder_url
        assert "mode=engineering" in builder_url
        assert "bed_x=220" in builder_url

        resources = await client.list_resources()
        resource = next(
            item
            for item in resources.resources
            if str(item.uri) == "ui://terraintiles/builder-v1.html"
        )
        assert resource.mime_type == "text/html;profile=mcp-app"

        content = await client.read_resource(str(resource.uri))
        text = content.contents[0].text
        assert "TerrainTiles builder" in text
        assert "terraintiles-web-g1o5ka.v2.appdeploy.ai" in text
        assert "tt_embed=chatgpt" in text
        assert "ui/notifications/tool-result" in text

        return {
            "mode": mode,
            "protocol_version": str(client.protocol_version),
            "tools": sorted(tools),
            "tiles": plan.structured_content["total_tiles"],
            "builder_prefill": builder_url,
            "resource": str(resource.uri),
        }


async def main() -> None:
    health = wait_for_expected_deploy()
    auto = await probe("auto")
    legacy = await probe("legacy")
    print(
        json.dumps(
            {
                "ok": True,
                "health": health,
                "auto": auto,
                "legacy": legacy,
            },
            indent=2,
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    asyncio.run(main())
