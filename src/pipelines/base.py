"""構成 A〜E の共通部分：検索 → 判定 → 生成の結果を PipelineResult にまとめ、ログ（T13）に書き出す。"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from src.config import load_config
from src.decision import DecisionClient
from src.generator import Generator
from src.retriever import Hit, Retriever


@dataclass
class PipelineResult:
    config: str
    question: str
    retrieved_ids: list[str]
    retrieved_scores: list[float]
    passed_ids: list[str]                      # LLM へ渡したチャンク
    excluded_ids: list[str]                    # 判定で除外したチャンク
    decision: dict[str, Any] = field(default_factory=dict)   # D1・LLM フィルタの確率など
    answer: str = ""
    abstained: bool = False                    # 回答を控えた（構成 C のゲート、または「わかりません」）
    gated: bool = False                        # 構成 C のゲートで生成しなかった
    latency_ms: dict[str, float] = field(default_factory=dict)
    tokens: dict[str, int] = field(default_factory=dict)
    qid: str = ""
    run_id: str = ""


class Pipeline:
    name = ""

    def __init__(self, retriever: Retriever, generator: Generator, decision: DecisionClient | None = None) -> None:
        self.cfg = load_config()
        self.params = self.cfg["configs"][self.name]
        self.max_chunks = self.cfg["common"]["max_context_chunks"]
        self.retriever = retriever
        self.generator = generator
        self.decision = decision

    def question_def(self, key: str) -> dict[str, Any]:
        return self.cfg["decision_questions"][key]

    async def judge(self, question: str, hits: list[Hit], result: PipelineResult) -> list[Hit]:
        """構成ごとの判定処理。渡すチャンクを返す。"""
        return hits[: self.max_chunks]

    async def run(self, question: str) -> PipelineResult:
        total_start = time.perf_counter()
        ret = await asyncio.to_thread(self.retriever.search, question, self.params["retrieve_k"])
        result = PipelineResult(
            config=self.name,
            question=question,
            retrieved_ids=[h.id for h in ret.hits],
            retrieved_scores=[round(h.score, 5) for h in ret.hits],
            passed_ids=[],
            excluded_ids=[],
            latency_ms={"retrieve": ret.latency_ms},
            tokens={"embed": ret.embed_tokens},
        )

        judge_start = time.perf_counter()
        passed = await self.judge(question, ret.hits, result)
        result.latency_ms["judge"] = (time.perf_counter() - judge_start) * 1000
        result.passed_ids = [h.id for h in passed]
        result.excluded_ids = [h.id for h in ret.hits if h.id not in result.passed_ids]

        if result.gated:
            result.answer, result.abstained = "わかりません", True
            result.latency_ms["generate"] = 0.0
        else:
            gen = await asyncio.to_thread(self.generator.generate, question, [h.content for h in passed])
            result.answer = gen.answer
            result.abstained = gen.is_unknown
            result.latency_ms["generate"] = gen.latency_ms
            result.tokens["gen_in"] = gen.input_tokens
            result.tokens["gen_out"] = gen.output_tokens
        result.latency_ms["total"] = (time.perf_counter() - total_start) * 1000
        result.latency_ms = {k: round(v, 1) for k, v in result.latency_ms.items()}
        return result


def write_log(path: Path, result: PipelineResult) -> None:
    """T13 ログ：1 質問 × 1 構成を results/<run_id>/<config>.jsonl に 1 行で追記する。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(asdict(result), ensure_ascii=False) + "\n")
