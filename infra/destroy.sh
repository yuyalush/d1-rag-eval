#!/usr/bin/env bash
# リソースグループごと削除し、論理削除された Foundry アカウントも完全に削除（purge）する。
# 予算アラートも RG 内にあるため一緒に消える。
set -euo pipefail

RG=${RG:-rg-d1rag-eval}

if [[ "$(az group exists --name "$RG")" != "true" ]]; then
  echo "$RG は存在しません"
  exit 0
fi

ACCOUNTS=$(az cognitiveservices account list --resource-group "$RG" --query "[].[name,location]" -o tsv)

read -r -p "$RG を削除します。よろしいですか？ [y/N] " answer
[[ "$answer" == "y" || "$answer" == "Y" ]] || exit 1

az group delete --name "$RG" --yes

while read -r name location; do
  [[ -z "$name" ]] && continue
  echo "purge: $name ($location)"
  az cognitiveservices account purge --name "$name" --resource-group "$RG" --location "$location"
done <<< "$ACCOUNTS"

echo "削除が完了しました。予算アラートが残っていないか確認してください:"
echo "  az consumption budget list -o table"
