# AGENTS.md — Google Trends 新词监测项目索引

> 本文件是写给维护者（人和 AI Agent）的项目地图。改动代码前先读这里。

## 1. 项目目的

从 184 个词根（`roots.txt`，按 14 大类组织，含日/西/葡/德/法/韩多语种工具类）出发，每日从 Google Trends 自动发现
**新兴且处于上升趋势的搜索词**，判定标准复刻人工流程：

词根 rising 查询（≥1000%）→ 与基准词 `GPTs` 对比估算搜索量 → 确认上升趋势 →
查一年历史判定是否为新词 → 写入累计新词表。

**不使用 Google Trends 官方 API**（无 Key 可用），通过 `pytrends` 访问其内部接口。

## 2. 文件地图

```
D:\Projects\trends-monitor\
├── agents.md              # 本文件
├── README.md              # 使用说明（面向用户）
├── roots.txt              # 词根清单（每行一个，# 注释；增删后轮换自动适配）
├── trends_scan.py         # 核心库 + CLI：所有扫描/判定/输出逻辑都在这
├── __pycache__/           # 运行缓存，勿提交
└── output/
    ├── new_words_table.xlsx    # ★ 累计新词表（query 去重，含 first_seen）
    ├── scan_YYYY-MM-DD.xlsx    # 当日明细：new_words / candidates / errors 三表
    ├── progress_YYYY-MM-DD.jsonl # 断点续扫进度（每词根完成即追加一行）
    └── scan_cursor.txt         # 轮换游标（定时任务用，值为下一个待扫词根下标）
```

定时任务入口（不在本目录）：
`...\daimon\agents\main\blueprint\automations\automation_bb0d9727-b27f-4c8a-9e6b-76d10de7d433\assets\trends_scan_entry.py`

## 3. 运行方式

| 方式 | 命令/触发 | 说明 |
|---|---|---|
| CLI 全量/分批 | `python trends_scan.py`（`--skip N --limit M`、`--min-rise 2000`） | 交互/调试用 |
| 定时任务 | 每天 2:33 / 8:33 / 14:33 / 20:33（cron `33 2,8,14,20 * * *`，Asia/Shanghai） | 每次自动取 10 个词根轮换 |

定时任务为 **Python 代码任务**：触发时本地直接跑脚本，不启动对话、不耗额度；
需要 Kimi 桌面应用在线，错过不补跑（下轮自动续）。

## 4. 核心流程（trends_scan.py）

```
run_scan(roots, min_rise, scan_date)
 ├─ load_progress()            # 读 progress_当日.jsonl，恢复已完成的词根
 ├─ scan_roots(剩余词根)        # 每词根完成 → flush() 追加进度行
 │   ├─ get_rising_queries()   # related_queries(rising)，筛 ≥min_rise（"Breakout"=5000）
 │   ├─ get_interest(候选+GPTs) # 一批 4 候选+基准词合并请求
 │   │   → analyze_compare()   # est_daily = 候选/GPTs 均值比 × 5000；rising_trend；exceeds_baseline
 │   └─ get_interest(候选, 1年) # 仅对有看点的候选，一批 5 个
 │       → analyze_year()      # is_new = 近 21 天之前全年最大热度 ≤ 1
 └─ write_outputs()            # 当日 Excel + 合并累计表（按 query 去重）
```

关键函数：`call_with_retry()`（429 退避重试，增量等待）、`load_known_queries()`
（累计表中的词跳过一年复查）。配置区在文件顶部（基准词、估算搜索量、阈值、间隔等）。

## 5. 状态文件语义（改动前必须理解）

- **`scan_cursor.txt`**：只被定时入口读写。CLI 全量扫描不影响它。
- **`progress_*.jsonl`**：当日断点续扫依据；同词根重复记录取最后一次。
  **次日自动失效**（文件名按日期），新一天重新全量扫当日批次。
- **`new_words_table.xlsx`**：唯一跨天累积的状态，合并时按 `query` 去重保留首次日期。
  手动删词：直接编辑此表即可（下次扫描会重新识别为未知词）。

## 6. 限流红线（重要）

Google 对未授权接口按 **IP + 客户端指纹** 限流，触发后该 IP 会被封锁 1 小时以上
（浏览器人工查询不受影响，两者判定通道不同）。项目历史上因密集调试两次触发封锁：

