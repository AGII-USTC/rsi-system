"""Private Pi provider and RSI role settings for the dashboard."""
from __future__ import annotations

import json
import os
import fcntl
from datetime import datetime, timezone
from contextlib import contextmanager, ExitStack
from pathlib import Path
import re
import secrets
import shlex
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from rsi_system.core.config import load_research_config, private_config_dir
from rsi_system.core.reasoning_capabilities import (
    THINKING_LEVELS,
    capability_record,
    model_capabilities,
    normalize_levels,
)


ROLES = ("coordinator", "researcher", "hypothesis", "coder", "analyzer", "critic", "reviewer")
APIS = ("openai-responses", "openai-completions", "anthropic-messages")
# Pi accepts ``off`` and ``max`` in addition to the levels supported by older
# dashboard releases.  Keep this alias for existing imports.
THINKING = THINKING_LEVELS
IDENTIFIER = re.compile(r"^[A-Za-z][A-Za-z0-9._-]{0,63}$")
MODEL_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")

# Anthropic's public model names are useful when an API-compatible gateway does
# not expose ``GET /models``.  They are intentionally only identifiers: the
# dashboard still lets the user edit the list and does not claim capabilities.
ANTHROPIC_KNOWN_MODELS = (
    "claude-3-5-sonnet-latest",
    "claude-3-5-haiku-latest",
    "claude-3-opus-latest",
    "claude-3-haiku-20240307",
)


class _NoRedirect(HTTPRedirectHandler):
    """Prevent a provider from redirecting a credential-bearing request."""

    def redirect_request(self, request, fp, code, msg, headers, newurl):
        return None


_DISCOVERY_OPENER = build_opener(_NoRedirect())


def _validate_connection(base_url: object, api: object, api_key: object) -> tuple[str, str, str]:
    """Validate discovery input without reading or changing saved credentials."""
    if (not isinstance(base_url, str) or not 1 <= len(base_url) <= 500
            or base_url != base_url.strip()
            or any(ord(char) < 32 or ord(char) == 127 for char in base_url)):
        raise ValueError("请输入 API 地址")
    try:
        parsed = urlsplit(base_url)
        parsed.port
    except ValueError as exc:
        raise ValueError("API 地址端口无效") from exc
    local_http = parsed.scheme == "http" and parsed.hostname in {"127.0.0.1", "localhost"}
    if (parsed.scheme != "https" and not local_http) or not parsed.netloc or parsed.username or parsed.password or parsed.fragment or parsed.query:
        raise ValueError("API 地址必须是 HTTPS，或本机 HTTP 地址")
    if api not in APIS:
        raise ValueError("不支持的 API 协议")
    if not isinstance(api_key, str) or not api_key.strip() or len(api_key) > 4096 or "\n" in api_key or "\r" in api_key:
        raise ValueError("请输入 API Key（已有密钥不会回传）")
    return base_url, api, api_key.strip()


def _discovery_url(base_url: str) -> str:
    return base_url.rstrip("/") + "/models"


def _safe_json_request(url: str, api: str, api_key: str, *, timeout: float = 4.0) -> tuple[int, object]:
    """Fetch a bounded JSON response, with no redirects and no response logging."""
    headers = {"Accept": "application/json"}
    if api == "anthropic-messages":
        headers.update({"x-api-key": api_key, "anthropic-version": "2023-06-01"})
    else:
        headers["Authorization"] = f"Bearer {api_key}"
    request = Request(url, headers=headers, method="GET")
    try:
        # urlopen does not include request headers in BaseHTTPRequestHandler logs,
        # and the URL itself is validated to contain no query or credentials.
        with _DISCOVERY_OPENER.open(request, timeout=timeout) as response:
            status = int(getattr(response, "status", 200))
            raw = response.read(1_000_001)
    except HTTPError as exc:
        # Do not include provider response bodies: gateways sometimes echo
        # request headers or account metadata in an error document.
        raise ValueError(f"供应商返回错误（HTTP {exc.code}）") from None
    except (URLError, TimeoutError, OSError) as exc:
        raise ValueError("无法连接供应商，请检查 API 地址和网络") from exc
    if len(raw) > 1_000_000:
        raise ValueError("供应商响应过大")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("供应商返回了无法读取的 JSON") from exc
    return status, payload


