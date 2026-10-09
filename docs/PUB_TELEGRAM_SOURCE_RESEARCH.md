# PUB Telegram 信息来源与采集方式调研

调研日期：2026-10-09（Asia/Singapore / UTC+08）。本阶段研究对象是洪水发生与消退通报；频道里的暴雨及洪水风险消息是辅助类别，不代表项目已经建立洪水风险预测 baseline。

## 来源与可追溯性

PUB 官方资料说明，Joint Operations Centre（JOC）由人员全天监测；Catchment and Waterways Operations System（CWOS）整合超过 1,000 个排水沟/运河水位传感器、超过 500 路 CCTV、雨量计、天气雷达及洪水响应车辆位置。PUB 的 X-band 雷达提供提前约 30 分钟的雨量分布预测，用于调度 Quick Response Teams（QRT）。这是已证实的业务上游体系。[PUB 当前监测说明](https://www.pub.gov.sg/Public/KeyInitiatives/Flood-Resilience/Flood-Forecasting-and-Monitoring)

Telegram 是 PUB 对公众发布消息的渠道。官方材料将 myENV、Telegram、X 与 ERP 2.0 列为传播平台；QRT 车辆也将现场位置和影像传回 JOC。[PUB 洪水响应介绍](https://www.pub.gov.sg/-/media/PUB/Flood-Resilience/Documents/Flood-Resilience-A2-Panels.pdf)

上述资料没有提供“每条发生通报由哪个传感器、哪帧 CCTV、哪位现场人员确认”的公开映射，也没有说明所有消息都是某个阈值自动触发。本次未确认 CWOS 内部原始数据的公开 API。因此，数据集可以保留发布方与消息证据，不能自行补出单条通报的传感器证据链。

暴雨消息文本明确标注 NEA；PUB 的 SMS 页面说明 Heavy Rain Warning 来自 Meteorological Service Singapore（NEA）。这能够支持机构来源归属，不能证明 Telegram 与某个 RSS 接口之间存在一对一同步契约。[PUB SMS 服务](https://www.pub.gov.sg/Public/KeyInitiatives/Flood-Resilience/Flood-Forecasting-and-Monitoring/Subscribe-to-SMS-Alerts)

## 哪些上游数据可访问

| 数据 | 官方公开渠道 | 本次访问结果 | 能支持什么 |
|---|---|---|---|
| NEA 测站降雨 | [Rainfall 数据集](https://data.gov.sg/datasets/d_6580738cdd7db79374ed3152159fbd69/view)，`https://api-open.data.gov.sg/v2/real-time/api/rainfall` | 无密钥 HTTP 200 | 测站位置与每 5 分钟降雨观测；属于天气观测，不等于洪水发生 |
| PUB 水位测站目录 | [PUB Water Level Sensors](https://data.gov.sg/datasets/d_31333fa5cf0834f012d840365b336610/view) | poll-download HTTP 201、code 0；XLSX 下载 HTTP 200 | 实测文件 328 行测站，字段 Station ID、Station Name、X、Y；没有观测时间或水位数值，是静态测站图层 |
| 动态水位 | PUB [SMS 服务](https://www.pub.gov.sg/Public/KeyInitiatives/Flood-Resilience/Flood-Forecasting-and-Monitoring/Subscribe-to-SMS-Alerts) | 官方提供按测站订阅阈值消息 | 公开通知服务；本次未确认可批量查询历史动态水位的公开 REST API |
| Heavy Rain Warning | [NEA RSS 列表](https://www.nea.gov.sg/rss/rss-feeds)，`http://www.weather.gov.sg/files/rss/rssHeavyRain_new.xml` | HTTPS www 地址本机 HTTP 403；网页检索工具识别为 XML、无法渲染 | 官方公布了 RSS，但本次未完成内容读取与历史能力验证 |
| CCTV | 官方 [排水手册](https://www.pub.gov.sg/-/media/PUB/PDF/managingUrbanRunoff.pdf) 仍列旧地址 `https://app.pub.gov.sg/waterlevel/pages/WaterLevelSensors.aspx` | 本机 DNS 解析失败，检索工具也未取得页面 | 说明曾有公开查看渠道；不能据旧链接声称当前可下载全部 CCTV 或历史影像 |
| 雷达、车辆位置、CWOS 汇总 | PUB 监测说明 | 未发现有文档支持的开放接口 | 可描述机构上游流程，暂不能直接接入原始数据 |

水位测站目录标有 Open Data Licence；这个许可仅适用于该目录，不自动覆盖 Telegram 消息。X/Y 的坐标系须在后续 GIS 转换时核实，不能直接当经纬度。

## 当前频道身份

当前 [公开频道](https://t.me/s/pubfloodalerts) 显示名称 PUB Flood Alerts、认证标识及 PUB 官方频道说明；PUB 的 [2022 官方新闻](https://www.pub.gov.sg/Resources/News-Room/PressReleases/2022/12/Building-Singapore-Flood-Resilience-in-a-Changing-Climate) 也指向 `t.me/pubfloodalerts`。

旧 [2021 官方新闻](https://www.pub.gov.sg/Resources/News-Room/PressReleases/2021/11/PUB-boosts-flood-response-capabilities-ahead-of-Northeast-Monsoon) 说明频道于 2021-11-10 上线，但当时链接是 `t.me/sgflood`。本次实测 `https://t.me/s/sgflood` 返回另一个非 PUB 频道，标题包含 SHIP GRAVEYARD、内容为俄文、可见消息时间为 2024-05。因此，不能将当前 `sgflood` 当作 PUB 历史别名，也不能合并其消息。旧 URL 不是稳定频道身份；用 MTProto 时应保存不可变的频道 ID。

## 批量历史读取方式

1. **Telegram 用户 API / MTProto。** 官方 [`messages.getHistory`](https://core.telegram.org/method/messages.getHistory) 返回倒序历史，支持 offset_id、offset_date、limit、max_id、min_id；官方明确该方法只能由用户账号调用。[分页说明](https://core.telegram.org/api/offsets) 给出了使用 offset_id 读取更早消息的方法。需要用户 Telegram 账号授权，以及从 [应用创建流程](https://core.telegram.org/api/obtaining_api_id) 获得 api_id/api_hash；这些并不是 Bot token。技术上适合历史回填，并可保存消息编辑时间、媒体及频道 ID，但可访问历史仍受删除、权限与服务限制影响。
2. **Bot API。** [`getUpdates` / webhook](https://core.telegram.org/bots/api#getting-updates) 面向机器人可接收的更新，未交付更新最多保留 24 小时。它不能替代过去一年频道历史查询；我们的机器人也无法自行取得 PUB 频道管理权限。
3. **公开网页预览。** 本次 `https://t.me/s/pubfloodalerts` 实测 HTTP 200，页面有 20 个带 data-post 的消息及 `/s/pubfloodalerts?before=1612` 的历史分页链接。可沿页面实际给出的 before 链接读取历史，保存消息 ID、发布时间、原文与原始页面。此方式不需要 Telegram 登录；它属于网页结构解析，本次未发现 Telegram 对该 HTML 或 before 参数提供稳定 API 契约，不能保证未来页面结构、可见范围或历史完整性。

公开网页的 `<time datetime>` 是发布时间；正文 `Issued` 或方括号时间可能更早。采集时必须同时保留，不能把发布时刻当成洪水精确开始时刻。以频道+消息 ID 去重；翻页边界、请求异常和停止理由写入 manifest。消息 ID 缺号可能来自删除等原因，不能直接推断缺失消息的类别。跨过一年起点只能证明已读取可见历史边界，不能证明从未删除过消息。

## 复用许可与 AI 用途

本次未在频道说明找到明确的 Open Data Licence 或同等开放复用授权。[PUB 网站条款](https://www.pub.gov.sg/termsofuse) 为其网站材料提供有限的个人、非商业查看/下载/打印许可，并保留其他权利；其是否直接覆盖 Telegram 发布内容，本次未确认。不能将 data.gov.sg 上的数据许可自动延伸到该频道。

还存在直接相关的平台条款：[Telegram API Terms 第 1.5 条](https://core.telegram.org/api/terms) 指向 [Content Licensing 条款](https://telegram.org/tos/content-licensing)。后者明确限制平台数据用于 AI/ML 的采集、聚合、训练、验证、基准评测与部署，并规定相关用户明确、知情、持续的同意例外。限制表述涵盖平台数据，不能声称改用公开网页或只做规则处理即可自动豁免。

本次依用户要求进行本地可访问样本的描述性模式观察，不调用 LLM、不公开发布整库；这些实施边界不构成许可已确认的结论。后续建立 AI 研究数据集、评测或公开发布之前，应向 PUB 申请直接提供历史发生/消退通报、字段说明与明确复用许可，并核实相关平台条件。这也是取得稳定历史覆盖和单条业务证据的更直接路径。

## 对当前 baseline 的含义

采集所有可见消息以发现类别，但洪水事件主体统计使用“发生通报”和“消退通报”，暴雨与洪水风险仅作为来源自带的上下文。风险消息标签表示 PUB 的发布内容，不表示本项目产生了风险预测。发生与消退按地点和时间关联时，应保存候选配对与无法配对情况；消息数、地点条目数和推测的事件数分别统计。
