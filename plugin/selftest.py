import asyncio
import json

from server import (
    RESOURCE_URI,
    mcp,
    plan_terrain_model,
    render_terrain_builder,
    terrain_builder_resource,
    terrain_capabilities,
)


def main() -> None:
    caps = terrain_capabilities()
    assert caps.account_required is False
    assert "MDT" in caps.elevation_policy and "MDS" in caps.elevation_policy

    plan = plan_terrain_model(
        bbox=[-9.145, 38.720, -9.138, 38.726],
        product="MDT",
        scale=2500,
        printer_bed_x_mm=220,
        printer_bed_y_mm=220,
        printer_bed_z_mm=250,
        safety_margin_mm=5,
    )
    assert plan.total_tiles >= 1
    assert plan.print_width_mm > 0 and plan.print_height_mm > 0

    builder = render_terrain_builder(
        note="Lisbon test",
        bbox=[-9.145, 38.720, -9.138, 38.726],
        scale=2500,
        elevation="auto_mds",
        mode="engineering",
        printer_bed_x_mm=220,
        printer_bed_y_mm=220,
        printer_bed_z_mm=250,
    )
    assert "tt_embed=chatgpt" in builder.builder_url
    assert "bbox=" in builder.builder_url
    assert "scale=2500" in builder.builder_url
    assert "elevation=auto_mds" in builder.builder_url
    assert "mode=engineering" in builder.builder_url
    assert "bed_x=220" in builder.builder_url

    html = terrain_builder_resource()
    assert "iframe" in html.lower()
    assert "terraintiles-web-g1o5ka.v2.appdeploy.ai" in html
    assert "tt_embed=chatgpt" in html
    assert "ui/notifications/tool-result" in html

    async def inspect() -> None:
        tools = await mcp.list_tools()
        names = {tool.name for tool in tools}
        assert names == {
            "terrain_capabilities",
            "plan_terrain_model",
            "render_terrain_builder",
        }
        for tool in tools:
            assert tool.annotations is not None
            assert tool.annotations.read_only_hint is True
            assert tool.annotations.destructive_hint is False
            assert tool.annotations.open_world_hint is not None
        resources = await mcp.list_resources()
        assert any(str(resource.uri) == RESOURCE_URI for resource in resources)

    asyncio.run(inspect())
    print(json.dumps({"ok": True, "tiles": plan.total_tiles, "size_mm": [plan.print_width_mm, plan.print_height_mm]}))


if __name__ == "__main__":
    main()
