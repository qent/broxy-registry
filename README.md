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
│   ├── *.json
└── icons/
    └── *.png
```

## Conventions

- `index.json` uses `schemaVersion: 1` and `servers[].path` references.
- Each server `name` is strict schema style: `io.qent.broxy/<id>`.
- This repo stores only **safe templates**. No real tokens, PATs, local private paths, or secrets.
- `packages[*].registryType: "oci"` is not used in this registry.
- For local server execution, Docker is represented as a standard `stdio` runtime (`runtimeHint: "docker"`) when it is the official setup path.
- Icons are stored as PNG (`128x128`) and referenced via:
  - `https://raw.githubusercontent.com/qent/broxy-registry/main/icons/<file>.png`
- Optional Broxy setup instructions are stored in `_meta.install_steps` as a list of markdown strings.

## Broxy install steps extension

Broxy supports a repository-specific metadata extension:

- path: `_meta.install_steps`
- type: `string[]`
- optional: yes

Semantics per step string:

- markdown text (`**bold**`, `*italic*`);
- external link: `[label](https://...)`;
- form field reference by name: `[FieldName]` (for Broxy install form field embedding).

Behavior in Broxy:

- `_meta.install_steps` absent/empty: legacy schema-driven form/one-click flow;
- `_meta.install_steps` non-empty: Broxy opens step-driven install UI and renders these steps.

Example (`servers/github.json`):

```json
"_meta": {
  "install_steps": [
    "Open your **GitHub** [Developer Settings](https://github.com/settings/apps).",
    "Select [Tokens (classic)](https://github.com/settings/tokens) from **Personal access tokens**.",
    "Click [Generate new token (classic)](https://github.com/settings/tokens/new) from **Generate new token** selector.",
    "Fill **New personal access token (classic)** form and submit with the **Generate token** button.",
    "Use created token below [Authorization]"
  ]
}
```

## Icon assets

- All catalog icons in `icons/*.png` are normalized to `128x128`.
- For icon refreshes, prefer vector or high-resolution public brand assets from official/open repositories.
- Keep transparency when available; avoid embedding low-resolution favicon assets.

## Validation

```bash
jq . index.json
jq . servers/*.json
npx -y ajv-cli validate --strict=false -s /tmp/server.schema.json -d "servers/*.json"
for f in icons/*.png; do sips -g pixelWidth -g pixelHeight "$f"; done
```
