# scripts/

評価パイプライン（`src/`）の前後で使う補助スクリプトです。いずれもリポジトリのルートで `uv run python -m scripts.<名前>` の形で実行します（`python scripts/xxx.py` では `src` を読み込めません）。

Azure を呼ぶスクリプトは、`az login` 済みの資格情報（`DefaultAzureCredential`）と `.env` の接続先を使います。WSL では先に `export AZURE_CONFIG_DIR="$HOME/.azure-wsl"` を実行してください（ルートの README の「セットアップ」を参照）。

| スクリプト | 使う場面 | Azure | 主な出力 |
| --- | --- | --- | --- |
| [`smoke_test.py`](#smoke_testpy) | デプロイ直後の疎通確認 | 使う | 画面のみ |
| [`fetch_corpus.py`](#fetch_corpuspy) | コーパス（Microsoft Learn の記事）の取得 | 使わない | `data/raw/` |
| [`review_cli.py`](#review_clipy) | テスト用質問の人手レビュー（推奨） | `r` コマンドのみ | `data/qa/test_human_review.jsonl`、`data/qa/test.jsonl` |
| [`ai_review.py`](#ai_reviewpy) | AI による一次レビューの材料作りと反映 | 使わない | `data/qa/test_ai_review.csv` |
| [`review_sheet.py`](#review_sheetpy) | CSV（Excel）でのレビュー、チャンクの全文表示 | 使わない | `data/qa/test_review.csv` |
| [`cost_report.py`](#cost_reportpy) | Azure の使用料の確認 | 使う | `docs/azure_cost.md`、`results/cost_snapshots/` |

`data/qa/*.csv` は Microsoft Learn の記事本文を含むため、Git の管理外です。必要になったら手元で作り直してください。

---

## smoke_test.py

Decision-1、埋め込み（embed）、生成（gen）、採点（judge）、PostgreSQL を 1 回ずつ呼び、5 つとも使えるかを確かめます。各呼び出しの応答時間、トークン数、`config/models.yaml` の単価から計算した 1 回あたりの費用を表示します。Decision-1 は応答の形を確認できるよう、生のレスポンスも表示します。

```bash
uv run python -m scripts.smoke_test
```

- 5/5 OK になれば準備完了です。失敗した接続先にはエラーの内容が出ます。
- デプロイ直後の初回呼び出しは数十秒〜数分かかることがあります（gen で 229 秒の例あり）。評価の前に一度実行しておくと、レイテンシの外れ値を避けられます。
- 費用は 1 回あたり 0.001 ドル未満です。

## fetch_corpus.py

Microsoft Learn 日本語版の Azure Storage 系（Blob・Files・Queues・Tables）の記事を、Markdown で `data/raw/` に保存します。各サービスの目次（`toc.json`）から記事一覧を作り、SDK 別のクイックスタート、チュートリアル、FAQ、トラブルシューティングは除きます。

```bash
uv run python -m scripts.fetch_corpus --max-docs 400            # 全件（今回は 302 記事、約 5 分）
uv run python -m scripts.fetch_corpus --max-docs 50 --interval 2 # 50 記事だけ、2 秒間隔で
```

| オプション | 既定値 | 内容 |
| --- | --- | --- |
| `--max-docs` | 200 | 取得する記事の上限。全件取るには 400 など大きめの値を指定する |
| `--interval` | 1.0 | リクエストの間隔（秒） |

- 記事のメタデータ（URL、タイトル、更新日、翻訳種別、原文の Git URL）は `data/raw/manifest.jsonl` に記録します。
- 取得済みの記事は飛ばすので、中断しても続きから再開できます。
- 取得後は `uv run python -m src.chunking` でチャンク化、`uv run python -m src.ingest` で DB に取り込みます。
- 元記事は [MicrosoftDocs/azure-docs](https://github.com/MicrosoftDocs/azure-docs)（CC BY 4.0）です。取得した本文は再配布しないでください（ルートの README の「出典」を参照）。

## review_cli.py

テスト用の質問を、ターミナルで 1 問ずつレビューする対話型ツールです。AI の一次レビューの提案（`data/qa/test_ai_review.jsonl`）を初期値として表示し、人は提案に同意するなら Enter、違う場合だけ自分で判定します。根拠チャンクの全文、前後のチャンク、コーパスの語句検索、実際の検索結果をその場で表示できるので、「全文を読まないと判断できない」質問にも対応できます。

```bash
uv run python -m scripts.review_cli                  # 未判定の質問から順にレビュー
uv run python -m scripts.review_cli --start t050     # t050 から
uv run python -m scripts.review_cli --only ai-fix    # AI が fix / drop を提案した質問だけ
uv run python -m scripts.review_cli status           # 進捗の表示
uv run python -m scripts.review_cli apply            # 判定を data/qa/test.jsonl に反映
```

| オプション | 内容 |
| --- | --- |
| `review`（既定）/ `status` / `apply` | レビュー、進捗表示、反映 |
| `--only` | 対象の絞り込み。`pending`（既定、未判定のみ）・`all`・`ai-fix`・`ai-drop` |
| `--start` | 指定した質問 ID から始める |
| `--accept-ai-for-rest` | `apply` のとき、未判定の質問に AI の提案を使う |

**レビュー画面のキー**（`?` で一覧を表示）

| キー | 動作 |
| --- | --- |
| Enter / `a` | AI の提案をそのまま採用して次へ |
| `o` / `f` / `d` | ok（そのまま使う）／ fix（質問・模範解答・根拠を修正。AI の修正案が入力欄に入った状態で始まる）／ drop（評価から除外） |
| `m` | メモ（note）だけを書き換える |
| `u` | この質問の判定を取り消す |
| `v` / `v 2` / `v c01234 ...` | 根拠と囮のチャンクの全文／ 2 番目の根拠の全文／ 指定したチャンクの全文（less で表示、`q` で戻る） |
| `c` | 根拠チャンクの前後のチャンクも含めて全文 |
| `s <語句>` | コーパス全体を語句（正規表現可）で検索 |
| `r` | この質問で実際に検索した上位 10 件（Azure の embed を使う） |
| `n` / `b` / `g t050` | 次へ／前へ（判定せずに移動）／指定した質問へ |
| `l` | 進捗の一覧 |
| `q` | 終了（判定は都度保存済み） |

- 判定は 1 問ごとに `data/qa/test_human_review.jsonl` に保存されるので、途中で終了しても再開できます。
- `apply` は、レビュー前の質問（`data/qa/test.orig.jsonl`、初回に自動で退避）から毎回作り直すので、何度実行しても同じ結果になります。
- Windows のターミナルから WSL で使えます。チャンクの本文を表示するために `data/chunks.jsonl` が必要です。

## ai_review.py

AI（Claude など）に一次レビューをさせるための補助スクリプトです。質問ごとの判断材料を表示し、AI が書いた提案を CSV にまとめ、人が確認した結果を反映します。今回は `material` の出力を Claude が読み、提案を `data/qa/test_ai_review.jsonl` に書きました。

```bash
uv run python -m scripts.ai_review material t001 t010   # t001〜t010 の判断材料を表示
uv run python -m scripts.ai_review export                # test_ai_review.jsonl から data/qa/test_ai_review.csv を作る
uv run python -m scripts.ai_review apply                 # 人が確認した CSV を data/qa/test.jsonl に反映
```

- **判断材料**：質問と模範解答、根拠チャンクの全文と前後のチャンク（先頭 300 文字）、囮チャンクの全文。回答不能の質問は実際の検索上位 5 件（先頭 400 文字）。
- **提案の形式**（`test_ai_review.jsonl`、1 行 1 問）：`qid`、`ai_status`（ok / fix / drop）、`ai_fixed_question`、`ai_fixed_answer`、`ai_fixed_evidence`、`ai_reason`。
- **CSV での確認**：AI の提案に同意する行は `status` を空欄のまま、同意しない行だけ `status` を記入します。修正内容は `ai_fixed_*` 列を直接書き換えてかまいません。`apply` は `status` が空欄なら `ai_status` を採用します。
- CSV より `review_cli.py` のほうが確認しやすいので、通常は `review_cli.py` を使ってください。

## review_sheet.py

テスト用の質問を CSV（Excel で開ける UTF-8 BOM 付き）に書き出してレビューし、結果を戻すスクリプトです。最初に用意した方法で、今は `review_cli.py` の利用を推奨します。`show` はチャンクの全文を確認したいときに単独でも使えます。

```bash
uv run python -m scripts.review_sheet export            # data/qa/test_review.csv を作る
uv run python -m scripts.review_sheet import            # CSV の修正を data/qa/test.jsonl に反映（元は test.orig.jsonl に退避）
uv run python -m scripts.review_sheet show c01926 c01927 # チャンクの全文と元記事の URL を表示
```

**CSV の記入方法**

| 列 | 内容 |
| --- | --- |
| `status` | ok（そのまま）／ fix（`fixed_*` の内容を反映）／ drop（除外） |
| `fixed_question`・`fixed_answer` | fix のときの修正後の質問・模範解答 |
| `fixed_evidence` | fix のときの根拠チャンク ID（スペース区切り。回答不能は空） |
| `evidence_text`・`distractor_text` | 確認用の根拠・囮チャンクの本文（先頭 600 文字） |

## cost_report.py

Azure の使用料を取得して `docs/azure_cost.md` にまとめます。次の 2 つを組み合わせ、まだ請求に反映されていない分も見積もります。

- **Cost Management Query API**：請求として確定した金額（ActualCost、請求通貨）を日別 × メーター別に取得。メーター名から「どのデプロイの」「どの種類のトークン（入力・出力・キャッシュ）か」を読み取ります。
- **Azure Monitor メトリック**（`InputTokens` / `OutputTokens`）：デプロイ別・日別のトークン数。ほぼリアルタイムに出ます。

メトリックのトークン数が請求済みのトークン数より多い分を「未反映」とし、確定分から求めた実効単価で見積もります。確定分がないデプロイは、`config/models.yaml` の単価 × 確定分から逆算した為替レートを使います。

```bash
uv run python -m scripts.cost_report --from 2026-10-16 --to 2026-10-21
uv run python -m scripts.cost_report --from 2026-10-01 --to 2026-10-31 --resource-group my-rg --subscription <ID>
```

| オプション | 既定値 | 内容 |
| --- | --- | --- |
| `--from` / `--to` | （必須） | 集計期間（YYYY-MM-DD、UTC） |
| `--resource-group` | `rg-d1rag-eval` | 対象のリソースグループ |
| `--subscription` | az の既定 | 対象のサブスクリプション ID |

**出力の内容**

- 確定額と未反映分の見積もりの合計
- デプロイ × 日のトークン数・確定額・未反映の見積もり
- 3 つの方法の比較（トークン数 × 一覧単価、確定額、公式の推定費用 `FoundryModelEstimatedCost`）
- 実効単価と一覧単価、メーター別の内訳

**注意**

- 確定額の反映には使用から 12〜24 時間ほどかかります。直後に実行すると、ほとんどが見積もりになります。
- Cost Management API は短時間に続けて呼ぶと 429 を返します。スクリプトは応答で指示された秒数だけ待って再試行します。
- 取得のたびに生データを `results/cost_snapshots/<取得時刻>.json` に残すので、反映の遅れを後から追えます。最新のものは `results/azure_cost.json` です。
- `FoundryModelEstimatedCost` は一部のリクエストしか記録されず、確定額と大きく食い違いました（2026-10-20 確認）。参考として並べているだけで、見積もりには使っていません。
- リソースグループを削除するとメトリックも消えます。費用の確認は削除前に行ってください。
