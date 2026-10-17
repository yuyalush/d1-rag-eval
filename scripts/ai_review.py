"""T08 の一次レビュー（AI）用の補助スクリプト。人が最終確認する前に、質問ごとの判断材料と提案をまとめる。

    uv run python -m scripts.ai_review material t001 t010   # t001〜t010 の判断材料を表示
    uv run python -m scripts.ai_review export                # data/qa/test_ai_review.jsonl から CSV を作る
    uv run python -m scripts.ai_review apply                 # 人が確認した test_ai_review.csv を test.jsonl に反映

判断材料：質問・模範解答、根拠チャンクの全文と前後のチャンク（先頭 300 文字）、囮チャンクの全文、
回答不能の質問は実際の検索上位 5 件（先頭 400 文字）。
提案（data/qa/test_ai_review.jsonl、1 行 1 問）：{"qid", "ai_status": ok|fix|drop, "ai_fixed_question",
"ai_fixed_answer", "ai_fixed_evidence", "ai_reason"}
出力（data/qa/test_ai_review.csv）：レビュー用 CSV と同じ列に ai_* 列を足したもの。
人の確認：AI の提案に同意する行は status を空欄のまま、同意しない行だけ status（ok/fix/drop）を記入する。
修正内容は ai_fixed_* 列を直接書き換えてよい。apply は status が空欄なら ai_status を採用する。
"""

from __future__ import annotations

import csv
import json
import sys

from src.config import ROOT

QA = ROOT / "data/qa"
CHUNKS = ROOT / "data/chunks.jsonl"
AI = QA / "test_ai_review.jsonl"


def load():
    qa = {q["qid"]: q for q in map(json.loads, (QA / "test.jsonl").read_text(encoding="utf-8").splitlines())}
    chunks = [json.loads(line) for line in CHUNKS.read_text(encoding="utf-8").splitlines()]
    return qa, chunks


def material(start: str, end: str) -> None:
    qa, chunks = load()
    index = {c["id"]: i for i, c in enumerate(chunks)}
    retriever = None
    for qid in sorted(q for q in qa if start <= q <= end):
        q = qa[qid]
        print(f"\n################ {qid} [{q['type']}]")
        print(f"Q: {q['question']}\nA: {q['answer']}")
        for cid in q["evidence_chunk_ids"]:
            i = index[cid]
            prev_c = chunks[i - 1] if i > 0 and chunks[i - 1]["doc_id"] == chunks[i]["doc_id"] else None
            next_c = chunks[i + 1] if i + 1 < len(chunks) and chunks[i + 1]["doc_id"] == chunks[i]["doc_id"] else None
            print(f"\n--- 根拠 {cid} ({chunks[i]['doc_id']})\n{chunks[i]['text']}")
            if prev_c:
                print(f"\n  (前 {prev_c['id']}) {prev_c['text'][:300]!r}")
            if next_c:
                print(f"  (次 {next_c['id']}) {next_c['text'][:300]!r}")
        distractor = q.get("meta", {}).get("distractor")
        if distractor:
            print(f"\n--- 囮 {distractor} ({chunks[index[distractor]]['doc_id']})\n{chunks[index[distractor]]['text']}")
        if q["type"] == "unanswerable":
            if retriever is None:
                from src.retriever import Retriever
                retriever = Retriever()
            for h in retriever.search(q["question"], 5).hits:
                print(f"\n--- 検索 {h.id} sim={h.score:.3f} ({h.doc_id})\n{h.content[:400]}")
    if retriever:
        retriever.close()


def export() -> None:
    qa, chunks = load()
    ai = {r["qid"]: r for r in map(json.loads, AI.read_text(encoding="utf-8").splitlines())}
    text = {c["id"]: c["text"] for c in chunks}
    out = QA / "test_ai_review.csv"
    cols = ["qid", "type", "question", "answer", "evidence_chunk_ids", "ai_status", "ai_fixed_question",
            "ai_fixed_answer", "ai_fixed_evidence", "ai_reason", "status", "note", "evidence_text", "distractor_text"]
    with out.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        for qid in sorted(qa):
            q, a = qa[qid], ai.get(qid, {})
            d = q.get("meta", {}).get("distractor")
            w.writerow({
                "qid": qid, "type": q["type"], "question": q["question"], "answer": q["answer"],
                "evidence_chunk_ids": " ".join(q["evidence_chunk_ids"]),
                "ai_status": a.get("ai_status", ""), "ai_fixed_question": a.get("ai_fixed_question", ""),
                "ai_fixed_answer": a.get("ai_fixed_answer", ""), "ai_fixed_evidence": a.get("ai_fixed_evidence", ""),
                "ai_reason": a.get("ai_reason", ""), "status": "", "note": "",
                "evidence_text": "\n---\n".join(f"[{c}] {text[c]}" for c in q["evidence_chunk_ids"]),
                "distractor_text": f"[{d}] {text[d]}" if d else "",
            })
    missing = sorted(set(qa) - set(ai))
    print(f"{out} を作成（AI 判断 {len(ai)} 問、未判断 {len(missing)} 問{': ' + ' '.join(missing[:10]) if missing else ''}）")


def apply() -> None:
    from scripts.review_sheet import import_

    with (QA / "test_ai_review.csv").open(encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))
    reviews = []
    overridden = 0
    for r in rows:
        status = r["status"].strip() or r["ai_status"].strip()
        overridden += bool(r["status"].strip()) and r["status"].strip() != r["ai_status"].strip()
        note = r["note"].strip() or f"AI: {r['ai_reason'].strip()}"
        reviews.append({"qid": r["qid"], "status": status, "fixed_question": r["ai_fixed_question"],
                        "fixed_answer": r["ai_fixed_answer"], "fixed_evidence": r["ai_fixed_evidence"], "note": note})
    print(f"人が AI の提案を変更した行: {overridden}")
    import_("test", reviews)


if __name__ == "__main__":
    {"material": lambda: material(sys.argv[2], sys.argv[3]), "export": export, "apply": apply}[sys.argv[1]]()