def _extract_models(payload: object) -> list[dict]:
    if isinstance(payload, dict):
        candidates = payload.get("data", payload.get("models"))
    else:
        candidates = payload
    if not isinstance(candidates, list):
        raise ValueError("供应商返回的数据中没有模型列表")
    models: list[dict] = []
    seen: set[str] = set()
    for item in candidates:
        model_id = item if isinstance(item, str) else item.get("id") if isinstance(item, dict) else None
        if not isinstance(model_id, str) or not MODEL_ID.fullmatch(model_id) or model_id in seen:
            continue
        seen.add(model_id)
        model: dict[str, object] = {"id": model_id}
        if isinstance(item, dict):
            # Keep only non-sensitive, scalar metadata.  The editor still
            # provides manual context and output controls for every model.
            if isinstance(item.get("name"), str) and len(item["name"]) <= 200:
                model["name"] = item["name"]
            # OpenAI-compatible gateways use a few different spellings for
            # these optional limits.  Keep only positive integer metadata and
            # never copy arbitrary fields from the provider response.
            for output_key, aliases in {
                "context_window": ("context_window", "contextWindow", "context_length", "max_context_tokens", "input_token_limit"),
                "max_tokens": ("max_tokens", "maxTokens", "max_output_tokens", "max_completion_tokens", "output_token_limit"),
            }.items():
                for alias in aliases:
                    value = item.get(alias)
                    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
                        model[output_key] = value
                        break
        models.append(model)
        if len(models) >= 100:
            break
    if not models:
        raise ValueError("供应商未返回可用模型")
    return models


def _billing_default() -> dict:
    return {
        "balance": None,
        "currency": None,
        "remaining": None,
        "credit_limit": None,
        "debt": None,
        "outstanding": None,
        "status": "unknown",
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "source": None,
        "availability": "unknown",
    }


def _number(value: object) -> float | int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value


def _billing_summary(payload: object, source: str) -> dict:
    result = _billing_default()
    result.update(source=source, availability="available", status="available")
    if not isinstance(payload, dict):
        return result
    # Read a small allowlist from the credit summary.  Never return the full
    # response, which may contain account or credential metadata.
    aliases = {
        "balance": ("balance", "total_available", "available"),
        "remaining": ("remaining", "total_available", "available"),
        "credit_limit": ("credit_limit", "total_granted", "hard_limit", "hard_limit_usd"),
        "debt": ("debt", "amount_due"),
        "outstanding": ("outstanding", "amount_due"),
    }
    for field, keys in aliases.items():
        for key in keys:
            value = _number(payload.get(key))
            if value is not None:
                result[field] = value
                break
    currency = payload.get("currency", payload.get("currency_code"))
    if isinstance(currency, str) and re.fullmatch(r"[A-Za-z]{3,8}", currency):
        result["currency"] = currency.upper()
    status = payload.get("status")
    if isinstance(status, str) and status.lower() in {"active", "ok", "available", "suspended", "overdue", "past_due"}:
        result["status"] = status.lower()
    return result


def _billing_urls(base_url: str) -> list[str]:
    parsed = urlsplit(base_url)
    origin = urlunsplit((parsed.scheme, parsed.netloc, "", "", ""))
    base_path = parsed.path.rstrip("/")
    candidates = [
        f"{base_path}/dashboard/billing/credit_grants",
        "/v1/dashboard/billing/credit_grants",
        "/dashboard/billing/credit_grants",
    ]
    urls: list[str] = []
    for path in candidates:
        url = urlunsplit((parsed.scheme, parsed.netloc, path, "", "")) if path.startswith("/") else origin + path
        if url not in urls:
            urls.append(url)
    return urls


def _private_json(path: Path, value: dict) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.is_symlink():
        raise ValueError("configuration path must not be a symlink")
    temp = path.with_name(f".{path.name}.{secrets.token_hex(6)}.tmp")
    try:
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        os.replace(temp, path)
        os.chmod(path, 0o600)
    finally:
        temp.unlink(missing_ok=True)


