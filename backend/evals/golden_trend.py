"""Golden Answer Set 趋势追踪 —— 记录每次 run 的通过率，展示"改动→回归→趋势"闭环。

借鉴 open_deep_research 的 LangSmith 评测管线思路，但自建轻量版（不引入 LangSmith）。
每次 python -m evals.golden_answers run（离线/online）结尾追加一行 JSONL，--trend 查看历史趋势。
追踪是增强项，任何失败静默不影响测试主流程。

用法：
  python -m evals.golden_trend             # 查看趋势（默认最近20次）
  python -m evals.golden_trend --trend 50  # 查看最近50次
"""
from __future__ import annotations
import json
from datetime import datetime
from pathlib import Path

TREND_FILE = Path(__file__).resolve().parents[1] / "memory" / "golden_trend.jsonl"


def record_trend(mode: str, passed: int, failed: int, extra: dict | None = None) -> None:
    """追加一次 run 的结果到趋势文件。失败静默（不影响测试主流程）。"""
    try:
        TREND_FILE.parent.mkdir(parents=True, exist_ok=True)
        total = passed + failed
        rec = {
            "ts": datetime.now().isoformat(timespec="seconds"),
            "mode": mode,
            "passed": passed,
            "failed": failed,
            "total": total,
            "pass_rate": round(passed / total, 4) if total else 0.0,
        }
        if extra:
            rec.update(extra)
        with open(TREND_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:  # noqa: BLE001
        pass


def show_trend(n: int = 20) -> int:
    """打印最近 n 次 run 的趋势表，返回最近一次的 failed 数。"""
    if not TREND_FILE.exists():
        print("尚无趋势记录。先跑一次 python -m evals.golden_answers 生成记录。")
        return 0
    try:
        lines = TREND_FILE.read_text(encoding="utf-8").strip().split("\n")
        recs = [json.loads(l) for l in lines if l.strip()]
    except Exception as e:  # noqa: BLE001
        print(f"读取趋势文件失败: {e}")
        return 0
    if not recs:
        print("趋势文件为空。")
        return 0
    recs = recs[-n:]
    print(f"\n=== Golden 趋势（最近 {len(recs)} 次）===\n")
    print(f"{'时间':<20}{'模式':<8}{'通过':<6}{'失败':<6}{'通过率':<8}{'趋势':<6}")
    print("-" * 56)
    prev_rate = None
    for r in recs:
        rate = r.get("pass_rate", 0)
        if prev_rate is None:
            arrow = "—"
        elif rate > prev_rate + 0.01:
            arrow = "↑"
        elif rate < prev_rate - 0.01:
            arrow = "↓"
        else:
            arrow = "→"
        print(f"{str(r.get('ts', ''))[:19]:<20}{str(r.get('mode', '')):<8}"
              f"{r.get('passed', 0):<6}{r.get('failed', 0):<6}{rate:<8.1%}{arrow:<6}")
        prev_rate = rate
    print("-" * 56)
    last = recs[-1]
    print(f"最近一次：{last.get('mode', '')} · 通过率 {last.get('pass_rate', 0):.1%} · "
          f"{last.get('passed', 0)} 通过 / {last.get('failed', 0)} 失败\n")
    return last.get("failed", 0)


if __name__ == "__main__":
    import sys
    n = 20
    if "--trend" in sys.argv and len(sys.argv) > 2:
        try:
            n = int(sys.argv[2])
        except ValueError:
            pass
    show_trend(n)
