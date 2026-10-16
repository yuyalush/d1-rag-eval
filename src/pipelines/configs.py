"""T12 構成 A〜E。共通インターフェースは Pipeline.run(question) -> PipelineResult。"""

from __future__ import annotations

import asyncio
import json

from src.clients import openai_client
from src.config import load_settings
from src.pipelines.base import Pipeline, PipelineResult
from src.retriever import Hit


def record_api_latency(result: PipelineResult, rs: list) -> None:
    """レート制限の待ちを除いた Decision-1 の API 応答時間。並列呼び出しなので最大値が判定段の下限になる。"""
    lat = [r.latency_ms for r in rs]
    result.latency_ms["d1_api_max"] = result.latency_ms.get("d1_api_max", 0.0) + max(lat)
    result.latency_ms["d1_api_sum"] = result.latency_ms.get("d1_api_sum", 0.0) + sum(lat)
    result.decision["d1_retries"] = result.decision.get("d1_retries", 0) + sum(r.retries for r in rs)


class ABaseline(Pipeline):
    """A：上位 5 件をそのまま生成へ。"""

    name = "a_baseline"


class BD1Filter(Pipeline):
    """B：上位 10 件を 1 件ずつ noul で判定し、閾値以上を確率順に最大 5 件。"""

    name = "b_d1_filter"

    async def relevance(self, question: str, hits: list[Hit], result: PipelineResult) -> list[tuple[float, Hit]]:
        q = {"relevant": self.question_def("relevant")}
        rs = await self.decision.decide_many([({"question": question, "passage": h.content}, q) for h in hits])
        probs = [r.answers["relevant"].probability for r in rs]
        result.decision["relevant"] = {h.id: round(p, 5) for h, p in zip(hits, probs)}
        result.tokens["d1_in"] = result.tokens.get("d1_in", 0) + sum(r.input_tokens for r in rs)
        record_api_latency(result, rs)
        threshold = self.params["threshold"]
        kept = sorted(((p, h) for p, h in zip(probs, hits) if p >= threshold), key=lambda x: -x[0])
        return kept[: self.max_chunks]

    async def judge(self, question: str, hits: list[Hit], result: PipelineResult) -> list[Hit]:
        return [h for _, h in await self.relevance(question, hits, result)]


class CD1Gate(BD1Filter):
    """C：B の通過チャンクをまとめて choice で十分性を判定し、「不十分」が最大なら回答を控える。"""

    name = "c_d1_gate"

    async def judge(self, question: str, hits: list[Hit], result: PipelineResult) -> list[Hit]:
        passed = [h for _, h in await self.relevance(question, hits, result)]
        if not passed:  # 根拠ゼロ件なら判定するまでもなく控える
            result.gated = True
            return []
        r = await self.decision.decide(
            {"question": question, "passages": [h.content for h in passed]},
            {"sufficiency": self.question_def("sufficiency")},
        )
        a = r.answers["sufficiency"]
        result.decision["sufficiency"] = {"choice": a.choice, "probabilities": {k: round(v, 5) for k, v in a.probabilities.items()}}
        result.tokens["d1_in"] = result.tokens.get("d1_in", 0) + r.input_tokens
        record_api_latency(result, [r])
        if max(a.probabilities, key=a.probabilities.get) == self.params["abstain_on"]:
            result.gated = True
        return passed


class DD1Rerank(Pipeline):
    """D：上位 20 件を score（4 段階）で採点し、スコア降順で上位 5 件。"""

    name = "d_d1_rerank"

    async def judge(self, question: str, hits: list[Hit], result: PipelineResult) -> list[Hit]:
        q = {"usefulness": self.question_def("usefulness")}
        rs = await self.decision.decide_many([({"question": question, "passage": h.content}, q) for h in hits])
        scores = [r.answers["usefulness"].score for r in rs]
        result.decision["usefulness"] = {h.id: round(s, 5) for h, s in zip(hits, scores)}
        result.tokens["d1_in"] = sum(r.input_tokens for r in rs)
        record_api_latency(result, rs)
        ranked = sorted(zip(scores, range(len(hits)), hits), key=lambda x: (-x[0], x[1]))
        return [h for _, _, h in ranked[: self.max_chunks]]


class ELLMFilter(Pipeline):
    """E：B と同じ判定文を gen デプロイ（汎用 LLM）に与え、関連あり／なしを JSON で返させる。"""

    name = "e_llm_filter"

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.client = openai_client()
        self.deployment = load_settings().gen_deployment
        self.prompt = self.cfg["llm_filter_prompt"]
        self.semaphore = asyncio.Semaphore(self.cfg["common"]["llm_filter_concurrency"])

    def _ask(self, question: str, passage: str) -> tuple[bool, int, int]:
        r = self.client.chat.completions.create(
            model=self.deployment,
            messages=[{"role": "user", "content": self.prompt.format(question=question, passage=passage)}],
            response_format={"type": "json_object"},
            temperature=0,
            reasoning_effort="none",
            max_completion_tokens=20,
        )
        try:
            relevant = bool(json.loads(r.choices[0].message.content or "{}").get("relevant", False))
        except json.JSONDecodeError:
            relevant = False
        return relevant, r.usage.prompt_tokens, r.usage.completion_tokens

    async def judge(self, question: str, hits: list[Hit], result: PipelineResult) -> list[Hit]:
        async def one(h: Hit):
            async with self.semaphore:
                return await asyncio.to_thread(self._ask, question, h.content)

        rs = await asyncio.gather(*(one(h) for h in hits))
        result.decision["llm_relevant"] = {h.id: rel for h, (rel, _, _) in zip(hits, rs)}
        result.tokens["filter_in"] = sum(r[1] for r in rs)
        result.tokens["filter_out"] = sum(r[2] for r in rs)
        # LLM は確率を返さないので、関連ありのものを検索順に最大 5 件
        return [h for h, (rel, _, _) in zip(hits, rs) if rel][: self.max_chunks]


PIPELINES: dict[str, type[Pipeline]] = {p.name: p for p in (ABaseline, BD1Filter, CD1Gate, DD1Rerank, ELLMFilter)}
