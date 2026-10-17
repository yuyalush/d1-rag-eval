"""T06 取り込み：data/chunks.jsonl を埋め込み、pgvector の chunks テーブルへ投入する。

- テーブルと HNSW インデックスは db/schema.sql で作る（なければ作成）
- 再実行時は、未投入または本文が変わったチャンクだけ埋め込む（差分のみ）
- chunks.jsonl にないチャンクは削除する

    uv run python -m src.ingest
"""

from __future__ import annotations

import hashlib
import json
import time

import numpy as np

from src.clients import openai_client, pg_connect
from src.config import ROOT, load_settings

CHUNKS = ROOT / "data/chunks.jsonl"
BATCH = 64


def md5(text: str) -> str:
    return hashlib.md5(text.encode("utf-8")).hexdigest()


def main() -> None:
    settings = load_settings()
    chunks = [json.loads(line) for line in CHUNKS.read_text(encoding="utf-8").splitlines()]
    broken = [c["id"] for c in chunks if "�" in c["text"]]
    if broken:
        raise SystemExit(f"壊れた文字（U+FFFD）を含むチャンクが {len(broken)} 件あります: {broken[:10]}。src.chunking を再実行してください")

    with pg_connect(register=False) as conn:
        conn.execute((ROOT / "db/schema.sql").read_text(encoding="utf-8"))
    with pg_connect() as conn:
        existing = dict(conn.execute("SELECT id, md5(content) FROM chunks").fetchall())
        wanted = {c["id"] for c in chunks}
        stale = [i for i in existing if i not in wanted]
        if stale:
            conn.execute("DELETE FROM chunks WHERE id = ANY(%s)", (stale,))
        todo = [c for c in chunks if existing.get(c["id"]) != md5(c["text"])]
        print(f"チャンク {len(chunks)} 件：投入済み {len(chunks) - len(todo)}、埋め込み対象 {len(todo)}、削除 {len(stale)}")

        client = openai_client()
        total_tokens = 0
        start = time.perf_counter()
        for i in range(0, len(todo), BATCH):
            batch = todo[i : i + BATCH]
            r = client.embeddings.create(model=settings.embed_deployment, input=[c["text"] for c in batch])
            total_tokens += r.usage.prompt_tokens
            with conn.cursor() as cur:
                cur.executemany(
                    """INSERT INTO chunks (id, doc_id, content, embedding) VALUES (%s, %s, %s, %s)
                       ON CONFLICT (id) DO UPDATE SET doc_id = EXCLUDED.doc_id, content = EXCLUDED.content,
                       embedding = EXCLUDED.embedding""",
                    [(c["id"], c["doc_id"], c["text"], np.array(e.embedding, dtype=np.float32)) for c, e in zip(batch, r.data)],
                )
            if (i // BATCH) % 10 == 0:
                print(f"  {min(i + BATCH, len(todo))}/{len(todo)}")

        count = conn.execute("SELECT count(*) FROM chunks").fetchone()[0]
        index = conn.execute(
            "SELECT indexname FROM pg_indexes WHERE tablename = 'chunks' AND indexdef ILIKE '%hnsw%'"
        ).fetchone()
    print(
        f"完了：DB 件数 {count}（jsonl {len(chunks)}、{'一致' if count == len(chunks) else '不一致'}）、"
        f"HNSW インデックス {index[0] if index else 'なし'}、"
        f"埋め込み {total_tokens:,} トークン（約 ${total_tokens * 0.02 / 1e6:.4f}）、{time.perf_counter() - start:.0f} 秒"
    )


if __name__ == "__main__":
    main()
