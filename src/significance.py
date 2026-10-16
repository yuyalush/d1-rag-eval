"""T16 有意差判定：構成 A との差をブートストラップ 95% 信頼区間で算出する。

各質問について実行回の平均を取り、同じ質問どうしの差（構成 X − 構成 A）を質問単位で再標本化する（対応のある比較）。
信頼区間が 0 をまたがなければ有意とみなす。

    uv run python -m src.significance --run-id test-final --split test
"""

from __future__ import annotations

import argparse

import numpy as np
import pandas as pd

from src.evaluate import RESULTS, summarize

# (列名, 対象の質問) 。answerable=None は全問
METRICS = [
    ("accuracy", None),
    ("faithfulness", True),
    ("precision", True),           # Context Precision
    ("recall", True),              # Context Recall
    ("abstained", False),          # 回答不能での控え率（高いほど良い）
    ("abstained", True),           # 過剰拒否率（低いほど良い）
    ("latency_ms", None),
    ("cost_usd", None),
]


def bootstrap_ci(diff: np.ndarray, n_boot: int = 10000, seed: int = 0) -> tuple[float, float]:
    rng = np.random.default_rng(seed)
    idx = rng.integers(0, len(diff), size=(n_boot, len(diff)))
    means = diff[idx].mean(axis=1)
    return float(np.percentile(means, 2.5)), float(np.percentile(means, 97.5))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--split", default="test", choices=["dev", "test"])
    parser.add_argument("--baseline", default="a_baseline")
    args = parser.parse_args()

    out_dir = RESULTS / args.run_id
    summarize(out_dir, args.split)  # per_question.csv を最新にする
    df = pd.read_csv(out_dir / "per_question.csv")
    df["abstained"] = df["abstained"].astype(float)

    rows = []
    for metric, answerable in METRICS:
        sub = df if answerable is None else df[df.answerable == answerable]
        per_q = sub.groupby(["config", "qid"])[metric].mean().unstack("config")
        if args.baseline not in per_q:
            continue
        for cfg in per_q.columns:
            if cfg == args.baseline:
                continue
            pair = per_q[[args.baseline, cfg]].dropna()
            if pair.empty:
                continue
            diff = (pair[cfg] - pair[args.baseline]).to_numpy()
            lo, hi = bootstrap_ci(diff)
            label = metric if answerable is None else f"{metric}[{'answerable' if answerable else 'unanswerable'}]"
            rows.append({
                "metric": label, "config": cfg, "n": len(pair),
                "baseline": round(pair[args.baseline].mean(), 4), "value": round(pair[cfg].mean(), 4),
                "diff": round(diff.mean(), 4), "ci_low": round(lo, 4), "ci_high": round(hi, 4),
                "significant": bool(lo > 0 or hi < 0),
            })
    result = pd.DataFrame(rows)
    result.to_csv(out_dir / "significance.csv", index=False)
    (out_dir / "significance.md").write_text(result.to_markdown(index=False), encoding="utf-8")
    print(result.to_string(index=False))


if __name__ == "__main__":
    main()
