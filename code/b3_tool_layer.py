from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import importlib
import inspect
import json
import sys
import errno
from pathlib import Path
from time import perf_counter, sleep, time
from typing import Any

from common.io_utils import append_jsonl, read_json, read_yaml, write_json
from common.logging_utils import now_iso
from common.path_utils import bootstrap_project_root, resolve_cli_path, resolve_from_file
from common.schemas import make_skill_result, make_tool_message, normalize_tool_call


bootstrap_project_root()


JSON_TYPES = {
    "string": str,
    "integer": int,
    "number": (int, float),
    "boolean": bool,
    "object": dict,
    "array": list,
}

DEFAULT_RETRY = {
    "max_retries": 0,
    "base_delay_ms": 200,
    "max_delay_ms": 2000,
    "by_exception": {},
}

DEFAULT_CACHE = {
    "enabled": False,
    "cache_errors": False,
    "ttl_seconds": None,
}

DEFAULT_TIMEOUT = {
    "default_timeout_s": 10.0,
}


def _load_tools_config(tools_config: str | Path) -> tuple[Path, dict]:
    config_path = Path(tools_config).resolve()
    config = read_yaml(config_path)
    if not isinstance(config, dict):
        raise ValueError("tools.yaml must contain an object")
    if not isinstance(config.get("tools"), dict) or not isinstance(config.get("toolsets"), dict):
        raise ValueError("tools.yaml must define tools and toolsets")
    return config_path, config


def _resolve_toolset(config: dict, toolset: str | None) -> tuple[str, list[str]]:
    selected = toolset or config.get("default_toolset")
    if not isinstance(selected, str) or selected not in config["toolsets"]:
        raise ValueError(f"toolset does not exist: {selected}")
    names = config["toolsets"][selected]
    if not isinstance(names, list) or not all(isinstance(name, str) for name in names):
        raise ValueError(f"toolset {selected} must be a list of tool names")
    return selected, names


def _parameter_schema(tool: dict) -> dict:
    raw_parameters = tool.get("parameters", {})
    if not isinstance(raw_parameters, dict):
        raise ValueError("tool parameters must be an object")
    properties = {}
    for name, definition in raw_parameters.items():
        if not isinstance(definition, dict) or definition.get("type") not in JSON_TYPES:
            raise ValueError(f"invalid parameter schema for {name}")
        properties[name] = dict(definition)
    required = tool.get("required", [])
    if not isinstance(required, list) or not all(name in properties for name in required):
        raise ValueError("required parameters must reference declared properties")
    return {
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": False,
    }


def get_tools_schema(
    tools_config: str,
    toolset: str,
    outdir: str | None = None,
) -> list[dict]:
    _, config = _load_tools_config(tools_config)
    selected, tool_names = _resolve_toolset(config, toolset)
    schema = []
    for name in tool_names:
        tool = config["tools"].get(name)
        if not isinstance(tool, dict):
            raise ValueError(f"toolset references missing tool: {name}")
        for field in ("module", "function", "description", "returns"):
            if field not in tool:
                raise ValueError(f"tool {name} missing {field}")
        returns = tool["returns"]
        if not isinstance(returns, dict):
            raise ValueError(f"tool {name} returns must be an object")
        schema.append(
            {
                "type": "function",
                "function": {
                    "name": name,
                    "description": tool["description"],
                    "parameters": _parameter_schema(tool),
                    "x-returns": {"type": "object", "properties": returns},
                },
            }
        )
    if outdir:
        output_dir = Path(outdir)
        write_json(schema, output_dir / "tools_schema.json")
        write_json(
            {"status": "success", "toolset": selected, "tool_count": len(schema), "tools": tool_names},
            output_dir / "tool_schema_report.json",
        )
    return schema


def _validate_args(args: dict, definition: dict) -> None:
    parameter_schema = _parameter_schema(definition)
    properties = parameter_schema["properties"]
    missing = [name for name in parameter_schema["required"] if name not in args]
    if missing:
        raise ValueError(f"missing required parameters: {', '.join(missing)}")
    unknown = sorted(set(args) - set(properties))
    if unknown:
        raise ValueError(f"unknown parameters: {', '.join(unknown)}")
    for name, value in args.items():
        expected_name = properties[name]["type"]
        expected = JSON_TYPES[expected_name]
        if expected_name in {"integer", "number"} and isinstance(value, bool):
            valid = False
        else:
            valid = isinstance(value, expected)
        if not valid:
            raise ValueError(f"parameter {name} must be {expected_name}")
        if expected_name == "array" and "items" in properties[name]:
            item_type = properties[name]["items"].get("type")
            if item_type in JSON_TYPES and not all(isinstance(item, JSON_TYPES[item_type]) for item in value):
                raise ValueError(f"parameter {name} contains invalid items")


