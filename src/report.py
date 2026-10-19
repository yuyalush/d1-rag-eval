"""T17 結果レポート：最終評価の結果を results/<run_id>/report.md にまとめる（docs/ にもコピーする）。

前提：src.evaluate（実行）→ src.score（LLM 採点）→ src.significance（有意差）を実行済みであること。

    uv run python -m src.report --run-id test-final --split test
"""

from __future__ import annotations

import argparse
import json
import shutil

import pandas as pd

from src.config import ROOT, load_config
from src.evaluate import RESULTS, load_qa, summarize

CONFIG_LABELS = {
    "a_baseline": "A ベースライン",
    "b_d1_filter": "B D1 関連性フィルタ",
    "c_d1_gate": "C B＋十分性ゲート",
    "d_d1_rerank": "D D1 リランク",
    "e_llm_filter": "E 汎用 LLM フィルタ",
}
TYPE_LABELS = {"single": "単一根拠", "multi": "複数根拠", "confusing": "紛らわしい", "unanswerable": "回答不能"}


def fmt(v, digits: int = 3) -> str:
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return "—"
    return f"{v:.{digits}f}"


def load_logs(out_dir, name: str) -> list[dict]:
    rows = []
    for log in sorted(out_dir.glob(f"{name}__r*.jsonl")):
        rows += [json.loads(line) | {"run": int(log.stem.split("__r")[1])} for line in log.read_text(encoding="utf-8").splitlines()]
    return rows


