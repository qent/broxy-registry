#!/usr/bin/env python3
from __future__ import annotations

import argparse
import ast
import asyncio
import json
import os
import re
import shutil
import sys
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urljoin, urlparse

try:
    import httpx
except Exception as exc:  # pragma: no cover
    httpx = None
    HTTPX_IMPORT_ERROR = exc
else:
    HTTPX_IMPORT_ERROR = None

try:
    from mcp import ClientSession
    from mcp.client.sse import sse_client
    from mcp.client.stdio import StdioServerParameters, stdio_client
    from mcp.client.streamable_http import streamable_http_client
except Exception as exc:  # pragma: no cover
    ClientSession = None
    StdioServerParameters = None
    stdio_client = None
    streamable_http_client = None
    sse_client = None
    MCP_IMPORT_ERROR = exc
else:
    MCP_IMPORT_ERROR = None

STATUS_PASS = "pass"
STATUS_PASS_OAUTH = "pass_oauth_challenge"
STATUS_FAIL = "fail"

VAR_PATTERN = re.compile(r"\{([^{}]+)\}")

FALLBACK_RUNTIME_BY_REGISTRY = {
    "npm": "npx",
    "pypi": "uvx",
    "docker": "docker",
    "oci": "docker",
    "nuget": "dnx",
}


@dataclass
class TargetSpec:
    server_file: Path
    server_name: str
    server_id: str
    short_id: str
    capabilities: set[str]
    server_remote_only: bool
    kind: str
    index: int
    transport: str
    spec: dict[str, Any]

    @property
    def target_label(self) -> str:
        return f"{self.kind}[{self.index}]"


@dataclass
class TargetResult:
    server_file: str
    server_name: str
    server_id: str
    target: str
    kind: str
    transport: str
    status: str
    duration_s: float
    reason: str


@dataclass
class InputResolution:
    value: str | None
    missing: list[str]
    unresolved: list[str]


@dataclass
class RuntimeOptions:
    startup_timeout: float
    rpc_timeout: float
    http_timeout: float


@dataclass
class OAuthRegistrationAnalysis:
    status: str
    detail: str


def first_non_none(*values: Any) -> Any:
    for value in values:
        if value is not None:
            return value
    return None


