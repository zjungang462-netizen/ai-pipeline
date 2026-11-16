"""纯 API 五阶段演示：仅凭百炼/方舟/DeepSeek Key 即可端到端跑通流水线。

用法（在项目根目录，PowerShell）：
    $env:DASHSCOPE_API_KEY="sk-xxxx"; python demo/run_api_demo.py "做一个命令行待办工具"
    $env:DEEPSEEK_API_KEY="sk-xxxx";  python demo/run_api_demo.py "做一个命令行待办工具" deepseek
    $env:ARK_API_KEY="xxxx";          python demo/run_api_demo.py "做一个命令行待办工具" ark

不依赖任何 CLI 工具，全部阶段走 OpenAI 兼容 API。Key 只从环境变量读取。
"""
from __future__ import annotations

import sys
import uuid
from pathlib import Path

# 让脚本可直接运行（把项目根加入 sys.path）
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from orchestrator.config import parse_config
from orchestrator.contracts import StageStatus
from orchestrator.engine import Engine
from orchestrator.adapters.factory import build_all

# 与 CLI _print_results 对齐：完成 0、待审批 11、失败/中断/未完成一律非零（1）。
EXIT_OK = 0
EXIT_FAILED = 1
EXIT_AWAITING_APPROVAL = 11


def result_exit_code(results):
    """演示入口退出码：不得把失败或等待审批当成功。"""
    states = list(results.values())
    if not states:
        return EXIT_FAILED
    if any(state not in (StageStatus.DONE.value, StageStatus.AWAITING_APPROVAL.value)
           for state in states):
        return EXIT_FAILED
    if any(state == StageStatus.AWAITING_APPROVAL.value for state in states):
        return EXIT_AWAITING_APPROVAL
    return EXIT_OK


def build_demo_config(base_dir: Path, provider: str = "dashscope"):
    if provider == "ark":
        base_url = "https://ark.cn-beijing.volces.com/api/v3"
        model = "doubao-seed-2-0-code"   # 替换为你的方舟推理接入点 ID
        key_env = "ARK_API_KEY"
    elif provider == "deepseek":
        base_url = "https://api.deepseek.com"   # OpenAI 兼容（官方文档）
        model = "deepseek-chat"                 # 稳定别名；也可 deepseek-flash
        key_env = "DEEPSEEK_API_KEY"
    else:
        base_url = "https://dashscope.aliyuncs.com/compatible-mode/v1"
        model = "qwen3-coder-plus"
        key_env = "DASHSCOPE_API_KEY"

    stages = []
    for name, kind in [("requirements", "report"), ("design", "report"),
                       ("coding", "code"), ("testing", "test"), ("review", "review")]:
        if kind == "test":
            stages.append({"name": name, "kind": kind,
                           "test_cmd": [sys.executable, "-m", "unittest", "discover", "-s", "tests"],
                           "feedback_to": "coding", "max_rounds": 2})
            continue
        spec = {
            "name": name, "adapter": "api", "kind": kind,
            "prompt": str(base_dir / "prompts" / f"{name}.md"),
            "approval": name == "design",
        }
        if kind == "review":
            spec.update(feedback_to="coding", max_rounds=2)
        stages.append(spec)
    raw = {
        "task_workdir": "demo_workspace",
        "state_dir": ".demo_state",
        "stages": stages,
        "adapters": {"api": {"type": "openai_compat", "base_url": base_url,
                             "model": model, "api_key_env": key_env}},
    }
    return parse_config(raw, base_dir=base_dir)


def main():
    task = sys.argv[1] if len(sys.argv) > 1 else "做一个命令行待办事项工具"
    provider = sys.argv[2] if len(sys.argv) > 2 else "dashscope"
    cfg = build_demo_config(ROOT, provider)
    stage_kind = {s.adapter: s.kind for s in cfg.stages}
    adapters = build_all(cfg.adapters, stage_kind)

    # preflight：缺 Key 直接明示
    for name, ad in adapters.items():
        rep = ad.preflight()
        print(f"[preflight] {name}: ok={rep.ok} {rep.detail}")
        if not rep.ok:
            print("请先设置对应的 API Key 环境变量再运行。")
            return 1

    run_id = "demo-" + uuid.uuid4().hex[:8]
    eng = Engine(cfg, adapters, run_id=run_id, task=task)
    print(f"\n开始流水线 run_id={run_id} task={task}\n")
    results = eng.run()
    for name, status in results.items():
        print(f"  {name}: {status}")
    print(f"\n状态与产物目录: {cfg.state_dir / 'runs' / run_id}")
    exit_code = result_exit_code(results)
    if exit_code == EXIT_OK:
        print("演示完成：所有阶段均为 done。")
    elif exit_code == EXIT_AWAITING_APPROVAL:
        print("演示未完成：有阶段等待审批，可用 cli.py approve 继续（第 4 阶段提供）。")
    else:
        print("演示未完成：存在失败/中断/未完成阶段，请查看状态目录中的诊断信息。")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