def _private_key(path: Path, value: str) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.is_symlink():
        raise ValueError("key path must not be a symlink")
    temp = path.with_name(f".{path.name}.{secrets.token_hex(6)}.tmp")
    try:
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(value.strip() + "\n")
        os.replace(temp, path)
        os.chmod(path, 0o600)
    finally:
        temp.unlink(missing_ok=True)


def _public_url(raw: object) -> str:
    if not isinstance(raw, str):
        return ""
    try:
        parsed = urlsplit(raw)
        hostname = parsed.hostname
        if not hostname:
            return ""
        authority = f"[{hostname}]" if ":" in hostname else hostname
        if parsed.port:
            authority += f":{parsed.port}"
        return urlunsplit((parsed.scheme, authority, parsed.path, "", ""))
    except ValueError:
        return ""


class Configuration:
    def __init__(self, root: Path, *, config_dir: Path | None = None, pi_dir: Path | None = None):
        self.root = root.resolve()
        self.config_dir = config_dir or private_config_dir()
        self.pi_dir = pi_dir or Path.home() / ".pi" / "agent"

    @property
    def models_path(self) -> Path:
        return self.pi_dir / "models.json"

    def _models(self) -> dict:
        if not self.models_path.exists():
            return {"providers": {}}
        data = json.loads(self.models_path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or not isinstance(data.get("providers", {}), dict):
            raise ValueError("Pi models.json has an invalid structure")
        data.setdefault("providers", {})
        return data

    def _research(self) -> dict:
        return load_research_config(self.root, config_dir=self.config_dir)

    @contextmanager
    def _locked(self):
        paths = {self.config_dir / ".dashboard.lock", self.pi_dir / ".rsi-models.lock"}
        with ExitStack() as stack:
            for path in sorted(paths):
                path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                if path.is_symlink():
                    raise ValueError("configuration lock must not be a symlink")
                fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
                stream = stack.enter_context(os.fdopen(fd, "r+"))
                fcntl.flock(stream, fcntl.LOCK_EX)
            yield

    def public(self) -> dict:
        models = self._models()["providers"]
        config = self._research()
        providers = []
        for name, provider in sorted(models.items()):
            if not isinstance(provider, dict):
                continue
            configured_key = provider.get("apiKey")
            public_models = []
            for item in provider.get("models", []):
                if not isinstance(item, dict) or not item.get("id"):
                    continue
                capabilities = model_capabilities(item)
                public_models.append({
                    "id": item.get("id"),
                    "context_window": item.get("contextWindow"),
                    "max_tokens": item.get("maxTokens"),
                    # A missing tier intentionally means "use the role default".
                    "thinking": item.get("thinking") if item.get("thinking") in THINKING else None,
                    "thinking_levels": list(capabilities.levels) if capabilities.levels is not None else None,
                    "thinking_capability": "known" if capabilities.known else "unknown",
                })
            providers.append({
                "name": name,
                "base_url": _public_url(provider.get("baseUrl", "")),
                "api": provider.get("api", ""),
                "has_key": bool(configured_key),
                "models": public_models,
            })
        roles = config.get("models", {})
        if not isinstance(roles, dict):
            roles = {}
        return {"providers": providers,
                "roles": {role: {key: value[key] for key in ("provider", "model") if key in value}
                          if isinstance(value, dict) else {} for role, value in
                          ((role, roles.get(role, {})) for role in ROLES)},
                "thinking": config.get("thinking", "high")}

    def discover_provider(self, body: dict) -> dict:
        """Discover models and a credential-free billing summary.

        This operation is deliberately stateless: the supplied key is sent
        only in the outbound request and is never written, logged, or included
        in the returned payload.  Saved provider credentials are not consulted
        when a user is editing an existing provider.
        """
        # ``name`` and ``models`` are accepted so the form can send its
        # current draft unchanged.  Discovery remains stateless and does not
        # save either the draft or the temporary key.
        if set(body) - {"name", "base_url", "api", "api_key", "models"}:
            raise ValueError("模型发现请求包含未知字段")
        base_url, api, api_key = _validate_connection(body.get("base_url"), body.get("api"), body.get("api_key"))
        if api == "anthropic-messages":
            # Anthropic's first-party API does not expose the OpenAI
            # ``GET /models`` contract.  Return a small known list so users can
            # continue editing the model textarea without a misleading probe.
            models = [{"id": model_id} for model_id in ANTHROPIC_KNOWN_MODELS]
            model_source = "known-list"
        else:
            _status, payload = _safe_json_request(_discovery_url(base_url), api, api_key, timeout=4.0)
            models = _extract_models(payload)
            model_source = "endpoint"

        billing = _billing_default()
        if api in {"openai-responses", "openai-completions"}:
            billing_attempted = False
            for billing_url in _billing_urls(base_url):
                billing_attempted = True
                try:
                    _status, billing_payload = _safe_json_request(billing_url, api, api_key, timeout=3.0)
                except ValueError:
                    # Billing support is optional.  Probe the common paths but
                    # never make a discovery failure depend on billing access.
                    continue
                billing = _billing_summary(billing_payload, urlsplit(billing_url).path)
                break
            if billing_attempted and billing["availability"] == "unknown":
                billing["availability"] = "unavailable"
                billing["status"] = "unavailable"
                billing["source"] = "provider billing endpoint"
        else:
            billing["availability"] = "unavailable"
            billing["status"] = "unavailable"
            billing["source"] = "供应商未提供"
        return {
            "models": models,
            "model_source": model_source,
            "billing": billing,
        }

    def save_provider(self, body: dict) -> dict:
        if set(body) - {"name", "base_url", "api", "models", "api_key", "context_window",
                       "max_tokens", "model_settings"}:
            raise ValueError("供应商配置包含未知字段")
        name, base_url, api = body.get("name"), body.get("base_url"), body.get("api")
        model_ids = body.get("models")
        api_key = body.get("api_key", "")
        if not isinstance(name, str) or not IDENTIFIER.fullmatch(name):
            raise ValueError("供应商名称只能使用英文字母、数字、点、下划线和连字符")
        if (not isinstance(base_url, str) or not 1 <= len(base_url) <= 500
                or base_url != base_url.strip() or any(ord(char) < 32 or ord(char) == 127 for char in base_url)):
            raise ValueError("请输入 API 地址")
        parsed = urlsplit(base_url)
        try:
            parsed.port
        except ValueError as exc:
            raise ValueError("API 地址端口无效") from exc
        local_http = parsed.scheme == "http" and parsed.hostname in {"127.0.0.1", "localhost"}
        if (parsed.scheme != "https" and not local_http) or not parsed.netloc or parsed.username or parsed.password or parsed.fragment or parsed.query:
            raise ValueError("API 地址必须是 HTTPS，或本机 HTTP 地址")
        if api not in APIS:
            raise ValueError("不支持的 API 协议")
        if (not isinstance(model_ids, list) or not 1 <= len(model_ids) <= 20 or
                any(not isinstance(item, str) or not MODEL_ID.fullmatch(item) for item in model_ids) or
                len(set(model_ids)) != len(model_ids)):
            raise ValueError("请填写 1 至 20 个不重复的模型 ID")
        if not isinstance(api_key, str) or len(api_key) > 4096 or "\n" in api_key or "\r" in api_key:
            raise ValueError("API Key 格式无效")
        context_window, max_tokens = body.get("context_window"), body.get("max_tokens")
        if context_window is not None and (type(context_window) is not int or not 1024 <= context_window <= 4_000_000):
            raise ValueError("上下文范围无效")
        if max_tokens is not None and (type(max_tokens) is not int or not 1 <= max_tokens <= 4_000_000):
            raise ValueError("最大输出范围无效")
        model_settings = body.get("model_settings", {})
        if not isinstance(model_settings, dict) or set(model_settings) - set(model_ids):
            raise ValueError("模型参数必须指向当前供应商中的模型")
        for model_id, settings in model_settings.items():
            if (not isinstance(settings, dict) or not settings or
                    set(settings) - {"context_window", "max_tokens", "thinking", "thinking_levels"}):
                raise ValueError(f"{model_id} 的参数字段无效")
            thinking = settings.get("thinking")
            if thinking is not None and thinking not in THINKING:
                raise ValueError(f"{model_id} 的推理档位无效")
            if "thinking_levels" in settings:
                levels = settings["thinking_levels"]
                if levels is not None and (normalize_levels(levels) is None or
                                           len(set(levels)) != len(levels)):
                    raise ValueError(f"{model_id} 的推理能力配置无效")
            if "context_window" in settings:
                window = settings["context_window"]
                if type(window) is not int or not 1024 <= window <= 4_000_000:
                    raise ValueError(f"{model_id} 的上下文范围无效")
            if "max_tokens" in settings:
                output = settings["max_tokens"]
                if type(output) is not int or not 1 <= output <= 4_000_000:
                    raise ValueError(f"{model_id} 的最大输出范围无效")
        with self._locked():
            data = self._models()
            providers = data.setdefault("providers", {})
            old = providers.get(name, {})
            if not isinstance(old, dict):
                raise ValueError("已有供应商配置无效")
            existing = {item.get("id"): item for item in old.get("models", []) if isinstance(item, dict)}
            selections = self._research().get("models", {})
            if not isinstance(selections, dict):
                raise ValueError("已有角色配置无效")
            for role, selection in selections.items():
                if (isinstance(selection, dict) and selection.get("provider") == name
                        and selection.get("model") not in model_ids):
                    raise ValueError(f"请先调整 {role} 的模型，再移除该模型")
            entries = []
            for model_id in model_ids:
                previous = existing.get(model_id)
                if previous is None:
                    window = context_window if context_window is not None else 128_000
                    output = max_tokens if max_tokens is not None else 16_384
                    # Pi defaults missing ``reasoning`` to false, but the
                    # dashboard must distinguish that known state from an
                    # unverified custom model.  Leave the field absent until
                    # the user or Pi metadata declares capabilities.
                    item = {"id": model_id, "name": model_id,
                            "input": ["text"], "contextWindow": window, "maxTokens": output}
                else:
                    item = dict(previous)
                if model_id in model_settings:
                    settings = model_settings[model_id]
                    if "context_window" in settings:
                        item["contextWindow"] = settings["context_window"]
                    if "max_tokens" in settings:
                        item["maxTokens"] = settings["max_tokens"]
                    if "thinking" in settings:
                        if settings["thinking"] is None:
                            item.pop("thinking", None)
                        else:
                            item["thinking"] = settings["thinking"]
                    if "thinking_levels" in settings:
                        levels = settings["thinking_levels"]
                        if levels is None:
                            item.pop("reasoning", None)
                            item.pop("thinkingLevelMap", None)
                        else:
                            normalized = normalize_levels(levels)
                            assert normalized is not None
                            item["reasoning"] = any(level != "off" for level in normalized)
                            if item["reasoning"]:
                                mapping = {}
                                for level in THINKING_LEVELS:
                                    if level not in normalized:
                                        mapping[level] = None
                                    elif level == "off":
                                        mapping[level] = "off"
                                    elif level in {"xhigh", "max"}:
                                        mapping[level] = level
                                item["thinkingLevelMap"] = mapping
                            else:
                                item.pop("thinkingLevelMap", None)
                # A selected tier in the old API was the only explicit model
                # declaration available.  Materialize it for a previously
                # unknown model so legacy configs remain runnable safely.
                has_capability_metadata = ("reasoning" in item or "thinkingLevelMap" in item or
                                           "thinking_levels" in item or "thinkingLevels" in item)
                capabilities = model_capabilities(item)
                selected = item.get("thinking")
                if selected in THINKING and not has_capability_metadata:
                    item["reasoning"] = selected != "off"
                    if item["reasoning"]:
                        item["thinkingLevelMap"] = {
                            level: (level if level == selected else None)
                            for level in THINKING_LEVELS
                        }
                    else:
                        item.pop("thinkingLevelMap", None)
                    capabilities = model_capabilities(item)
                if selected is not None and selected not in THINKING:
                    item.pop("thinking", None)
                    selected = None
                # The previous dashboard version created custom models with
                # ``reasoning: true`` and allowed xhigh without a map. Keep
                # that saved explicit choice by materializing the native Pi
                # marker once, while new models still require a declaration.
                if (selected in {"xhigh", "max"} and item.get("reasoning") is True and
                        "thinkingLevelMap" not in item and "thinking_levels" not in model_settings.get(model_id, {})):
                    item["thinkingLevelMap"] = {selected: selected}
                    capabilities = model_capabilities(item)
                if selected is not None and capabilities.known and selected not in (capabilities.levels or ()):
                    raise ValueError(f"{model_id} 的推理档位不在模型支持集合中")
                if (type(item.get("maxTokens")) is int and type(item.get("contextWindow")) is int
                        and item["maxTokens"] > item["contextWindow"]):
                    raise ValueError(f"{model_id} 的输出长度超过上下文")
                entries.append(item)
            entry = dict(old)
            entry.update(baseUrl=base_url, api=api, models=entries)
            if api_key.strip():
                key_path = self.config_dir / "keys" / f"{name}.key"
                _private_key(key_path, api_key)
                entry["apiKey"] = f"!cat {shlex.quote(str(key_path.resolve()))}"
            providers[name] = entry
            _private_json(self.models_path, data)
            # Keep a credential-free index in the private research config so the
            # runtime can resolve a model's tier without reading models.json (which
            # also contains provider credentials).  Older configs simply omit it.
            private_path = self.config_dir / "research.json"
            private = json.loads(private_path.read_text(encoding="utf-8")) if private_path.is_file() else {}
            if not isinstance(private, dict):
                raise ValueError("私有研究配置无效")
            thinking_index = {}
            capability_index = {}
            for provider_name, provider_value in data["providers"].items():
                if not isinstance(provider_value, dict):
                    continue
                model_index = {}
                model_capability_index = {}
                for model in provider_value.get("models", []):
                    if not isinstance(model, dict) or not model.get("id"):
                        continue
                    if model.get("thinking") in THINKING:
                        model_index[model["id"]] = model["thinking"]
                    model_capability_index[model["id"]] = capability_record(model_capabilities(model))
                if model_index:
                    thinking_index[provider_name] = model_index
                if model_capability_index:
                    capability_index[provider_name] = model_capability_index
            private["model_thinking"] = thinking_index
            private["model_capabilities"] = capability_index
            _private_json(private_path, private)
        return self.public()

    def save_roles(self, body: dict) -> dict:
        if set(body) - {"roles", "thinking"}:
            raise ValueError("角色配置包含未知字段")
        roles, thinking = body.get("roles"), body.get("thinking")
        if not isinstance(roles, dict) or set(roles) - set(ROLES) or thinking not in THINKING:
            raise ValueError("角色配置无效")
        with self._locked():
            providers = self._models()["providers"]
            selected = {}
            for role in ROLES:
                value = roles.get(role, {})
                if value in (None, ""):
                    value = {}
                if not isinstance(value, dict) or set(value) - {"provider", "model"}:
                    raise ValueError(f"{role} 的模型配置无效")
                provider, model = value.get("provider"), value.get("model")
                if not provider and not model:
                    continue
                if not isinstance(provider, str) or not isinstance(model, str):
                    raise ValueError(f"{role} 的模型配置无效")
                available = providers.get(provider, {}).get("models", [])
                if not any(item.get("id") == model for item in available if isinstance(item, dict)):
                    raise ValueError(f"{role} 指向了未配置的模型")
                selected[role] = {"provider": provider, "model": model}
            private_path = self.config_dir / "research.json"
            private = json.loads(private_path.read_text(encoding="utf-8")) if private_path.is_file() else {}
            if not isinstance(private, dict):
                raise ValueError("私有研究配置无效")
            private.update(models=selected, thinking=thinking)
            _private_json(private_path, private)
        return self.public()