def stringify(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def dedupe_preserve_order(items: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        if item not in seen:
            seen.add(item)
            result.append(item)
    return result


def format_exception(exc: BaseException) -> str:
    if isinstance(exc, BaseExceptionGroup):
        flattened: list[str] = []

        def walk(error: BaseException) -> None:
            if isinstance(error, BaseExceptionGroup):
                for nested in error.exceptions:
                    walk(nested)
                return
            message = f"{error.__class__.__name__}: {error}"
            flattened.append(message)

        walk(exc)
        flattened = dedupe_preserve_order(flattened)
        if not flattened:
            return f"{exc.__class__.__name__}: {exc}"
        if len(flattened) == 1:
            return flattened[0]
        return f"{flattened[0]} (+{len(flattened) - 1} more)"
    return f"{exc.__class__.__name__}: {exc}"


def parse_www_authenticate_params(header_value: str | None) -> dict[str, str]:
    if not header_value:
        return {}

    params: dict[str, str] = {}
    for match in re.finditer(r'([a-zA-Z_][a-zA-Z0-9_-]*)=("[^"]*"|[^,\\s]+)', header_value):
        key = match.group(1)
        raw_value = match.group(2).strip()
        if raw_value.startswith('"') and raw_value.endswith('"') and len(raw_value) >= 2:
            value = raw_value[1:-1]
        else:
            value = raw_value
        params[key] = value
    return params


def protected_resource_metadata_candidates(
    url: str,
    header_value: str | None,
) -> list[str]:
    candidates: list[str] = []
    params = parse_www_authenticate_params(header_value)

    for key in ("resource_metadata", "oauth_metadata", "authorization_uri"):
        value = params.get(key)
        if value and value.startswith(("http://", "https://")):
            candidates.append(value)

    parsed = urlparse(url)
    origin = f"{parsed.scheme}://{parsed.netloc}"
    path = parsed.path.strip("/")

    candidates.append(f"{origin}/.well-known/oauth-protected-resource")
    if path:
        candidates.append(f"{origin}/.well-known/oauth-protected-resource/{path}")
    candidates.append(f"{origin}/.well-known/oauth-authorization-server")
    candidates.append(f"{origin}/.well-known/openid-configuration")

    return dedupe_preserve_order(candidates)


async def fetch_json_if_ok(client: httpx.AsyncClient, url: str) -> dict[str, Any] | None:
    try:
        response = await client.get(url)
    except Exception:
        return None
    if response.status_code != 200:
        return None
    try:
        return response.json()
    except Exception:
        return None


def auth_server_metadata_urls(base_url: str) -> list[str]:
    base = base_url.rstrip("/")
    return [
        f"{base}/.well-known/oauth-authorization-server",
        f"{base}/.well-known/openid-configuration",
    ]


async def analyze_oauth_registration_support(
    transport: str,
    url: str,
    headers: dict[str, str],
    timeout_seconds: float,
) -> OAuthRegistrationAnalysis:
    if transport not in {"streamable-http", "sse"}:
        return OAuthRegistrationAnalysis(
            status="unknown",
            detail=f"unsupported transport for oauth analysis: {transport}",
        )

    timeout = httpx.Timeout(timeout_seconds, read=timeout_seconds)
    request_headers = dict(headers)
    if transport == "sse":
        request_headers.setdefault("Accept", "text/event-stream")

    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            init_response = await client.post(
                url,
                headers=request_headers,
                json=mcp_initialize_payload(),
            )
            if not is_oauth_challenge_response(init_response):
                return OAuthRegistrationAnalysis(
                    status="unknown",
                    detail="oauth challenge not returned during analysis probe",
                )

            metadata_candidates = protected_resource_metadata_candidates(
                url,
                init_response.headers.get("www-authenticate"),
            )

            auth_servers: list[str] = []
            metadata_probed: list[str] = []
            for metadata_url in metadata_candidates:
                metadata = await fetch_json_if_ok(client, metadata_url)
                if not metadata:
                    continue
                metadata_probed.append(metadata_url)

                if "authorization_servers" in metadata:
                    values = metadata.get("authorization_servers") or []
                    auth_servers.extend(str(item) for item in values if isinstance(item, str))
                    if metadata.get("authorization_server"):
                        auth_servers.append(str(metadata.get("authorization_server")))
                    continue

                if any(key in metadata for key in ("authorization_endpoint", "token_endpoint", "issuer")):
                    parsed = urlparse(metadata_url)
                    auth_servers.append(f"{parsed.scheme}://{parsed.netloc}")

            if not auth_servers:
                parsed = urlparse(url)
                auth_servers.append(f"{parsed.scheme}://{parsed.netloc}")

            auth_servers = dedupe_preserve_order(auth_servers)
            registration_supported = False
            registration_mechanisms: list[str] = []
            checked_metadata_urls: list[str] = []

            for auth_server in auth_servers:
                for metadata_url in auth_server_metadata_urls(auth_server):
                    metadata = await fetch_json_if_ok(client, metadata_url)
                    if not metadata:
                        continue
                    checked_metadata_urls.append(metadata_url)
                    if metadata.get("registration_endpoint"):
                        registration_supported = True
                        registration_mechanisms.append("registration_endpoint")
                    if bool(metadata.get("client_id_metadata_document_supported")):
                        registration_supported = True
                        registration_mechanisms.append("client_id_metadata_document_supported")

            if registration_supported:
                mechanisms = ", ".join(dedupe_preserve_order(registration_mechanisms))
                return OAuthRegistrationAnalysis(
                    status="registration_supported",
                    detail=f"oauth challenge detected; registration support: {mechanisms}",
                )

            checked_urls = ", ".join(dedupe_preserve_order([*metadata_probed, *checked_metadata_urls]))
            detail = (
                "oauth challenge detected but no oauth client registration support found "
                "(missing registration_endpoint/client_id_metadata_document_supported); "
                "configure clientId or clientIdMetadataUrl"
            )
            if checked_urls:
                detail = f"{detail}; checked metadata: {checked_urls}"
            return OAuthRegistrationAnalysis(
                status="registration_unavailable",
                detail=detail,
            )
    except Exception as exc:
        return OAuthRegistrationAnalysis(
            status="analysis_error",
            detail=f"oauth metadata analysis failed: {format_exception(exc)}",
        )


def load_dotenv_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values

    for line in path.read_text(encoding="utf-8").splitlines():
        raw = line.strip()
        if not raw or raw.startswith("#"):
            continue

        if raw.startswith("export "):
            raw = raw[len("export ") :].strip()

        if "=" not in raw:
            continue

        key, value = raw.split("=", 1)
        key = key.strip()
        if not key:
            continue

        value = value.strip()
        if value.startswith(("'", '"')) and value.endswith(("'", '"')) and len(value) >= 2:
            try:
                parsed = ast.literal_eval(value)
            except Exception:
                parsed = value[1:-1]
            values[key] = stringify(parsed)
            continue

        if " #" in value:
            value = value.split(" #", 1)[0].rstrip()

        values[key] = value

    return values


def substitute_template(
    template: str,
    variables: dict[str, Any],
    inv_values: dict[str, str],
    env_values: dict[str, str],
) -> tuple[str, list[str], list[str]]:
    missing_required: list[str] = []
    unresolved: list[str] = []

    def replacer(match: re.Match[str]) -> str:
        var_name = match.group(1)
        var_spec = variables.get(var_name) or {}
        candidate = first_non_none(
            inv_values.get(var_name),
            env_values.get(var_name),
            var_spec.get("value"),
            var_spec.get("default"),
        )
        if candidate is None:
            if bool(var_spec.get("isRequired")):
                missing_required.append(var_name)
            unresolved.append(var_name)
            return match.group(0)
        return stringify(candidate)

    resolved = VAR_PATTERN.sub(replacer, template)
    return resolved, dedupe_preserve_order(missing_required), dedupe_preserve_order(unresolved)


def resolve_input_value(
    input_spec: dict[str, Any],
    inv_values: dict[str, str],
    env_values: dict[str, str],
) -> InputResolution:
    missing: list[str] = []
    unresolved: list[str] = []

    name = input_spec.get("name")
    value_hint = input_spec.get("valueHint")

    candidate = None
    for key in (name, value_hint):
        if not key:
            continue
        if key in inv_values:
            candidate = inv_values[key]
            break
        if key in env_values:
            candidate = env_values[key]
            break

    if candidate is None:
        candidate = first_non_none(input_spec.get("value"), input_spec.get("default"))

    if candidate is None:
        if bool(input_spec.get("isRequired")):
            missing.append(name or value_hint or "<input>")
        return InputResolution(value=None, missing=missing, unresolved=[])

    value = stringify(candidate)
    template_vars = input_spec.get("variables") or {}
    value, missing_vars, unresolved_vars = substitute_template(value, template_vars, inv_values, env_values)

    label = name or value_hint or "value"
    missing.extend(f"{label}.{var}" for var in missing_vars)
    unresolved.extend(unresolved_vars)

    if unresolved and bool(input_spec.get("isRequired")):
        for var in unresolved:
            missing.append(f"{label}.{var}")

    return InputResolution(
        value=value,
        missing=dedupe_preserve_order(missing),
        unresolved=dedupe_preserve_order(unresolved),
    )


def normalize_filepath(value: str) -> tuple[str | None, str | None]:
    try:
        path = Path(value).expanduser().resolve(strict=False)
    except Exception as exc:
        return None, f"invalid filepath '{value}': {exc}"

    if not path.exists():
        return None, f"filepath does not exist: {path}"

    return str(path), None


def resolve_environment_variables(
    env_specs: list[dict[str, Any]],
    inv_values: dict[str, str],
    env_values: dict[str, str],
) -> tuple[dict[str, str], list[str]]:
    resolved: dict[str, str] = {}
    errors: list[str] = []

    for env_spec in env_specs:
        name = env_spec.get("name")
        if not name:
            errors.append("environment variable entry is missing 'name'")
            continue

        resolution = resolve_input_value(env_spec, inv_values, env_values)
        if resolution.missing:
            for item in resolution.missing:
                errors.append(f"missing required input: {item}")
            continue

        if resolution.value is None:
            continue

        value = resolution.value
        if env_spec.get("format") == "filepath":
            normalized, err = normalize_filepath(value)
            if err:
                errors.append(err)
                continue
            value = normalized or value

        if resolution.unresolved and VAR_PATTERN.search(value):
            if bool(env_spec.get("isRequired")):
                errors.append(f"unresolved template variables in required input: {name}")
            continue

        resolved[name] = value

    return resolved, dedupe_preserve_order(errors)


def resolve_argument_tokens(
    arg_specs: list[dict[str, Any]],
    inv_values: dict[str, str],
    env_values: dict[str, str],
) -> tuple[list[str], list[str]]:
    tokens: list[str] = []
    errors: list[str] = []

    for arg_spec in arg_specs:
        arg_type = arg_spec.get("type")
        if arg_type not in {"positional", "named"}:
            errors.append(f"unsupported argument type: {arg_type}")
            continue

        resolution = resolve_input_value(arg_spec, inv_values, env_values)
        if resolution.missing:
            for item in resolution.missing:
                errors.append(f"missing required input: {item}")
            continue

        value = resolution.value
        if value is not None and arg_spec.get("format") == "filepath":
            normalized, err = normalize_filepath(value)
            if err:
                errors.append(err)
                continue
            value = normalized or value

        if value is not None and resolution.unresolved and VAR_PATTERN.search(value):
            if bool(arg_spec.get("isRequired")):
                errors.append(
                    f"unresolved template variables in required argument: {arg_spec.get('name') or arg_spec.get('valueHint') or '<arg>'}"
                )
            continue

        if arg_type == "positional":
            if value is None:
                continue
            tokens.append(value)
            continue

        name = arg_spec.get("name")
        if not name:
            errors.append("named argument entry is missing 'name'")
            continue

        if value is None:
            tokens.append(name)
        else:
            tokens.extend([name, value])

    return tokens, dedupe_preserve_order(errors)


def resolve_remote_url(
    remote_spec: dict[str, Any],
    inv_values: dict[str, str],
    env_values: dict[str, str],
) -> tuple[str | None, list[str]]:
    url = remote_spec.get("url")
    if not isinstance(url, str) or not url:
        return None, ["remote transport is missing url"]

    url, missing, unresolved = substitute_template(
        url,
        remote_spec.get("variables") or {},
        inv_values,
        env_values,
    )

    errors: list[str] = []
    for item in missing:
        errors.append(f"missing required input: url.{item}")

    if unresolved and VAR_PATTERN.search(url):
        errors.append("remote url contains unresolved template variables")

    if not (url.startswith("http://") or url.startswith("https://")):
        errors.append(f"remote url is not http/https: {url}")

    return (url if not errors else None), dedupe_preserve_order(errors)


def resolve_headers(
    header_specs: list[dict[str, Any]],
    inv_values: dict[str, str],
    env_values: dict[str, str],
    *,
    enforce_required: bool = True,
) -> tuple[dict[str, str], list[str]]:
    headers: dict[str, str] = {}
    errors: list[str] = []

    for header_spec in header_specs:
        name = header_spec.get("name")
        if not name:
            errors.append("header entry is missing 'name'")
            continue

        resolution = resolve_input_value(header_spec, inv_values, env_values)
        if resolution.missing:
            if enforce_required:
                for item in resolution.missing:
                    errors.append(f"missing required input: {item}")
            continue

        value = resolution.value
        if value is None:
            if enforce_required and bool(header_spec.get("isRequired")):
                errors.append(f"missing required header value: {name}")
            continue

        if resolution.unresolved and VAR_PATTERN.search(value):
            if enforce_required and bool(header_spec.get("isRequired")):
                errors.append(f"unresolved template variables in required header: {name}")
            continue

        headers[name] = value

    return headers, dedupe_preserve_order(errors)


def get_server_capabilities(server_data: dict[str, Any]) -> set[str]:
    catalog_meta = (
        server_data.get("_meta", {})
        .get("io.qent.broxy/catalog", {})
    )
    capabilities = catalog_meta.get("capabilities") or []
    return {str(item) for item in capabilities if isinstance(item, str)}


def discover_targets(servers_dir: Path) -> tuple[list[TargetSpec], list[TargetResult]]:
    targets: list[TargetSpec] = []
    preflight_results: list[TargetResult] = []

    for server_file in sorted(servers_dir.glob("*.json")):
        try:
            server_data = json.loads(server_file.read_text(encoding="utf-8"))
        except Exception as exc:
            preflight_results.append(
                TargetResult(
                    server_file=str(server_file),
                    server_name=server_file.stem,
                    server_id=server_file.stem,
                    target="parse",
                    kind="parse",
                    transport="-",
                    status=STATUS_FAIL,
                    duration_s=0.0,
                    reason=f"failed to parse JSON: {exc}",
                )
            )
            continue

        server_name = str(server_data.get("name") or server_file.stem)
        server_id = server_file.stem
        short_id = server_name.split("/", 1)[1] if "/" in server_name else server_id
        capabilities = get_server_capabilities(server_data)
        packages = server_data.get("packages") or []
        remotes = server_data.get("remotes") or []
        server_remote_only = bool(remotes) and not bool(packages)

        if not packages and not remotes:
            preflight_results.append(
                TargetResult(
                    server_file=str(server_file),
                    server_name=server_name,
                    server_id=server_id,
                    target="server",
                    kind="server",
                    transport="-",
                    status=STATUS_FAIL,
                    duration_s=0.0,
                    reason="server has no packages and no remotes",
                )
            )
            continue

        for index, package in enumerate(packages):
            transport = str((package.get("transport") or {}).get("type") or "")
            targets.append(
                TargetSpec(
                    server_file=server_file,
                    server_name=server_name,
                    server_id=server_id,
                    short_id=short_id,
                    capabilities=capabilities,
                    server_remote_only=server_remote_only,
                    kind="package",
                    index=index,
                    transport=transport,
                    spec=package,
                )
            )

        for index, remote in enumerate(remotes):
            transport = str(remote.get("type") or "")
            targets.append(
                TargetSpec(
                    server_file=server_file,
                    server_name=server_name,
                    server_id=server_id,
                    short_id=short_id,
                    capabilities=capabilities,
                    server_remote_only=server_remote_only,
                    kind="remote",
                    index=index,
                    transport=transport,
                    spec=remote,
                )
            )

    return targets, preflight_results


def filter_targets(targets: list[TargetSpec], only_filters: list[str]) -> list[TargetSpec]:
    if not only_filters:
        return targets

    requested = {item.strip() for item in only_filters if item.strip()}
    if not requested:
        return targets

    filtered: list[TargetSpec] = []
    for target in targets:
        if (
            target.server_name in requested
            or target.server_id in requested
            or target.short_id in requested
        ):
            filtered.append(target)

    return filtered


async def call_with_timeout(coro: Any, timeout_seconds: float) -> Any:
    async with asyncio.timeout(timeout_seconds):
        return await coro


async def verify_capabilities(
    session: ClientSession,
    capabilities: set[str],
    rpc_timeout: float,
) -> list[str]:
    warnings: list[str] = []

    async def probe(name: str, coro: Any) -> None:
        try:
            await call_with_timeout(coro, rpc_timeout)
        except Exception as exc:
            message = format_exception(exc)
            if "method not found" in message.lower():
                warnings.append(f"{name}: method not found (ignored)")
                return
            raise

    if "tools" in capabilities:
        await probe("list_tools", session.list_tools())
    if "resources" in capabilities:
        await probe("list_resources", session.list_resources())

    return warnings


def append_warnings_to_reason(base_reason: str, warnings: list[str]) -> str:
    if not warnings:
        return base_reason
    return f"{base_reason}; warnings: {', '.join(warnings)}"


def build_package_command(
    package: dict[str, Any],
    inv_values: dict[str, str],
    env_values: dict[str, str],
) -> tuple[str | None, list[str], dict[str, str], list[str]]:
    errors: list[str] = []

    runtime = package.get("runtimeHint") or FALLBACK_RUNTIME_BY_REGISTRY.get(str(package.get("registryType") or ""))
    if not runtime:
        errors.append(
            "cannot determine runtime command: neither runtimeHint nor supported fallback by registryType"
        )
        return None, [], {}, errors

    if shutil.which(runtime) is None:
        errors.append(f"runtime not found: {runtime}")

    runtime_tokens, runtime_errors = resolve_argument_tokens(
        package.get("runtimeArguments") or [],
        inv_values,
        env_values,
    )
    package_tokens, package_errors = resolve_argument_tokens(
        package.get("packageArguments") or [],
        inv_values,
        env_values,
    )
    env_map, env_errors = resolve_environment_variables(
        package.get("environmentVariables") or [],
        inv_values,
        env_values,
    )

    errors.extend(runtime_errors)
    errors.extend(package_errors)
    errors.extend(env_errors)

    identifier = package.get("identifier")
    if not identifier:
        errors.append("missing package identifier")

    args = [*runtime_tokens]
    if identifier:
        args.append(str(identifier))
    args.extend(package_tokens)

    return runtime, args, env_map, dedupe_preserve_order(errors)


async def check_package_target(
    target: TargetSpec,
    inv_values: dict[str, str],
    env_values: dict[str, str],
    options: RuntimeOptions,
    cwd: Path,
) -> tuple[str, str]:
    runtime, args, env_map, errors = build_package_command(target.spec, inv_values, env_values)
    if errors:
        return STATUS_FAIL, "; ".join(errors)
    if runtime is None:
        return STATUS_FAIL, "runtime resolution failed"

    server_params = StdioServerParameters(
        command=runtime,
        args=args,
        env=env_map or None,
        cwd=str(cwd),
    )

    async with asyncio.timeout(options.startup_timeout):
        async with stdio_client(server_params) as (read_stream, write_stream):
            async with ClientSession(read_stream, write_stream) as session:
                await call_with_timeout(session.initialize(), options.rpc_timeout)
                warnings = await verify_capabilities(session, target.capabilities, options.rpc_timeout)

    return STATUS_PASS, append_warnings_to_reason("initialize + capability checks succeeded", warnings)


def is_oauth_challenge_response(response: httpx.Response) -> bool:
    return response.status_code in {401, 403} and "www-authenticate" in response.headers


def mcp_initialize_payload() -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-12-11",
            "capabilities": {},
            "clientInfo": {
                "name": "broxy-registry-checker",
                "version": "1.0.0",
            },
        },
    }