- ✅ 每天四班（间隔 6 小时）、每班 10 词根是当前安全水位（单次 ~25 分钟、0 失败）
- ❌ 禁止：短时间内反复手动触发、并发跑多个扫描、把 `REQUEST_INTERVAL` 调到 5s 以下
- 遇到 429：脚本自动退避（最长 ~50 分钟），**不要人工加塞重试**
- 手动测试用 `python trends_scan.py --limit 1~2`，跑完间隔几分钟再试

## 7. 输出字段字典（candidates / new_words 表）

| 字段 | 含义 |
|---|---|
| `root` / `query` | 来源词根 / 候选搜索词 |
| `rise_pct` | 上升幅度 %（"Breakout" 记 5000） |
| `cand_avg7` / `baseline_avg7` | 候选词 / GPTs 过去 7 天平均热度指数 |
| `est_daily` | 估算日搜索量（相对 GPTs 日均 5000 次的比值） |
| `rising_trend` | 后 3 天均值 > 前 3 天 |
| `exceeds_baseline` | 近 3 天均值 ≥ GPTs（**仅展示，不参与判定**） |
| `year_max_before` / `nonzero_days_before` | 近 21 天之前一年的最大热度 / 非零天数 |
| `is_new` | `year_max_before` ≤ 1 |
| `first_seen` | 仅累计表有：首次入库日期 |

入库规则：`is_new` 且 `rising_trend` 且 `est_daily` ≥ `MIN_EST_DAILY`（默认 50 次/日）。
（2026-09 调整：原 `exceeds_baseline` 门槛因组内归一化对新词几乎永不触发，且会误伤
非英语词根；改为绝对体量下限挡 0→1 噪声，`exceeds_baseline` 降级为展示字段。）

## 8. 环境与已知坑

- Python 用 Kimi Work 托管运行时（已装 `pytrends 4.9.2`）；`requests` 自动走系统代理，
  curl 不通属正常现象，勿据此判断网络故障。
- **pandas 3.x**：`to_excel` 的 sheet 名必须 `sheet_name=` 关键字传参；无 `df.append`。
- 文件一律 UTF-8；CLI 运行时加 `-X utf8` 防 Windows 控制台编码问题。
- 出口为共享代理 IP，限流阈值会随时段波动；排障先看 run logs 再探测单请求。

## 9. 定时任务运维

- 任务 ID：`automation_bb0d9727-b27f-4c8a-9e6b-76d10de7d433`（标题：Google Trends 新词每日扫描）
- 查运行记录：`AutomationControl listRuns`；产物：`readRunArtifact`；日志：`readRunLogs`
- 运行超时 1 小时属正常上限；超时/跳过后**无需人工干预**，下一轮自动续扫
- 一轮全覆盖约 19 批次 ≈ 5 天（每天 4 班）；词根增删后轮换自动按新清单继续

## 10. 词根维护原则（加词根前必读）

新增词根（尤其非英语语种）必须满足「母语者直觉搜索词」标准，**禁止从英文直译**：

1. **优先母语原生搜索格式**：动词不定式/动名词优先于翻译后缀。
   例：西语 `hacer`、葡语 `fazer`/`baixar`、法语 `faire`/`télécharger`、德语 `umwandeln`/`erstellen`、
   韩语 `만들기`（○○ 만들기）、日语 `診断`（○○診断）。
2. **「免费/在线」用各语言的真实用词**：葡语 `grátis`、法语 `en ligne`（而非 online）、
   德语 `Kostenlos`、日语 `無料`、韩语 `묵료`——这是各语种的刚需修饰词。
3. **各语言有独特模式时必须收录**：日语 〜メーカー、韩语 ~기/~기계、西/葡语 ~dor 后缀。
4. **跨语种重复词只收一次**（如 Generator 已在英文类，德语类不再重复收录）。
5. **加词根后用码点校验非拉丁字符**（日语假名、韩文、法语重音符号易在编辑中损坏）；
   韩语「묵료→무료」、日语「診断」为历史踩坑点。
6. 增删后轮换自动适配；词根总数与覆盖周期见第 1 节，文档中的数量表述要同步更新。
