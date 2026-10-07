# Flood Event Agent baseline：开发约束

这个仓库是原型的精简公开副本。源目录采用 `C:/Users/SatelluS/我的云端硬盘/flood geo ai`，依据是实现所在地；这不是从空 `answers` 得到的用户确认。拟公开目标为 `Satelius/flood-geo-ai`。CoAutoResearch 研究控制仓库、会话、历史报告、凭据、实时数据库和日志不属于公开范围。

## 结构与运行

后端要求 Python 3.10+，仅标准库；前端提供 Leaflet 1.9.4 的本地 JS/CSS 与 BSD 2-Clause 许可，不要求 Node 构建。`run.py` 启动仅监听 `127.0.0.1` 的服务并加载人工示范。`clients.py` 负责外部请求，`normalize.py` 负责规范化和状态，`store.py` 负责原始快照与回放，`extract.py` 只返回原文文字候选，`web/` 负责展示。

在仓库根目录运行既有离线检查和示范：

```powershell
python -B -m unittest discover -s tests -v
python -B run.py
python -B run.py --import-archive data/research/pub-2025-05-22.json
```

后二者是不同的启动方式，不要同时占用默认端口。测试和离线示范不需要真实凭据，不得为检查而启动 `--poll`。默认 SQLite 文件是 `data/flood.sqlite3`，运行数据不得提交。公开副本不包含 `reports/`；源码中的 `/report` 入口会返回缺失报告的 404，应继续明确这一限制，不能声称历史报告随仓库可运行。

## 数据契约

- 保存原始响应与失败证据；消息按来源和稳定键去重。保持 `demo`、`live`、`archive` 隔离，不跨来源取消或混合评测。
- 保留缺失字段、无效坐标、`unknown`、`cancelled` 和 `expired` 的语义。`observed_active` 仅表示本地观测窗口内未见匹配取消。
- GeoJSON 使用 `[经度, 纬度]`；警报 `Point` 表示广播中心。广播半径不产生淹水面。取消引用匹配 `sender`、`identifier`、`sent`。
- 现有 `alert_message_count` 是所有非 Cancel 消息，包括 unknown 类型；不要在文档中改称严格 Alert 数或真实洪水事件数。现有直方图只表示已观测发布日分布，没有覆盖状态日历。
- 规则与 LLM 候选需逐字段回查原文，保持 `verification_status=candidate`。文字候选、合成道路段和 `via` 路线都不是道路封闭或避洪安全证据。

## 修改边界与证据

优先保持函数单一、命名清晰、兼容已有行为。先读相关源码和既有测试，再作必要修改；不要只为了制造工作量新增镜像实现的测试。说明经过何种检查以及尚未验证什么，不把历史 44-test 或旧接口结果写成新结果。重大修改需由另一位审查者复核最终代码、文档和待提交内容。

道路拓扑、GeoLibre、本地安装、历史回填、评测集和 A/B/C 对比属于后续独立研究，不能从当前演示推断已验证。后续顺序是历史可用性调查 → 模式分析 → 可获得范围回填和持续归档 → 覆盖日历/直方图 → 人工评测集 → 同条件 A/B/C；新加坡窗口为 `2024-10-07` 至 `2026-10-06`，当天单列未结束。

真实模型调用保留用户政策：仅 `gpt-6-luna`，reasoning 至多 `xhigh`。当前 `extract.py` **没有强制该政策且参数兼容性未验证**，因此保持 `LLM_API_KEY` / `LLM_MODEL` 未配置；适配器改造和实测需要后续正式批准的任务，不能以当前运行命令默认为授权。

## 公开文件与许可

禁止提交 `.env`、token、API key、个人信息、SQLite、日志、缓存、私有会话和源 Git 历史。`.env.example` 的凭据值必须为空。保留 Leaflet 许可和第三方说明；OneMap 只保留链接及 Singapore Land Authority 署名，不分发其标志图片或瓦片。真实样本仅保留 `data/research/pub-2025-05-22.json`，署名与日期限制见 `THIRD_PARTY_NOTICES.md`。没有为用户代码指定开源许可，不要自动新增 `LICENSE` 或宣称第三方机构认可项目。


## 本轮交付与来源修订

此前 publication-attempt-01 因原保存凭据文件中无法选出 GitHub PAT，在账户 API、远端检查、创建或推送前停止；该历史尝试没有验证公开链接。后续尝试必须按其独立操作记录与远端核验判断，不能将历史停止状态当作永久现状，也不能预先宣称成功。恢复时使用明确且经授权的 token，再沿正式流程检查账号、远端历史和最终内容；不要将凭据复制进本仓库。

样本历史探测元数据 `experiments/service_probe.json` 通过 `raw_path` 与 SHA-256 绑定当前保留字节，声明获取时间为 `2026-10-04T17:13:02.198911+00:00`。这是历史探测声明时间，不是本轮获取或独立时钟验证；2026-10-07 仅为本地检查日期。元数据及其他响应不随副本分发。

r1 保存实现已有 44 项离线回归成功记录；r2/r3/r4 只修订文档和来源说明，代码、测试、配置及两份样本字节不变，以逐文件一致性复用 r1 检查，不重跑或改写原记录。该检查不能证明 LLM、地理精度、覆盖完整或道路安全。最终文档和待提交内容须独立复核；不得将较早版本的审查批准套用到改变后的字节。