async def probe_oauth_challenge(
    transport: str,
    url: str,
    headers: dict[str, str],
    timeout_seconds: float,
) -> bool:
    timeout = httpx.Timeout(timeout_seconds, read=timeout_seconds)
    probes: list[tuple[str, str, dict[str, Any]]] = []

    if transport == "streamable-http":
        probes.append(("POST", url, {"json": mcp_initialize_payload()}))
        probes.append(("GET", url, {}))
    elif transport == "sse":
        probes.append(("GET", url, {}))
        probes.append(("POST", url, {"json": mcp_initialize_payload()}))

    async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
        for method, target_url, kwargs in probes:
            request_headers = dict(headers)
            if method == "GET" and transport == "sse":
                request_headers.setdefault("Accept", "text/event-stream")
            try:
                response = await client.request(method, target_url, headers=request_headers, **kwargs)
            except Exception:
                continue
            if is_oauth_challenge_response(response):
                return True

    return False


async def check_remote_target(
    target: TargetSpec,
    inv_values: dict[str, str],
    env_values: dict[str, str],
    options: RuntimeOptions,
) -> tuple[str, str]:
    url, url_errors = resolve_remote_url(target.spec, inv_values, env_values)
    headers, header_errors = resolve_headers(
        target.spec.get("headers") or [],
        inv_values,
        env_values,
    )

    if url_errors:
        return STATUS_FAIL, "; ".join(dedupe_preserve_order(url_errors))
    if url is None:
        return STATUS_FAIL, "remote url resolution failed"

    if header_errors:
        return STATUS_FAIL, "; ".join(dedupe_preserve_order(header_errors))

    try:
        if target.transport == "streamable-http":
            timeout = httpx.Timeout(options.http_timeout, read=options.http_timeout)
            async with httpx.AsyncClient(headers=headers, timeout=timeout) as client:
                async with asyncio.timeout(options.startup_timeout):
                    async with streamable_http_client(url, http_client=client) as streams:
                        if len(streams) == 2:
                            read_stream, write_stream = streams
                        elif len(streams) == 3:
                            read_stream, write_stream, _ = streams
                        else:
                            return STATUS_FAIL, f"unsupported stream tuple size: {len(streams)}"
                        async with ClientSession(read_stream, write_stream) as session:
                            await call_with_timeout(session.initialize(), options.rpc_timeout)
                            warnings = await verify_capabilities(session, target.capabilities, options.rpc_timeout)
        elif target.transport == "sse":
            async with asyncio.timeout(options.startup_timeout):
                async with sse_client(
                    url,
                    headers=headers,
                    timeout=options.http_timeout,
                    sse_read_timeout=options.http_timeout,
                ) as (read_stream, write_stream):
                    async with ClientSession(read_stream, write_stream) as session:
                        await call_with_timeout(session.initialize(), options.rpc_timeout)
                        warnings = await verify_capabilities(session, target.capabilities, options.rpc_timeout)
        else:
            return STATUS_FAIL, f"unsupported remote transport: {target.transport}"
    except Exception as exc:
        if target.server_remote_only:
            if await probe_oauth_challenge(target.transport, url, headers, options.http_timeout):
                oauth_analysis = await analyze_oauth_registration_support(
                    target.transport,
                    url,
                    headers,
                    options.http_timeout,
                )
                if oauth_analysis.status == "registration_supported":
                    return STATUS_PASS_OAUTH, oauth_analysis.detail
                if oauth_analysis.status == "registration_unavailable":
                    return STATUS_FAIL, oauth_analysis.detail
                return STATUS_FAIL, oauth_analysis.detail
        return STATUS_FAIL, f"remote check failed: {format_exception(exc)}"

    return STATUS_PASS, append_warnings_to_reason("initialize + capability checks succeeded", warnings)


