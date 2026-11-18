# -*- coding: utf-8 -*-
"""NLU 评测：同一套 prompt / 同一批用例，对比不同模型的意图识别与参数抽取。

用法：
    python scripts/eval_nlu.py
    python scripts/eval_nlu.py --models qwen-flash,deepseek-v4.1-flash
    python scripts/eval_nlu.py --limit 10 --no-pipeline      # 快速冒烟

两种模式：
    raw      只发 prompt、只解析模型返回的 JSON —— 测“模型本身”的抽取能力
    pipeline 走项目真实的 LLMClient.extract_intent_and_params（含规则引擎补字段）——
             测“接进项目后的最终效果”

另外附带 rules 基线（纯规则引擎、不调模型），用来判断“花这个钱值不值”。

评测集：scripts/eval_nlu_cases.json（日期用 {{TOKEN}} 占位，按运行当天解析）。
输出：终端对比表 + Markdown 报告（默认 scripts/eval_nlu_report.md）。
"""

import argparse
import hashlib
import json
import os
import re
import statistics
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

PROJ = Path(__file__).resolve().parent.parent
APP = PROJ / "hotel_booking_agent"
sys.path.insert(0, str(APP))

from dotenv import load_dotenv          # noqa: E402
load_dotenv(APP / ".env", override=True)

import requests                          # noqa: E402
from llm import LLMClient, _COMPAT_URL   # noqa: E402

DEFAULT_MODELS = "qwen-flash,qwen3.8-flash,deepseek-v4.1-flash"
CALL_SLEEP = 0.2                         # 请求间隔，避免触发限流
INTENT_MAP = {"预订": "book", "预定": "book", "搜索": "search", "查询": "search",
              "查订单": "order_query", "取消": "cancel", "闲聊": "chat"}
FIELD_NAMES = ("city", "check_in", "check_out", "min_star", "max_price",
               "facilities", "keyword", "location", "order_id", "order_status",
               "guest_name")


# ---------- 日期占位符 ----------

def _month_for(day: int, today: datetime):
    """最近一个“不早于今天”的某月某日所属的 (年, 月)。"""
    year, month = today.year, today.month
    if today.day > day:
        month += 1
        if month > 12:
            month, year = 1, year + 1
    return year, month


def build_date_tokens(today: datetime) -> dict:
    pair_year, pair_month = _month_for(12, today)      # 12/15 同一月份
    next24_year, next24_month = _month_for(24, today)
    sat = today + timedelta(days=(5 - today.weekday()) % 7)   # 本周六
    sun = today + timedelta(days=(6 - today.weekday()) % 7)   # 本周日
    fri = today + timedelta(days=(4 - today.weekday()) % 7)   # 本周五
    dec1 = datetime(today.year, 12, 1)
    if dec1.date() < today.date():
        dec1 = datetime(today.year + 1, 12, 1)
    next_week_sat = sat + timedelta(days=7)
    return {
        "TODAY": today,
        "TOMORROW": today + timedelta(days=1),
        "DAY_AFTER": today + timedelta(days=2),
        "SAT": sat,
        "SUN": sun,
        "FRI": fri,
        "DEC1": dec1,
        "NEXT_WEEK_SAT": next_week_sat,
        "NEXT_WEEK_SUN": next_week_sat + timedelta(days=1),
        "PAIR12": datetime(pair_year, pair_month, 12),
        "PAIR15": datetime(pair_year, pair_month, 15),
        "NEXT24": datetime(next24_year, next24_month, 24),
    }


def resolve_value(value, tokens: dict):
    if not isinstance(value, str):
        return value
    m = re.fullmatch(r"\{\{([A-Z0-9_]+)(?:\+(\d+))?\}\}", value.strip())
    if not m:
        return value
    base = tokens[m.group(1)]
    return (base + timedelta(days=int(m.group(2) or 0))).strftime("%Y-%m-%d")


def resolve_gold(gold: dict, tokens: dict) -> dict:
    return {k: resolve_value(v, tokens) for k, v in gold.items()}


# ---------- 调模型 ----------

def _headers():
    return {"Authorization": f"Bearer {os.environ['DASHSCOPE_API_KEY']}",
            "Content-Type": "application/json"}


