# model-tui

[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)
[![Zero Dependencies](https://img.shields.io/badge/dependencies-0%20(stdlib%20only)-brightgreen)](pyproject.toml)
[![GitHub Stars](https://img.shields.io/github/stars/sdailabs/modeltui?style=social)](https://github.com/sdailabs/modeltui/stargazers)

**Stop wasting expiring AI coding subscription quotas.**

`model-tui` is a zero-proxy, zero-dependency Python CLI launcher that checks 5-hour and weekly quota reset windows across your AI coding subscription CLIs (at **0 LLM tokens**) and launches whichever available channel resets earliest.

## Why not an API proxy (LiteLLM / Magpie / OpenRouter)?

API proxies sit in the HTTP path, require API keys or local proxy daemons, and can conflict with subscription-based TUI tools. `model-tui` does **not** proxy requests or touch your prompts:

- **Zero proxy**: It inspects quota metadata once, then `exec`s your native interactive CLI (`claude`, `codex`, etc.) directly.
- **Zero LLM tokens**: Uses native quota endpoints/probes and local session cooldown detection — never sends a test prompt.
- **Earlier-reset routing**: If Subscription A resets in 4 hours and Subscription B resets in 5 days, burn Subscription A first so its quota does not expire unused.
- **Interactive TUI only**: Explicitly rejects non-interactive print flags (`-p`, `--print`, `--output-format`, etc.) so subscription CLIs stay interactive.

## Supported Providers

| Provider | Support Level | Built-in Models | Zero-Token Quota Probe |
| :--- | :--- | :--- | :--- |
| **Claude Code (`claude`)** | Built-in | `opus`, `sonnet`, `haiku` (plus `claude-*` aliases) | Yes — OAuth usage API (5-hour & 7-day weekly reset windows) + local cooldown fallback |
| **Any other CLI / 2nd profile** (Codex CLI, Gemini CLI, Cursor CLI, Copilot CLI, etc.) | Pluggable (`probe_cmd` / `<provider>:<model>`) | Configured in `~/.config/model-tui.json` or ad-hoc `--model <provider>:<model>` | Via custom `probe_cmd` returning the 4-field JSON contract below |

Want a built-in zero-token probe for another subscription CLI (Codex, Gemini CLI, Cursor, Copilot, etc.)? [Open an issue](https://github.com/sdailabs/modeltui/issues/new) with how your CLI exposes quota or reset timestamps, or submit a PR.

## Install

Requires Python 3.10+ (standard library only, zero third-party packages).

```bash
pip install .
# or run directly:
./model_tui.py --help
```

## Quickstart

```bash
# Inspect registered models and channels
model-tui --list-models

# Dry-run quota routing decision (prints JSON, does not launch)
model-tui --model opus --check

# Launch interactive TUI with effort level
model-tui --model opus --effort high

# Force a specific provider or use ad-hoc <provider>:<model> syntax
model-tui --model sonnet --provider claude
model-tui --model codex:o3 --check

# Bypass or inspect route cache (default TTL: 300s)
model-tui --model opus --check --no-cache
model-tui --model opus --check --cache-only
```

## Pluggable `probe_cmd` Contract

Configure multi-channel models in `~/.config/model-tui.json` (or set `MODEL_TUI_CONFIG=/path/to/config.json`).

When a model defines 2 or more channels, `model-tui` runs each channel's probe concurrently and picks the available channel whose quota window resets earliest (weekly reset first, then 5-hour reset tie-breaker, avoiding channels below 10% remaining when healthier channels exist).

Each custom `probe_cmd` must print a single JSON object to stdout:

```json
{
  "remaining": 0.65,
  "five_hour_reset": "2026-11-27T14:00:00Z",
  "weekly_reset": "2026-12-01T00:00:00Z",
  "available": true
}
```

| Field | Type | Description |
| :--- | :--- | :--- |
| `remaining` | `float \| null` | Remaining quota fraction in `0.0..1.0` |
| `five_hour_reset` | `ISO8601 \| null` | Next 5-hour rolling window reset timestamp |
| `weekly_reset` | `ISO8601 \| null` | Next weekly window reset timestamp |
| `available` | `bool` | Whether the channel can accept a new session |

### Example: 2-Channel Config (`~/.config/model-tui.json`)

The built-in `claude` channel automatically probes Claude Code's OAuth usage endpoint and local 5-hour limit cooldown without needing a custom `probe_cmd`. You can pair it with any second subscription CLI (or second profile) via `probe_cmd`:

```json
{
  "models": {
    "opus": {
      "aliases": ["claude-opus"],
      "channels": [
        {
          "backend": "claude",
          "cmd": ["{claude_bin}", "--model", "opus", "--effort", "{effort}"]
        },
        {
          "backend": "alt-cli",
          "cmd": ["alt-cli", "--model", "opus-{effort}"],
          "probe_cmd": ["alt-cli-quota-probe", "--json"]
        }
      ]
    }
  }
}
```

## Example `--check` JSON Output

```json
{
  "model": "opus",
  "effort": "medium",
  "backend": "alt-cli",
  "cmd": ["alt-cli", "--model", "opus-medium"],
  "available": true,
  "remaining": 0.72,
  "reset_time": null,
  "reason": "earlier_weekly_reset",
  "channels": {
    "claude": {
      "backend": "claude",
      "available": true,
      "remaining": 0.85,
      "five_hour_reset": "2026-11-27T15:00:00Z",
      "five_hour_reset_ts": 1795705200.0,
      "weekly_reset": "2026-12-03T00:00:00Z",
      "weekly_reset_ts": 1796169600.0,
      "reset_time": null,
      "probe_status": "ok"
    },
    "alt-cli": {
      "backend": "alt-cli",
      "available": true,
      "remaining": 0.72,
      "five_hour_reset": "2026-11-27T13:30:00Z",
      "five_hour_reset_ts": 1795699800.0,
      "weekly_reset": "2026-11-29T00:00:00Z",
      "weekly_reset_ts": 1795824000.0,
      "reset_time": null,
      "probe_status": "ok"
    }
  },
  "route_cached": false
}
```

## Environment Variables

- `MODEL_TUI_CONFIG`: Path to JSON model/channel config (default: `~/.config/model-tui.json`)
- `MODEL_TUI_ROUTE_TTL_SEC`: Route cache TTL in seconds (default: `300`, set `0` to disable)
- `MODEL_TUI_ROUTE_CACHE`: Override route cache file path
- `CLAUDE_BIN`: Override `claude` executable path
- `CLAUDE_IGNORE_COOLDOWN=1`: Bypass cold-launch block when the selected channel is in cooldown

## License

MIT