async def check_target(
    target: TargetSpec,
    inv_values: dict[str, str],
    env_values: dict[str, str],
    options: RuntimeOptions,
    cwd: Path,
    secret_values: set[str],
) -> TargetResult:
    started = time.perf_counter()

    try:
        if target.kind == "package":
            status, reason = await check_package_target(target, inv_values, env_values, options, cwd)
        else:
            status, reason = await check_remote_target(target, inv_values, env_values, options)
    except Exception as exc:  # pragma: no cover
        status, reason = STATUS_FAIL, f"unexpected error: {format_exception(exc)}"

    duration = time.perf_counter() - started
    sanitized_reason = mask_known_secrets(reason, secret_values)

    return TargetResult(
        server_file=str(target.server_file),
        server_name=target.server_name,
        server_id=target.server_id,
        target=target.target_label,
        kind=target.kind,
        transport=target.transport,
        status=status,
        duration_s=round(duration, 3),
        reason=sanitized_reason,
    )


def mask_known_secrets(text: str, secret_values: set[str]) -> str:
    if not text:
        return text

    masked = text
    for secret in sorted((item for item in secret_values if item and len(item) >= 4), key=len, reverse=True):
        masked = masked.replace(secret, "***")
    return masked


def summarize_results(results: list[TargetResult]) -> dict[str, Any]:
    summary = {
        "total": len(results),
        STATUS_PASS: 0,
        STATUS_PASS_OAUTH: 0,
        STATUS_FAIL: 0,
    }

    failed_servers: set[str] = set()
    for result in results:
        summary[result.status] = summary.get(result.status, 0) + 1
        if result.status == STATUS_FAIL:
            failed_servers.add(result.server_id)

    summary["failed_servers"] = sorted(failed_servers)
    summary["generated_at"] = datetime.now(timezone.utc).isoformat()
    return summary


