# Flood Event Agent baseline

30 年历史扩展的执行规则与来源限制见 [`docs/HISTORY_BACKFILL_30Y_PLAN.md`](docs/HISTORY_BACKFILL_30Y_PLAN.md)。

Python 3.10+，核心原型无需第三方运行依赖。独立的 PUB Telegram 研究采集脚本使用 `requests` 和 `beautifulsoup4`。项目用于研究演示，不提供道路安全判断。

```powershell
python run.py
```

访问 http://127.0.0.1:8765 查看交互地图。首次启动加载固定日期 2026-09-24 的人工合成示范。按时间回放可观察警报、取消、过期、未知及无效坐标；所有示范数据与实时、历史数据隔离。历史覆盖报告和旧 baseline 报告由本地数据生成，不包含在公开代码仓库中；只有本地生成对应文件后才能访问。

```powershell
python run.py --import-archive data/research/pub-2025-05-22.json
python run.py --poll
python -m unittest discover -s tests -v
```

`--poll` 明确启用每 120 秒实时采集，默认通过网页按钮手动采集。复制 `.env.example` 为 `.env` 并填入服务凭据；已有 `.env` 时保留其内容。真实 OneMap 路由需要 ONEMAP_TOKEN；旧 `/api/extract` 的可选 LLM 提取需要 LLM_API_KEY / LLM_MODEL。凭据只在本地后端使用。未配置凭据时显示可读错误，不以示范结果冒充真实服务调用。2026-10-05 已完成 PUB 与 OneMap 真实调用。历史回填与日历实验未调用 LLM；2026-10-09 完成了隔离的 reading 级 LLM→GIS 基线批次，结果见下文。网络失败时离线演示仍可使用。

数据保存在 `data/flood.sqlite3`。原始响应先保存，消息按 source 和稳定键去重，位置按内容键去重，保留无效位置的原文。警报点代表广播中心，半径仅作属性，不生成淹水面。缺少标识、消息类型或覆盖证据时状态为 unknown。取消需匹配 sender、identifier、sent 三元引用；archive 回放使用发布时间并明确本地覆盖限制。

GET `/api/events?source=demo&at=2026-09-24T10:20:00%2B08:00` 返回 GeoJSON、metadata、timeline 与 histogram。GET `/api/status` 返回观测与配置状态。POST `/api/ingest` 输入 source=live，或 source=archive 和 date。POST `/api/route` 输入 start/end=[经度,纬度]、route_type、可选 via。POST `/api/extract` 输入 message_key、source、mode=rules/llm。GET `/api/demo-route` 返回明确标注的合成路线。

历史接口按日期归档并记覆盖状态。30 年目标窗口的示例查询为 GET `/api/coverage?source=archive&start=1996-10-08&end=2026-10-07`；日期首尾均包含，最多查询 12,000 天。PUB API 的 `date` 参数是单日/日期时间筛选，不是多日区间；批量历史依日期逐日查询。官方没有承诺历史保留年限，因此成功空响应只表示该次接口查询未返回可归一化警报，不证明该日期现实中没有洪水或更长时段档案完整。`not_attempted`、`running`、`failed`、`incomplete`、`unavailable` 都是未知状态；这些日期的主计数字段返回 JSON `null`，本地已收到的内容通过 `observed_*_lower_bound` 单列。只有完整分页结束后才会标记 `complete_zero` 或 `complete_nonzero`。日历计数以唯一消息的有效 `sent` 为准，缺失或无效时回退到有效 `record.datetime`；不会用请求日或抓取时间填历史日期。每次日期查询的 `queried_undated_message_count` 记录该响应中没有有效发布时间的非 Cancel 消息数；这些消息不会归入任何日期，Cancel 不纳入警报消息计数。只有 `query_metrics_version=1` 的新口径采集会返回 `queried_*` 分页统计；旧记录的指标版本为 0，其查询审计字段在接口中返回 `null`，避免把旧 `queried_alert_message_count`（明确 Alert）与新口径（全部非 Cancel 消息）混用。`message_count` 和旧字段 `alert_message_count` 仍统计非 Cancel 的去重消息，后者保留兼容语义；`explicit_alert_message_count` 只计明确 `msgType=Alert`，`unknown_msg_type_count` 单列未知类型，`reading_count` 计地点条目。

开始历史回填：

```powershell
Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8765/api/backfill `
  -ContentType 'application/json' `
  -Body '{"start":"1996-10-08","end":"2026-10-07","budget":20}'
```

最近 30 年逐日回填已完成，批次 ID 为 `5bef63d4c09d465287bc14a43aa1cfcb`：10,957/10,957 天完成，包含 21,180 页和 262,869 条 API 原始 records。实际 `record.datetime` 仅覆盖 2025-05-22 至 2026-10-07；完成 30 年逐日查询不代表 PUB 有 30 年历史档案。可用 GET `/api/backfill/5bef63d4c09d465287bc14a43aa1cfcb` 查看批次终态。

`budget` 是本次最多尝试的日期数。保存返回的 `batch_id`，用 GET `/api/backfill/{batch_id}` 查看进度；POST `/api/backfill/{batch_id}/pause` 在当前日期请求完成后暂停，POST `/api/backfill/{batch_id}/resume` 续跑。失败和未完成日期会从头重采并依靠消息去重；`unavailable` 默认不重试，可在续跑请求中传 `retry_unavailable: true`。默认真实请求共享两秒间隔，单个日期最多跟随100页；分页循环、结构错误、HTTP失败不会标成零警报日。单日详情可用 GET `/api/events?source=archive&at=2025-05-22T23:59:59%2B08:00&date=2025-05-22`，`date` 过滤只限制返回详情，消息状态仍按 `at` 计算。