def _error_result(name: str, args: dict, exc: Exception, latency_ms: float = 0.0) -> dict:
    return make_skill_result(
        name,
        "error",
        args,
        None,
        {"type": type(exc).__name__, "message": str(exc)},
        latency_ms,
    )


def _load_retry_settings(config: dict) -> dict:
    settings = config.get("settings", {})
    retry = settings.get("retry", {})
    if retry is None:
        retry = {}
    if not isinstance(retry, dict):
        raise ValueError("settings.retry must be an object")
    merged = dict(DEFAULT_RETRY)
    for key in ("max_retries", "base_delay_ms", "max_delay_ms"):
        if key in retry:
            merged[key] = retry[key]
    by_exception = retry.get("by_exception", {})
    if by_exception is None:
        by_exception = {}
    if not isinstance(by_exception, dict) or not all(isinstance(k, str) and isinstance(v, dict) for k, v in by_exception.items()):
        raise ValueError("settings.retry.by_exception must be an object of exception -> policy")
    merged["by_exception"] = {k: dict(v) for k, v in by_exception.items()}
    if not isinstance(merged["max_retries"], int) or merged["max_retries"] < 0 or merged["max_retries"] > 5:
        raise ValueError("settings.retry.max_retries must be an integer between 0 and 5")
    for key in ("base_delay_ms", "max_delay_ms"):
        value = merged[key]
        if not isinstance(value, int) or value < 0:
            raise ValueError(f"settings.retry.{key} must be a non-negative integer")
    if merged["max_delay_ms"] < merged["base_delay_ms"]:
        merged["max_delay_ms"] = merged["base_delay_ms"]
    for exc_name, policy in merged["by_exception"].items():
        for key in ("max_retries", "base_delay_ms", "max_delay_ms"):
            if key not in policy:
                continue
            value = policy[key]
            if key == "max_retries":
                if not isinstance(value, int) or value < 0 or value > 5:
                    raise ValueError(f"settings.retry.by_exception.{exc_name}.max_retries must be 0..5")
            else:
                if not isinstance(value, int) or value < 0:
                    raise ValueError(f"settings.retry.by_exception.{exc_name}.{key} must be a non-negative integer")
        if "base_delay_ms" in policy and "max_delay_ms" in policy and policy["max_delay_ms"] < policy["base_delay_ms"]:
            policy["max_delay_ms"] = policy["base_delay_ms"]
    return merged


def _get_tool_retry_settings(definition: dict, base_retry: dict) -> dict:
    override = definition.get("retry", {})
    if override is None:
        override = {}
    if not isinstance(override, dict):
        raise ValueError("tool retry must be an object")
    merged = dict(base_retry)
    for key in ("max_retries", "base_delay_ms", "max_delay_ms"):
        if key in override:
            merged[key] = override[key]
    by_exception = override.get("by_exception", {})
    if by_exception is None:
        by_exception = {}
    if not isinstance(by_exception, dict) or not all(isinstance(k, str) and isinstance(v, dict) for k, v in by_exception.items()):
        raise ValueError("tool retry.by_exception must be an object of exception -> policy")
    merged_by_exception = {}
    if isinstance(base_retry.get("by_exception"), dict):
        merged_by_exception.update({k: dict(v) for k, v in base_retry["by_exception"].items() if isinstance(v, dict)})
    for exc_name, policy in by_exception.items():
        merged_by_exception[exc_name] = dict(policy)
    merged["by_exception"] = merged_by_exception
    if not isinstance(merged["max_retries"], int) or merged["max_retries"] < 0 or merged["max_retries"] > 5:
        raise ValueError("tool retry.max_retries must be an integer between 0 and 5")
    for key in ("base_delay_ms", "max_delay_ms"):
        value = merged[key]
        if not isinstance(value, int) or value < 0:
            raise ValueError(f"tool retry.{key} must be a non-negative integer")
    if merged["max_delay_ms"] < merged["base_delay_ms"]:
        merged["max_delay_ms"] = merged["base_delay_ms"]
    for exc_name, policy in merged["by_exception"].items():
        for key in ("max_retries", "base_delay_ms", "max_delay_ms"):
            if key not in policy:
                continue
            value = policy[key]
            if key == "max_retries":
                if not isinstance(value, int) or value < 0 or value > 5:
                    raise ValueError(f"tool retry.by_exception.{exc_name}.max_retries must be 0..5")
            else:
                if not isinstance(value, int) or value < 0:
                    raise ValueError(f"tool retry.by_exception.{exc_name}.{key} must be a non-negative integer")
        if "base_delay_ms" in policy and "max_delay_ms" in policy and policy["max_delay_ms"] < policy["base_delay_ms"]:
            policy["max_delay_ms"] = policy["base_delay_ms"]
    return merged