def categorize_error(reason: str) -> tuple[str, str]:
    text = reason.lower()
    if "timeouterror" in text:
        return "timeout", "Increase timeouts or verify dependent services/credentials."
    if "connection closed" in text:
        return "connection_closed", "Server process exited early; inspect runtime logs and required startup config."
    if "method not found" in text:
        return "method_not_found", "Protocol/capability mismatch between client expectation and server implementation."
    if "configure clientid or clientidmetadataurl" in text or "registration support found" in text:
        return "oauth_client_registration_missing", "Configure OAuth clientId/clientIdMetadataUrl or use a server with DCR/CIMD support."
    if "httpstatuserror" in text and "400 bad request" in text:
        return "http_400_bad_request", "Check auth headers/token format and endpoint requirements."
    if "connecterror" in text:
        return "connect_error", "Endpoint is unreachable from this environment."
    if "brokenresourceerror" in text:
        return "broken_resource", "Underlying process/resource stream broke; check runtime/container stability."
    if "mcperror" in text:
        return "mcp_protocol_error", "MCP-level error; verify server protocol behavior and compatibility."
    return "other", "Inspect full error and server logs."


def build_failure_analysis(results: list[TargetResult]) -> dict[str, Any]:
    failed = [item for item in results if item.status == STATUS_FAIL]
    by_category: dict[str, dict[str, Any]] = {}

    for item in failed:
        category, recommendation = categorize_error(item.reason)
        bucket = by_category.setdefault(
            category,
            {
                "count": 0,
                "servers": [],
                "examples": [],
                "recommendation": recommendation,
            },
        )
        bucket["count"] += 1
        bucket["servers"].append(item.server_id)
        if item.reason not in bucket["examples"]:
            bucket["examples"].append(item.reason)

    for bucket in by_category.values():
        bucket["servers"] = sorted(dedupe_preserve_order(bucket["servers"]))

    return {
        "failed_total": len(failed),
        "categories": dict(sorted(by_category.items(), key=lambda kv: kv[1]["count"], reverse=True)),
    }