def call_raw(model: str, prompt: str):
    """只发 prompt，返回 (解析后的 dict | None, 耗时秒, 错误串 | None)。"""
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "response_format": {"type": "json_object"},
    }
    started = time.perf_counter()
    try:
        resp = requests.post(_COMPAT_URL, headers=_headers(), json=payload,
                             timeout=(8, 90))
        elapsed = time.perf_counter() - started
        if resp.status_code != 200:
            code = ""
            try:
                code = resp.json().get("error", {}).get("code", "")
            except Exception:
                pass
            return None, elapsed, f"HTTP {resp.status_code} {code}"
        text = ((resp.json().get("choices") or [{}])[0].get("message") or {}).get("content") or ""
        m = re.search(r"\{.*\}", text, re.DOTALL)
        if not m:
            return None, elapsed, "no_json"
        return json.loads(m.group()), elapsed, None
    except Exception as exc:                       # 网络/解析异常都算失败
        return None, time.perf_counter() - started, f"{type(exc).__name__}"


def capture_prompt(client: LLMClient, text: str) -> str:
    """用真实 LLMClient 生成 prompt（保证和线上完全一致），但把请求截获。"""
    captured = {}
    original = client._chat

    def fake_chat(prompt, json_mode=False):
        captured["prompt"] = prompt
        return None

    client._chat = fake_chat
    try:
        client.extract_intent_and_params(text)
    finally:
        client._chat = original
    return captured["prompt"]


# ---------- 比对 ----------

def norm_intent(value) -> str:
    text = str(value or "").strip()
    return INTENT_MAP.get(text, text)


def norm_city(value) -> str:
    return str(value or "").strip().rstrip("市").strip()


def norm_location(value) -> str:
    text = re.sub(r"\s+", "", str(value or ""))
    return re.sub(r"(附近|周边|旁边|一带|步行街)$", "", text)


def contains(a, b) -> bool:
    a, b = norm_location(a), norm_location(b)
    return bool(a and b) and (a in b or b in a)


def field_ok(name: str, pred, gold) -> bool:
    if pred is None or pred == "":
        return False
    if name == "city":
        return norm_city(pred) == norm_city(gold)
    if name in ("location", "keyword"):
        return contains(pred, gold)
    if name in ("max_price", "min_star"):
        try:
            return float(pred) == float(gold)
        except (TypeError, ValueError):
            return False
    if name == "order_status":
        return str(pred).upper() == str(gold).upper()
    if name == "facilities":
        if not isinstance(pred, list):
            return False
        return {str(x) for x in pred} == {str(x) for x in gold}
    return str(pred).strip() == str(gold).strip()


def score_case(result: dict, gold: dict) -> dict:
    """result: {"intent":..., "params":...}；返回逐字段结果。"""
    pred_intent = norm_intent(result.get("intent"))
    params = result.get("params") or {}
    intent_ok = pred_intent == gold["intent"]
    fields = {}
    for name, want in gold.items():
        if name == "intent":
            continue
        fields[name] = field_ok(name, params.get(name), want)
    return {
        "intent_ok": intent_ok,
        "fields": fields,
        "field_ok": sum(1 for v in fields.values() if v),
        "field_total": len(fields),
        "perfect": intent_ok and all(fields.values()),
    }


# ---------- 汇总 ----------

def summarize(rows: list) -> dict:
    ok_cases = [r for r in rows if r["error"] is None]
    latencies = sorted(r["latency"] for r in ok_cases)
    fields_ok = sum(r["score"]["field_ok"] for r in ok_cases)
    fields_total = sum(r["score"]["field_total"] for r in ok_cases)
    return {
        "cases": len(rows),
        "ok": len(ok_cases),
        "errors": len(rows) - len(ok_cases),
        "intent_acc": (sum(1 for r in ok_cases if r["score"]["intent_ok"]) / len(ok_cases)
                       if ok_cases else 0.0),
        "field_acc": (fields_ok / fields_total) if fields_total else 0.0,
        "perfect_rate": (sum(1 for r in ok_cases if r["score"]["perfect"]) / len(ok_cases)
                         if ok_cases else 0.0),
        "json_valid": (sum(1 for r in ok_cases if r["json_ok"]) / len(ok_cases)
                       if ok_cases else 0.0),
        "p50_ms": statistics.median(latencies) * 1000 if latencies else 0.0,
        "p95_ms": (latencies[min(len(latencies) - 1, int(len(latencies) * 0.95))]
                   * 1000 if latencies else 0.0),
    }


def fmt_row(name: str, s: dict) -> str:
    return (f"{name:<30s} {s['intent_acc']*100:6.1f}% {s['field_acc']*100:6.1f}% "
            f"{s['perfect_rate']*100:6.1f}% {s['json_valid']*100:6.1f}% "
            f"{s['p50_ms']:7.0f} {s['p95_ms']:7.0f} {s['errors']:5d}")


