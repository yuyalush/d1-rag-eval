"""T08 人手レビュー用シート：テスト用の質問を CSV に書き出し、修正結果を JSONL へ戻す。

    uv run python -m scripts.review_sheet export          # data/qa/test_review.csv を作る（Excel で開ける UTF-8 BOM 付き）
    uv run python -m scripts.review_sheet import          # CSV の修正を data/qa/test.jsonl に反映（元は test.orig.jsonl に退避）
    uv run python -m scripts.review_sheet show c01926 ... # チャンクの全文と元記事の URL を表示

CSV の記入方法
- status：ok（そのまま）／fix（fixed_question・fixed_answer・fixed_evidence を反映）／drop（除外）
- fixed_evidence：根拠チャンク ID をスペース区切り（回答不能は空）
- 根拠の本文は evidence_text 列で確認する（判断しやすいよう先頭 600 文字）
"""

from __future__ import annotations

import csv
import json
import shutil
import sys

from src.config import ROOT

QA = ROOT / "data/qa"
CHUNKS = ROOT / "data/chunks.jsonl"
COLUMNS = ["qid", "type", "question", "answer", "evidence_chunk_ids", "evidence_text", "distractor_text",
           "status", "fixed_question", "fixed_answer", "fixed_evidence", "note"]


def load_chunks() -> dict[str, str]:
    return {d["id"]: d["text"] for d in map(json.loads, CHUNKS.read_text(encoding="utf-8").splitlines())}


def export(split: str = "test") -> None:
    chunks = load_chunks()
    rows = [json.loads(line) for line in (QA / f"{split}.jsonl").read_text(encoding="utf-8").splitlines()]
    out = QA / f"{split}_review.csv"
    with out.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=COLUMNS)
        w.writeheader()
        for r in sorted(rows, key=lambda r: (r["type"], r["qid"])):
            distractor = r.get("meta", {}).get("distractor")
            w.writerow({
                "qid": r["qid"], "type": r["type"], "question": r["question"], "answer": r["answer"],
                "evidence_chunk_ids": " ".join(r["evidence_chunk_ids"]),
                "evidence_text": "\n---\n".join(f"[{cid}] {chunks[cid][:600]}" for cid in r["evidence_chunk_ids"]),
                "distractor_text": f"[{distractor}] {chunks[distractor][:600]}" if distractor else "",
                "status": "", "fixed_question": "", "fixed_answer": "", "fixed_evidence": "", "note": "",
            })
    print(f"{out} に {len(rows)} 問を書き出しました")


def import_(split: str = "test", reviews: list[dict] | None = None) -> None:
    src = QA / f"{split}.jsonl"
    rows = {r["qid"]: r for r in map(json.loads, src.read_text(encoding="utf-8").splitlines())}
    if reviews is None:
        with (QA / f"{split}_review.csv").open(encoding="utf-8-sig", newline="") as f:
            reviews = list(csv.DictReader(f))
    missing = [r["qid"] for r in reviews if r["status"].strip() not in ("ok", "fix", "drop")]
    if missing:
        sys.exit(f"status が未記入または不正な行があります: {missing[:10]}{' ...' if len(missing) > 10 else ''}")

    chunks = load_chunks()
    counts = {"ok": 0, "fix": 0, "drop": 0}
    out = []
    for rv in reviews:
        status = rv["status"].strip()
        counts[status] += 1
        if status == "drop":
            continue
        r = rows[rv["qid"]]
        if status == "fix":
            r["question"] = rv["fixed_question"].strip() or r["question"]
            r["answer"] = rv["fixed_answer"].strip() or r["answer"]
            if rv["fixed_evidence"].strip():
                ids = rv["fixed_evidence"].split()
                unknown = [i for i in ids if i not in chunks]
                if unknown:
                    sys.exit(f"{rv['qid']}: 存在しないチャンク ID {unknown}")
                r["evidence_chunk_ids"] = ids
        r.setdefault("meta", {})["review"] = {"status": status, "note": rv["note"].strip()}
        out.append(r)

    backup = QA / f"{split}.orig.jsonl"
    if not backup.exists():
        shutil.copy(src, backup)
    with src.open("w", encoding="utf-8") as f:
        for r in sorted(out, key=lambda r: r["qid"]):
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    print(f"反映しました: {counts}、残り {len(out)} 問（元データは {backup.name}）")


def show(ids: list[str]) -> None:
    """チャンクの全文と元記事の URL を表示する（レビューで evidence_text の 600 文字では足りないとき用）。"""
    rows = {d["id"]: d for d in map(json.loads, CHUNKS.read_text(encoding="utf-8").splitlines())}
    urls = {d["doc_id"]: d["url"] for d in map(json.loads, (ROOT / "data/raw/manifest.jsonl").read_text(encoding="utf-8").splitlines())}
    for cid in ids:
        if cid not in rows:
            print(f"[{cid}] 見つかりません\n")
            continue
        c = rows[cid]
        print(f"===== [{cid}] {urls.get(c['doc_id'], c['doc_id'])}\n{c['text']}\n")


if __name__ == "__main__":
    if sys.argv[1] == "show":
        show(sys.argv[2:])
    else:
        {"export": export, "import": import_}[sys.argv[1]]()
