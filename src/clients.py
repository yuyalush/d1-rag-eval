"""Foundry（OpenAI 互換 API）と PostgreSQL への接続。認証は Entra ID（DefaultAzureCredential）。"""

from __future__ import annotations

from functools import cache

import psycopg
from azure.identity import DefaultAzureCredential, get_bearer_token_provider
from openai import OpenAI
from pgvector.psycopg import register_vector

from src.config import load_settings

COGNITIVE_SCOPE = "https://cognitiveservices.azure.com/.default"
PG_SCOPE = "https://ossrdbms-aad.database.windows.net/.default"


@cache
def credential() -> DefaultAzureCredential:
    return DefaultAzureCredential()


@cache
def token_provider():
    return get_bearer_token_provider(credential(), COGNITIVE_SCOPE)


@cache
def openai_client() -> OpenAI:
    s = load_settings()
    return OpenAI(base_url=f"{s.foundry_endpoint.rstrip('/')}/openai/v1/", api_key=token_provider(), max_retries=5)


def pg_connect(register: bool = True) -> psycopg.Connection:
    """ローカル（PG_PASSWORD あり）はパスワード、Azure は Entra トークンで接続する。"""
    s = load_settings()
    conn = psycopg.connect(
        host=s.pg_host,
        dbname=s.pg_db,
        user=s.pg_user,
        password=s.pg_password or credential().get_token(PG_SCOPE).token,
        sslmode="disable" if s.pg_host in ("localhost", "127.0.0.1") else "require",
        connect_timeout=10,
        autocommit=True,
    )
    if register:
        register_vector(conn)
    return conn