def render_failure_analysis_markdown(summary: dict[str, Any], analysis: dict[str, Any], results: list[TargetResult]) -> str:
    lines: list[str] = []
    lines.append("# MCP Runtime Failure Analysis")
    lines.append("")
    lines.append(f"- Generated at (UTC): `{summary.get('generated_at', '')}`")
    lines.append(f"- Total targets: **{summary.get('total', 0)}**")
    lines.append(f"- Pass: **{summary.get(STATUS_PASS, 0)}**")
    lines.append(f"- Pass OAuth challenge: **{summary.get(STATUS_PASS_OAUTH, 0)}**")
    lines.append(f"- Fail: **{summary.get(STATUS_FAIL, 0)}**")
    lines.append("")
    lines.append("## Failure Categories")
    lines.append("")

    categories = analysis.get("categories", {})
    if not categories:
        lines.append("No failures.")
    else:
        for category, payload in categories.items():
            lines.append(f"- `{category}`: **{payload['count']}**")
            lines.append(f"  Recommendation: {payload['recommendation']}")
            lines.append(f"  Servers: {', '.join(payload['servers'])}")

    lines.append("")
    lines.append("## Failed Targets")
    lines.append("")
    lines.append("| Server | Transport | Reason |")
    lines.append("|---|---|---|")
    for item in sorted((entry for entry in results if entry.status == STATUS_FAIL), key=lambda x: x.server_id):
        lines.append(f"| `{item.server_id}` | `{item.transport}` | `{item.reason}` |")

    lines.append("")
    return "\n".join(lines)


