# Google Trends 新词监测

自动完成「词根 → 上升查询筛选 → 与 GPTs 对比 → 新词判定 → 新词表」的每日流程，
无需官方 API（基于 pytrends 访问 Google Trends 内部接口）。

## 使用方法

1. 编辑 `roots.txt`，每行一个词根（`#` 开头为注释），把 50+ 词根填进去；
2. 运行：

```bash
python trends_scan.py                 # 全量扫描
python trends_scan.py --min-rise 2000 # 提高上升幅度阈值（%，默认 1000）
python trends_scan.py --limit 3       # 只扫前 3 个词根（调试）
```

## 输出（output/ 目录）

- `scan_YYYY-MM-DD.xlsx`：当日扫描明细
  - `new_words`：通过全部判定的新词（此前一年热度为 0）
  - `candidates`：所有 ≥ 阈值 的候选词及对比指标
  - `errors`：失败的请求
- `new_words_table.xlsx`：累计新词表（按词去重，保留首次发现日期）

## 判定逻辑（与手动流程一一对应）

1. 词根「热度上升的查询」中筛出变化 ≥ `MIN_RISE`（默认 1000%，"Breakout" 记为 5000%）；
2. 候选词与基准词 `GPTs` 对比过去 7 天：按 GPTs 日均 5000 次搜索估算候选词搜索量，
   标记 `rising_trend`（后 3 天均值 > 前 3 天）与 `exceeds_baseline`（近 3 天均值 ≥ GPTs）；
3. 对有看点的候选词拉取过去一年数据：`year_max_before` ≤ 1 判定为 `is_new`；
4. `is_new` 且（上升 或 超过基准词）→ 写入新词表。

## 注意事项

- **定时任务已按分批轮换执行**：每天 2:33 / 8:33 / 14:33 / 20:33 各一次，每次自动取 10 个词根
  （扫描位置存于 `output/scan_cursor.txt`，约 5 天全覆盖一轮后循环）；
- Google Trends 未授权接口有频控：脚本已内置 429 退避重试，且**每个词根完成即写入
  `output/progress_日期.jsonl`**，运行中断后下次自动断点续扫，不会丢进度；
- 高频触发会招致 Google 对该 IP 的长时间（1 小时+）封锁，浏览器手动查询不受影响；
  请勿短时间内反复手动触发；
- 若单批运行超时，可分批：`python trends_scan.py --skip 0 --limit 15`、
  `--skip 15 --limit 15`……结果会自动合并进累计表；
- 主要配置（基准词、估算搜索量、地区、阈值等）见 `trends_scan.py` 顶部「配置」区。
