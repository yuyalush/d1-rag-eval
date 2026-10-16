"""T04 疎通確認：Decision-1、埋め込み、gen、judge、PostgreSQL を 1 回ずつ呼ぶ。

各呼び出しのトークン数と config/models.yaml の単価から 1 回あたりの費用を表示する。
レスポンスの形を確認するため、Decision-1 の生レスポンスも表示する。

    uv run python -m scripts.smoke_test
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable

import httpx
import psycopg
import yaml
from azure.identity import DefaultAzureCredential, get_bearer_token_provider
from openai import OpenAI

from src.config import ROOT, load_settings

SCOPE = "https://cognitiveservices.azure.com/.default"

settings = load_settings()
credential = DefaultAzureCredential()
token_provider = get_bearer_token_provider(credential, SCOPE)
client = OpenAI(base_url=f"{settings.foundry_endpoint.rstrip('/')}/openai/v1/", api_key=token_provider)

models = yaml.safe_load((ROOT / "config/models.yaml").read_text(encoding="utf-8"))
prices = {d["name"]: d["price"] for d in models["deployments"]}


def cost_usd(deployment: str, input_tokens: int, output_tokens: int = 0) -> float:
    p = prices[deployment]
    return (input_tokens * p["input"] + output_tokens * p["output"]) / 1_000_000


def check_decision() -> str:
    body = {
        "model": settings.decision_deployment,
        "state": {
            "question": "Azure Blob Storage のアクセス層にはどんな種類がありますか？",
            "passage": "Blob Storage にはホット、クール、コールド、アーカイブの 4 つのアクセス層があります。",
        },
        "questions": {
            "relevant": {
                "type": "noul",
                "instructions": "Does the passage contain information needed to answer the question?",
                "criteria": {
                    "true": "The passage states facts that directly answer all or part of the question",
                    "false": "The passage is off-topic or only shares keywords with the question",
                },
            }
        },
    }
    r = httpx.post(
        f"{settings.foundry_endpoint.rstrip('/')}/providers/microsoft/v1/systemone",
        json=body,
        headers={"Authorization": f"Bearer {token_provider()}"},
        timeout=60,
    )
    r.raise_for_status()
    payload = r.json()
    print("    raw:", json.dumps(payload, ensure_ascii=False))
    usage = payload.get("usage", {})
    in_tok = usage.get("input_tokens") or usage.get("prompt_tokens") or 0
    return f"answers={json.dumps(payload['answers'], ensure_ascii=False)} tokens={in_tok} cost=${cost_usd('decision-eval', in_tok):.8f}"


def check_embed() -> str:
    r = client.embeddings.create(model=settings.embed_deployment, input="Azure Blob Storage のアクセス層")
    in_tok = r.usage.prompt_tokens
    return f"dim={len(r.data[0].embedding)} tokens={in_tok} cost=${cost_usd('embed', in_tok):.8f}"


def check_chat(deployment_key: str, deployment: str) -> str:
    r = client.chat.completions.create(
        model=deployment,
        messages=[{"role": "user", "content": "「疎通確認OK」とだけ答えてください。"}],
        reasoning_effort="none",
        max_completion_tokens=20,
    )
    u = r.usage
    return (
        f"model={r.model} reply={r.choices[0].message.content!r} "
        f"tokens={u.prompt_tokens}/{u.completion_tokens} "
        f"cost=${cost_usd(deployment_key, u.prompt_tokens, u.completion_tokens):.8f}"
    )


def check_postgres() -> str:
    password = settings.pg_password or credential.get_token("https://ossrdbms-aad.database.windows.net/.default").token
    with psycopg.connect(
        host=settings.pg_host,
        dbname=settings.pg_db,
        user=settings.pg_user,
        password=password,
        sslmode="disable" if settings.pg_host == "localhost" else "require",
        connect_timeout=10,
    ) as conn:
        version = conn.execute("SELECT extversion FROM pg_extension WHERE extname = 'vector'").fetchone()
        count = conn.execute("SELECT count(*) FROM chunks").fetchone()
    return f"host={settings.pg_host} pgvector={version[0] if version else None} chunks={count[0]}"


def main() -> None:
    checks: list[tuple[str, Callable[[], str]]] = [
        ("Decision-1", check_decision),
        ("embed", check_embed),
        ("gen", lambda: check_chat("gen", settings.gen_deployment)),
        ("judge", lambda: check_chat("judge", settings.judge_deployment)),
        ("PostgreSQL", check_postgres),
    ]
    ok = 0
    for name, fn in checks:
        start = time.perf_counter()
        try:
            detail = fn()
            ok += 1
            print(f"[OK]   {name:11} {(time.perf_counter() - start) * 1000:7.0f} ms  {detail}")
        except Exception as e:  # 疎通確認なので全件の結果を出す
            print(f"[FAIL] {name:11} {type(e).__name__}: {e}")
    print(f"{ok}/{len(checks)} OK")


if __name__ == "__main__":
    main()