def _load_cache_settings(config_path: Path, config: dict) -> dict:
    settings = config.get("settings", {})
    cache = settings.get("cache", {})
    if cache is None:
        cache = {}
    if not isinstance(cache, dict):
        raise ValueError("settings.cache must be an object")
    merged = dict(DEFAULT_CACHE)
    for key in ("enabled", "cache_errors", "ttl_seconds"):
        if key in cache:
            merged[key] = cache[key]
    if not isinstance(merged["enabled"], bool):
        raise ValueError("settings.cache.enabled must be boolean")
    if not isinstance(merged["cache_errors"], bool):
        raise ValueError("settings.cache.cache_errors must be boolean")
    ttl_value = merged["ttl_seconds"]
    if ttl_value is None:
        merged["ttl_seconds"] = None
    else:
        if not isinstance(ttl_value, (int, float)) or isinstance(ttl_value, bool) or ttl_value < 0:
            raise ValueError("settings.cache.ttl_seconds must be a non-negative number or null")
        merged["ttl_seconds"] = float(ttl_value) if ttl_value > 0 else None
    cache_file = cache.get("cache_file")
    if merged["enabled"]:
        if not isinstance(cache_file, str) or not cache_file:
            raise ValueError("settings.cache.cache_file is required when cache is enabled")
        merged["cache_path"] = resolve_from_file(cache_file, config_path)
    else:
        merged["cache_path"] = None
    return merged


def _load_timeout_settings(config: dict) -> dict:
    settings = config.get("settings", {})
    timeout = settings.get("timeout", {})
    if timeout is None:
        timeout = {}
    if not isinstance(timeout, dict):
        raise ValueError("settings.timeout must be an object")
    merged = dict(DEFAULT_TIMEOUT)
    if "default_timeout_s" in timeout:
        merged["default_timeout_s"] = timeout["default_timeout_s"]
    value = merged["default_timeout_s"]
    if value is None:
        merged["default_timeout_s"] = None
        return merged
    if not isinstance(value, (int, float)) or isinstance(value, bool) or value <= 0:
        raise ValueError("settings.timeout.default_timeout_s must be a positive number or null")
    merged["default_timeout_s"] = float(value)
    return merged


def _get_tool_timeout_seconds(definition: dict, timeout_settings: dict) -> float | None:
    timeout_s = definition.get("timeout_s", timeout_settings["default_timeout_s"])
    if timeout_s is None:
        return None
    if not isinstance(timeout_s, (int, float)) or isinstance(timeout_s, bool) or timeout_s <= 0:
        raise ValueError("tool timeout_s must be a positive number or null")
    return float(timeout_s)


def _is_cacheable_tool(definition: dict) -> bool:
    cacheable = definition.get("cacheable", True)
    if not isinstance(cacheable, bool):
        raise ValueError("tool cacheable must be boolean")
    return cacheable


