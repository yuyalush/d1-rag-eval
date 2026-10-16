"""T15 閾値・質問文のチューニング（開発用セットのみ。LLM 採点は使わない）。

1. 各質問を 1 回だけ検索（上位 20 件）してキャッシュする
2. 構成 B：relevant の質問文の候補ごとに上位 10 件を判定し、閾値 0.3〜0.8 で
   Context Precision / Recall とチャンク判定の F1・AUC を算出
3. 構成 C：B の最良設定で通過したチャンクに対し、sufficiency の質問文の候補（選択肢の順序入れ替えを含む）で
   回答不能での控え率と過剰拒否率を算出
4. 構成 D：usefulness の質問文の候補ごとに上位 20 件を採点し、上位 5 件の Precision / Recall を算出

    uv run python -m src.tune
"""

from __future__ import annotations

import asyncio
import json

import pandas as pd

from src.config import ROOT, load_config
from src.decision import DecisionClient, choice, noul, score
from src.evaluate import auc, load_qa
from src.retriever import Retriever

OUT = ROOT / "results/tune"
THRESHOLDS = [0.3, 0.4, 0.5, 0.6, 0.7, 0.8]
MAX_CHUNKS = 5

RELEVANT_VARIANTS = {
    "en_base": noul(
        "Does the passage contain information needed to answer the question?",
        "The passage states facts that directly answer all or part of the question",
        "The passage is off-topic or only shares keywords with the question",
    ),
    "en_partial": noul(
        "Would this passage help answer the question, even partially?",
        "The passage contains at least one fact needed for the answer",
        "The passage does not contain any fact needed for the answer",
    ),
    "ja_base": noul(
        "この文章は質問に答えるために必要な情報を含んでいますか？",
        "質問の全部または一部に直接答える事実が書かれている",
        "話題が違う、または質問とキーワードが共通するだけ",
    ),
}

SUFF_OPTIONS = load_config()["decision_questions"]["sufficiency"]["criteria"]
SUFFICIENCY_VARIANTS = {
    "en_base": load_config()["decision_questions"]["sufficiency"],
    "en_reversed": choice(  # 選択肢の順序による影響を確認する
        load_config()["decision_questions"]["sufficiency"]["instructions"],
        dict(reversed(list(SUFF_OPTIONS.items()))),
    ),
    "ja_base": choice(
        "これらの文章を合わせると、質問に答えるのに十分な根拠がありますか？",
        {
            "sufficient": "質問のすべての部分に文章から答えられる",
            "partial": "一部には答えられるが、答えられない部分がある",
            "insufficient": "文章は質問に答えていない",
            "cannot_tell": "文章からは判断できない",
        },
    ),
}

USEFULNESS_VARIANTS = {
    "en_base": load_config()["decision_questions"]["usefulness"],
    "ja_base": score("この文章は質問に答えるのにどの程度役立ちますか？", ["無関係", "関連する話題のみ", "部分的に答えている", "完全に答えている"]),
}