def run(args):
    cases_path = Path(args.cases)
    cases_bytes = cases_path.read_bytes()
    case_sha = hashlib.sha256(cases_bytes).hexdigest()[:16]
    data = json.loads(cases_bytes.decode("utf-8"))
    cases = data["cases"]
    if args.limit:
        cases = cases[:args.limit]

    try:
        git_commit = subprocess.check_output(
            ["git", "-C", str(PROJ), "rev-parse", "--short", "HEAD"],
            text=True, stderr=subprocess.DEVNULL).strip()
        git_dirty = bool(subprocess.check_output(
            ["git", "-C", str(PROJ), "status", "--porcelain"],
            text=True, stderr=subprocess.DEVNULL).strip())
    except Exception:
        git_commit, git_dirty = "", None

    today = datetime.now()
    tokens = build_date_tokens(today)

    client = LLMClient()
    rule_client = LLMClient()
    rule_client.use_real_llm = False

    models = [m.strip() for m in args.models.split(",") if m.strip()]
    print(f"用例 {len(cases)} 条 | 模型 {models} | 今天 {today:%Y-%m-%d}\n")

    # rules 基线（不联网）
    rule_rows = []
    for case in cases:
        gold = resolve_gold(case["gold"], tokens)
        started = time.perf_counter()
        intent, params = rule_client.extract_intent_and_params(case["input"])
        rule_rows.append({"id": case["id"], "latency": time.perf_counter() - started,
                          "error": None, "json_ok": True,
                          "result": {"intent": intent, "params": params},
                          "score": score_case({"intent": intent, "params": params}, gold),
                          "gold": gold})

    summaries = {"rules": summarize(rule_rows)}
    details = {"rules": rule_rows}

    for model in models:
        raw_rows, pipe_rows = [], []
        for index, case in enumerate(cases, 1):
            gold = resolve_gold(case["gold"], tokens)
            prompt = capture_prompt(client, case["input"])

            parsed, elapsed, error = call_raw(model, prompt)
            raw_result = {"intent": (parsed or {}).get("intent"),
                          "params": {k: v for k, v in (parsed or {}).items()
                                     if k != "intent" and v}}
            raw_rows.append({
                "id": case["id"], "latency": elapsed, "error": error,
                "json_ok": parsed is not None, "result": raw_result,
                "score": score_case(raw_result, gold), "gold": gold,
                "prompt": prompt,
            })
            time.sleep(CALL_SLEEP)

            if not args.no_pipeline:
                client.model = model
                started = time.perf_counter()
                try:
                    intent, params = client.extract_intent_and_params(case["input"])
                    pipe_error = None
                except Exception as exc:
                    intent, params, pipe_error = "chat", {}, f"{type(exc).__name__}"
                pipe_rows.append({
                    "id": case["id"], "latency": time.perf_counter() - started,
                    "error": pipe_error, "json_ok": True,
                    "result": {"intent": intent, "params": params},
                    "score": score_case({"intent": intent, "params": params}, gold),
                    "gold": gold,
                })
                time.sleep(CALL_SLEEP)
            if index % 10 == 0:
                print(f"  {model}: {index}/{len(cases)}")

        summaries[f"{model} / raw"] = summarize(raw_rows)
        details[f"{model} / raw"] = raw_rows
        if not args.no_pipeline:
            summaries[f"{model} / pipeline"] = summarize(pipe_rows)
            details[f"{model} / pipeline"] = pipe_rows

    # ---------- 难例子集 ----------
    hard_ids = {c["id"] for c in cases if c.get("hard")}
    hard_summaries = {}
    for name, rows in details.items():
        subset = [r for r in rows if r["id"] in hard_ids]
        if subset:
            hard_summaries[name] = summarize(subset)

    # ---------- 终端表格 ----------
    header = (f"{'配置':<30s} {'意图':>7s} {'字段':>7s} {'全对':>7s} "
              f"{'JSON':>7s} {'P50ms':>7s} {'P95ms':>7s} {'失败':>5s}")
    print("\n" + header)
    print("-" * len(header))
    for name, summary in summaries.items():
        print(fmt_row(name, summary))
    if hard_summaries:
        print(f"\n难例子集（{len(hard_ids)} 条）")
        print(header)
        print("-" * len(header))
        for name, summary in hard_summaries.items():
            print(fmt_row(name, summary))

    # ---------- Markdown 报告 ----------
    lines = [f"# NLU 评测报告（{today:%Y-%m-%d %H:%M}）", "",
             f"- 用例：{len(cases)} 条（`{cases_path.name}`，sha256 `{case_sha}`）",
             f"- 模型：{', '.join(models)}",
             f"- 代码：`{git_commit or 'unknown'}`"
             f"{'（工作区含未提交改动）' if git_dirty else ''}；Python {sys.version.split()[0]}",
             "- 模式：`raw` 只解析模型 JSON；`pipeline` 走项目真实抽取链路（含规则补齐）",
             "- 指标：意图准确率 / 字段准确率（micro）/ 全对率 / JSON 可解析率 / 延迟",
             "", "| 配置 | 意图 | 字段 | 全对 | JSON | P50 | P95 | 失败 |",
             "|---|---|---|---|---|---|---|---|"]
    for name, s in summaries.items():
        lines.append(f"| {name} | {s['intent_acc']*100:.1f}% | {s['field_acc']*100:.1f}% | "
                     f"{s['perfect_rate']*100:.1f}% | {s['json_valid']*100:.1f}% | "
                     f"{s['p50_ms']:.0f} ms | {s['p95_ms']:.0f} ms | {s['errors']} |")

    if hard_summaries:
        lines += ["", f"## 难例子集（{len(hard_ids)} 条）", "",
                  "| 配置 | 意图 | 字段 | 全对 | P50 | P95 |",
                  "|---|---|---|---|---|---|"]
        for name, s in hard_summaries.items():
            lines.append(f"| {name} | {s['intent_acc']*100:.1f}% | {s['field_acc']*100:.1f}% | "
                         f"{s['perfect_rate']*100:.1f}% | {s['p50_ms']:.0f} ms | "
                         f"{s['p95_ms']:.0f} ms |")

    lines += ["", "## 失败明细（每配置最多 8 条）", ""]
    for name, rows in details.items():
        fails = [r for r in rows if r["error"] or not r["score"]["perfect"]][:8]
        if not fails:
            continue
        lines.append(f"### {name}")
        for r in fails:
            bad_fields = [k for k, ok in r["score"]["fields"].items() if not ok]
            pred = {k: r["result"].get("params", {}).get(k) for k in bad_fields}
            want = {k: r["gold"].get(k) for k in bad_fields}
            reason = r["error"] or f"意图={r['result'].get('intent')}"
            lines.append(f"- `{r['id']}`：{reason}；缺/错字段 预测={pred} 期望={want}")
        lines.append("")

    report_path = Path(args.out)
    report_path.write_text("\n".join(lines), encoding="utf-8")

    # ---------- 机器可读结果（便于溯源/复现/二次分析） ----------
    def row_payload(row):
        return {
            "id": row["id"],
            "latency_ms": round(row["latency"] * 1000),
            "error": row["error"],
            "json_ok": row["json_ok"],
            "result": {"intent": row["result"].get("intent"),
                       "params": row["result"].get("params") or {}},
            "gold": row["gold"],
            "score": row["score"],
        }

    results = {
        "run_at": today.isoformat(timespec="seconds"),
        "python": sys.version.split()[0],
        "git_commit": git_commit,
        "git_dirty": git_dirty,
        "case_file": str(cases_path.relative_to(PROJ)) if cases_path.is_relative_to(PROJ)
                     else str(cases_path),
        "case_count": len(cases),
        "case_sha256": case_sha,
        "models": models,
        "summaries": summaries,
        "hard_summaries": hard_summaries,
        "rows": {name: [row_payload(r) for r in rows]
                 for name, rows in details.items()},
    }
    json_path = Path(args.json_out)
    json_path.write_text(json.dumps(results, ensure_ascii=False, indent=2),
                         encoding="utf-8")
    print(f"\n报告已写入：{report_path}")
    print(f"明细已写入：{json_path}")
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description="NLU 模型对比评测")
    parser.add_argument("--cases", default=str(PROJ / "scripts" / "eval_nlu_cases.json"))
    parser.add_argument("--models", default=DEFAULT_MODELS)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--out", default=str(PROJ / "scripts" / "eval_nlu_report.md"))
    parser.add_argument("--json-out",
                        default=str(PROJ / "scripts" / "eval_nlu_results.json"))
    parser.add_argument("--no-pipeline", action="store_true",
                        help="只跑 raw（跳过项目真实链路）")
    args = parser.parse_args(argv)
    return run(args)


if __name__ == "__main__":
    sys.exit(main())
