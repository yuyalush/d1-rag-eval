"""T10 Decision-1 クライアント。

- noul / choice / score の質問を組み立て、1 リクエストに複数の質問をまとめて送る
- レスポンスを型付きオブジェクトに変換する（形は T04 の疎通確認で確認したもの）
- 429・5xx・JSON 以外の応答は指数バックオフで再試行し、並列数はセマフォで制限する
"""

from __future__ import annotations

import asyncio
import json
import random
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

from src.clients import token_provider
from src.config import load_settings

RETRY_STATUS = {408, 429, 500, 502, 503, 504}


# ---- 質問の組み立て ----------------------------------------------------------

def noul(instructions: str, true: str | None = None, false: str | None = None) -> dict[str, Any]:
    q: dict[str, Any] = {"type": "noul", "instructions": instructions}
    if true and false:
        q["criteria"] = {"true": true, "false": false}
    return q


def choice(instructions: str, options: dict[str, str]) -> dict[str, Any]:
    return {"type": "choice", "instructions": instructions, "criteria": options}


def score(instructions: str, levels: list[str]) -> dict[str, Any]:
    """levels は低い順に並べる。"""
    return {"type": "score", "instructions": instructions, "criteria": levels}


# ---- レスポンスの型 ----------------------------------------------------------

@dataclass
class NoulAnswer:
    probability: float


@dataclass
class ChoiceAnswer:
    choice: str
    confidence: float
    probabilities: dict[str, float]


@dataclass
class ScoreAnswer:
    score: float  # レベル番号（0 始まり）の確率加重平均
    confidence: float
    probabilities: dict[int, float]
    legend: dict[int, str]


Answer = NoulAnswer | ChoiceAnswer | ScoreAnswer


@dataclass
class DecisionResult:
    answers: dict[str, Answer]
    input_tokens: int
    latency_ms: float
    retries: int = 0
    raw: dict[str, Any] = field(default_factory=dict, repr=False)


class DecisionError(RuntimeError):
    pass


def parse_answer(name: str, a: dict[str, Any]) -> Answer:
    kind = a.get("type")
    if kind == "noul":
        return NoulAnswer(float(a["noul"]))
    if kind == "choice":
        return ChoiceAnswer(a["choice"], float(a["confidence"]), {k: float(v) for k, v in a["probabilities"].items()})
    if kind == "score":
        return ScoreAnswer(
            float(a["score"]),
            float(a["confidence"]),
            {int(k): float(v) for k, v in a["probabilities"].items()},
            {int(k): v for k, v in a.get("legend", {}).items()},
        )
    raise DecisionError(f"unknown answer type for {name!r}: {kind!r}")


def parse_response(payload: dict[str, Any], latency_ms: float = 0.0, retries: int = 0) -> DecisionResult:
    answers = payload.get("answers")
    if not isinstance(answers, dict):
        raise DecisionError(f"no answers in response: {payload!r}")
    usage = payload.get("usage") or {}
    return DecisionResult(
        answers={name: parse_answer(name, a) for name, a in answers.items()},
        input_tokens=int(usage.get("input_tokens", 0)),
        latency_ms=latency_ms,
        retries=retries,
        raw=payload,
    )


# ---- クライアント ------------------------------------------------------------

class DecisionClient:
    def __init__(self, concurrency: int = 4, max_retries: int = 6, timeout: float = 60.0) -> None:
        s = load_settings()
        self.url = f"{s.foundry_endpoint.rstrip('/')}/providers/microsoft/v1/systemone"
        self.deployment = s.decision_deployment
        self.max_retries = max_retries
        self.semaphore = asyncio.Semaphore(concurrency)
        self.http = httpx.AsyncClient(timeout=timeout)
        self.get_token = token_provider()

    async def decide(self, state: str | dict[str, Any], questions: dict[str, dict[str, Any]]) -> DecisionResult:
        body = {"model": self.deployment, "state": state, "questions": questions}
        async with self.semaphore:
            for attempt in range(self.max_retries + 1):
                start = time.perf_counter()
                try:
                    r = await self.http.post(self.url, json=body, headers={"Authorization": f"Bearer {self.get_token()}"})
                    if r.status_code in RETRY_STATUS:
                        raise _Retryable(f"HTTP {r.status_code}", r.headers.get("retry-after"))
                    if r.status_code >= 400:
                        raise DecisionError(f"HTTP {r.status_code}: {r.text[:300]}")
                    try:
                        payload = r.json()
                    except json.JSONDecodeError:
                        raise _Retryable("non-JSON response", None) from None
                    return parse_response(payload, (time.perf_counter() - start) * 1000, attempt)
                except (_Retryable, httpx.TransportError) as e:
                    if attempt == self.max_retries:
                        raise DecisionError(f"gave up after {attempt + 1} attempts: {e}") from e
                    retry_after = getattr(e, "retry_after", None)
                    delay = float(retry_after) if retry_after else min(2**attempt, 30) + random.random()
                    await asyncio.sleep(delay)
        raise AssertionError("unreachable")

    async def decide_many(
        self, items: list[tuple[str | dict[str, Any], dict[str, dict[str, Any]]]]
    ) -> list[DecisionResult]:
        """(state, questions) のリストを並列（concurrency 上限）で判定する。順序は入力と同じ。"""
        return await asyncio.gather(*(self.decide(s, q) for s, q in items))

    async def aclose(self) -> None:
        await self.http.aclose()


class _Retryable(Exception):
    def __init__(self, message: str, retry_after: str | None) -> None:
        super().__init__(message)
        self.retry_after = retry_after
