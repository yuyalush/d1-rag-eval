#!/usr/bin/env bash
# 使い方:
#   infra/deploy.sh            RG 作成 → デプロイ → 出力値を .env へ書き出す
#   infra/deploy.sh what-if    変更内容の確認のみ（RG は作成される）
# 環境変数:
#   RG=rg-d1rag-eval  RG_LOCATION=japaneast  DEPLOY_POSTGRES=false  BUDGET_AMOUNT=10000
set -euo pipefail
cd "$(dirname "$0")/.."
[[ -f .env ]] || cp .env.example .env

MODE=${1:-create}
RG=${RG:-rg-d1rag-eval}
RG_LOCATION=${RG_LOCATION:-japaneast}
DEPLOY_POSTGRES=${DEPLOY_POSTGRES:-false}
BUDGET_AMOUNT=${BUDGET_AMOUNT:-10000}
WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT

# WSL から Windows 版 az を使う場合はファイルパスを Windows 形式にする
winpath() {
  if command -v wslpath >/dev/null 2>&1 && [[ "$(command -v az)" == /mnt/* ]]; then
    wslpath -w "$1"
  else
    echo "$1"
  fi
}

# 自分のオブジェクト ID とメールはアクセストークンから取る（Graph の権限が不要）
read -r PRINCIPAL_ID USER_NAME < <(az account get-access-token --query accessToken -o tsv | uv run python -c '
import base64, json, sys
p = sys.stdin.read().strip().split(".")[1]
c = json.loads(base64.urlsafe_b64decode(p + "=" * (-len(p) % 4)))
name = c.get("upn") or c.get("unique_name") or c.get("email") or ""
print(c["oid"], name.split("#")[-1])
')
CLIENT_IP=$(curl -fsS https://api.ipify.org)

uv run python infra/helpers.py params "$WORK/params.json" \
  "principalId=$PRINCIPAL_ID" \
  "budgetContactEmails=[\"$USER_NAME\"]" \
  "budgetAmount=$BUDGET_AMOUNT" \
  "deployPostgres=$DEPLOY_POSTGRES" \
  "pgAdminName=$USER_NAME" \
  "clientIp=$CLIENT_IP"

az group create --name "$RG" --location "$RG_LOCATION" --output none

TEMPLATE=$(winpath infra/main.bicep)
PARAMS=$(winpath "$WORK/params.json")

if [[ "$MODE" == "what-if" ]]; then
  az deployment group what-if --resource-group "$RG" --template-file "$TEMPLATE" --parameters "@$PARAMS"
  exit 0
fi

az deployment group create \
  --resource-group "$RG" \
  --name d1rag-main \
  --template-file "$TEMPLATE" \
  --parameters "@$PARAMS" \
  --query properties.outputs -o json > "$WORK/outputs.json"

echo ".env を更新しました:"
uv run python infra/helpers.py env "$WORK/outputs.json"
