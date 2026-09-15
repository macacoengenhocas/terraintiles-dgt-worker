# TerrainTiles DGT Worker

Temporary European diagnostic worker for TerrainTiles Studio.

## Purpose

Tests whether an already-authenticated DGT CDD asset request can be materialized from the Render Frankfurt network path. The worker never receives or stores the CDD password. During a diagnostic probe it receives only an ephemeral authenticated cookie header and a validated DGT asset URL.

## Endpoints

- `GET /health` — service/version state.
- `POST /dgt/probe` — disabled unless `TT_DIAGNOSTIC_ENABLED=1`.

`/dgt/probe` accepts JSON containing `href` and `cookie`. The initial URL is restricted to `https://cdd.dgterritorio.gov.pt/dgt-be/...`; redirects must remain HTTPS and resolve only to public addresses. CDD cookies are not forwarded across host changes. The probe reads at most 128 KiB and reports HTTP/object metadata plus TIFF magic detection.

## Render

- Runtime: Python 3
- Build command: `python -m py_compile worker.py`
- Start command: `python worker.py`
- Environment during the temporary proof only: `TT_DIAGNOSTIC_ENABLED=1`

After the network proof, set `TT_DIAGNOSTIC_ENABLED=0`. This diagnostic endpoint is not intended to remain enabled as a production relay.

## Security constraints

- No DGT username/password in this repository.
- Request bodies/cookies are not logged.
- 16 KiB maximum request body.
- 128 KiB maximum remote read.
- In-process rate limit: 6 probes per 10 minutes.
- Initial asset host/path allow-list.
- HTTPS-only redirects; private/local redirect destinations rejected.
