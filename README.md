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
│   ├── box.json
│   ├── brave.json
│   ├── context7.json
│   ├── filesystem.json
│   ├── dropbox.json
│   ├── exa.json
│   ├── github.json
│   ├── jetbrains.json
│   ├── linear.json
│   ├── notion.json
│   ├── slack.json
│   ├── stripe.json
│   ├── time.json
│   ├── todoist.json
│   └── vercel.json
└── icons/
    └── *.png
```

## Conventions

- `index.json` uses `schemaVersion: 1` and `servers[].path` references.
- Each server `name` is strict schema style: `io.qent.broxy/<id>`.
- This repo stores only **safe templates**. No real tokens, PATs, local private paths, or secrets.
- Icons are stored as PNG (`128x128`) and referenced via:
  - `https://raw.githubusercontent.com/qent/broxy-registry/main/icons/<file>.png`

## Icon assets (Updated: 2026-04-01)

- All catalog icons in `icons/*.png` are normalized to `128x128`.
- For icon refreshes, prefer vector or high-resolution public brand assets from official/open repositories.
- Keep transparency when available; avoid embedding low-resolution favicon assets.

| Catalog IDs | Source |
|---|---|
| `box`, `brave`, `dropbox`, `github`, `jetbrains`, `linear`, `notion`, `stripe`, `todoist`, `vercel` | `simple-icons` project (SVG logos): `https://github.com/simple-icons/simple-icons` |
| `context7` | `upstash/context7` official icon: `https://raw.githubusercontent.com/upstash/context7/master/public/context7-icon.svg` |
| `filesystem` | Heroicons (`folder`): `https://raw.githubusercontent.com/tailwindlabs/heroicons/master/src/24/solid/folder.svg` |
| `time` | Heroicons (`clock`): `https://raw.githubusercontent.com/tailwindlabs/heroicons/master/src/24/solid/clock.svg` |
| `slack` | Slack marketing asset (`400x400`): `https://a.slack-edge.com/80588/marketing/img/icons/icon_slack_hash_colored.png` |
| `exa` | Exa homepage inline logomark SVG path (navbar logo): `https://exa.ai` |

## Servers

| Catalog ID | Server name (`name`) | Main transport | Auth/config template fields |
|---|---|---|---|
| `context7` | `io.qent.broxy/context7` | `remotes: streamable-http` | Optional header `CONTEXT7_API_KEY` |
| `box` | `io.qent.broxy/box` | `remotes: streamable-http` | MCP OAuth flow |
| `todoist` | `io.qent.broxy/todoist` | `remotes: streamable-http` | OAuth flow on client side |
| `dropbox` | `io.qent.broxy/dropbox` | `remotes: streamable-http` | MCP OAuth flow |
| `brave` | `io.qent.broxy/brave` | `packages: oci + stdio` | Required env `BRAVE_API_KEY` |
| `exa` | `io.qent.broxy/exa` | `remotes: streamable-http` | No required template field |
| `jetbrains` | `io.qent.broxy/jetbrains` | `remotes: sse` | Required header `IJ_MCP_SERVER_PROJECT_PATH` (`filepath`) |
| `filesystem` | `io.qent.broxy/filesystem` | `packages: npm + stdio` | Required repeated positional `allowed_directory` (`filepath`) |
| `github` | `io.qent.broxy/github` | `remotes: streamable-http` | Required header `Authorization: Bearer {github_pat}` |
| `linear` | `io.qent.broxy/linear` | `remotes: streamable-http` | MCP OAuth flow |
| `notion` | `io.qent.broxy/notion` | `remotes: streamable-http` | OAuth flow on client side |
| `slack` | `io.qent.broxy/slack` | `remotes: streamable-http` | OAuth flow on client side |
| `stripe` | `io.qent.broxy/stripe` | `remotes: streamable-http` | MCP OAuth flow |
| `time` | `io.qent.broxy/time` | `packages: pypi + stdio` | No required template field |
| `vercel` | `io.qent.broxy/vercel` | `remotes: streamable-http` | OAuth flow on client side |

## Validation

```bash
jq . index.json
jq . servers/*.json
npx -y ajv-cli validate --strict=false -s /tmp/server.schema.json -d "servers/*.json"
for f in icons/*.png; do sips -g pixelWidth -g pixelHeight "$f"; done
```

## Broxy integration check

After pushing to `main`:

```bash
cd /Users/dolf/Repos/bro
./gradlew build
```

Then verify generated bundle contains expected names under `servers[]`.
