# AGENTS.md

Guidance for agents contributing to this repository.

## Required format

All files in `servers/*.json` MUST follow generic `server.json` format:
- https://raw.githubusercontent.com/modelcontextprotocol/registry/refs/heads/main/docs/reference/server-json/generic-server-json.md

Use MCP schema for validation:
- `https://static.modelcontextprotocol.io/schemas/2025-12-11/server.schema.json`

## Safety rules

- Never commit real secrets (API keys, PATs, tokens).
- Never commit personal local paths from a developer machine.
- Use template variables (`{...}`), `isSecret`, `format: filepath`, and placeholders.
- Keep server descriptions concise and schema-compliant.

## Registry rules

- Update `index.json` for every added/removed server file.
- Keep `servers[].id` stable and human-readable.
- Keep `servers[].path` pointing to `servers/<id>.json`.
- Keep icon URLs in server JSON pointing to raw files in this repository.
- Keep icon metadata aligned with actual files (`icons[].sizes` must match real PNG size, currently `128x128`).

## Checks before push

```bash
jq . index.json
jq . servers/*.json
curl -fsSL https://static.modelcontextprotocol.io/schemas/2025-12-11/server.schema.json -o /tmp/server.schema.json
npx -y ajv-cli validate --strict=false -s /tmp/server.schema.json -d "servers/*.json"
for f in icons/*.png; do file "$f"; done
for f in icons/*.png; do sips -g pixelWidth -g pixelHeight "$f"; done
```
