# TerrainTiles Plugin — submission pack draft

Version: 0.1.0
Date: 2026-09-18

## Listing draft

Name: TerrainTiles
Short description: Turn real terrain into modular 3D-printable topographic models from ChatGPT.
Long description: Plan model scale and printer fit from real geographic areas, then open the interactive TerrainTiles builder in ChatGPT to choose terrain, configure detail and modularization, validate geometry, and export printable STL/3MF packages.

Suggested category: Productivity / Design & Engineering

## MCP annotations and justifications

### terrain_capabilities
- readOnlyHint: true — returns static product capabilities; it never creates, updates, deletes, publishes or sends user data.
- destructiveHint: false — it cannot overwrite or delete data.
- openWorldHint: false — it does not access the public internet or an unbounded external entity.
- idempotentHint: true — repeated calls return the same capability policy for the same deployed version.

### plan_terrain_model
- readOnlyHint: true — performs local geometry/scale calculations only.
- destructiveHint: false — it cannot alter external state.
- openWorldHint: false — the calculation uses only the supplied bounding box and printer settings.
- idempotentHint: true — identical inputs produce identical planning output for a given version.

### render_terrain_builder
- readOnlyHint: true — renders an interface but does not itself create/update/delete external records.
- destructiveHint: false — opening the builder cannot delete or overwrite user data.
- openWorldHint: true — the embedded TerrainTiles interface can contact its hosted terrain workflow and public mapping/elevation sources.
- idempotentHint: true — opening the builder repeatedly has no additional external state effect.

## External frame domain justification

https://terraintiles-web-g1o5ka.v2.appdeploy.ai is the existing TerrainTiles builder. It provides the map, terrain-source selection, geometry generation, validation and STL/3MF export UI. The MCP widget frames only this single application origin.

## Positive review cases (5)

1. "Plan a 1:2500 terrain model for bbox [-9.145,38.720,-9.138,38.726] on a 220×220 mm printer." Expected: plan_terrain_model returns positive dimensions and module count.
2. "What terrain sources and output formats does TerrainTiles support?" Expected: terrain_capabilities returns MDT/MDS fallback policy and STL/3MF outputs.
3. "Open TerrainTiles so I can choose an area on the map." Expected: render_terrain_builder opens the interactive UI in the conversation.
4. "Plan the same Lisbon bbox as MDS at 1:5000." Expected: source policy includes DGT → Copernicus GLO-30 DSM → Terrarium.
5. "My bed is 180×180 mm with 5 mm margin; how many pieces?" Expected: plan uses the supplied bed and returns the required tiles without changing external state.

## Negative review cases (3)

1. Invalid bbox with west >= east. Expected: tool validation/error explains the bounding box is invalid; no fabricated plan.
2. Area smaller than roughly 5×5 m. Expected: clear validation error asking for a larger selection.
3. Safety margin that leaves <=10 mm usable bed. Expected: clear validation error; no divide-by-zero or nonsensical tile count.

## Release notes

Initial pre-release MCP integration for TerrainTiles. Adds read-only terrain planning, product capability discovery, and an MCP Apps widget that opens the existing TerrainTiles builder inside ChatGPT.

## Still required before public submission

- Verified developer/company identity in the OpenAI organization/project used for submission.
- Final public support contact and final commercial Terms/Privacy wording.
- Production MCP URL inserted into mcp.json and plugin listing.
- Domain challenge token set at /.well-known/openai-apps-challenge when the portal provides it.
- Current tool scan in the submission portal.
- Demo recording URL and final screenshots after the UI template is confirmed by the scan.
