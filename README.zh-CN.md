# model-tui

[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)
[![Zero Dependencies](https://img.shields.io/badge/dependencies-0%20(stdlib%20only)-brightgreen)](pyproject.toml)
[![GitHub Stars](https://img.shields.io/github/stars/sdailabs/modeltui?style=social)](https://github.com/sdailabs/modeltui/stargazers)

[English](README.md) | 简体中文

**别再让快过期的 AI 编程订阅额度白白清零。**

`model-tui` 是一个零代理、零第三方依赖的 Python CLI 启动器：启动前以 **0 LLM Token** 检查你多个 AI 编程订阅 CLI 的 5 小时与周配额重置窗口，然后直接启动**最快重置且可用**的那个原生交互式 CLI。

## 为什么做薄启动器，而不是 API 代理？

如果你同时付费订阅了多个 AI 编程工具（如 Claude Max、Codex 等），用本地 HTTP 代理、OAuth 中转或无头 `-p` 包装脚本去强切订阅，很容易触发厂商服务条款（ToS）风控，带来**封号风险**。

`model-tui` 从不夹在你和厂商之间：

- **无代理、无中间人，全程留在官方原生 CLI**：只在启动前检查一次配额元数据，随后直接用 `exec` 把终端交给原生交互式 CLI。启动完成瞬间 `model-tui` 即退出——你的网络流量、鉴权和对话内容与手动运行 `claude` 100% 一致。
- **仅限交互式 TUI（防封号护栏）**：显式拒绝非交互/无头参数（`-p`、`--print`、`--output-format` 等），防止误将面向人工交互的订阅用于自动化批跑。
- **0 LLM Token 探测**：通过原生配额接口与本地冷却状态读取重置时间，绝不发测试 prompt 消耗额度。
- **优先消耗最早重置的窗口**：如果订阅 A 还有 4 小时重置、订阅 B 还有 5 天重置，优先启动 A，避免即将过期的额度浪费。

## 目前支持的 Provider

| Provider | 支持级别 | 内置模型 | 零 Token 配额探测 |
| :--- | :--- | :--- | :--- |
| **Claude Code (`claude`)** | 内置 | `opus`、`sonnet`、`haiku`（及 `claude-*` 别名） | 支持 — OAuth 用量接口（5 小时与 7 天周重置窗口）+ 本地冷却检测兜底 |
| **任意其他 CLI / 第二账号**（Codex CLI、Gemini CLI、Cursor CLI、Copilot CLI 等） | 可插拔（`probe_cmd` / `<provider>:<model>`） | 通过 `~/.config/model-tui.json` 配置或临时使用 `--model <provider>:<model>` | 通过自定义 `probe_cmd` 输出下方 4 字段 JSON 契约即可接入 |

希望内置支持其他订阅 CLI（Codex、Gemini CLI、Cursor、Copilot 等）的零 Token 探针？欢迎[提 Issue](https://github.com/sdailabs/modeltui/issues/new) 附上该 CLI 暴露配额或重置时间的方式，或直接提 PR。

## 安装

需要 Python 3.10+（仅使用标准库，零第三方依赖）。

```bash
pip install .
# 或直接运行：
./model_tui.py --help
```

## 快速开始

```bash
# 查看已注册的模型与通道
model-tui --list-models

# 仅模拟路由决策（打印 JSON，不启动 CLI）
model-tui --model opus --check

# 指定推理强度启动交互式 TUI
model-tui --model opus --effort high

# 强制指定某个 provider，或使用临时 <provider>:<model> 语法
model-tui --model sonnet --provider claude
model-tui --model codex:o3 --check

# 跳过或仅读取路由缓存（默认 TTL：300 秒）
model-tui --model opus --check --no-cache
model-tui --model opus --check --cache-only
```

## 可插拔 `probe_cmd` 探针契约

在 `~/.config/model-tui.json` 中配置多通道模型（或通过环境变量 `MODEL_TUI_CONFIG=/path/to/config.json` 指定路径）。

当某个模型配置了 2 个及以上通道时，`model-tui` 会并发运行各通道探针，优先选择**周重置时间最早**的可用通道（若相同则比较 5 小时重置时间；当存在更健康通道时，自动避开剩余额度低于 10% 的通道）。

每个自定义 `probe_cmd` 只需向标准输出打印单个 JSON 对象：

```json
{
  "remaining": 0.65,
  "five_hour_reset": "2026-11-27T14:00:00Z",
  "weekly_reset": "2026-12-01T00:00:00Z",
  "available": true
}
```

| 字段 | 类型 | 说明 |
| :--- | :--- | :--- |
| `remaining` | `float \| null` | 剩余配额比例，范围 `0.0..1.0` |
| `five_hour_reset` | `ISO8601 \| null` | 下一次 5 小时滚动窗口重置时间 |
| `weekly_reset` | `ISO8601 \| null` | 下一次周窗口重置时间 |
| `available` | `bool` | 该通道当前是否可接受新会话 |

### 示例：双通道配置（`~/.config/model-tui.json`）

内置的 `claude` 通道会自动探测 Claude Code 的 OAuth 用量接口和本地 5 小时限流冷却，无需额外写 `probe_cmd`。你可以通过 `probe_cmd` 搭配任意第二个订阅 CLI（或第二个账号配置）：

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

## `--check` 输出示例

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

## 环境变量

- `MODEL_TUI_CONFIG`：JSON 模型与通道配置文件路径（默认：`~/.config/model-tui.json`）
- `MODEL_TUI_ROUTE_TTL_SEC`：路由缓存 TTL 秒数（默认 `300`，设为 `0` 禁用缓存）
- `MODEL_TUI_ROUTE_CACHE`：覆盖路由缓存文件路径
- `CLAUDE_BIN`：覆盖 `claude` 可执行文件路径
- `CLAUDE_IGNORE_COOLDOWN=1`：当选中通道处于冷却期时跳过冷启动拦截

## 开源协议

MIT
