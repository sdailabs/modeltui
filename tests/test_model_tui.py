"""Public-seam unit tests for model-tui."""
from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import model_tui


def _probe_script_cmd(payload: dict) -> list[str]:
    code = f"import json; print(json.dumps({payload!r}))"
    return [sys.executable, "-c", code]


class ModelTuiTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp_dir = Path(self._tmp.name)
        self.cfg_path = self.tmp_dir / "model-tui.json"
        self.route_cache = self.tmp_dir / "route-cache.json"
        self.cc_cache = self.tmp_dir / "cc-cooldown.json"
        self.cc_projects = self.tmp_dir / "claude-projects"
        self.cc_projects.mkdir()
        self._env_patch = mock.patch.dict(
            os.environ,
            {
                "MODEL_TUI_CONFIG": str(self.cfg_path),
                "MODEL_TUI_ROUTE_CACHE": str(self.route_cache),
                "MODEL_TUI_CC_COOLDOWN_CACHE": str(self.cc_cache),
                "CLAUDE_PROJECTS_DIR": str(self.cc_projects),
                "CLAUDE_CREDENTIALS_PATH": str(self.tmp_dir / "missing-creds.json"),
                "CLAUDE_USAGE_JSON": "",
                "MODEL_TUI_ROUTE_TTL_SEC": "300",
            },
            clear=False,
        )
        self._env_patch.start()

    def tearDown(self) -> None:
        self._env_patch.stop()
        self._tmp.cleanup()

    def _run_cli(self, argv: list[str]) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = model_tui.main(argv)
        return rc, out.getvalue(), err.getvalue()

    def test_builtin_claude_single_channel_and_oauth_usage(self) -> None:
        usage = {
            "five_hour": {"utilization": 25, "resets_at": "2026-11-27T15:00:00Z"},
            "seven_day": {"utilization": 40, "resets_at": "2026-12-01T00:00:00Z"},
        }
        with mock.patch.dict(os.environ, {"CLAUDE_USAGE_JSON": json.dumps(usage)}):
            rc, out, _ = self._run_cli(["--model", "opus-high", "--check"])
        self.assertEqual(rc, 0)
        data = json.loads(out)
        self.assertEqual(data["model"], "opus")
        self.assertEqual(data["effort"], "high")
        self.assertEqual(data["backend"], "claude")
        self.assertTrue(data["available"])
        self.assertAlmostEqual(data["remaining"], 0.60, places=2)
        self.assertEqual(data["cmd"], ["claude", "--model", "opus", "--effort", "high"])

    def test_two_channel_probe_cmd_picks_earlier_weekly_reset(self) -> None:
        now_ts = model_tui._parse_iso_ts("2026-11-27T10:00:00Z")
        assert now_ts is not None
        cfg = {
            "models": {
                "opus": {
                    "aliases": ["claude-opus"],
                    "channels": [
                        {
                            "backend": "sub-later",
                            "cmd": ["sub-later", "--model", "{model}", "--effort", "{effort}"],
                            "probe_cmd": _probe_script_cmd(
                                {
                                    "remaining": 0.80,
                                    "five_hour_reset": "2026-11-27T14:00:00Z",
                                    "weekly_reset": "2026-12-04T00:00:00Z",
                                    "available": True,
                                }
                            ),
                        },
                        {
                            "backend": "sub-earlier",
                            "cmd": ["sub-earlier", "--model", "{model}", "--effort", "{effort}"],
                            "probe_cmd": _probe_script_cmd(
                                {
                                    "remaining": 0.55,
                                    "five_hour_reset": "2026-11-27T15:00:00Z",
                                    "weekly_reset": "2026-11-29T00:00:00Z",
                                    "available": True,
                                }
                            ),
                        },
                    ],
                }
            }
        }
        self.cfg_path.write_text(json.dumps(cfg), encoding="utf-8")

        sel = model_tui.select_backend_details(model="opus", effort="medium", now_ts=now_ts)
        self.assertEqual(sel["backend"], "sub-earlier")
        self.assertEqual(sel["reason"], "earlier_weekly_reset")
        self.assertTrue(sel["available"])
        self.assertFalse(sel["route_cached"])

    def test_two_channel_probe_cmd_tiebreaks_on_five_hour_reset(self) -> None:
        now_ts = model_tui._parse_iso_ts("2026-11-27T10:00:00Z")
        assert now_ts is not None
        cfg = {
            "models": {
                "opus": {
                    "channels": [
                        {
                            "backend": "sub-a",
                            "cmd": ["sub-a", "--model", "{model}"],
                            "probe_cmd": _probe_script_cmd(
                                {
                                    "remaining": 0.70,
                                    "five_hour_reset": "2026-11-27T14:30:00Z",
                                    "weekly_reset": "2026-12-01T00:00:00Z",
                                    "available": True,
                                }
                            ),
                        },
                        {
                            "backend": "sub-b",
                            "cmd": ["sub-b", "--model", "{model}"],
                            "probe_cmd": _probe_script_cmd(
                                {
                                    "remaining": 0.70,
                                    "five_hour_reset": "2026-11-27T12:00:00Z",
                                    "weekly_reset": "2026-12-01T00:00:20Z",
                                    "available": True,
                                }
                            ),
                        },
                    ]
                }
            }
        }
        self.cfg_path.write_text(json.dumps(cfg), encoding="utf-8")

        sel = model_tui.select_backend_details(model="opus", now_ts=now_ts)
        self.assertEqual(sel["backend"], "sub-b")
        self.assertEqual(sel["reason"], "earlier_5h_reset")

    def test_exhausted_channel_failover(self) -> None:
        now_ts = model_tui._parse_iso_ts("2026-11-27T10:00:00Z")
        assert now_ts is not None
        cfg = {
            "models": {
                "opus": {
                    "channels": [
                        {
                            "backend": "primary",
                            "cmd": ["primary", "--model", "{model}"],
                            "probe_cmd": _probe_script_cmd(
                                {
                                    "remaining": 0.0,
                                    "five_hour_reset": "2026-11-27T12:00:00Z",
                                    "weekly_reset": "2026-11-28T00:00:00Z",
                                    "available": False,
                                }
                            ),
                        },
                        {
                            "backend": "backup",
                            "cmd": ["backup", "--model", "{model}"],
                            "probe_cmd": _probe_script_cmd(
                                {
                                    "remaining": 0.45,
                                    "five_hour_reset": "2026-11-27T14:00:00Z",
                                    "weekly_reset": "2026-12-03T00:00:00Z",
                                    "available": True,
                                }
                            ),
                        },
                    ]
                }
            }
        }
        self.cfg_path.write_text(json.dumps(cfg), encoding="utf-8")

        sel = model_tui.select_backend_details(model="opus", now_ts=now_ts)
        self.assertEqual(sel["backend"], "backup")
        self.assertEqual(sel["reason"], "failover_only_available")
        self.assertTrue(sel["available"])

    def test_route_cache_ttl_and_cache_only_and_no_cache(self) -> None:
        now_ts = model_tui._parse_iso_ts("2026-11-27T10:00:00Z")
        assert now_ts is not None
        cfg = {
            "models": {
                "opus": {
                    "channels": [
                        {
                            "backend": "alpha",
                            "cmd": ["alpha", "--model", "{model}"],
                            "probe_cmd": _probe_script_cmd(
                                {
                                    "remaining": 0.80,
                                    "five_hour_reset": "2026-11-27T14:00:00Z",
                                    "weekly_reset": "2026-12-02T00:00:00Z",
                                    "available": True,
                                }
                            ),
                        },
                        {
                            "backend": "beta",
                            "cmd": ["beta", "--model", "{model}"],
                            "probe_cmd": _probe_script_cmd(
                                {
                                    "remaining": 0.60,
                                    "five_hour_reset": "2026-11-27T13:00:00Z",
                                    "weekly_reset": "2026-11-29T00:00:00Z",
                                    "available": True,
                                }
                            ),
                        },
                    ]
                }
            }
        }
        self.cfg_path.write_text(json.dumps(cfg), encoding="utf-8")

        # Cache miss before first probe
        miss = model_tui.select_backend_details(model="opus", now_ts=now_ts, cache_only=True)
        self.assertEqual(miss["backend"], "unknown")
        self.assertEqual(miss["reason"], "cache_miss")

        # Live probe populates route cache
        first = model_tui.select_backend_details(model="opus", now_ts=now_ts)
        self.assertEqual(first["backend"], "beta")
        self.assertFalse(first["route_cached"])

        # Within TTL -> cache hit without probing
        cached = model_tui.select_backend_details(model="opus", now_ts=now_ts + 60, cache_only=True)
        self.assertEqual(cached["backend"], "beta")
        self.assertTrue(cached["route_cached"])

        # Past TTL -> cache miss
        expired = model_tui.select_backend_details(model="opus", now_ts=now_ts + 301, cache_only=True)
        self.assertEqual(expired["backend"], "unknown")
        self.assertEqual(expired["reason"], "cache_miss")

        # --no-cache bypasses cache
        bypassed = model_tui.select_backend_details(model="opus", now_ts=now_ts + 60, use_cache=False)
        self.assertEqual(bypassed["backend"], "beta")
        self.assertFalse(bypassed["route_cached"])

    def test_adhoc_provider_model_syntax(self) -> None:
        rc, out, _ = self._run_cli(["--model", "codex:o3", "--check"])
        self.assertEqual(rc, 0)
        data = json.loads(out)
        self.assertEqual(data["model"], "codex:o3")
        self.assertEqual(data["backend"], "codex")
        self.assertEqual(data["cmd"], ["codex", "--model", "o3"])

    def test_list_models_outputs_registry_json(self) -> None:
        rc, out, _ = self._run_cli(["--list-models"])
        self.assertEqual(rc, 0)
        data = json.loads(out)
        self.assertIn("opus", data)
        self.assertIn("sonnet", data)
        self.assertEqual(data["opus"]["channels"][0]["backend"], "claude")

    def test_forbidden_print_flags_rejected(self) -> None:
        for flag in ("-p", "--print", "--output-format=json", "-vp"):
            rc, _out, err = self._run_cli(["--model", "opus", flag, "hello"])
            self.assertEqual(rc, 2, f"Expected exit 2 for {flag}")
            self.assertIn("forbidden", err.lower())


if __name__ == "__main__":
    unittest.main()
