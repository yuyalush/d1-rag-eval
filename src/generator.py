"""T11 generator：渡されたチャンクだけを根拠に gen デプロイで回答する（全構成で共通のプロンプト）。"""

from __future__ import annotations

import time
from dataclasses import dataclass

from src.clients import openai_client
from src.config import load_settings

UNKNOWN = "わかりません"

SYSTEM_PROMPT = f"""あなたは Azure Storage のドキュメントに基づいて質問に答えるアシスタントです。
次のルールを必ず守ってください。
- 回答は「参考情報」に書かれている内容だけを根拠にする。参考情報にないことは推測しない。
- 参考情報に答えがない、または参考情報が空の場合は「{UNKNOWN}」とだけ答える。
- 回答は日本語で、必要なことだけを簡潔に書く。"""


@dataclass
class Generation:
    answer: str
    input_tokens: int
    output_tokens: int
    latency_ms: float

    @property
    def is_unknown(self) -> bool:
        return self.answer.strip().startswith(UNKNOWN)


def build_user_prompt(question: str, passages: list[str]) -> str:
    if passages:
        context = "\n\n".join(f"[{i}]\n{p}" for i, p in enumerate(passages, 1))
    else:
        context = "（なし）"
    return f"# 参考情報\n{context}\n\n# 質問\n{question}"


class Generator:
    def __init__(self, max_tokens: int = 800) -> None:
        self.settings = load_settings()
        self.client = openai_client()
        self.max_tokens = max_tokens

    def generate(self, question: str, passages: list[str]) -> Generation:
        start = time.perf_counter()
        r = self.client.chat.completions.create(
            model=self.settings.gen_deployment,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": build_user_prompt(question, passages)},
            ],
            temperature=0,
            reasoning_effort="none",
            max_completion_tokens=self.max_tokens,
        )
        return Generation(
            answer=(r.choices[0].message.content or "").strip(),
            input_tokens=r.usage.prompt_tokens,
            output_tokens=r.usage.completion_tokens,
            latency_ms=(time.perf_counter() - start) * 1000,
        )