LLM 只能提取原文候选，不输出几何或封路判断；输出逐字段核对是否原文子串。规则提取是当前可重复 baseline。经 via 生成的 OneMap 路线属于情景模拟，未证明避洪。公开 OneMap 驾车接口没有直接避让区或出发时间参数。

官方资料：

- https://data.gov.sg/datasets/d_f1404e08587ce555b9ea3f565e2eb9a3/view
- https://api-open.data.gov.sg/v2/real-time/api/weather/flood-alerts
- https://www.onemap.gov.sg/apidocs/routing
- https://www.onemap.gov.sg/apidocs/authentication
- https://datatracker.ietf.org/doc/html/rfc7946

## 实验与中文报告

### Reading 级 LLM→GIS 基线

2026-10-09 对冻结的 16 条 `item.readings[i]` 运行了一次 `gpt-6-luna` / `medium` Responses API 批次，每条最多一次请求；16 次均收到响应，没有重试。结果为 11 条通过结构与证据自动校验、5 条 `evidence_error`，16 条均由官方 `area.circle` 生成 WGS84 广播中心 Point。自动通过不等于语义正确，人工标签尚未完成，因此不报告准确率、precision、recall 或 F1。

5 条证据校验失败均与证据路径格式有关：模型输出使用 `/description` 形式的 JSON Pointer，而当前校验器按计划约定的 `$.description` 形式解析。原始模型响应已保留，报告页逐条展示；该状态不构成 5 条语义错误的判断。下一轮应在 prompt/schema 中写清路径语法，或在校验器中一致支持两种路径格式，再另行冻结实验协议。

中文实现报告由本地封账结果生成。报告、原始请求/响应、GeoJSON 与审阅页不随代码公开，因为它们包含数据样本或模型输出；本地配置密钥也不进入这些产物。本实验只覆盖固定 16 条探索样本，不扫描完整数据库，不关联真实道路段，也不证明实际洪水范围或路线避险效果。

runner 支持读取本地环境变量 `OPENAI_API_KEY` 或 `LLM_API_KEY`。先用 `--prepare-only` 冻结 manifest，再使用 manifest SHA 运行；`--seal-only` 只从本地 ledger 恢复，不读取凭据也不联网。不要把密钥放进命令行或报告。

### PUB Telegram 事件通报语料

2026-10-09 增加独立的公开网页采集与模式分析。窗口按 SGT 为 `[2025-10-09, 2026-10-09)`；15 页取得 298 条可见消息，其中 14 条洪水发生、15 条消退、151 条来源自带的风险通知、117 条暴雨通知、1 条其他消息。发生/消退共 29 条地点文本，尚未配对为真实事件或关联道路段。当前项目没有风险预测 baseline。

来源、上游数据可访问性、Telegram 历史读取方式及许可限制见 [`docs/PUB_TELEGRAM_SOURCE_RESEARCH.md`](docs/PUB_TELEGRAM_SOURCE_RESEARCH.md)。原始网页、采集缓存、消息、逐条标签和搜索报告都属于本地研究产物，不随代码公开。

```powershell
python -m pip install requests beautifulsoup4
python scripts/collect_pub_telegram.py --start 2025-10-09 --end 2026-10-09
python scripts/analyze_pub_telegram.py
```

采集器使用缓存、顺序分页、有限重试和日期边界停止。公开网页遍历完成不代表官方档案完整，消息 ID 缺号也不能直接解释为遗漏的事件；12 条消息有可见编辑标志，采集的是当前可见版本。独立语料未写入 `data/flood.sqlite3`，没有调用 LLM。Telegram 当前条款限制 AI 开发与评测用途；这里的技术采集结果不构成研究复用许可，应向 PUB 申请直接历史导出及明确许可后再开展相关 LLM 数据集实验。

同一输出目录重跑会复用采集时的缓存。需要新抓当前版本时用 `--output data/telegram_pub_YYYYMMDD` 指定新目录，保留旧样本用于复核；它目前不是自动刷新的增量订阅器。采集与解析的 12 项离线测试可用 `python -m unittest tests.test_pub_telegram tests.test_pub_telegram_analysis -v` 运行。

历史覆盖报告由本地数据库生成并写入 `reports/history.html`，不随代码公开。准备本地历史数据后可重新生成：

```powershell
python scripts/report_history_availability.py
```

旧 v0.1 报告和历史研究图表由本地数据生成，不随代码公开。历史统计依赖本地 archive/live 响应中的 `record.datetime`，空响应不证明现实中没有洪水，也不能证明官方历史档案完整。报告区分逐日查询覆盖、实际历史数据范围、真实接口调用与合成情景，不把历史警报与当前路线当成同期交通观测。

```powershell
python scripts/probe_sources.py
python scripts/run_route_probe.py
python scripts/capture_runtime.py
```

`capture_runtime.py` 需要本地原型服务已启动；服务探测与路由实验需要网络，真实路线实验需要有效 OneMap 令牌。重新执行将更新本次实验结果。浏览器核验记录和原型截图来自实际操作，并不由这组命令自动生成。
