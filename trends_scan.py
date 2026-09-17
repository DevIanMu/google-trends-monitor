# -*- coding: utf-8 -*-
"""
Google Trends 新词监测脚本

流程（对应手动操作）：
1. 对每个词根，取「热度上升的查询」(related_queries rising)，筛出变化 >= MIN_RISE 的词；
2. 候选词与基准词 GPTs 对比（过去 7 天），按基准词日均搜索量估算候选词搜索量，
   判断是否处于上升态势、是否有超过 GPTs 的趋势；
3. 对通过对比的词，拉取过去 1 年数据，若此前热度基本为 0 则判定为「新词」；
4. 结果写入 output/scan_YYYY-MM-DD.xlsx，并把新词合并进 output/new_words_table.xlsx（累计表，按词去重）。

用法：
    python trends_scan.py                 # 扫描 roots.txt 中的全部词根
    python trends_scan.py --min-rise 2000 # 调整上升幅度阈值（%）
    python trends_scan.py --limit 3       # 只扫描前 3 个词根（调试）

注意：Google Trends 未授权接口有频控，脚本已内置退避重试；
50 个词根全量扫描预计耗时 30~60 分钟，建议作为每日定时任务运行。
"""

import argparse
import datetime as dt
import json
import sys
import time
from pathlib import Path

import pandas as pd
from pytrends.request import TrendReq
from pytrends import exceptions as pytrends_exc

# ---------------- 配置 ----------------
BASELINE_TERM = "GPTs"          # 对比基准词
BASELINE_DAILY_SEARCHES = 5000  # 基准词日均搜索量（用于估算候选词搜索量）
GEO = ""                        # 地区："" = 全球
TIMEFRAME_RISING = "now 7-d"    # 上升查询的时间窗
TIMEFRAME_COMPARE = "now 7-d"   # 与基准词对比的时间窗
TIMEFRAME_YEAR = "today 12-m"   # 新词判定的时间窗
REQUEST_INTERVAL = 8            # 请求之间的常规间隔（秒）
RATE_LIMIT_WAIT = 90            # 遇到 429 后的退避等待（秒），会按重试次数递增
MAX_RETRIES = 8                 # 单请求最大重试次数（累计退避最长约 50 分钟）

ROOTS_FILE = Path(__file__).parent / "roots.txt"
OUT_DIR = Path(__file__).parent / "output"
TABLE_FILE = OUT_DIR / "new_words_table.xlsx"  # 累计新词表




def log(msg: str):
    print(f"[{dt.datetime.now():%H:%M:%S}] {msg}", flush=True)


def fresh_client() -> TrendReq:
    return TrendReq(hl="en-US", tz=360, timeout=(10, 25))


def call_with_retry(fn, desc: str):
    """执行 fn(client)，遇 429 退避重试，间隔递增。"""
    last_err = None
    for attempt in range(1, MAX_RETRIES + 1):
        client = fresh_client()
        try:
            time.sleep(REQUEST_INTERVAL)
            return fn(client)
        except pytrends_exc.TooManyRequestsError:
            wait = RATE_LIMIT_WAIT * attempt
            log(f"  触发限流(429)，等待 {wait}s 后重试 ({attempt}/{MAX_RETRIES})：{desc}")
            time.sleep(wait)
            last_err = "429"
        except (pytrends_exc.ResponseError, Exception) as e:  # noqa: BLE001
            last_err = f"{type(e).__name__}: {e}"
            log(f"  请求失败：{desc} -> {last_err}")
            break
    raise RuntimeError(f"{desc} 失败：{last_err}")


def get_rising_queries(root: str, min_rise: float) -> pd.DataFrame:
    def _q(client):
        client.build_payload([root], timeframe=TIMEFRAME_RISING, geo=GEO)
        rel = client.related_queries()
        return rel.get(root, {})

    data = call_with_retry(_q, f"词根[{root}] rising 查询")
    rising = data.get("rising")
    if rising is None or rising.empty:
        return pd.DataFrame(columns=["query", "value"])

    df = rising.copy()
    # "Breakout" 表示 >5000%，统一转成数值
    df["value"] = df["value"].apply(lambda v: 5000 if str(v).lower() == "breakout" else float(v))
    df = df[df["value"] >= min_rise].reset_index(drop=True)
    return df


