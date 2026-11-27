#!/usr/bin/env python3
"""Zero-proxy, zero-token quota-aware CLI launcher (`model-tui`).

Compares 5-hour and weekly quota reset windows across AI coding subscription
CLIs and launches whichever available channel resets earliest — without an API
proxy and without spending any LLM tokens.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import datetime as dt
import http.client
import json
import math
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

__version__ = "0.1.0"

DEFAULT_ROUTE_CACHE_TTL_SEC = 5 * 60
MAX_ROUTE_CACHE_TTL_SEC = 12 * 3600
CC_COOLDOWN_MAX_HORIZON_SEC = 6 * 3600
CC_SCAN_LOOKBACK_SEC = 15 * 60
CC_FALLBACK_COOLDOWN_SEC = 15 * 60
CC_USAGE_URL_DEFAULT = "https://api.anthropic.com/api/oauth/usage"
CC_USAGE_TIMEOUT_SEC = 5.0
RESET_TIE_TOLERANCE_SEC = 60.0
EARLIER_RESET_MIN_REMAINING = 0.10

_CC_LIMIT_RE = re.compile(
    r"hit your 5-hour limit(?:\s*[^\w\n]{0,8}\s*resets\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm)(?:\s*\(([^)]+)\))?)?",
    re.IGNORECASE,
)
_EFFORT_SUFFIX_RE = re.compile(r"^(?P<base>.+)-(?P<effort>low|medium|high)$", re.IGNORECASE)

_FORBIDDEN_PRINT_FLAGS = {
    "-p",
    "--print",
    "--output-format",
    "--input-format",
    "--include-partial-messages",
    "--json-schema",
}

BUILTIN_MODELS: dict[str, dict[str, Any]] = {
    "opus": {
        "aliases": ["claude-opus"],
        "channels": [
            {
                "backend": "claude",
                "cmd": ["{claude_bin}", "--model", "opus", "--effort", "{effort}"],
            },
        ],
    },
    "sonnet": {
        "aliases": ["claude-sonnet"],
        "channels": [
            {
                "backend": "claude",
                "cmd": ["{claude_bin}", "--model", "sonnet", "--effort", "{effort}"],
            },
        ],
    },
    "haiku": {
        "aliases": ["claude-haiku"],
        "channels": [
            {
                "backend": "claude",
                "cmd": ["{claude_bin}", "--model", "haiku", "--effort", "{effort}"],
            },
        ],
    },
}


def _default_config_path() -> Path:
    override = os.environ.get("MODEL_TUI_CONFIG", "").strip()
    return Path(override) if override else (Path.home() / ".config" / "model-tui.json")


def load_model_registry() -> dict[str, dict[str, Any]]:
    registry: dict[str, dict[str, Any]] = {
        k: {"aliases": list(v.get("aliases", [])), "channels": [dict(c) for c in v.get("channels", [])]}
        for k, v in BUILTIN_MODELS.items()
    }
    cfg_path = _default_config_path()
    try:
        raw = json.loads(cfg_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return registry
    models = raw.get("models") if isinstance(raw, dict) else None
    if not isinstance(models, dict):
        return registry
    for name, spec in models.items():
        if not isinstance(name, str) or not isinstance(spec, dict):
            continue
        channels = spec.get("channels")
        if not isinstance(channels, list) or not channels:
            continue
        valid_channels: list[dict[str, Any]] = []
        for ch in channels:
            if (
                isinstance(ch, dict)
                and isinstance(ch.get("backend"), str)
                and ch["backend"].strip()
                and isinstance(ch.get("cmd"), list)
                and ch["cmd"]
            ):
                item = dict(ch)
                item["backend"] = ch["backend"].strip().lower()
                item["cmd"] = [str(part) for part in ch["cmd"]]
                if isinstance(ch.get("probe_cmd"), list) and ch["probe_cmd"]:
                    item["probe_cmd"] = [str(part) for part in ch["probe_cmd"]]
                else:
                    item.pop("probe_cmd", None)
                valid_channels.append(item)
        if valid_channels:
            aliases = [str(a) for a in spec.get("aliases", []) if isinstance(a, str)]
            registry[name.strip().lower()] = {"aliases": aliases, "channels": valid_channels}
    return registry


def _render_cmd(template: list[str], *, effort: str, model: str, claude_bin: str) -> list[str]:
    vals = {"{effort}": effort, "{model}": model, "{claude_bin}": claude_bin}
    rendered: list[str] = []
    for part in template:
        s = str(part)
        for token, repl in vals.items():
            if token in s:
                s = s.replace(token, repl)
        rendered.append(s)
    return rendered


def resolve_model_spec(
    model_raw: str,
    *,
    effort: str,
    explicit_effort: bool,
    provider: str | None,
    claude_bin: str,
) -> tuple[str, str, list[dict[str, Any]]]:
    """Resolve a model alias, full name, or `<provider>:<model>` into channels."""
    raw = (model_raw or "opus").strip()
    key = raw.lower()
    eff = effort
    registry = load_model_registry()

    def _lookup(candidate: str) -> tuple[str, dict[str, Any]] | None:
        if candidate in registry:
            return candidate, registry[candidate]
        for cname, spec in registry.items():
            if candidate in [a.lower() for a in spec.get("aliases", [])]:
                return cname, spec
        return None

    matched = _lookup(key)
    if matched is None:
        m = _EFFORT_SUFFIX_RE.match(key)
        if m:
            base_match = _lookup(m.group("base"))
            if base_match is not None:
                matched = base_match
                if not explicit_effort:
                    eff = m.group("effort").lower()

    if matched is not None:
        canonical, spec = matched
        channels: list[dict[str, Any]] = []
        for ch in spec["channels"]:
            rendered = {
                **ch,
                "backend": str(ch["backend"]).strip().lower(),
                "cmd": _render_cmd(ch["cmd"], effort=eff, model=canonical, claude_bin=claude_bin),
            }
            if isinstance(ch.get("probe_cmd"), list) and ch["probe_cmd"]:
                rendered["probe_cmd"] = _render_cmd(ch["probe_cmd"], effort=eff, model=canonical, claude_bin=claude_bin)
            channels.append(rendered)
    elif ":" in raw:
        prov_part, model_part = raw.split(":", 1)
        prov_key = prov_part.strip().lower()
        model_id = model_part.strip()
        canonical = f"{prov_key}:{model_id}"
        if prov_key == "claude":
            cmd = [claude_bin, "--model", model_id, "--effort", eff]
        else:
            cmd = [prov_key, "--model", model_id]
        channels = [{"backend": prov_key, "cmd": cmd}]
    else:
        canonical = key
        channels = [{"backend": "claude", "cmd": [claude_bin, "--model", raw, "--effort", eff]}]

    if provider:
        prov_norm = provider.strip().lower()
        filtered = [c for c in channels if c["backend"] == prov_norm]
        if filtered:
            channels = filtered
        elif prov_norm == "claude":
            channels = [{"backend": "claude", "cmd": [claude_bin, "--model", canonical, "--effort", eff]}]
        else:
            channels = [{"backend": prov_norm, "cmd": [prov_norm, "--model", raw]}]

    return canonical, eff, channels


def _num(val: Any) -> float | None:
    if isinstance(val, bool) or val is None:
        return None
    try:
        f = float(val)
        return f if math.isfinite(f) else None
    except (ValueError, TypeError):
        return None


def _parse_iso_ts(raw: Any) -> float | None:
    if not isinstance(raw, str) or not raw.strip():
        return None
    s = raw.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        parsed = dt.datetime.fromisoformat(s)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.timestamp()


def _route_cache_ttl_sec() -> float:
    raw = os.environ.get("MODEL_TUI_ROUTE_TTL_SEC", "").strip()
    if raw:
        try:
            val = float(raw)
        except ValueError:
            return float(DEFAULT_ROUTE_CACHE_TTL_SEC)
        if not math.isfinite(val) or val <= 0:
            return 0.0
        return min(val, float(MAX_ROUTE_CACHE_TTL_SEC))
    return float(DEFAULT_ROUTE_CACHE_TTL_SEC)


def _default_route_cache_path(model_key: str = "opus") -> Path:
    override = os.environ.get("MODEL_TUI_ROUTE_CACHE", "").strip()
    if override:
        return Path(override)
    safe = re.sub(r"[^a-z0-9._-]+", "-", model_key.lower()).strip("-") or "default"
    return Path.home() / ".cache" / f"model-tui-route-{safe}.json"


def _clear_file(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


def _read_route_cache(
    cache_path: Path,
    now_ts: float,
    *,
    allowed_backends: set[str],
    cc_cooldown: tuple[float, str, str] | None,
) -> dict[str, Any] | None:
    if _route_cache_ttl_sec() <= 0:
        return None
    try:
        data = json.loads(cache_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    exp = data.get("expires_ts")
    if not isinstance(exp, (int, float)) or isinstance(exp, bool) or now_ts >= float(exp):
        return None
    backend = data.get("backend")
    if not isinstance(backend, str) or backend not in allowed_backends:
        return None
    if backend == "claude" and cc_cooldown is not None:
        return None
    channels = data.get("channels")
    if isinstance(channels, dict):
        for det in channels.values():
            if not isinstance(det, dict):
                continue
            for ts_key in ("weekly_reset_ts", "five_hour_reset_ts"):
                ts_val = det.get(ts_key)
                if isinstance(ts_val, (int, float)) and not isinstance(ts_val, bool) and float(ts_val) <= now_ts:
                    return None
    return data


def _write_route_cache(cache_path: Path, now_ts: float, payload: dict[str, Any]) -> None:
    ttl = _route_cache_ttl_sec()
    if ttl <= 0:
        return
    tmp = cache_path.with_name(f"{cache_path.name}.{os.getpid()}.tmp")
    try:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        record = {
            "expires_ts": now_ts + ttl,
            "backend": payload.get("backend"),
            "reason": payload.get("reason"),
            "available": payload.get("available"),
            "remaining": payload.get("remaining"),
            "reset_time": payload.get("reset_time"),
            "channels": payload.get("channels"),
        }
        tmp.write_text(json.dumps(record), encoding="utf-8")
        os.replace(tmp, cache_path)
    except OSError:
        _clear_file(tmp)


def _default_cc_cache_path() -> Path:
    override = os.environ.get("MODEL_TUI_CC_COOLDOWN_CACHE", "").strip()
    return Path(override) if override else (Path.home() / ".cache" / "model-tui-cc-cooldown.json")


def _default_cc_projects_dir() -> Path:
    override = os.environ.get("CLAUDE_PROJECTS_DIR", "").strip()
    return Path(override) if override else (Path.home() / ".claude" / "projects")


def _default_cc_credentials_path() -> Path:
    override = os.environ.get("CLAUDE_CREDENTIALS_PATH", "").strip()
    return Path(override) if override else (Path.home() / ".claude" / ".credentials.json")


def parse_cc_limit_reset(text: str, ref_ts: float) -> tuple[float, str, str] | None:
    """Parse Claude Code 5-hour limit banner into (reset_ts, reset_iso, raw_label)."""
    matches = list(_CC_LIMIT_RE.finditer(text or ""))
    if not matches:
        return None
    m = matches[-1]
    hour_s, min_s, ampm, tz_name = m.group(1), m.group(2), m.group(3), m.group(4)
    if hour_s and ampm:
        h12 = int(hour_s)
        minute = int(min_s) if min_s else 0
        if 1 <= h12 <= 12 and 0 <= minute <= 59:
            h24 = (h12 % 12) + (12 if ampm.lower() == "pm" else 0)
            tz: dt.tzinfo
            if tz_name and tz_name.strip():
                try:
                    tz = ZoneInfo(tz_name.strip())
                except (ZoneInfoNotFoundError, ValueError):
                    tz = dt.datetime.now().astimezone().tzinfo or dt.timezone.utc
            else:
                tz = dt.datetime.now().astimezone().tzinfo or dt.timezone.utc
            ref_local = dt.datetime.fromtimestamp(ref_ts, tz=tz)
            cand = ref_local.replace(hour=h24, minute=minute, second=0, microsecond=0)
            if cand <= ref_local:
                cand += dt.timedelta(days=1)
            reset_ts = min(cand.timestamp(), ref_ts + CC_COOLDOWN_MAX_HORIZON_SEC)
            reset_iso = dt.datetime.fromtimestamp(reset_ts, tz=dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            raw_label = f"{hour_s}:{minute:02d}{ampm.lower()}" + (f" ({tz_name.strip()})" if tz_name else "")
            return reset_ts, reset_iso, raw_label
    reset_ts = ref_ts + CC_FALLBACK_COOLDOWN_SEC
    reset_iso = dt.datetime.fromtimestamp(reset_ts, tz=dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return reset_ts, reset_iso, "5h-limit"


def _read_cc_cooldown(cache_path: Path, now_ts: float) -> tuple[float, str, str] | None:
    try:
        data = json.loads(cache_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    ts = data.get("reset_ts")
    if not isinstance(ts, (int, float)) or isinstance(ts, bool):
        return None
    rts = float(ts)
    if now_ts < rts <= now_ts + CC_COOLDOWN_MAX_HORIZON_SEC:
        return rts, str(data.get("reset_time") or ""), str(data.get("raw_label") or "")
    _clear_file(cache_path)
    return None


def _write_cc_cooldown(cache_path: Path, reset_ts: float, reset_iso: str, raw_label: str) -> None:
    tmp = cache_path.with_name(f"{cache_path.name}.{os.getpid()}.tmp")
    try:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"reset_ts": reset_ts, "reset_time": reset_iso, "raw_label": raw_label}
        tmp.write_text(json.dumps(payload), encoding="utf-8")
        os.replace(tmp, cache_path)
    except OSError:
        _clear_file(tmp)


def _extract_cc_limit_from_record(obj: Any, now_ts: float) -> tuple[float, str, str] | None:
    """Extract (reset_ts, reset_iso, raw_label) from genuine Claude Code rate-limit records."""
    if not isinstance(obj, dict) or obj.get("type") != "assistant":
        return None
    if obj.get("isApiErrorMessage") is not True:
        return None
    msg = obj.get("message") if isinstance(obj.get("message"), dict) else {}
    is_rate_limit = (
        obj.get("error") == "rate_limit"
        or obj.get("apiError") == "usage_limit_reached"
        or obj.get("apiErrorType") == "rate_limit_error"
    )
    if not is_rate_limit and msg.get("model") != "<synthetic>":
        return None

    for container in (
        obj.get("quotaLimits"),
        (obj.get("apiErrorParams") or {}).get("rate_limit_info") if isinstance(obj.get("apiErrorParams"), dict) else None,
    ):
        if isinstance(container, dict):
            ra = container.get("resetsAt")
            if isinstance(ra, (int, float)) and not isinstance(ra, bool):
                rts = float(ra)
                if now_ts < rts <= now_ts + CC_COOLDOWN_MAX_HORIZON_SEC:
                    iso = dt.datetime.fromtimestamp(rts, tz=dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                    rl_type = str(container.get("rateLimitType") or "5h-limit")
                    return rts, iso, rl_type
                return None

    rec_ts = _parse_iso_ts(obj.get("timestamp"))
    if rec_ts is None:
        return None
    content = msg.get("content")
    text = ""
    if isinstance(content, list):
        text = " ".join(str(x.get("text") or "") for x in content if isinstance(x, dict))
    elif isinstance(content, str):
        text = content
    parsed = parse_cc_limit_reset(text, ref_ts=rec_ts)
    if parsed is not None and now_ts < parsed[0] <= now_ts + CC_COOLDOWN_MAX_HORIZON_SEC:
        return parsed
    return None


def detect_cc_cooldown(
    *,
    cache_path: Path | None = None,
    projects_dir: Path | None = None,
    now_ts: float | None = None,
) -> tuple[float, str, str] | None:
    """Return active Claude Code 5-hour limit (reset_ts, reset_iso, raw_label) or None."""
    now = time.time() if now_ts is None else now_ts
    cpath = _default_cc_cache_path() if cache_path is None else cache_path
    cached = _read_cc_cooldown(cpath, now)
    if cached is not None:
        return cached

    pdir = _default_cc_projects_dir() if projects_dir is None else projects_dir
    if not pdir.is_dir():
        return None
    cutoff = now - CC_SCAN_LOOKBACK_SEC
    recent: list[tuple[float, Path]] = []
    try:
        for p in pdir.rglob("*.jsonl"):
            try:
                mt = p.stat().st_mtime
            except OSError:
                continue
            if mt >= cutoff:
                recent.append((mt, p))
    except OSError:
        return None
    recent.sort(key=lambda x: x[0], reverse=True)
    for _mtime, fpath in recent[:5]:
        try:
            with fpath.open("rb") as fh:
                fh.seek(0, os.SEEK_END)
                size = fh.tell()
                start = max(0, size - 32768)
                fh.seek(start)
                tail = fh.read().decode("utf-8", errors="ignore")
        except OSError:
            continue
        lines = tail.splitlines()
        if start > 0 and lines:
            lines = lines[1:]
        last_asst: dict[str, Any] | None = None
        for raw_line in reversed(lines):
            raw_line = raw_line.strip()
            if not raw_line or not raw_line.startswith("{"):
                continue
            try:
                obj = json.loads(raw_line)
            except ValueError:
                continue
            if isinstance(obj, dict) and obj.get("type") == "assistant":
                last_asst = obj
                break
        if last_asst is None:
            continue
        parsed = _extract_cc_limit_from_record(last_asst, now)
        if parsed is not None:
            _write_cc_cooldown(cpath, parsed[0], parsed[1], parsed[2])
            return parsed
    return None


def parse_cc_usage(payload: Any, min_remaining: float = 0.01) -> dict[str, Any] | None:
    """Parse Claude Code OAuth `/api/oauth/usage` payload into remaining & reset timestamps."""
    if not isinstance(payload, dict):
        return None
    five = payload.get("five_hour") if isinstance(payload.get("five_hour"), dict) else {}
    seven = payload.get("seven_day") if isinstance(payload.get("seven_day"), dict) else {}

    def _bucket_remaining(bucket: dict[str, Any]) -> float | None:
        if not bucket:
            return None
        if bucket.get("locked_reason"):
            return 0.0
        used_d = _num(bucket.get("used_dollars"))
        limit_d = _num(bucket.get("limit_dollars"))
        rem_d = _num(bucket.get("remaining_dollars"))
        if used_d is not None and limit_d is not None and limit_d > 0:
            return max(0.0, min(1.0, (limit_d - used_d) / limit_d))
        if rem_d is not None and limit_d is not None and limit_d > 0:
            return max(0.0, min(1.0, rem_d / limit_d))
        util = _num(bucket.get("utilization"))
        if util is not None:
            return max(0.0, min(1.0, (100.0 - util) / 100.0))
        return None

    rems: list[float] = []
    exhausted_resets: list[tuple[float, str]] = []
    wk_raw = str(seven.get("resets_at") or "").strip()
    wk_ts = _parse_iso_ts(wk_raw)
    if wk_ts is not None and wk_ts <= 0.0:
        wk_ts, wk_raw = None, ""
    fh_raw = str(five.get("resets_at") or "").strip()
    fh_ts = _parse_iso_ts(fh_raw)
    if fh_ts is not None and fh_ts <= 0.0:
        fh_ts, fh_raw = None, ""

    for bucket, rts, raw in ((seven, wk_ts, wk_raw), (five, fh_ts, fh_raw)):
        rem = _bucket_remaining(bucket)
        if rem is not None:
            rems.append(rem)
            if rem <= min_remaining and rts is not None:
                exhausted_resets.append((rts, raw))

    if not rems and wk_ts is None and fh_ts is None:
        return None
    ex_ts, ex_iso = max(exhausted_resets, key=lambda x: x[0]) if exhausted_resets else (None, None)
    return {
        "min_remaining": min(rems) if rems else None,
        "exhausted_reset_ts": ex_ts,
        "exhausted_reset_iso": ex_iso,
        "weekly_reset_ts": wk_ts,
        "weekly_reset_iso": wk_raw or None,
        "five_hour_reset_ts": fh_ts,
        "five_hour_reset_iso": fh_raw or None,
    }


def probe_cc_usage(
    *,
    min_remaining: float = 0.01,
    timeout_sec: float = CC_USAGE_TIMEOUT_SEC,
    usage_url: str = CC_USAGE_URL_DEFAULT,
) -> dict[str, Any] | None:
    """Zero-token probe of Claude Code OAuth usage (supports CLAUDE_USAGE_JSON override)."""
    raw_override = os.environ.get("CLAUDE_USAGE_JSON", "").strip()
    if raw_override:
        try:
            text = raw_override if raw_override.startswith("{") else Path(raw_override).read_text(encoding="utf-8")
            return parse_cc_usage(json.loads(text), min_remaining=min_remaining)
        except (OSError, ValueError):
            return None

    creds_path = _default_cc_credentials_path()
    if not creds_path.is_file():
        return None
    try:
        creds = json.loads(creds_path.read_text(encoding="utf-8"))
        oauth = creds.get("claudeAiOauth") if isinstance(creds, dict) else None
        tok = str(oauth.get("accessToken") or "").strip() if isinstance(oauth, dict) else ""
    except (OSError, ValueError):
        return None
    if not tok:
        return None

    req = urllib.request.Request(
        usage_url,
        headers={
            "Authorization": f"Bearer {tok}",
            "anthropic-beta": "oauth-2025-04-20",
            "Accept": "application/json",
            "User-Agent": f"model-tui/{__version__}",
        },
        method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout_sec) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except (OSError, urllib.error.URLError, http.client.HTTPException, ValueError):
        return None
    return parse_cc_usage(payload, min_remaining=min_remaining)


def parse_probe_payload(
    backend: str,
    payload: Any,
    *,
    min_remaining: float = 0.01,
) -> dict[str, Any] | None:
    """Normalize a custom `probe_cmd` JSON payload into standard channel probe details."""
    if not isinstance(payload, dict):
        return None
    rem = _num(payload.get("remaining"))
    if rem is not None:
        rem = max(0.0, min(1.0, rem))

    fh_raw = payload.get("five_hour_reset")
    fh_iso = fh_raw.strip() if isinstance(fh_raw, str) and fh_raw.strip() else None
    fh_ts = _parse_iso_ts(fh_iso) if fh_iso else None
    if fh_ts is not None and fh_ts <= 0.0:
        fh_ts, fh_iso = None, None

    wk_raw = payload.get("weekly_reset")
    wk_iso = wk_raw.strip() if isinstance(wk_raw, str) and wk_raw.strip() else None
    wk_ts = _parse_iso_ts(wk_iso) if wk_iso else None
    if wk_ts is not None and wk_ts <= 0.0:
        wk_ts, wk_iso = None, None

    raw_avail = payload.get("available")
    if isinstance(raw_avail, bool):
        available = raw_avail
    elif rem is not None:
        available = rem > min_remaining
    else:
        available = True

    if rem is not None and rem <= min_remaining:
        available = False

    reset_time = None
    if not available:
        reset_time = fh_iso or wk_iso

    return {
        "backend": backend,
        "available": available,
        "remaining": rem,
        "five_hour_reset": fh_iso,
        "five_hour_reset_ts": fh_ts,
        "weekly_reset": wk_iso,
        "weekly_reset_ts": wk_ts,
        "reset_time": reset_time,
        "probe_status": "ok" if available else "exhausted",
    }


def probe_channel(
    channel: dict[str, Any],
    *,
    min_remaining: float = 0.01,
    timeout_sec: int = 10,
    cc_cooldown: tuple[float, str, str] | None = None,
) -> dict[str, Any]:
    """Run a 0-token quota probe for a single channel."""
    backend = channel["backend"]
    probe_cmd = channel.get("probe_cmd")

    if isinstance(probe_cmd, list) and probe_cmd:
        try:
            proc = subprocess.run(
                probe_cmd,
                capture_output=True,
                text=True,
                timeout=timeout_sec,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            return {
                "backend": backend,
                "available": False,
                "remaining": None,
                "five_hour_reset": None,
                "five_hour_reset_ts": None,
                "weekly_reset": None,
                "weekly_reset_ts": None,
                "reset_time": None,
                "probe_status": f"probe_error:{type(exc).__name__}",
            }
        if proc.returncode != 0:
            return {
                "backend": backend,
                "available": False,
                "remaining": None,
                "five_hour_reset": None,
                "five_hour_reset_ts": None,
                "weekly_reset": None,
                "weekly_reset_ts": None,
                "reset_time": None,
                "probe_status": f"probe_rc:{proc.returncode}",
            }
        out = (proc.stdout or "").strip()
        try:
            payload = json.loads(out) if out.startswith("{") else None
        except json.JSONDecodeError:
            payload = None
        parsed = parse_probe_payload(backend, payload, min_remaining=min_remaining)
        if parsed is None:
            return {
                "backend": backend,
                "available": False,
                "remaining": None,
                "five_hour_reset": None,
                "five_hour_reset_ts": None,
                "weekly_reset": None,
                "weekly_reset_ts": None,
                "reset_time": None,
                "probe_status": "probe_bad_json",
            }
        return parsed

    if backend == "claude" or channel.get("probe") == "claude":
        if cc_cooldown is not None:
            return {
                "backend": backend,
                "available": False,
                "remaining": 0.0,
                "five_hour_reset": cc_cooldown[1] or None,
                "five_hour_reset_ts": cc_cooldown[0],
                "weekly_reset": None,
                "weekly_reset_ts": None,
                "reset_time": cc_cooldown[1] or None,
                "probe_status": "cooldown",
            }
        cc_details = probe_cc_usage(
            min_remaining=min_remaining,
            timeout_sec=min(float(timeout_sec), CC_USAGE_TIMEOUT_SEC),
        )
        if cc_details is None:
            return {
                "backend": backend,
                "available": True,
                "remaining": None,
                "five_hour_reset": None,
                "five_hour_reset_ts": None,
                "weekly_reset": None,
                "weekly_reset_ts": None,
                "reset_time": None,
                "probe_status": "no_usage_data",
            }
        rem = cc_details["min_remaining"]
        exhausted = rem is not None and rem <= min_remaining
        return {
            "backend": backend,
            "available": not exhausted,
            "remaining": rem,
            "five_hour_reset": cc_details["five_hour_reset_iso"],
            "five_hour_reset_ts": cc_details["five_hour_reset_ts"],
            "weekly_reset": cc_details["weekly_reset_iso"],
            "weekly_reset_ts": cc_details["weekly_reset_ts"],
            "reset_time": cc_details["exhausted_reset_iso"] if exhausted else None,
            "probe_status": "exhausted" if exhausted else "ok",
        }

    return {
        "backend": backend,
        "available": True,
        "remaining": None,
        "five_hour_reset": None,
        "five_hour_reset_ts": None,
        "weekly_reset": None,
        "weekly_reset_ts": None,
        "reset_time": None,
        "probe_status": "no_probe",
    }


def pick_best_channel(
    probes: list[dict[str, Any]],
    *,
    now_ts: float,
    min_remaining: float = 0.01,
) -> tuple[dict[str, Any], str]:
    """Select the best channel: available first, then earliest resetting quota window."""
    available = [
        p for p in probes if p.get("available") and (p.get("remaining") is None or p["remaining"] > min_remaining)
    ]
    if not available:
        return probes[0], "all_channels_unavailable"
    if len(available) == 1:
        reason = "failover_only_available" if len(probes) > 1 else "single_channel"
        return available[0], reason

    healthy = [
        p for p in available if p.get("remaining") is None or p["remaining"] > EARLIER_RESET_MIN_REMAINING
    ]
    candidates = healthy if healthy else available
    if len(candidates) == 1:
        return candidates[0], "avoid_low_remaining"

    with_wk = [
        p
        for p in candidates
        if isinstance(p.get("weekly_reset_ts"), (int, float)) and float(p["weekly_reset_ts"]) > now_ts
    ]
    if len(with_wk) >= 2:
        with_wk.sort(key=lambda p: float(p["weekly_reset_ts"]))
        earliest_wk = float(with_wk[0]["weekly_reset_ts"])
        if earliest_wk < float(with_wk[1]["weekly_reset_ts"]) - RESET_TIE_TOLERANCE_SEC:
            return with_wk[0], "earlier_weekly_reset"
        # Weekly resets tied within tolerance -> tie-break on 5h reset window
        candidates = [p for p in with_wk if float(p["weekly_reset_ts"]) <= earliest_wk + RESET_TIE_TOLERANCE_SEC]

    with_5h = [
        p
        for p in candidates
        if isinstance(p.get("five_hour_reset_ts"), (int, float)) and float(p["five_hour_reset_ts"]) > now_ts
    ]
    if len(with_5h) >= 2:
        with_5h.sort(key=lambda p: float(p["five_hour_reset_ts"]))
        if float(with_5h[0]["five_hour_reset_ts"]) < float(with_5h[1]["five_hour_reset_ts"]) - RESET_TIE_TOLERANCE_SEC:
            return with_5h[0], "earlier_5h_reset"

    return candidates[0], "default_order"


def select_backend_details(
    *,
    effort: str = "medium",
    min_remaining: float = 0.01,
    model: str = "opus",
    explicit_effort: bool = True,
    provider: str | None = None,
    claude_bin: str = "claude",
    timeout_sec: int = 10,
    route_cache_path: Path | None = None,
    use_cache: bool = True,
    cache_only: bool = False,
    now_ts: float | None = None,
    cc_cooldown: tuple[float, str, str] | None = None,
) -> dict[str, Any]:
    """Resolve model channels, probe concurrently when >=2 channels, and pick the optimal route."""
    now = time.time() if now_ts is None else now_ts
    forced_provider = provider.strip().lower() if provider else None
    canonical, eff, channels = resolve_model_spec(
        model,
        effort=effort,
        explicit_effort=explicit_effort,
        provider=forced_provider,
        claude_bin=claude_bin,
    )
    by_backend = {c["backend"]: c for c in channels}

    if len(channels) == 1:
        ch = channels[0]
        backend_name = ch["backend"]
        probe_res = probe_channel(
            ch,
            min_remaining=min_remaining,
            timeout_sec=timeout_sec,
            cc_cooldown=cc_cooldown,
        )
        reason = "forced_provider" if forced_provider else "single_channel"
        if not probe_res["available"]:
            reason = f"{backend_name}_{probe_res['probe_status']}"
        return {
            "model": canonical,
            "effort": eff,
            "backend": backend_name,
            "cmd": ch["cmd"],
            "available": probe_res["available"],
            "remaining": probe_res["remaining"],
            "reset_time": probe_res["reset_time"],
            "reason": reason,
            "channels": {backend_name: probe_res},
            "route_cached": False,
        }

    rpath = _default_route_cache_path(canonical) if route_cache_path is None else route_cache_path
    if use_cache:
        rcached = _read_route_cache(
            rpath,
            now,
            allowed_backends=set(by_backend.keys()),
            cc_cooldown=cc_cooldown,
        )
        if rcached is not None:
            backend = str(rcached["backend"])
            return {
                "model": canonical,
                "effort": eff,
                "backend": backend,
                "cmd": by_backend[backend]["cmd"],
                "available": bool(rcached.get("available", True)),
                "remaining": rcached.get("remaining"),
                "reset_time": rcached.get("reset_time"),
                "reason": str(rcached.get("reason") or "cached"),
                "channels": rcached.get("channels") or {},
                "route_cached": True,
            }

    if cache_only:
        return {
            "model": canonical,
            "effort": eff,
            "backend": "unknown",
            "cmd": [],
            "available": False,
            "remaining": None,
            "reset_time": None,
            "reason": "cache_miss",
            "channels": {},
            "route_cached": False,
        }

    # Probe all configured channels concurrently (0 LLM tokens)
    with ThreadPoolExecutor(max_workers=min(len(channels), 8)) as pool:
        futs = [
            pool.submit(
                probe_channel,
                ch,
                min_remaining=min_remaining,
                timeout_sec=timeout_sec,
                cc_cooldown=cc_cooldown,
            )
            for ch in channels
        ]
        probes = [f.result() for f in futs]

    chosen_probe, reason = pick_best_channel(probes, now_ts=now, min_remaining=min_remaining)
    chosen_backend = chosen_probe["backend"]
    channels_map = {p["backend"]: p for p in probes}
    result = {
        "model": canonical,
        "effort": eff,
        "backend": chosen_backend,
        "cmd": by_backend[chosen_backend]["cmd"],
        "available": bool(chosen_probe["available"]),
        "remaining": chosen_probe["remaining"],
        "reset_time": chosen_probe["reset_time"],
        "reason": reason,
        "channels": channels_map,
        "route_cached": False,
    }
    if use_cache and result["available"]:
        _write_route_cache(rpath, now, result)
    elif use_cache:
        _clear_file(rpath)
    return result


def _has_forbidden_print_flag(extra: list[str]) -> str | None:
    for arg in extra:
        if arg == "--":
            break
        base = arg.split("=", 1)[0]
        if base in _FORBIDDEN_PRINT_FLAGS:
            return arg
        if arg.startswith("-") and not arg.startswith("--") and len(arg) > 2 and "p" in arg[1:]:
            return arg
    return None


def main(argv: list[str] | None = None) -> int:
    prog_name = Path(sys.argv[0]).name if sys.argv else "model-tui"
    ap = argparse.ArgumentParser(
        prog=prog_name,
        description="Zero-proxy, zero-token CLI launcher that routes across AI coding subscriptions by earliest quota reset.",
    )
    ap.add_argument("--model", default="opus", help="Model alias, full id, or <provider>:<model> (default: opus)")
    ap.add_argument("--provider", default=None, help="Force a specific provider/backend (e.g. claude, codex)")
    ap.add_argument("--effort", choices=("low", "medium", "high"), default=None, help="Reasoning effort level")
    ap.add_argument("--min-remaining", type=float, default=0.01, help="Minimum remaining fraction to treat a channel as available")
    ap.add_argument("--timeout", type=int, default=10, help="Per-probe timeout in seconds")
    ap.add_argument("--no-cache", action="store_true", help="Bypass cached route decisions")
    ap.add_argument("--cache-only", action="store_true", help="With --check, return cached decision only without live probes")
    ap.add_argument("--check", action="store_true", help="Print selected backend JSON and exit without launching")
    ap.add_argument("--check-cc", action="store_true", help="Check Claude Code 5-hour limit cooldown JSON (0=ok, 75=cooled down)")
    ap.add_argument("--list-models", action="store_true", help="Print registered models and channels as JSON")
    ap.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    args, extra = ap.parse_known_args(argv)

    if args.list_models:
        print(json.dumps(load_model_registry(), indent=2))
        return 0

    if args.check_cc:
        cc_cd = detect_cc_cooldown()
        if cc_cd is not None:
            print(json.dumps({"cc_available": False, "reset_ts": cc_cd[0], "reset_time": cc_cd[1], "raw_label": cc_cd[2]}))
            return 75
        print(json.dumps({"cc_available": True}))
        return 0

    bad_flag = _has_forbidden_print_flag(extra)
    if bad_flag is not None:
        print(
            f"Error: non-interactive print flag '{bad_flag}' is forbidden in {prog_name} (interactive TUI only).",
            file=sys.stderr,
        )
        return 2

    claude_bin = os.environ.get("CLAUDE_BIN", "claude")
    cc_cd = detect_cc_cooldown()
    sel = select_backend_details(
        effort=args.effort or "medium",
        explicit_effort=args.effort is not None,
        model=args.model,
        provider=args.provider,
        min_remaining=args.min_remaining,
        claude_bin=claude_bin,
        timeout_sec=args.timeout,
        use_cache=not args.no_cache,
        cache_only=args.cache_only and args.check,
        cc_cooldown=cc_cd,
    )

    if args.check:
        print(json.dumps(sel, indent=2))
        return 0

    backend = sel["backend"]
    cmd = sel["cmd"]
    if not cmd:
        print(f"[{prog_name}] No launch command resolved for model '{args.model}'.", file=sys.stderr)
        return 1

    if not sel["available"] and os.environ.get("CLAUDE_IGNORE_COOLDOWN", "").strip() != "1":
        reset_msg = f" until {sel['reset_time']}" if sel.get("reset_time") else ""
        print(
            f"[{prog_name}] Selected backend '{backend}' is unavailable ({sel['reason']}){reset_msg}; "
            "skipping cold launch. Set CLAUDE_IGNORE_COOLDOWN=1 to bypass.",
            file=sys.stderr,
        )
        return 75

    print(f"[{prog_name}] Launching {backend} ({sel['reason']})...", file=sys.stderr)
    full_cmd = [*cmd, *extra]
    os.execvp(full_cmd[0], full_cmd)
    return 0


def cli_main() -> None:
    raise SystemExit(main())


if __name__ == "__main__":
    cli_main()
