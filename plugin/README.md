# TerrainTiles ChatGPT Plugin / MCP

The production MCP source lives in `plugin/` in this repository so it remains independent of the existing DGT worker process.

## Local validation

```bash
python -m pip install -r plugin/requirements.txt
python -m py_compile plugin/server.py
python plugin/selftest.py
python plugin/server.py
```

MCP endpoint: `http://localhost:10000/mcp`

The Render deployment sets `PUBLIC_ORIGIN` to its public HTTPS origin. `OPENAI_APPS_CHALLENGE` remains unset until the OpenAI submission portal issues the exact domain-verification token.