def get_interest(kw_list, timeframe: str) -> pd.DataFrame:
    def _q(client):
        client.build_payload(kw_list, timeframe=timeframe, geo=GEO)
        df = client.interest_over_time()
        if df is None or df.empty:
            raise RuntimeError("interest_over_time 返回空数据")
        return df

    return call_with_retry(_q, f"热度数据 {kw_list} [{timeframe}]")


def analyze_compare(cand: str, cmp_df: pd.DataFrame) -> dict:
    """候选词与基准词过去 7 天的对比指标。"""
    res = {"cand_avg7": None, "baseline_avg7": None, "est_daily": None,
           "rising_trend": False, "exceeds_baseline": False, "last3_vs_first3": None}
    if cmp_df is None or cand not in cmp_df.columns:
        return res
    if BASELINE_TERM not in cmp_df.columns:
        return res

    c = cmp_df[cand].astype(float)
    b = cmp_df[BASELINE_TERM].astype(float)
    res["cand_avg7"] = round(c.mean(), 1)
    res["baseline_avg7"] = round(b.mean(), 1)

    if b.mean() > 0:
        ratio = c.mean() / b.mean()
        res["est_daily"] = round(ratio * BASELINE_DAILY_SEARCHES)
    first3, last3 = c.iloc[:3].mean(), c.iloc[-3:].mean()
    if first3 > 0:
        res["last3_vs_first3"] = round(last3 / first3, 2)
    res["rising_trend"] = bool(last3 > first3)
    if b.iloc[-3:].mean() > 0:
        res["exceeds_baseline"] = bool(c.iloc[-3:].mean() >= b.iloc[-3:].mean())
    return res


def analyze_year(cand: str, year_df: pd.DataFrame, recent_days: int = 21) -> dict:
    """过去一年热度：此前是否基本为 0。"""
    res = {"year_max_before": None, "nonzero_days_before": None, "is_new": False}
    if year_df is None or cand not in year_df.columns:
        return res
    s = year_df[cand].astype(float)
    before = s.iloc[:-recent_days] if len(s) > recent_days else s.iloc[:0]
    if len(before) == 0:
        return res
    res["year_max_before"] = float(before.max())
    res["nonzero_days_before"] = int((before > 0).sum())
    # 判定：近期之前全年最大值 <= 1（热度指数 0~100 尺度下视为此前没有热度）
    res["is_new"] = bool(before.max() <= 1)
    return res


def chunks(lst, n):
    for i in range(0, len(lst), n):
        yield lst[i:i + n]


def load_known_queries() -> set:
    """累计新词表中已收录的词（跳过其一年历史复查，节省请求）。"""
    if not TABLE_FILE.exists():
        return set()
    try:
        old = pd.read_excel(TABLE_FILE, sheet_name="new_words")
        return set(old["query"].dropna().astype(str).str.lower())
    except Exception:
        return set()


