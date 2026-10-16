"""T14 評価ランナー：構成 × 実行回数 × 質問を回してログを残し、指標を CSV と Markdown 表に出す。

- ログは results/<run_id>/<config>__r<n>.jsonl（T13 の形式）。既にある (qid) は飛ばすので中断・再開できる
- 指標（LLM 採点なし）：Context Precision / Recall、回答控え率（回答不能 / 回答可能）、レイテンシ p50/p95、1 問あたりコスト
- LLM 採点（正答率・Faithfulness）は src/score.py で別に行う（テスト用セットの最終評価だけ）

    uv run python -m src.evaluate --split dev --configs a_baseline,b_d1_filter --runs 1 --run-id dev-check
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import time
from pathlib import Path

import pandas as pd
import yaml

from src.config import ROOT, load_config
from src.decision import DecisionClient
from src.generator import Generator
from src.pipelines.base import write_log
from src.pipelines.configs import PIPELINES
from src.retriever import Retriever

RESULTS = ROOT / "results"
GRADE_POINTS = {"correct": 1.0, "partial": 0.5, "wrong": 0.0}


def load_qa(split: str) -> list[dict]:
    return [json.loads(line) for line in (ROOT / f"data/qa/{split}.jsonl").read_text(encoding="utf-8").splitlines()]


def prices() -> dict[str, dict[str, float]]:
    models = yaml.safe_load((ROOT / "config/models.yaml").read_text(encoding="utf-8"))
    return {d["name"]: d["price"] for d in models["deployments"]}


def cost_usd(tokens: dict[str, int], p: dict[str, dict[str, float]]) -> float:
    usd = tokens.get("embed", 0) * p["embed"]["input"]
    usd += tokens.get("d1_in", 0) * p["decision-eval"]["input"]
    usd += (tokens.get("gen_in", 0) + tokens.get("filter_in", 0)) * p["gen"]["input"]
    usd += (tokens.get("gen_out", 0) + tokens.get("filter_out", 0)) * p["gen"]["output"]
    return usd / 1e6


async def run_all(split: str, configs: list[str], runs: int, run_id: str, limit: int | None) -> Path:
    qa = load_qa(split)[:limit]
    out_dir = RESULTS / run_id
    retriever, generator = Retriever(), Generator()
    decision = DecisionClient(concurrency=load_config()["common"]["decision_concurrency"], rpm=load_config()["common"]["decision_rpm"])
    # デプロイ直後や間隔が空いた後の初回応答は遅いため、計測前に 1 回ずつ呼んでおく
    generator.generate("ウォームアップ", [])
    await PIPELINES["b_d1_filter"](retriever, generator, decision).run("Blob のアクセス層は？")
    try:
        for r in range(1, runs + 1):
            for name in configs:
                pipe = PIPELINES[name](retriever, generator, decision)
                log = out_dir / f"{name}__r{r}.jsonl"
                done = {json.loads(line)["qid"] for line in log.read_text(encoding="utf-8").splitlines()} if log.exists() else set()
                todo = [q for q in qa if q["qid"] not in done]
                start = time.perf_counter()
                for i, q in enumerate(todo, 1):
                    res = await pipe.run(q["question"])
                    res.qid, res.run_id = q["qid"], run_id
                    write_log(log, res)
                    if i % 10 == 0:
                        print(f"  r{r} {name}: {i}/{len(todo)}")
                print(f"r{r} {name}: {len(todo)} 問 {time.perf_counter() - start:.0f} 秒（既存 {len(done)}）")
    finally:
        await decision.aclose()
        retriever.close()
    return out_dir


def auc(labels: list[int], scores: list[float]) -> float | None:
    """ROC AUC（Mann-Whitney の U から計算）。"""
    pos = [s for y, s in zip(labels, scores) if y]
    neg = [s for y, s in zip(labels, scores) if not y]
    if not pos or not neg:
        return None
    return sum((p > n) + 0.5 * (p == n) for p in pos for n in neg) / (len(pos) * len(neg))


def d1_auc(out_dir: Path, qa: dict[str, dict]) -> dict[str, float | None]:
    """D1 判定のスコア（B/C は relevant、D は usefulness）が根拠チャンクを見分けられているか。

    ラベルは生成時の根拠チャンクのみ。検索された他の関連チャンクは負例扱いになるため、値は控えめに出る。
    """
    out: dict[str, float | None] = {}
    for name, key in (("b_d1_filter", "relevant"), ("c_d1_gate", "relevant"), ("d_d1_rerank", "usefulness")):
        labels, scores = [], []
        for log in out_dir.glob(f"{name}__r*.jsonl"):  # scores__ で始まるファイルはこのパターンに一致しない
            for line in log.read_text(encoding="utf-8").splitlines():
                res = json.loads(line)
                gold = set(qa[res["qid"]]["evidence_chunk_ids"])
                if not gold:
                    continue
                for cid, s in res["decision"].get(key, {}).items():
                    labels.append(int(cid in gold))
                    scores.append(s)
        if labels:
            out[name] = auc(labels, scores)
    return out


def summarize(out_dir: Path, split: str) -> pd.DataFrame:
    qa = {q["qid"]: q for q in load_qa(split)}
    p = prices()
    rows = []
    for log in sorted(out_dir.glob("*__r*.jsonl")):
        if log.name.startswith("scores__"):
            continue
        name, run = log.stem.split("__r")
        score_file = out_dir / f"scores__{log.name}"
        scores = {s["qid"]: s for s in map(json.loads, score_file.read_text(encoding="utf-8").splitlines())} if score_file.exists() else {}
        for line in log.read_text(encoding="utf-8").splitlines():
            res = json.loads(line)
            q = qa[res["qid"]]
            s = scores.get(res["qid"], {})
            gold = set(q["evidence_chunk_ids"])
            passed = res["passed_ids"]
            rows.append({
                "config": name, "run": int(run), "qid": res["qid"], "type": q["type"],
                "answerable": q["type"] != "unanswerable",
                "precision": (len(gold & set(passed)) / len(passed)) if passed and gold else None,
                "recall": (len(gold & set(passed)) / len(gold)) if gold else None,
                "retrieved_recall": (len(gold & set(res["retrieved_ids"])) / len(gold)) if gold else None,
                "abstained": res["abstained"],
                "latency_ms": res["latency_ms"]["total"],
                # レート制限の待ちを除いた時間：判定段を Decision-1 API の応答時間（並列の最大値）に置き換える
                "latency_api_ms": res["latency_ms"]["total"] - res["latency_ms"]["judge"] + res["latency_ms"]["d1_api_max"]
                if "d1_api_max" in res["latency_ms"] else res["latency_ms"]["total"],
                "cost_usd": cost_usd(res["tokens"], p),
                "accuracy": GRADE_POINTS.get(s["grade"]) if s else None,
                "faithfulness": s.get("faithfulness") if s else None,
            })
    df = pd.DataFrame(rows)

    def agg(g: pd.DataFrame) -> pd.Series:
        ans, unans = g[g.answerable], g[~g.answerable]
        lat = g.latency_ms.tolist()
        return pd.Series({
            "n": len(g),
            "accuracy": g.accuracy.mean(),          # LLM 採点（correct=1, partial=0.5）。未採点なら NaN
            "faithfulness": g.faithfulness.mean(),
            "context_precision": ans.precision.mean(),
            "context_recall": ans.recall.mean(),
            "retrieved_recall": ans.retrieved_recall.mean(),
            "abstain_unanswerable": unans.abstained.mean() if len(unans) else None,   # 適切な回答控え率
            "abstain_answerable": ans.abstained.mean() if len(ans) else None,         # 過剰拒否率
            "latency_p50_ms": statistics.median(lat),
            "latency_p95_ms": statistics.quantiles(lat, n=20)[-1] if len(lat) >= 20 else max(lat),
            "latency_api_p50_ms": g.latency_api_ms.median(),
            "cost_per_q_usd": g.cost_usd.mean(),
        })

    summary = df.groupby("config").apply(agg, include_groups=False)
    summary["d1_auc"] = pd.Series(d1_auc(out_dir, qa))
    summary = summary.round(4)
    df.to_csv(out_dir / "per_question.csv", index=False)
    summary.to_csv(out_dir / "summary.csv")
    (out_dir / "summary.md").write_text(summary.to_markdown(), encoding="utf-8")
    return summary


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--split", default="dev", choices=["dev", "test"])
    parser.add_argument("--configs", default=",".join(PIPELINES))
    parser.add_argument("--runs", type=int, default=1)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--summary-only", action="store_true")
    args = parser.parse_args()

    out_dir = RESULTS / args.run_id
    if not args.summary_only:
        asyncio.run(run_all(args.split, args.configs.split(","), args.runs, args.run_id, args.limit))
    print(summarize(out_dir, args.split).to_string())


if __name__ == "__main__":
    main()
