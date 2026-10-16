"""T09 retriever：質問を埋め込み、pgvector からコサイン類似度で上位 k 件を返す。"""

from __future__ import annotations

import time
from dataclasses import dataclass

import numpy as np

from src.clients import openai_client, pg_connect
from src.config import load_settings


@dataclass
class Hit:
    id: str
    doc_id: str
    content: str
    score: float  # コサイン類似度（1 - 距離）


@dataclass
class Retrieval:
    hits: list[Hit]
    embed_tokens: int
    latency_ms: float


class Retriever:
    def __init__(self, ef_search: int = 100) -> None:
        self.settings = load_settings()
        self.client = openai_client()
        self.conn = pg_connect()
        # k=20 でも取りこぼさないよう HNSW の探索幅を広げる
        self.conn.execute(f"SET hnsw.ef_search = {int(ef_search)}")

    def search(self, question: str, k: int) -> Retrieval:
        start = time.perf_counter()
        r = self.client.embeddings.create(model=self.settings.embed_deployment, input=question)
        vec = np.array(r.data[0].embedding, dtype=np.float32)
        rows = self.conn.execute(
            "SELECT id, doc_id, content, 1 - (embedding <=> %s) AS score FROM chunks ORDER BY embedding <=> %s LIMIT %s",
            (vec, vec, k),
        ).fetchall()
        return Retrieval(
            hits=[Hit(*row) for row in rows],
            embed_tokens=r.usage.prompt_tokens,
            latency_ms=(time.perf_counter() - start) * 1000,
        )

    def close(self) -> None:
        self.conn.close()
