# broxy-registry

Registry of MCP server templates for Broxy (`qent/broxy-registry`).

Broxy reads:
- `index.json`
- `servers/*.json`
- icon URLs from each server file (`icons[].src`)

All server files in this repository follow **generic `server.json`** format and are compatible with Broxy catalog ingestion.

Reference format:
- https://raw.githubusercontent.com/modelcontextprotocol/registry/refs/heads/main/docs/reference/server-json/generic-server-json.md

## Repository layout

```text
.
├── index.json
├── servers/
│   ├── brave.json
│   ├── context7.json
│   ├── downloads-files.json
│   ├── exa.json
│   ├── github.json
│   ├── intellij-idea-ce.json
│   ├── notion.json
│   └── todoist.json
└── icons/
    └── *.png
```

## Conventions

- `index.json` uses `schemaVersion: 1` and `servers[].path` references.
- Each server `name` is strict schema style: `io.qent.broxy/<id>`.
- This repo stores only **safe templates**. No real tokens, PATs, local private paths, or secrets.
- Icons are stored as PNG (`64x64`) and referenced via:
  - `https://raw.githubusercontent.com/qent/broxy-registry/main/icons/<file>.png`

## Servers

| Catalog ID | Server name (`name`) | Main transport | Auth/config template fields |
|---|---|---|---|
| `context7` | `io.qent.broxy/context7` | `remotes: streamable-http` | Optional header `CONTEXT7_API_KEY` |
| `todoist` | `io.qent.broxy/todoist` | `remotes: streamable-http` | OAuth flow on client side |
| `brave` | `io.qent.broxy/brave` | `packages: oci + stdio` | Required env `BRAVE_API_KEY` |
| `exa` | `io.qent.broxy/exa` | `remotes: streamable-http` | No required template field |
| `intellij-idea-ce` | `io.qent.broxy/intellij-idea-ce` | `remotes: sse` | Required header `IJ_MCP_SERVER_PROJECT_PATH` (`filepath`) |
| `downloads-files` | `io.qent.broxy/downloads-files` | `packages: npm + stdio` | Required repeated positional `allowed_directory` (`filepath`) |
| `github` | `io.qent.broxy/github` | `remotes: streamable-http` | Required header `Authorization: Bearer {github_pat}` |
| `notion` | `io.qent.broxy/notion` | `remotes: streamable-http` | OAuth flow on client side |

## Validation

```bash
jq . index.json
jq . servers/*.json
npx -y ajv-cli validate -s /tmp/server.schema.json -d "servers/*.json"
```

## Broxy integration check

After pushing to `main`:

```bash
cd /Users/dolf/Repos/bro
./gradlew build
```

Then verify generated bundle contains expected names under `servers[]`.
