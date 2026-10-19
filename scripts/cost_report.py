"""Azure の実際の使用料を取得し、docs/azure_cost.md にまとめる。

2 つの API を組み合わせる。
- Cost Management Query API：請求として確定した金額（ActualCost、請求通貨）を日別 × メーター別に取る。
  メーター名から「どのデプロイの」「どの種類のトークンか」を読み取る。反映には 1 日以上かかることがある
- Azure Monitor メトリック（InputTokens / OutputTokens）：デプロイ別・日別のトークン数。ほぼリアルタイムに出る

メトリックのトークン数が請求済みのトークン数より多い分を「未反映」とし、確定分から求めた実効単価
（確定分がないデプロイは config/models.yaml の単価 × 確定分から求めた為替レート）で見積もる。
日付はどちらも UTC。生データは results/azure_cost.json に保存する。

- 認証は az login 済みの資格情報（DefaultAzureCredential）。サブスクリプションは az の既定を使う
- メトリックの FoundryModelEstimatedCost は請求額と大きく食い違う（2026-10-20 確認）ので使わない

    uv run python -m scripts.cost_report --from 2026-10-16 --to 2026-10-20
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
from collections import defaultdict
from pathlib import Path

import httpx
import yaml

from src.clients import credential

ROOT = Path(__file__).resolve().parents[1]
MGMT = "https://management.azure.com"
COST_API = MGMT + "/subscriptions/{sub}/providers/Microsoft.CostManagement/query?api-version=2023-11-01"

# メーター名 → デプロイ名
DEPLOYMENTS = [
    (r"^Microsoft Decision 1", "decision-eval"),
    (r"^text-embedding-3-small", "embed"),
    (r"^6-luna", "gen"),
    (r"^6-sol", "judge"),
    (r"(?i)5[- ]?nano", "gen-nano"),
    (r"(?i)phi", "gen-phi"),
]
# メーター名の略語 → トークンの種類（入力側か出力側か）
KINDS = [(r"Cd Wr", "キャッシュ書き込み", "in"), (r"Cd Inp", "キャッシュ読み取り", "in"),
         (r"Opt|Output|Outp", "出力", "out"), (r"Inp|Input|Tokens", "入力", "in")]


def deployment_of(meter: str) -> str:
    return next((name for pat, name in DEPLOYMENTS if re.search(pat, meter)), f"その他（{meter}）")


def kind_of(meter: str) -> tuple[str, str]:
    label, side = next(((k, s) for pat, k, s in KINDS if re.search(pat, meter)), ("トークン", "in"))
    return label + ("（Flex）" if "Flex" in meter else ""), side


def tokens_of(meter: str, qty: float) -> float:
    """メーターの数量をトークン数に直す（「1M Tokens」は 100 万、「Tokens」だけなら 1,000 単位）。"""
    return qty * (1e6 if "1M" in meter else 1e3)


def headers() -> dict:
    return {"Authorization": f"Bearer {credential().get_token(MGMT + '/.default').token}"}


def query_cost(sub: str, rg: str, start: str, end: str) -> list[dict]:
    body = {
        "type": "ActualCost",
        "timeframe": "Custom",
        "timePeriod": {"from": f"{start}T00:00:00Z", "to": f"{end}T23:59:59Z"},
        "dataset": {
            "granularity": "Daily",
            "aggregation": {"cost": {"name": "Cost", "function": "Sum"}, "qty": {"name": "UsageQuantity", "function": "Sum"}},
            "grouping": [{"type": "Dimension", "name": "MeterSubCategory"}, {"type": "Dimension", "name": "Meter"}],
            "filter": {"dimensions": {"name": "ResourceGroupName", "operator": "In", "values": [rg]}},
        },
    }
    url, rows = COST_API.format(sub=sub), []
    while url:
        r = httpx.post(url, json=body, headers=headers(), timeout=60)
        r.raise_for_status()
        p = r.json()["properties"]
        cols = [c["name"] for c in p["columns"]]
        rows += [dict(zip(cols, row)) for row in p["rows"]]
        url = p.get("nextLink")
    return rows


def query_metrics(resource_id: str, start: str, end: str) -> dict[tuple[str, str], list[float]]:
    """(デプロイ名, YYYYMMDD) → [入力トークン, 出力トークン]"""
    r = httpx.get(f"{MGMT}{resource_id}/providers/Microsoft.Insights/metrics", headers=headers(), timeout=60, params={
        "api-version": "2023-10-01", "metricnames": "InputTokens,OutputTokens", "aggregation": "Total", "interval": "P1D",
        "timespan": f"{start}T00:00:00Z/{end}T23:59:59Z", "$filter": "ModelDeploymentName eq '*'"})
    r.raise_for_status()
    out = defaultdict(lambda: [0.0, 0.0])
    for m in r.json()["value"]:
        side = 0 if m["name"]["value"] == "InputTokens" else 1
        for ts in m["timeseries"]:
            dep = next(v["value"] for v in ts["metadatavalues"] if v["name"]["value"].lower() == "modeldeploymentname")
            for p in ts["data"]:
                if p.get("total"):
                    out[(dep, p["timeStamp"][:10].replace("-", ""))][side] += p["total"]
    return out


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--from", dest="start", required=True, help="YYYY-MM-DD")
    parser.add_argument("--to", dest="end", required=True, help="YYYY-MM-DD")
    parser.add_argument("--resource-group", default="rg-d1rag-eval")
    parser.add_argument("--subscription", help="省略時は az の既定")
    args = parser.parse_args()

    az = lambda *a: subprocess.run(["az", *a, "-o", "tsv"], capture_output=True, text=True, check=True).stdout.strip()  # noqa: E731
    sub = args.subscription or az("account", "show", "--query", "id")
    account_id = az("cognitiveservices", "account", "list", "-g", args.resource_group, "--subscription", sub, "--query", "[0].id")
    rows = query_cost(sub, args.resource_group, args.start, args.end)
    metrics = query_metrics(account_id, args.start, args.end)
    (ROOT / "results").mkdir(exist_ok=True)
    (ROOT / "results/azure_cost.json").write_text(json.dumps(
        {"cost": rows, "metrics": {f"{k[0]}|{k[1]}": v for k, v in metrics.items()}}, ensure_ascii=False, indent=1), encoding="utf-8")

    cur = rows[0]["Currency"] if rows else "JPY"
    models = {d["name"]: d for d in yaml.safe_load((ROOT / "config/models.yaml").read_text(encoding="utf-8"))["deployments"]}

    # ---- 確定分：デプロイ × 日、メーター別 ----
    billed = defaultdict(lambda: {"in": 0.0, "out": 0.0, "in_cost": 0.0, "out_cost": 0.0})
    by_meter = defaultdict(lambda: [0.0, 0.0])
    for r in rows:
        dep, day = deployment_of(r["Meter"]), str(r["UsageDate"])
        label, side = kind_of(r["Meter"])
        b = billed[(dep, day)]
        b[side] += tokens_of(r["Meter"], r["UsageQuantity"])
        b[f"{side}_cost"] += r["Cost"]
        m = by_meter[(dep, label, r["Meter"])]
        m[0] += tokens_of(r["Meter"], r["UsageQuantity"])
        m[1] += r["Cost"]

    # ---- 実効単価（円 / トークン）と為替レート ----
    unit = {}
    for dep in {d for d, _ in billed}:
        t = {k: sum(v[k] for (d, _), v in billed.items() if d == dep) for k in ("in", "out", "in_cost", "out_cost")}
        unit[dep] = (t["in_cost"] / t["in"] if t["in"] else None, t["out_cost"] / t["out"] if t["out"] else None)
    fx = None  # Decision-1 はキャッシュの区別がなく、単価がそのまま比べられる
    if unit.get("decision-eval", (None,))[0]:
        fx = unit["decision-eval"][0] * 1e6 / models["decision-eval"]["price"]["input"]

    def list_unit(dep: str, side: str) -> float | None:
        d = models.get(dep)
        if not d or not fx:
            return None
        price = d.get("flex_price", d["price"])  # judge は Flex で呼んでいる
        return price["input" if side == "in" else "output"] * fx / 1e6

    # ---- デプロイ × 日：トークン数（メトリック）、確定額、未反映の見積もり ----
    keys = sorted(set(metrics) | set(billed))
    table, totals = [], defaultdict(float)
    for dep, day in keys:
        mi, mo = metrics.get((dep, day), [0.0, 0.0])
        b = billed.get((dep, day), {"in": 0.0, "out": 0.0, "in_cost": 0.0, "out_cost": 0.0})
        cost = b["in_cost"] + b["out_cost"]
        est = 0.0
        for side, used in (("in", mi), ("out", mo)):
            rest = max(0.0, used - b[side])
            u = (unit.get(dep, (None, None))[0 if side == "in" else 1]) or list_unit(dep, side) or 0.0
            if dep == "decision-eval" and side == "out":
                u = 0.0  # 出力は課金されない
            est += rest * u
        table.append((dep, day, mi, mo, cost, est))
        totals["billed"] += cost
        totals["est"] += est

    fd = lambda d: f"{d[:4]}-{d[4:6]}-{d[6:]}"  # noqa: E731
    lines = [f"# Azure の使用料（{args.resource_group}、{args.start}〜{args.end}）", "",
             f"確定額は Cost Management Query API（ActualCost、請求通貨 {cur}）、トークン数は Azure Monitor メトリックから "
             "`scripts/cost_report.py` で取得した。日付は UTC。", "",
             f"**確定額 {totals['billed']:,.1f} {cur} ＋ 未反映分の見積もり {totals['est']:,.1f} {cur} ＝ "
             f"約 {totals['billed'] + totals['est']:,.0f} {cur}**", "",
             f"為替レートは Decision-1 の確定額から逆算した 1 USD ≒ {fx:,.1f} {cur}。" if fx else "", "",
             "## デプロイ × 日", "",
             "未反映分は、メトリックのトークン数のうち請求に出ていない分に、確定分の実効単価（確定分がなければ一覧単価 × 為替）を掛けた見積もり。", "",
             f"| 日付 | デプロイ | 入力トークン | 出力トークン | 確定額（{cur}） | 未反映の見積もり（{cur}） |",
             "| --- | --- | --- | --- | --- | --- |"]
    for dep, day, mi, mo, cost, est in table:
        lines.append(f"| {fd(day)} | {dep} | {mi:,.0f} | {mo:,.0f} | {cost:,.1f} | {est:,.1f} |")
    lines += ["", "## 確定分の実効単価", "",
              f"キャッシュ書き込みなど一覧単価にない料金を含めた、実際の 100 万トークンあたりの金額（{cur}）。", "",
              f"| デプロイ | 入力 | 出力 | 一覧単価での入力 | 一覧単価での出力 |", "| --- | --- | --- | --- | --- |"]
    for dep, (ui, uo) in sorted(unit.items()):
        li, lo = list_unit(dep, "in"), list_unit(dep, "out")
        f = lambda v: f"{v * 1e6:,.2f}" if v else "—"  # noqa: E731
        lines.append(f"| {dep} | {f(ui)} | {f(uo)} | {f(li)} | {f(lo)} |")
    lines += ["", "## メーター別（確定分の期間合計）", "",
              f"| デプロイ | 種類 | メーター | トークン数 | 金額（{cur}） |", "| --- | --- | --- | --- | --- |"]
    for (dep, label, meter), (q, c) in sorted(by_meter.items(), key=lambda x: (x[0][0], -x[1][1])):
        lines.append(f"| {dep} | {label} | {meter} | {q:,.0f} | {c:,.2f} |")
    out = ROOT / "docs/azure_cost.md"
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(out.read_text(encoding="utf-8"))


if __name__ == "__main__":
    main()