def load_scores(out_dir, name: str) -> dict[tuple[str, int], dict]:
    scores = {}
    for f in sorted(out_dir.glob(f"scores__{name}__r*.jsonl")):
        run = int(f.stem.split("__r")[1])
        for line in f.read_text(encoding="utf-8").splitlines():
            s = json.loads(line)
            scores[(s["qid"], run)] = s
    return scores


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--split", default="test")
    args = parser.parse_args()

    out_dir = RESULTS / args.run_id
    qa = {q["qid"]: q for q in load_qa(args.split)}
    summary = summarize(out_dir, args.split)
    per_q = pd.read_csv(out_dir / "per_question.csv")
    sig = pd.read_csv(out_dir / "significance.csv") if (out_dir / "significance.csv").exists() else None
    threshold = load_config()["configs"]["b_d1_filter"]["threshold"]
    chunks = {d["id"]: d["text"] for d in map(json.loads, (ROOT / "data/chunks.jsonl").read_text(encoding="utf-8").splitlines())}

    lines = [f"# 最終評価レポート（{args.run_id}）", ""]
    runs = sorted(per_q.run.unique())
    lines += [f"テスト用セット {len(qa)} 問 × 構成 {len(summary)} × {len(runs)} 回。値は全実行の平均。", ""]

    # ---- 構成ごとの指標 ----
    lines += ["## 構成ごとの指標", "",
              "| 構成 | 正答率 | Faithfulness | Context Precision | Context Recall | 回答不能での控え率 | 過剰拒否率 | p50（待ち除く） | p95 | 1 問あたり費用 |",
              "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |"]
    for cfg, r in summary.iterrows():
        lines.append(
            f"| {CONFIG_LABELS.get(cfg, cfg)} | {fmt(r.accuracy)} | {fmt(r.faithfulness)} | {fmt(r.context_precision)} | "
            f"{fmt(r.context_recall)} | {fmt(r.abstain_unanswerable)} | {fmt(r.abstain_answerable)} | "
            f"{r.latency_api_p50_ms / 1000:.1f} 秒 | {r.latency_p95_ms / 1000:.1f} 秒 | ${r.cost_per_q_usd:.5f} |")
    lines += ["", "正答率は correct=1・partial=0.5・wrong=0 の平均（回答不能の質問は控えれば correct）。"
              "p50（待ち除く）は Decision-1 と構成 E の判定 API の待ち時間（レート制限）を除いた値、p95 は待ちを含む実測値。", ""]

    # ---- 種別ごとの正答率 ----
    if per_q.accuracy.notna().any():
        pivot = per_q.groupby(["config", "type"]).accuracy.mean().unstack("type")
        cols = [t for t in TYPE_LABELS if t in pivot.columns]
        lines += ["## 質問の種別ごとの正答率", "", "| 構成 | " + " | ".join(f"{TYPE_LABELS[t]}（{sum(q['type'] == t for q in qa.values())} 問）" for t in cols) + " |",
                  "| --- |" + " --- |" * len(cols)]
        for cfg, r in pivot.iterrows():
            lines.append(f"| {CONFIG_LABELS.get(cfg, cfg)} | " + " | ".join(fmt(r[t]) for t in cols) + " |")
        lines.append("")

    # ---- 有意差 ----
    if sig is not None:
        lines += ["## 構成 A との差（ブートストラップ 95% 信頼区間）", "",
                  "| 指標 | 構成 | A | 値 | 差 | 95% 信頼区間 | 有意 |", "| --- | --- | --- | --- | --- | --- | --- |"]
        for _, r in sig.iterrows():
            lines.append(f"| {r.metric} | {CONFIG_LABELS.get(r.config, r.config)} | {fmt(r.baseline)} | {fmt(r.value)} | "
                         f"{r['diff']:+.3f} | [{r.ci_low:+.3f}, {r.ci_high:+.3f}] | {'○' if r.significant else ''} |")
        lines += ["", "質問ごとに実行回の平均を取り、同じ質問どうしの差を 10,000 回再標本化した。信頼区間が 0 をまたがなければ有意（○）。", ""]

    # ---- B と E の直接比較（Decision-1 と汎用 LLM の同じ判定） ----
    vs_e = out_dir / "significance_vs_e.csv"
    if vs_e.exists():
        e = pd.read_csv(vs_e)
        e = e[e.config == "b_d1_filter"]
        lines += ["## 構成 B（Decision-1）と構成 E（汎用 LLM）の比較", "",
                  "同じ判定文で、判定するモデルだけが異なる。差は B − E。", "",
                  "| 指標 | E | B | 差 | 95% 信頼区間 | 有意 |", "| --- | --- | --- | --- | --- | --- |"]
        for _, r in e.iterrows():
            lines.append(f"| {r.metric} | {fmt(r.baseline)} | {fmt(r.value)} | {r['diff']:+.3f} | "
                         f"[{r.ci_low:+.3f}, {r.ci_high:+.3f}] | {'○' if r.significant else ''} |")
        lines.append("")

    # ---- 失敗例：D1 が根拠チャンクを除外 ----
    lines += [f"## 失敗例：Decision-1 が根拠チャンクを除外した例（構成 B、閾値 {threshold}）", ""]
    b_logs = [r for r in load_logs(out_dir, "b_d1_filter") if r["run"] == runs[0]]
    b_scores = load_scores(out_dir, "b_d1_filter")
    misses = []
    for r in b_logs:
        gold = set(qa[r["qid"]]["evidence_chunk_ids"])
        for cid in gold & set(r["retrieved_ids"]):
            p = r["decision"]["relevant"].get(cid)
            if p is not None and p < threshold:
                misses.append((p, r, cid))
    lines += [f"run {runs[0]} で、検索 10 件に入った根拠チャンクのうち {len(misses)} 件を閾値未満として除外した。確率の低い順に最大 8 件を示す。", ""]
    for p, r, cid in sorted(misses, key=lambda x: x[0])[:8]:
        q = qa[r["qid"]]
        s = b_scores.get((r["qid"], r["run"]), {})
        lines += [f"### {r['qid']}（{TYPE_LABELS[q['type']]}）D1 確率 {p:.3f}", "",
                  f"- 質問：{q['question']}", f"- 模範解答：{q['answer']}",
                  f"- 除外した根拠 {cid}：{chunks[cid].split(chr(10), 1)[-1].strip()[:200]}…",
                  f"- 構成 B の回答：{r['answer'][:200]}（採点：{s.get('grade', '未採点')}）", ""]

    # ---- 失敗例：C のゲートで回答可能な質問を控えた ----
    c_logs = [r for r in load_logs(out_dir, "c_d1_gate") if r["run"] == runs[0]]
    gated = [r for r in c_logs if r["gated"] and qa[r["qid"]]["type"] != "unanswerable"]
    lines += ["## 失敗例：構成 C のゲートで回答可能な質問を控えた例", "",
              f"run {runs[0]} で {len(gated)} 問。通過チャンク 0 件で控えたものと、十分性の判定で控えたものを分けて示す。", ""]
    for r in gated[:8]:
        q = qa[r["qid"]]
        suff = r["decision"].get("sufficiency")
        why = "通過チャンク 0 件" if not r["passed_ids"] else f"十分性 {suff['choice']}（{json.dumps(suff['probabilities'], ensure_ascii=False)}）"
        lines.append(f"- {r['qid']}（{TYPE_LABELS[q['type']]}）{why}：{q['question'][:80]}")
    lines.append("")

    report = "\n".join(lines) + "\n"
    (out_dir / "report.md").write_text(report, encoding="utf-8")
    dest = ROOT / "docs" / f"{args.run_id}_report.md"
    shutil.copy(out_dir / "report.md", dest)
    print(f"{out_dir / 'report.md'} と {dest} を作成しました")


if __name__ == "__main__":
    main()