def print_failure_analysis(analysis: dict[str, Any]) -> None:
    categories = analysis.get("categories", {})
    if not categories:
        return

    print("\nFailure analysis:")
    for category, payload in categories.items():
        servers = ", ".join(payload["servers"])
        print(f"- {category}: {payload['count']} [{servers}]")
        print(f"  recommendation: {payload['recommendation']}")


def print_results_table(results: list[TargetResult]) -> None:
    headers = ["server_id", "target", "transport", "status", "duration_s", "reason"]
    rows = [
        [
            item.server_id,
            item.target,
            item.transport,
            item.status,
            f"{item.duration_s:.3f}",
            item.reason,
        ]
        for item in results
    ]

    widths = [len(header) for header in headers]
    for row in rows:
        for index, cell in enumerate(row):
            widths[index] = max(widths[index], len(cell))

    def format_row(values: list[str]) -> str:
        return " | ".join(value.ljust(widths[i]) for i, value in enumerate(values))

    print(format_row(headers))
    print("-+-".join("-" * width for width in widths))
    for row in rows:
        print(format_row(row))


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Runtime MCP server checker for servers/*.json definitions.",
    )
    parser.add_argument("--env-file", default=".env", help="Path to .env file with KEY=VALUE inputs.")
    parser.add_argument("--servers-dir", default="servers", help="Path to directory with server JSON files.")
    parser.add_argument(
        "--only",
        action="append",
        default=[],
        help="Filter by server full name (io.qent.broxy/<id>) or by short <id>. Can be repeated.",
    )
    parser.add_argument("--concurrency", type=int, default=4, help="Maximum concurrent checks.")
    parser.add_argument("--startup-timeout", type=float, default=300.0, help="Timeout in seconds per target startup.")
    parser.add_argument("--rpc-timeout", type=float, default=300.0, help="Timeout in seconds per MCP RPC call.")
    parser.add_argument("--http-timeout", type=float, default=300.0, help="Timeout in seconds for HTTP requests.")
    parser.add_argument("--report-json", default=None, help="Optional path to write JSON report.")
    parser.add_argument("--analysis-md", default=None, help="Optional path to write markdown failure analysis.")
    return parser.parse_args(argv)


async def run_checks(
    targets: list[TargetSpec],
    inv_values: dict[str, str],
    env_values: dict[str, str],
    options: RuntimeOptions,
    concurrency: int,
    cwd: Path,
    secret_values: set[str],
) -> list[TargetResult]:
    semaphore = asyncio.Semaphore(max(1, concurrency))
    results: list[TargetResult | None] = [None] * len(targets)

    async def run_one(index: int, target: TargetSpec) -> None:
        async with semaphore:
            results[index] = await check_target(target, inv_values, env_values, options, cwd, secret_values)

    tasks = [asyncio.create_task(run_one(i, target)) for i, target in enumerate(targets)]
    if tasks:
        await asyncio.gather(*tasks)

    return [item for item in results if item is not None]


def ensure_dependencies() -> tuple[bool, list[str]]:
    errors: list[str] = []

    if HTTPX_IMPORT_ERROR is not None:
        errors.append(f"missing dependency 'httpx': {HTTPX_IMPORT_ERROR}")

    if MCP_IMPORT_ERROR is not None:
        errors.append(f"missing dependency 'mcp': {MCP_IMPORT_ERROR}")

    return (not errors), errors


def main(argv: list[str]) -> int:
    args = parse_args(argv)

    dependencies_ok, dependency_errors = ensure_dependencies()
    if not dependencies_ok:
        print("Dependency check failed:", file=sys.stderr)
        for error in dependency_errors:
            print(f"  - {error}", file=sys.stderr)
        print("Install requirements, for example: pip install mcp httpx", file=sys.stderr)
        return 2

    root_dir = Path.cwd()
    servers_dir = (root_dir / args.servers_dir).resolve()
    if not servers_dir.exists() or not servers_dir.is_dir():
        print(f"servers directory not found: {servers_dir}", file=sys.stderr)
        return 2

    env_path = (root_dir / args.env_file).resolve()
    inv_values = load_dotenv_file(env_path)
    env_values = dict(os.environ)

    options = RuntimeOptions(
        startup_timeout=max(args.startup_timeout, 1.0),
        rpc_timeout=max(args.rpc_timeout, 1.0),
        http_timeout=max(args.http_timeout, 1.0),
    )

    targets, preflight_results = discover_targets(servers_dir)
    targets = filter_targets(targets, args.only)
    preflight_results = [
        result
        for result in preflight_results
        if not args.only
        or result.server_id in args.only
        or result.server_name in args.only
        or result.server_name.split("/", 1)[-1] in args.only
    ]

    if not targets and not preflight_results:
        print("No targets selected.", file=sys.stderr)
        return 1

    secret_values = set(inv_values.values())
    for key, value in env_values.items():
        upper_key = key.upper()
        if any(marker in upper_key for marker in ("TOKEN", "SECRET", "PASSWORD", "KEY", "PAT")):
            secret_values.add(value)

    checked_results = asyncio.run(
        run_checks(
            targets=targets,
            inv_values=inv_values,
            env_values=env_values,
            options=options,
            concurrency=max(args.concurrency, 1),
            cwd=root_dir,
            secret_values=secret_values,
        )
    )

    all_results = [*preflight_results, *checked_results]
    all_results.sort(key=lambda item: (item.server_id, item.target))

    print_results_table(all_results)

    summary = summarize_results(all_results)
    print(
        f"\nSummary: total={summary['total']} pass={summary.get(STATUS_PASS, 0)} "
        f"pass_oauth_challenge={summary.get(STATUS_PASS_OAUTH, 0)} fail={summary.get(STATUS_FAIL, 0)}"
    )
    failure_analysis = build_failure_analysis(all_results)
    print_failure_analysis(failure_analysis)

    if args.report_json:
        report_path = (root_dir / args.report_json).resolve()
        report = {
            "summary": summary,
            "analysis": failure_analysis,
            "results": [asdict(item) for item in all_results],
        }
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"JSON report written: {report_path}")

    if args.analysis_md:
        analysis_path = (root_dir / args.analysis_md).resolve()
        analysis_text = render_failure_analysis_markdown(summary, failure_analysis, all_results)
        analysis_path.write_text(analysis_text, encoding="utf-8")
        print(f"Markdown analysis written: {analysis_path}")

    return 1 if summary.get(STATUS_FAIL, 0) else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