def scan_roots(roots, min_rise: float, progress_file: Path = None, state: dict = None):
    """扫描词根列表；每个词根完成后立即写入进度文件（JSONL），支持断点续扫。

    state: {"candidates": [...], "new_words": [...], "errors": [...]}，续扫时传入已恢复的状态。
    """
    candidates = state["candidates"] if state else []
    new_words = state["new_words"] if state else []
    errors = state["errors"] if state else []
    # 已知词 = 累计表 + 本次进度中已确认的新词（跳过其一年历史复查）
    known = load_known_queries()
    for r in candidates:
        if r.get("is_new"):
            known.add(r["query"].lower())
    if known:
        log(f"已有 {len(known)} 个已知词，将跳过其一年历史复查")

    def flush(root):
        if not progress_file:
            return
        rec = {
            "root": root,
            "candidates": [c for c in candidates if c.get("root") == root],
            "new_words": [n for n in new_words if n.get("root") == root],
            "errors": [e for e in errors if e.get("root") == root],
        }
        with open(progress_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")

    for idx, root in enumerate(roots, 1):
        log(f"({idx}/{len(roots)}) 词根：{root}")
        try:
            rising = get_rising_queries(root, min_rise)
        except RuntimeError as e:
            errors.append({"root": root, "stage": "rising", "error": str(e)})
            flush(root)
            continue
        if rising.empty:
            log(f"  无 >= {min_rise}% 的上升查询")
            flush(root)
            continue
        log(f"  命中 {len(rising)} 个候选：{', '.join(rising['query'].tolist())}")

        for batch in chunks(rising["query"].tolist(), 4):  # 4 候选 + 1 基准 = 5 词上限
            terms = batch + [BASELINE_TERM]
            try:
                cmp_df = get_interest(terms, TIMEFRAME_COMPARE)
            except RuntimeError as e:
                errors.append({"root": root, "stage": "compare", "error": str(e)})
                continue
            for cand in batch:
                row = {"root": root, "query": cand,
                       "rise_pct": float(rising.loc[rising["query"] == cand, "value"].iloc[0])}
                row.update(analyze_compare(cand, cmp_df))
                # 已在累计表中的词：标记为新词但不再请求一年数据
                if cand.lower() in known:
                    row.update({"year_max_before": 0, "nonzero_days_before": 0, "is_new": True})
                candidates.append(row)

        # 与基准词对比有看点（上升 或 接近/超过基准词）的候选，进入一年历史判定
        followups = [r["query"] for r in candidates
                     if r["root"] == root and (r["exceeds_baseline"] or r["rising_trend"])
                     and r["query"].lower() not in known]
        for batch in chunks(followups, 5):
            try:
                year_df = get_interest(batch, TIMEFRAME_YEAR)
            except RuntimeError as e:
                errors.append({"root": root, "stage": "year", "error": str(e)})
                continue
            for cand in batch:
                stats = analyze_year(cand, year_df)
                for r in candidates:
                    if r["root"] == root and r["query"] == cand:
                        r.update(stats)
                        if stats["is_new"] and (r["exceeds_baseline"] or r["rising_trend"]):
                            new_words.append(dict(r))
                            known.add(cand.lower())
                        break
        flush(root)

    # 已知词若仍满足热度条件，也保留在 new_words 中（累计表会去重）
    seen = set()
    for r in candidates:
        if r.get("is_new") and (r["exceeds_baseline"] or r["rising_trend"]):
            if r["query"].lower() not in seen:
                seen.add(r["query"].lower())
                if not any(n["query"] == r["query"] for n in new_words):
                    new_words.append(dict(r))

    return candidates, new_words, errors


def load_progress(progress_file: Path):
    """读取进度文件，返回 (state, 已完成的词根列表)。同一词根多次记录时取最后一次。"""
    state = {"candidates": [], "new_words": [], "errors": []}
    done = []
    if not progress_file.exists():
        return state, done
    per_root = {}
    for line in progress_file.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        rec = json.loads(line)
        per_root[rec["root"]] = rec
    for root, rec in per_root.items():
        done.append(root)
        state["candidates"].extend(rec.get("candidates", []))
        state["new_words"].extend(rec.get("new_words", []))
        state["errors"].extend(rec.get("errors", []))
    return state, done


def run_scan(roots, min_rise: float, scan_date: str):
    """带断点续扫的完整流程：恢复进度 → 扫描剩余词根 → 写出结果。"""
    OUT_DIR.mkdir(exist_ok=True)
    progress_file = OUT_DIR / f"progress_{scan_date}.jsonl"
    state, done = load_progress(progress_file)
    if done:
        log(f"从进度文件恢复：已完成 {len(done)} 个词根，本次续扫剩余部分")
    remaining = [r for r in roots if r not in set(done)]
    if remaining:
        scan_roots(remaining, min_rise, progress_file, state)
    # 进度中可能记录重复词根的重跑结果，写出前按 (root, query) 去重
    seen = set()
    cand_dedup = []
    for c in state["candidates"]:
        key = (c.get("root"), c.get("query"))
        if key not in seen:
            seen.add(key)
            cand_dedup.append(c)
    state["candidates"] = cand_dedup
    scan_file = write_outputs(state["candidates"], state["new_words"], state["errors"], scan_date)
    return state["candidates"], state["new_words"], state["errors"], scan_file


def write_outputs(candidates, new_words, errors, scan_date: str):
    OUT_DIR.mkdir(exist_ok=True)
    scan_file = OUT_DIR / f"scan_{scan_date}.xlsx"

    df_cand = pd.DataFrame(candidates)
    df_new = pd.DataFrame(new_words)
    df_err = pd.DataFrame(errors)

    with pd.ExcelWriter(scan_file, engine="openpyxl") as w:
        (df_new if not df_new.empty else pd.DataFrame([{"提示": "本次未发现新词"}])).to_excel(w, sheet_name="new_words", index=False)
        (df_cand if not df_cand.empty else pd.DataFrame([{"提示": "无候选词"}])).to_excel(w, sheet_name="candidates", index=False)
        (df_err if not df_err.empty else pd.DataFrame([{"提示": "无错误"}])).to_excel(w, sheet_name="errors", index=False)
    log(f"本次扫描结果已写入：{scan_file}")

    # 合并进累计新词表（按 query 去重，保留首次发现日期）
    if not df_new.empty:
        df_new = df_new.copy()
        df_new["first_seen"] = scan_date
        old = None
        if TABLE_FILE.exists():
            try:
                old = pd.read_excel(TABLE_FILE, sheet_name="new_words")
            except Exception:
                log(f"  累计表文件损坏，将重建：{TABLE_FILE}")
        if old is not None:
            merged = pd.concat([old, df_new], ignore_index=True)
            merged = merged.drop_duplicates(subset=["query"], keep="first")
        else:
            merged = df_new
        with pd.ExcelWriter(TABLE_FILE, engine="openpyxl") as w:
            merged.to_excel(w, sheet_name="new_words", index=False)
        log(f"累计新词表已更新：{TABLE_FILE}（共 {len(merged)} 个词）")

    return scan_file


def main():
    ap = argparse.ArgumentParser(description="Google Trends 新词监测")
    ap.add_argument("--min-rise", type=float, default=1000, help="上升幅度阈值(%%)，默认 1000")
    ap.add_argument("--limit", type=int, default=0, help="只扫描前 N 个词根（调试用）")
    ap.add_argument("--skip", type=int, default=0, help="跳过前 N 个词根（配合 --limit 分批扫描）")
    args = ap.parse_args()

    if not ROOTS_FILE.exists():
        sys.exit(f"未找到词根文件：{ROOTS_FILE}，请先在每行填写一个词根。")
    roots = [l.strip() for l in ROOTS_FILE.read_text(encoding="utf-8").splitlines()
             if l.strip() and not l.strip().startswith("#")]
    if args.skip:
        roots = roots[args.skip:]
    if args.limit:
        roots = roots[: args.limit]
    if not roots:
        sys.exit("roots.txt 中没有有效词根。")

    scan_date = dt.date.today().isoformat()
    log(f"开始扫描 {len(roots)} 个词根（阈值：上升 >= {args.min_rise}%，基准词：{BASELINE_TERM}，"
        f"按日均 {BASELINE_DAILY_SEARCHES} 次估算）")

    candidates, new_words, errors, scan_file = run_scan(roots, args.min_rise, scan_date)

    print("\n===== 结果汇总 =====")
    print(f"候选词（>= {args.min_rise}%）：{len(candidates)} 个")
    print(f"新词：{len(new_words)} 个")
    for r in new_words:
        print(f"  ★ {r['query']}  |  词根:{r['root']}  上升:{r['rise_pct']:.0f}%  "
              f"估算日搜索量:{r.get('est_daily')}  超过{BASELINE_TERM}:{'是' if r.get('exceeds_baseline') else '否'}")
    if errors:
        print(f"失败请求：{len(errors)} 个（详见 errors 工作表）")
    print(f"明细文件：{scan_file}")


if __name__ == "__main__":
    main()
