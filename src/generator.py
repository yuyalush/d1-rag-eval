"""T11 generator：渡されたチャンクだけを根拠に gen デプロイで回答する（全構成で共通のプロンプト）。"""

from __future__ import annotations

import time
from dataclasses import dataclass

import yaml

from src.clients import openai_client
from src.config import ROOT, load_settings

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


def model_params(deployment: str, max_tokens: int) -> dict:
    """config/models.yaml の設定から、モデルごとに呼び出しのパラメーターを決める。

    - gpt-6 系（gen）：temperature=0、reasoning_effort=none
    - gpt-5 系（gen-nano）：推論モデルは temperature を指定できない。推論トークンも上限に含まれるので上限を広げる
    - Phi などの非推論モデル（gen-phi）：temperature=0、max_tokens
    """
    models = yaml.safe_load((ROOT / "config/models.yaml").read_text(encoding="utf-8"))
    d = next((m for m in models["deployments"] if m["name"] == deployment), {})
    effort = d.get("reasoning_effort")
    if d.get("format", "OpenAI") != "OpenAI":
        return {"temperature": 0, "max_tokens": max_tokens}
    if str(d.get("model", "")).startswith("gpt-5"):
        return {"reasoning_effort": effort or "minimal", "max_completion_tokens": max_tokens * 3}
    return {"temperature": 0, "reasoning_effort": effort or "none", "max_completion_tokens": max_tokens}


class Generator:
    def __init__(self, max_tokens: int = 800, deployment: str | None = None) -> None:
        self.settings = load_settings()
        self.client = openai_client()
        self.deployment = deployment or self.settings.gen_deployment
        self.params = model_params(self.deployment, max_tokens)

    def generate(self, question: str, passages: list[str]) -> Generation:
        start = time.perf_counter()
        r = self.client.chat.completions.create(
            model=self.deployment,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": build_user_prompt(question, passages)},
            ],
            **self.params,
        )
        return Generation(
            answer=(r.choices[0].message.content or "").strip(),
            input_tokens=r.usage.prompt_tokens,
            output_tokens=r.usage.completion_tokens,
            latency_ms=(time.perf_counter() - start) * 1000,
        )