def _make_cache_key(name: str, args: dict) -> str:
    payload = json.dumps(
        {"name": name, "args": args},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _load_cache(path: Path) -> dict:
    if not path.exists():
        return {}
    cache = read_json(path)
    if not isinstance(cache, dict):
        raise ValueError("tool cache must be a JSON object")
    return cache


def _clone_result(result: dict) -> dict:
    return json.loads(json.dumps(result, ensure_ascii=False, separators=(",", ":")))


def _get_tool_cache_ttl_seconds(definition: dict, cache_settings: dict) -> float | None:
    ttl = definition.get("cache_ttl_s", cache_settings.get("ttl_seconds"))
    if ttl is None:
        return None
    if not isinstance(ttl, (int, float)) or isinstance(ttl, bool) or ttl < 0:
        raise ValueError("tool cache_ttl_s must be a non-negative number or null")
    return float(ttl) if ttl > 0 else None


def _unpack_cache_entry(entry: Any) -> tuple[dict | None, float | None]:
    if not isinstance(entry, dict):
        return None, None
    if isinstance(entry.get("result"), dict):
        ts = entry.get("cached_at_ts")
        if isinstance(ts, (int, float)) and not isinstance(ts, bool):
            return entry["result"], float(ts)
        return entry["result"], None
    if isinstance(entry.get("skill_name"), str):
        return entry, None
    return None, None


def _pack_cache_entry(result: dict) -> dict:
    return {"cached_at": now_iso(), "cached_at_ts": time(), "result": result}


def _is_retriable_exception(exc: Exception) -> bool:
    if isinstance(exc, (TimeoutError, ConnectionError)):
        return True
    if isinstance(exc, OSError):
        winerror = getattr(exc, "winerror", None)
        if winerror in {32, 33}:
            return True
        if exc.errno is None:
            return False
        return exc.errno in {
            errno.EAGAIN,
            errno.EWOULDBLOCK,
            errno.EINTR,
            errno.ETIMEDOUT,
            errno.ECONNRESET,
            errno.ECONNABORTED,
        }
    return False


def _call_with_timeout(function: Any, kwargs: dict, timeout_s: float | None) -> Any:
    if timeout_s is None:
        return function(**kwargs)
    executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    future = executor.submit(function, **kwargs)
    try:
        return future.result(timeout=timeout_s)
    except concurrent.futures.TimeoutError as exc:
        future.cancel()
        raise TimeoutError(f"tool execution exceeded timeout of {timeout_s:g} seconds") from exc
    finally:
        executor.shutdown(wait=False, cancel_futures=True)


def _retry_policy_for_exception(retry: dict, exc: Exception) -> dict:
    policy = {k: retry[k] for k in ("max_retries", "base_delay_ms", "max_delay_ms")}
    by_exception = retry.get("by_exception", {})
    override = by_exception.get(type(exc).__name__) if isinstance(by_exception, dict) else None
    if isinstance(override, dict):
        for key in ("max_retries", "base_delay_ms", "max_delay_ms"):
            if key in override:
                policy[key] = override[key]
        if policy["max_delay_ms"] < policy["base_delay_ms"]:
            policy["max_delay_ms"] = policy["base_delay_ms"]
    return policy


def _call_with_retry(function: Any, kwargs: dict, retry: dict, timeout_s: float | None) -> Any:
    retry_count = 0
    last_error = None
    while True:
        try:
            output = _call_with_timeout(function, kwargs, timeout_s)
            return {
                "output": output,
                "retry_count": retry_count,
                "recovered_after_retry": retry_count > 0,
                "last_error": last_error,
            }
        except Exception as exc:
            last_error = {"type": type(exc).__name__, "message": str(exc)}
            policy = _retry_policy_for_exception(retry, exc)
            max_retries = policy["max_retries"]
            base_delay_ms = policy["base_delay_ms"]
            max_delay_ms = policy["max_delay_ms"]
            if retry_count >= max_retries or not _is_retriable_exception(exc):
                setattr(exc, "_retry_count", retry_count)
                setattr(exc, "_recovered_after_retry", False)
                setattr(exc, "_last_error", last_error)
                raise
            delay_ms = min(max_delay_ms, base_delay_ms * (2**retry_count))
            if delay_ms:
                sleep(delay_ms / 1000)
            retry_count += 1


def execute_tool_calls(
    tool_calls: list[dict],
    tools_config: str,
    toolset: str | None = None,
    outdir: str | None = None,
) -> list[dict]:
    config_path, config = _load_tools_config(tools_config)
    selected, allowed_tools = _resolve_toolset(config, toolset)
    base_retry = _load_retry_settings(config)
    timeout_settings = _load_timeout_settings(config)
    cache_settings = _load_cache_settings(config_path, config)
    cache_path = cache_settings["cache_path"]
    cache = _load_cache(cache_path) if cache_path else {}
    cache_dirty = False
    if not isinstance(tool_calls, list):
        raise ValueError("tool_calls must be a list")
    data_root_setting = config.get("settings", {}).get("data_root", "../data")
    resolved_data_root = resolve_from_file(data_root_setting, config_path)
    tool_messages = []
    log_records = []
    output_dir = Path(outdir) if outdir else None
    for index, raw_call in enumerate(tool_calls):
        start = perf_counter()
        cache_key = None
        cache_hit = False
        cache_age_s = None
        cache_expired = False
        executed = False
        timeout_seconds = None
        timed_out = False
        retry_count = 0
        recovered_after_retry = False
        last_error_type = None
        try:
            call = normalize_tool_call(raw_call, index)
        except Exception as exc:
            call = {"id": f"call_{index + 1:03d}", "name": "unknown", "args": {}}
            result = _error_result(call["name"], call["args"], exc)
        else:
            name = call["name"]
            args = call["args"]
            if name not in allowed_tools or name not in config["tools"]:
                result = _error_result(name, args, ValueError(f"tool is not available in {selected}: {name}"))
            else:
                definition = config["tools"][name]
                try:
                    _validate_args(args, definition)
                    timeout_seconds = _get_tool_timeout_seconds(definition, timeout_settings)
                    retry = _get_tool_retry_settings(definition, base_retry)
                    if cache_path and _is_cacheable_tool(definition):
                        cache_key = _make_cache_key(name, args)
                        cached_entry = cache.get(cache_key)
                        cached_result, cached_ts = _unpack_cache_entry(cached_entry)
                        ttl_s = _get_tool_cache_ttl_seconds(definition, cache_settings)
                        now_ts = time()
                        if ttl_s is not None:
                            if cached_ts is None:
                                cache_expired = True
                            else:
                                cache_age_s = round(now_ts - cached_ts, 3)
                                cache_expired = cache_age_s > ttl_s
                        if isinstance(cached_result, dict) and cached_result.get("skill_name") == name and not cache_expired:
                            result = _clone_result(cached_result)
                            result["input"] = args
                            result["latency_ms"] = round((perf_counter() - start) * 1000, 3)
                            cache_hit = True
                        else:
                            cache_hit = False
                    if cache_hit:
                        pass
                    else:
                        executed = True
                        module = importlib.import_module(definition["module"])
                        function = getattr(module, definition["function"])
                        kwargs = dict(args)
                        signature = inspect.signature(function)
                        if "data_root" in signature.parameters:
                            kwargs["data_root"] = str(resolved_data_root)
                        if "output_dir" in signature.parameters:
                            kwargs["output_dir"] = str(output_dir) if output_dir else None
                        run_info = _call_with_retry(function, kwargs, retry, timeout_seconds)
                        output = run_info["output"]
                        retry_count = run_info["retry_count"]
                        recovered_after_retry = run_info["recovered_after_retry"]
                        if isinstance(run_info["last_error"], dict):
                            last_error_type = run_info["last_error"].get("type")
                        latency_ms = round((perf_counter() - start) * 1000, 3)
                        result = make_skill_result(name, "success", args, output, None, latency_ms)
                        if cache_path and cache_key and (
                            cache_settings["cache_errors"] or result["status"] == "success"
                        ):
                            cache[cache_key] = _pack_cache_entry(result)
                            cache_dirty = True
                except (ImportError, AttributeError) as exc:
                    raise RuntimeError(f"cannot load configured tool {name}: {exc}") from exc
                except Exception as exc:
                    retry_count = getattr(exc, "_retry_count", retry_count)
                    recovered_after_retry = getattr(exc, "_recovered_after_retry", False)
                    last_error = getattr(exc, "_last_error", None)
                    if isinstance(last_error, dict):
                        last_error_type = last_error.get("type")
                    timed_out = isinstance(exc, TimeoutError) or last_error_type == "TimeoutError"
                    latency_ms = round((perf_counter() - start) * 1000, 3)
                    result = _error_result(name, args, exc, latency_ms)
        content = json.dumps(result, ensure_ascii=False, separators=(",", ":"))
        message = make_tool_message(call["id"], call["name"], content, result["status"])
        tool_messages.append(message)
        log_records.append(
            {
                "timestamp": now_iso(),
                "toolset": selected,
                "tool_call_id": call["id"],
                "name": call["name"],
                "status": result["status"],
                "args": call["args"],
                "skill_result": result,
                "latency_ms": result["latency_ms"],
                "cache_key": cache_key,
                "cache_hit": cache_hit,
                "cache_age_s": cache_age_s,
                "cache_expired": cache_expired,
                "executed": executed,
                "result_source": "cache" if cache_hit else ("live" if executed else "skipped"),
                "timeout_seconds": timeout_seconds,
                "timed_out": timed_out,
                "retry_count": retry_count,
                "recovered_after_retry": recovered_after_retry,
                "last_error_type": last_error_type,
            }
        )
    if outdir:
        write_json(tool_messages, output_dir / "tool_messages.json")
        for record in log_records:
            append_jsonl(record, output_dir / "tool_call_log.jsonl")
    if cache_path and cache_dirty:
        write_json(cache, cache_path)
    return tool_messages


def _generate_batch_tool_calls() -> list[dict]:
    calls: list[dict] = []
    index = 1

    def add(name: str, args: dict) -> None:
        nonlocal index
        calls.append({"id": f"call_{index:03d}", "name": name, "args": args})
        index += 1

    add("calculator", {"expression": "12345*6789"})
    add("calculator", {"expression": "12345*6789"})
    add("calculator", {"expression": "(1+2+3+4)*1000"})
    add("calculator", {"expression": "1/0"})
    add("file_reader", {"path": "docs/agent_intro.txt", "max_chars": 2000})
    add("file_reader", {"path": "docs/agent_intro.txt", "max_chars": 2000})
    add("file_reader", {"path": "docs/agent_intro.txt", "max_chars": True})
    add("local_file_search", {"query": "Agent", "root_dir": "docs", "top_k": 5})
    add("local_file_search", {"query": "Agent", "root_dir": "docs", "top_k": 5})
    add("table_analyzer", {"path": "tables/results.csv", "max_rows_preview": 5, "describe": True})
    add("table_analyzer", {"path": "tables/results.csv", "max_rows_preview": 5, "describe": True})
    add(
        "format_converter",
        {"text": "A\\nB\\nC", "target_format": "markdown", "output_filename": "converted_demo.md"},
    )
    add("unknown_tool", {"foo": "bar"})
    add("calculator", {})

    return calls


def _summarize_records(records: list[dict]) -> dict:
    count = len(records)
    success = sum(1 for r in records if r.get("status") == "success")
    error = sum(1 for r in records if r.get("status") == "error")
    latencies = [r.get("latency_ms") for r in records if isinstance(r.get("latency_ms"), (int, float))]
    retry_counts = [r.get("retry_count", 0) for r in records if isinstance(r.get("retry_count", 0), int)]
    retried = sum(1 for r in records if isinstance(r.get("retry_count"), int) and r.get("retry_count", 0) > 0)
    recovered = sum(1 for r in records if r.get("recovered_after_retry") is True)
    cache_hits = sum(1 for r in records if r.get("cache_hit") is True)
    timeouts = sum(1 for r in records if r.get("timed_out") is True)
    error_type_counts: dict[str, int] = {}
    for r in records:
        if r.get("status") != "error":
            continue
        error_type = r.get("last_error_type")
        if not isinstance(error_type, str) or not error_type:
            skill_result = r.get("skill_result")
            if isinstance(skill_result, dict):
                err = skill_result.get("error")
                if isinstance(err, dict) and isinstance(err.get("type"), str):
                    error_type = err["type"]
        if not isinstance(error_type, str) or not error_type:
            error_type = "UnknownError"
        error_type_counts[error_type] = error_type_counts.get(error_type, 0) + 1
    error_type_distribution = dict(sorted(error_type_counts.items(), key=lambda kv: (-kv[1], kv[0])))
    avg_latency = round(sum(latencies) / len(latencies), 3) if latencies else None
    failure_rate = round(error / count, 3) if count else 0.0
    retry_rate = round(retried / count, 3) if count else 0.0
    retry_recovery_rate = round(recovered / retried, 3) if retried else 0.0
    cache_hit_rate = round(cache_hits / count, 3) if count else 0.0
    timeout_rate = round(timeouts / count, 3) if count else 0.0
    avg_retry_count = round(sum(retry_counts) / len(retry_counts), 3) if retry_counts else 0.0
    return {
        "count": count,
        "success": success,
        "error": error,
        "failure_rate": failure_rate,
        "avg_latency_ms": avg_latency,
        "retried": retried,
        "retry_rate": retry_rate,
        "avg_retry_count": avg_retry_count,
        "recovered_after_retry": recovered,
        "retry_recovery_rate": retry_recovery_rate,
        "cache_hits": cache_hits,
        "cache_hit_rate": cache_hit_rate,
        "timeouts": timeouts,
        "timeout_rate": timeout_rate,
        "error_type_distribution": error_type_distribution,
    }


def build_batch_stats(
    tools_config: str,
    toolset: str | None,
    outdir: str,
) -> dict:
    output_dir = Path(outdir)
    tool_calls = _generate_batch_tool_calls()
    write_json({"tool_calls": tool_calls}, output_dir / "tool_calls_batch.json")
    execute_tool_calls(tool_calls, tools_config, toolset, outdir)

    log_path = output_dir / "tool_call_log.jsonl"
    records = []
    if log_path.exists():
        for line in log_path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                records.append(json.loads(line))

    by_tool: dict[str, list[dict]] = {}
    by_source: dict[str, list[dict]] = {}
    for record in records:
        name = record.get("name", "unknown")
        by_tool.setdefault(name, []).append(record)
        source = record.get("result_source", "unknown")
        by_source.setdefault(source, []).append(record)

    stats = {
        "summary": _summarize_records(records),
        "by_tool": {name: _summarize_records(items) for name, items in sorted(by_tool.items())},
        "by_source": {name: _summarize_records(items) for name, items in sorted(by_source.items())},
        "files": {
            "tool_calls_batch": "tool_calls_batch.json",
            "tool_messages": "tool_messages.json",
            "tool_call_log": "tool_call_log.jsonl",
        },
    }
    write_json(stats, output_dir / "stats.json")
    return stats


def run_retry_demo(
    tools_config: str,
    outdir: str,
    fail_times: int = 1,
) -> dict:
    _, config = _load_tools_config(tools_config)
    retry = _load_retry_settings(config)
    timeout_settings = _load_timeout_settings(config)
    output_dir = Path(outdir)
    args = {"demo_key": "retry-demo-case", "fail_times": fail_times}
    state = {"attempts": 0}

    def _demo_tool(*, demo_key: str, fail_times: int) -> dict:
        state["attempts"] += 1
        if state["attempts"] <= fail_times:
            raise TimeoutError(f"simulated transient timeout on attempt {state['attempts']}")
        return {
            "demo_key": demo_key,
            "attempts": state["attempts"],
            "recovered": state["attempts"] > 1,
            "message": "retry demo succeeded after transient failure",
        }

    start = perf_counter()
    retry_count = 0
    recovered_after_retry = False
    last_error_type = None
    timeout_seconds = timeout_settings["default_timeout_s"]
    timed_out = False
    try:
        run_info = _call_with_retry(_demo_tool, args, retry, timeout_seconds)
        output = run_info["output"]
        retry_count = run_info["retry_count"]
        recovered_after_retry = run_info["recovered_after_retry"]
        if isinstance(run_info["last_error"], dict):
            last_error_type = run_info["last_error"].get("type")
        latency_ms = round((perf_counter() - start) * 1000, 3)
        result = make_skill_result("retry_demo_internal", "success", args, output, None, latency_ms)
    except Exception as exc:
        retry_count = getattr(exc, "_retry_count", retry_count)
        recovered_after_retry = getattr(exc, "_recovered_after_retry", False)
        last_error = getattr(exc, "_last_error", None)
        if isinstance(last_error, dict):
            last_error_type = last_error.get("type")
        timed_out = isinstance(exc, TimeoutError) or last_error_type == "TimeoutError"
        latency_ms = round((perf_counter() - start) * 1000, 3)
        result = _error_result("retry_demo_internal", args, exc, latency_ms)

    content = json.dumps(result, ensure_ascii=False, separators=(",", ":"))
    tool_message = make_tool_message("call_retry_demo", "retry_demo_internal", content, result["status"])
    log_record = {
        "timestamp": now_iso(),
        "toolset": "retry_demo_internal",
        "tool_call_id": "call_retry_demo",
        "name": "retry_demo_internal",
        "status": result["status"],
        "args": args,
        "skill_result": result,
        "latency_ms": result["latency_ms"],
        "cache_key": None,
        "cache_hit": False,
        "executed": True,
        "result_source": "live",
        "timeout_seconds": timeout_seconds,
        "timed_out": timed_out,
        "retry_count": retry_count,
        "recovered_after_retry": recovered_after_retry,
        "last_error_type": last_error_type,
    }
    summary = {
        "retry_config": retry,
        "demo_args": args,
        "status": result["status"],
        "attempts_used": retry_count + 1,
        "retry_count": retry_count,
        "recovered_after_retry": recovered_after_retry,
        "last_error_type": last_error_type,
        "timeout_seconds": timeout_seconds,
        "timed_out": timed_out,
        "latency_ms": result["latency_ms"],
        "files": {
            "tool_messages": "tool_messages.json",
            "tool_call_log": "tool_call_log.jsonl",
            "retry_demo_summary": "retry_demo_summary.json",
        },
    }

    write_json([tool_message], output_dir / "tool_messages.json")
    append_jsonl(log_record, output_dir / "tool_call_log.jsonl")
    write_json(summary, output_dir / "retry_demo_summary.json")
    return summary


def run_timeout_demo(
    tools_config: str,
    outdir: str,
    sleep_seconds: float = 2.0,
    timeout_seconds: float = 1.0,
) -> dict:
    if sleep_seconds <= 0:
        raise ValueError("sleep_seconds must be positive")
    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be positive")

    _, config = _load_tools_config(tools_config)
    retry = _load_retry_settings(config)
    output_dir = Path(outdir)
    args = {
        "demo_key": "timeout-demo-case",
        "sleep_seconds": sleep_seconds,
        "timeout_seconds": timeout_seconds,
    }

    def _demo_tool(*, demo_key: str, sleep_seconds: float, timeout_seconds: float) -> dict:
        sleep(sleep_seconds)
        return {
            "demo_key": demo_key,
            "sleep_seconds": sleep_seconds,
            "timeout_seconds": timeout_seconds,
            "message": "timeout demo completed without timeout",
        }

    start = perf_counter()
    retry_count = 0
    recovered_after_retry = False
    last_error_type = None
    timed_out = False
    try:
        run_info = _call_with_retry(_demo_tool, args, retry, timeout_seconds)
        output = run_info["output"]
        retry_count = run_info["retry_count"]
        recovered_after_retry = run_info["recovered_after_retry"]
        if isinstance(run_info["last_error"], dict):
            last_error_type = run_info["last_error"].get("type")
        latency_ms = round((perf_counter() - start) * 1000, 3)
        result = make_skill_result("timeout_demo_internal", "success", args, output, None, latency_ms)
    except Exception as exc:
        retry_count = getattr(exc, "_retry_count", retry_count)
        recovered_after_retry = getattr(exc, "_recovered_after_retry", False)
        last_error = getattr(exc, "_last_error", None)
        if isinstance(last_error, dict):
            last_error_type = last_error.get("type")
        timed_out = isinstance(exc, TimeoutError) or last_error_type == "TimeoutError"
        latency_ms = round((perf_counter() - start) * 1000, 3)
        result = _error_result("timeout_demo_internal", args, exc, latency_ms)

    content = json.dumps(result, ensure_ascii=False, separators=(",", ":"))
    tool_message = make_tool_message("call_timeout_demo", "timeout_demo_internal", content, result["status"])
    log_record = {
        "timestamp": now_iso(),
        "toolset": "timeout_demo_internal",
        "tool_call_id": "call_timeout_demo",
        "name": "timeout_demo_internal",
        "status": result["status"],
        "args": args,
        "skill_result": result,
        "latency_ms": result["latency_ms"],
        "cache_key": None,
        "cache_hit": False,
        "executed": True,
        "result_source": "live",
        "timeout_seconds": timeout_seconds,
        "timed_out": timed_out,
        "retry_count": retry_count,
        "recovered_after_retry": recovered_after_retry,
        "last_error_type": last_error_type,
    }
    summary = {
        "retry_config": retry,
        "demo_args": args,
        "status": result["status"],
        "sleep_seconds": sleep_seconds,
        "timeout_seconds": timeout_seconds,
        "timed_out": timed_out,
        "attempts_used": retry_count + 1,
        "retry_count": retry_count,
        "recovered_after_retry": recovered_after_retry,
        "last_error_type": last_error_type,
        "latency_ms": result["latency_ms"],
        "files": {
            "tool_messages": "tool_messages.json",
            "tool_call_log": "tool_call_log.jsonl",
            "timeout_demo_summary": "timeout_demo_summary.json",
        },
    }

    write_json([tool_message], output_dir / "tool_messages.json")
    append_jsonl(log_record, output_dir / "tool_call_log.jsonl")
    write_json(summary, output_dir / "timeout_demo_summary.json")
    return summary


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Generate tool schema or execute tool calls.")
    parser.add_argument("--tools_config", required=True)
    parser.add_argument("--toolset", default=None)
    parser.add_argument("--tool_calls")
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--export_schema", action="store_true")
    action.add_argument("--execute", action="store_true")
    action.add_argument("--batch_stats", action="store_true")
    action.add_argument("--retry_demo", action="store_true")
    action.add_argument("--timeout_demo", action="store_true")
    parser.add_argument("--outdir", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        config_path = resolve_cli_path(args.tools_config)
        outdir = resolve_cli_path(args.outdir)
        if args.export_schema:
            if not args.toolset:
                _, config = _load_tools_config(config_path)
                args.toolset = config.get("default_toolset")
            get_tools_schema(str(config_path), args.toolset, str(outdir))
            print(outdir / "tools_schema.json")
        elif args.batch_stats:
            build_batch_stats(str(config_path), args.toolset, str(outdir))
            print(outdir / "stats.json")
        elif args.retry_demo:
            run_retry_demo(str(config_path), str(outdir))
            print(outdir / "retry_demo_summary.json")
        elif args.timeout_demo:
            run_timeout_demo(str(config_path), str(outdir))
            print(outdir / "timeout_demo_summary.json")
        else:
            if not args.tool_calls:
                raise ValueError("--tool_calls is required with --execute")
            payload = read_json(resolve_cli_path(args.tool_calls))
            tool_calls = payload.get("tool_calls") if isinstance(payload, dict) else payload
            execute_tool_calls(tool_calls, str(config_path), args.toolset, str(outdir))
            print(outdir / "tool_messages.json")
        return 0
    except Exception as exc:
        print(f"fatal: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
