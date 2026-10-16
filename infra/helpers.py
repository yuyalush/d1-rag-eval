"""deploy.sh から呼ぶ補助処理。

  params <out.json> [key=value ...]  config/models.yaml からデプロイパラメータファイルを作る
  env <outputs.json>                 デプロイ出力を .env に書き込む（既存の他の値は残す）
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
DEPLOYMENT_KEYS = ("name", "model", "format", "version", "sku", "capacity")


def write_params(out: str, extra: list[str]) -> None:
    models = yaml.safe_load((ROOT / "config/models.yaml").read_text(encoding="utf-8"))
    params: dict[str, object] = {
        "foundryLocation": models["foundry_region"],
        "pgLocation": models["postgres_region"],
        "deployments": [{k: d[k] for k in DEPLOYMENT_KEYS} for d in models["deployments"]],
    }
    for kv in extra:
        key, value = kv.split("=", 1)
        try:
            params[key] = json.loads(value)  # 数値・真偽値・配列
        except json.JSONDecodeError:
            params[key] = value
    body = {
        "$schema": "https://schema.management.azure.com/schemas/2019-04-01/deploymentParameters.json#",
        "contentVersion": "1.0.0.0",
        "parameters": {k: {"value": v} for k, v in params.items()},
    }
    Path(out).write_text(json.dumps(body, ensure_ascii=False, indent=2), encoding="utf-8")


def write_env(outputs_file: str) -> None:
    outputs = json.loads(Path(outputs_file).read_text(encoding="utf-8-sig"))
    updates = {"FOUNDRY_ENDPOINT": outputs["FOUNDRY_ENDPOINT"]["value"]}
    if outputs["PG_HOST"]["value"]:
        updates["PG_HOST"] = outputs["PG_HOST"]["value"]

    env_path = ROOT / ".env"
    if not env_path.exists():
        env_path.write_text((ROOT / ".env.example").read_text(encoding="utf-8"), encoding="utf-8")
    lines = env_path.read_text(encoding="utf-8").splitlines()
    for key, value in updates.items():
        for i, line in enumerate(lines):
            if line.startswith(f"{key}="):
                lines[i] = f"{key}={value}"
                break
        else:
            lines.append(f"{key}={value}")
    env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    for key, value in updates.items():
        print(f"  {key}={value}")


if __name__ == "__main__":
    cmd, *args = sys.argv[1:]
    if cmd == "params":
        write_params(args[0], args[1:])
    elif cmd == "env":
        write_env(args[0])
    else:
        sys.exit(f"unknown command: {cmd}")
