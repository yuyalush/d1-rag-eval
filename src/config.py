""".env と config.yaml の読み込み。"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class Settings:
    foundry_endpoint: str
    decision_deployment: str
    embed_deployment: str
    gen_deployment: str
    judge_deployment: str
    pg_host: str
    pg_db: str
    pg_user: str
    pg_password: str | None  # 設定時はパスワード認証（ローカル Docker）、未設定時は Entra 認証（Azure）


def load_settings() -> Settings:
    load_dotenv(ROOT / ".env")
    return Settings(
        foundry_endpoint=os.environ["FOUNDRY_ENDPOINT"],
        decision_deployment=os.getenv("DECISION_DEPLOYMENT", "decision-eval"),
        embed_deployment=os.getenv("EMBED_DEPLOYMENT", "embed"),
        gen_deployment=os.getenv("GEN_DEPLOYMENT", "gen"),
        judge_deployment=os.getenv("JUDGE_DEPLOYMENT", "judge"),
        pg_host=os.getenv("PG_HOST", "localhost"),
        pg_db=os.getenv("PG_DB", "ragdb"),
        pg_user=os.getenv("PG_USER", "rag"),
        pg_password=os.getenv("PG_PASSWORD") or None,
    )


def load_config(path: Path | None = None) -> dict:
    with open(path or ROOT / "config.yaml", encoding="utf-8") as f:
        return yaml.safe_load(f)
