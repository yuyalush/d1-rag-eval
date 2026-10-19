"""生成モデルの差し替え比較のまとめ：生成モデルごとに、構成 A〜E の指標と構成 A との差を並べる。

検索・Decision-1 の判定は全モデル共通（test-final のもの）で、違うのは回答を生成するモデルだけ。

    uv run python -m src.compare_gen
"""

from __future__ import annotations

import pandas as pd

from src.config import ROOT
from src.evaluate import RESULTS, summarize
from src.report import CONFIG_LABELS, TYPE_LABELS, fmt
from src.significance import compute

RUNS = {  # run_id: 生成モデル
    "test-final": "gpt-6-luna",
    "test-final-nano": "gpt-5-nano",
    "test-final-phi": "Phi-4-mini-instruct",
}
METRICS = [  # (significance の指標名, 表示名)
    ("accuracy", "正答率"),
    ("faithfulness[answerable]", "Faithfulness"),
    ("abstained[unanswerable]", "回答不能での控え率"),
    ("abstained[answerable]", "過剰拒否率"),
]


def complete(out_dir) -> bool:
    """全ログに採点がそろっているか。"""
    logs = [p for p in out_dir.glob("*__r*.jsonl") if not p.name.startswith("scores__")] if out_dir.exists() else []
    if len(logs) < 15:
        return False
    for log in logs:
        scores = out_dir / f"scores__{log.name}"
        if not scores.exists() or len(scores.read_text(encoding="utf-8").splitlines()) < len(log.read_text(encoding="utf-8").splitlines()):
            return False
    return True


def main() -> None:
    lines = ["# 生成モデルの差し替え比較", "",
             "検索結果と Decision-1 の判定（LLM に渡したチャンク）は最終評価（gpt-6-luna）と同じで、回答を生成するモデルだけを変えた。"
             "テスト用 96 問 × 3 回。構成 E の関連性判定は gpt-6-luna のまま。", ""]
    summaries, sigs = {}, {}
    for run_id, model in RUNS.items():
        out_dir = RESULTS / run_id
        if not complete(out_dir):
            print(f"{run_id}: 生成または採点が終わっていないので飛ばします")
            continue
        summaries[model] = summarize(out_dir, "test")
        sigs[model] = compute(out_dir, "test")
        per_q = pd.read_csv(out_dir / "per_question.csv")
        summaries[model] = summaries[model].assign(
            **{f"acc_{t}": per_q[per_q.type == t].groupby("config").accuracy.mean() for t in TYPE_LABELS})

    # ---- 生成モデル × 構成の指標 ----
    lines += ["## 生成モデル × 構成の指標", "",
              "| 生成モデル | 構成 | 正答率 | Faithfulness | 回答不能での控え率 | 過剰拒否率 | 複数根拠の正答率 | 1 問あたり費用 |",
              "| --- | --- | --- | --- | --- | --- | --- | --- |"]
    for model, s in summaries.items():
        for cfg, r in s.iterrows():
            lines.append(f"| {model} | {CONFIG_LABELS.get(cfg, cfg)} | {fmt(r.accuracy)} | {fmt(r.faithfulness)} | "
                         f"{fmt(r.abstain_unanswerable)} | {fmt(r.abstain_answerable)} | {fmt(r.acc_multi)} | ${r.cost_per_q_usd:.5f} |")
    lines.append("")

    # ---- 構成 A との差 ----
    lines += ["## 構成 A との差（生成モデルごと、ブートストラップ 95% 信頼区間。○ は有意）", ""]
    for key, label in METRICS:
        lines += [f"### {label}", "", "| 生成モデル | A の値 | " + " | ".join(CONFIG_LABELS[c] for c in ["b_d1_filter", "c_d1_gate", "d_d1_rerank", "e_llm_filter"]) + " |",
                  "| --- | --- |" + " --- |" * 4]
        for model, sig in sigs.items():
            sub = sig[sig.metric == key].set_index("config")
            if sub.empty:
                continue
            base = sub.baseline.iloc[0]
            cells = []
            for c in ["b_d1_filter", "c_d1_gate", "d_d1_rerank", "e_llm_filter"]:
                if c not in sub.index:
                    cells.append("—")
                    continue
                r = sub.loc[c]
                cells.append(f"{r['diff']:+.3f}{' ○' if r.significant else ''}")
            lines.append(f"| {model} | {fmt(base)} | " + " | ".join(cells) + " |")
        lines.append("")

    out = ROOT / "docs/gen_compare.md"
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    pd.concat({m: s for m, s in sigs.items()}, names=["gen_model"]).to_csv(ROOT / "docs/gen_compare_significance.csv")
    print(out.read_text(encoding="utf-8"))


if __name__ == "__main__":
    main()
