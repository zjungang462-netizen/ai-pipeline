"""第 2 阶段：纯 API 五阶段端到端离线集成测试（mock HTTP 驱动整个引擎）。"""
from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from orchestrator.config import parse_config
from orchestrator.contracts import StageStatus
from orchestrator.engine import Engine
from orchestrator.adapters.factory import build_all


def ok_response(text):
    return io.BytesIO(json.dumps({
        "choices": [{"message": {"content": text}}],
        "usage": {"total_tokens": 10},
    }).encode("utf-8"))


class TestAPIEndToEnd(unittest.TestCase):
    def test_five_stages_via_mock_api(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            raw = {
                "task_workdir": "workspace", "state_dir": ".state",
                "stages": [
                    {"name": n, "adapter": "api", "kind": k, "prompt": f"做{ n }"}
                    for n, k in [("requirements", "report"), ("design", "report"),
                                 ("coding", "code"), ("testing", "report"),
                                 ("review", "report")]
                ],
                "adapters": {"api": {"type": "openai_compat",
                                     "base_url": "https://example.test/v1",
                                     "model": "m", "api_key_env": "TEST_API_KEY"}},
            }
            cfg = parse_config(raw, base_dir=tmp)
            stage_kind = {s.adapter: s.kind for s in cfg.stages}
            adapters = build_all(cfg.adapters, stage_kind)

            # 每次调用返回对应阶段的确定性文本
            counter = {"n": 0}
            names = ["requirements", "design", "coding", "testing", "review"]

            def fake_urlopen(req, timeout=None):
                name = names[counter["n"] % 5]
                counter["n"] += 1
                if name == "coding":
                    return ok_response("```python\n# file: app.py\nprint('hello')\n```")
                return ok_response(f"{name} 阶段产出内容")
            # 用 side_effect 保证每次调用返回全新流（重试不会读到已关闭流）

            with mock.patch.dict(os.environ, {"TEST_API_KEY": "sk-test"}), \
                 mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
                eng = Engine(cfg, adapters, run_id="e2e", task="demo")
                res = eng.run()

            for name in names:
                self.assertEqual(res[name], StageStatus.DONE.value, f"{name}={res[name]}")
            # 报告类产物落盘
            reports = list((cfg.state_dir / "runs" / "e2e" / "reports").glob("*.md"))
            self.assertGreaterEqual(len(reports), 4)
            # token 用量累计
            self.assertGreater(eng.state.usage_total["tokens"], 0)


if __name__ == "__main__":
    unittest.main()