async def main_async() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    qa = load_qa("dev")
    retriever = Retriever()
    cache = OUT / "retrieval_dev.jsonl"
    if cache.exists():
        hits = {d["qid"]: d["hits"] for d in map(json.loads, cache.read_text(encoding="utf-8").splitlines())}
    else:
        hits = {}
        with cache.open("w", encoding="utf-8") as f:
            for q in qa:
                r = retriever.search(q["question"], 20)
                hits[q["qid"]] = [{"id": h.id, "content": h.content, "score": h.score} for h in r.hits]
                f.write(json.dumps({"qid": q["qid"], "hits": hits[q["qid"]]}, ensure_ascii=False) + "\n")
    retriever.close()
    client = DecisionClient(concurrency=load_config()["common"]["decision_concurrency"], rpm=load_config()["common"]["decision_rpm"])
    d1_tokens = 0

    # ---- B：relevant ----
    rows_b, probs_by_variant = [], {}
    for name, qdef in RELEVANT_VARIANTS.items():
        items = [(q, h) for q in qa for h in hits[q["qid"]][:10]]
        rs = await client.decide_many([({"question": q["question"], "passage": h["content"]}, {"relevant": qdef}) for q, h in items])
        d1_tokens += sum(r.input_tokens for r in rs)
        probs = {(q["qid"], h["id"]): r.answers["relevant"].probability for (q, h), r in zip(items, rs)}
        probs_by_variant[name] = probs
        labels = [int(h["id"] in q["evidence_chunk_ids"]) for q, h in items if q["evidence_chunk_ids"]]
        scores = [probs[(q["qid"], h["id"])] for q, h in items if q["evidence_chunk_ids"]]
        variant_auc = auc(labels, scores)
        for t in THRESHOLDS:
            tp = fp = fn = 0
            precisions, recalls, empty_unans = [], [], 0
            for q in qa:
                gold = set(q["evidence_chunk_ids"])
                ranked = sorted(((probs[(q["qid"], h["id"])], h["id"]) for h in hits[q["qid"]][:10]), reverse=True)
                above = [cid for p, cid in ranked if p >= t]
                passed = above[:MAX_CHUNKS]
                if not gold:
                    empty_unans += int(not passed)
                    continue
                tp += len(gold & set(above))
                fp += len(set(above) - gold)
                fn += len(gold - set(above))
                if passed:
                    precisions.append(len(gold & set(passed)) / len(passed))
                recalls.append(len(gold & set(passed)) / len(gold))
            p = tp / (tp + fp) if tp + fp else 0
            r = tp / (tp + fn) if tp + fn else 0
            n_unans = sum(1 for q in qa if not q["evidence_chunk_ids"])
            rows_b.append({
                "variant": name, "threshold": t, "auc": round(variant_auc, 4),
                "chunk_precision": round(p, 4), "chunk_recall": round(r, 4), "chunk_f1": round(2 * p * r / (p + r) if p + r else 0, 4),
                "context_precision": round(sum(precisions) / len(precisions), 4) if precisions else None,
                "context_recall": round(sum(recalls) / len(recalls), 4),
                "unanswerable_no_context": round(empty_unans / n_unans, 4),  # 回答不能で 1 件も通さなかった割合
            })
    df_b = pd.DataFrame(rows_b)
    # 最良：Context Recall が最大値から 0.02 以内の設定のうち、Context Precision が最も高いもの
    # （Recall は閾値でほとんど変わらず Precision だけが上がるため、Recall 最大だけで選ぶと低い閾値に偏る）
    near_best = df_b[df_b.context_recall >= df_b.context_recall.max() - 0.02]
    best_b = near_best.sort_values(["context_precision", "chunk_f1"], ascending=False).iloc[0]
    print("構成 B（relevant）:\n", df_b.to_string(index=False), "\n最良:", dict(best_b))

    # ---- C：sufficiency（B の最良設定で通過したチャンクに対して） ----
    probs = probs_by_variant[best_b["variant"]]
    passed_by_q = {}
    for q in qa:
        ranked = sorted(((probs[(q["qid"], h["id"])], h) for h in hits[q["qid"]][:10]), key=lambda x: -x[0])
        passed_by_q[q["qid"]] = [h for p, h in ranked if p >= best_b["threshold"]][:MAX_CHUNKS]
    rows_c = []
    targets = [q for q in qa if passed_by_q[q["qid"]]]
    for name, qdef in SUFFICIENCY_VARIANTS.items():
        rs = await client.decide_many([
            ({"question": q["question"], "passages": [h["content"] for h in passed_by_q[q["qid"]]]}, {"sufficiency": qdef}) for q in targets
        ])
        d1_tokens += sum(r.input_tokens for r in rs)
        gate = {q["qid"]: max(r.answers["sufficiency"].probabilities, key=r.answers["sufficiency"].probabilities.get) for q, r in zip(targets, rs)}
        unans = [q for q in qa if not q["evidence_chunk_ids"]]
        ans = [q for q in qa if q["evidence_chunk_ids"]]
        # 通過 0 件の質問は C では判定せずに控える
        abstain = lambda q: (not passed_by_q[q["qid"]]) or gate[q["qid"]] == "insufficient"  # noqa: E731
        rows_c.append({
            "variant": name,
            "abstain_unanswerable": round(sum(abstain(q) for q in unans) / len(unans), 4),
            "abstain_answerable": round(sum(abstain(q) for q in ans) / len(ans), 4),
            "choice_counts": dict(pd.Series(list(gate.values())).value_counts()),
        })
    df_c = pd.DataFrame(rows_c)
    print("\n構成 C（sufficiency）:\n", df_c.to_string(index=False))

    # ---- D：usefulness ----
    rows_d = []
    for name, qdef in USEFULNESS_VARIANTS.items():
        items = [(q, h) for q in qa for h in hits[q["qid"]][:20]]
        rs = await client.decide_many([({"question": q["question"], "passage": h["content"]}, {"usefulness": qdef}) for q, h in items])
        d1_tokens += sum(r.input_tokens for r in rs)
        sc = {(q["qid"], h["id"]): r.answers["usefulness"].score for (q, h), r in zip(items, rs)}
        precisions, recalls = [], []
        for q in qa:
            gold = set(q["evidence_chunk_ids"])
            if not gold:
                continue
            top = [h["id"] for _, i, h in sorted(((sc[(q["qid"], h["id"])], i, h) for i, h in enumerate(hits[q["qid"]][:20])), key=lambda x: (-x[0], x[1]))[:MAX_CHUNKS]]
            precisions.append(len(gold & set(top)) / len(top))
            recalls.append(len(gold & set(top)) / len(gold))
        labels = [int(h["id"] in q["evidence_chunk_ids"]) for q, h in items if q["evidence_chunk_ids"]]
        scores = [sc[(q["qid"], h["id"])] for q, h in items if q["evidence_chunk_ids"]]
        rows_d.append({"variant": name, "auc": round(auc(labels, scores), 4),
                       "context_precision@5": round(sum(precisions) / len(precisions), 4),
                       "context_recall@5": round(sum(recalls) / len(recalls), 4)})
    # 参考：埋め込み類似度の上位 5 件（構成 A 相当）
    precisions, recalls = [], []
    for q in qa:
        gold = set(q["evidence_chunk_ids"])
        if gold:
            top = [h["id"] for h in hits[q["qid"]][:MAX_CHUNKS]]
            precisions.append(len(gold & set(top)) / len(top))
            recalls.append(len(gold & set(top)) / len(gold))
    rows_d.append({"variant": "embedding_top5", "auc": None,
                   "context_precision@5": round(sum(precisions) / len(precisions), 4),
                   "context_recall@5": round(sum(recalls) / len(recalls), 4)})
    df_d = pd.DataFrame(rows_d)
    print("\n構成 D（usefulness）:\n", df_d.to_string(index=False))
    await client.aclose()

    df_b.to_csv(OUT / "b_relevant.csv", index=False)
    df_c.to_csv(OUT / "c_sufficiency.csv", index=False)
    df_d.to_csv(OUT / "d_usefulness.csv", index=False)
    (OUT / "summary.md").write_text(
        "## 構成 B（relevant）\n\n" + df_b.to_markdown(index=False)
        + f"\n\n最良: {dict(best_b)}\n\n## 構成 C（sufficiency）\n\n" + df_c.to_markdown(index=False)
        + "\n\n## 構成 D（usefulness）\n\n" + df_d.to_markdown(index=False) + "\n",
        encoding="utf-8",
    )
    print(f"\nD1 入力 {d1_tokens:,} トークン（約 ${d1_tokens * 0.042 / 1e6:.4f}）")


if __name__ == "__main__":
    asyncio.run(main_async())
