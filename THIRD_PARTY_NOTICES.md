# 第三方来源与许可说明

## PUB / data.gov.sg 公开回归样本

本副本仅保留 `data/research/pub-2025-05-22.json`，作为既有离线时间回放与坐标回归测试的输入。数据提供方为 **PUB**，分发平台为 **data.gov.sg**，数据集为 **Flood Alerts across Singapore**：

- 数据集页面：<https://data.gov.sg/datasets/d_f1404e08587ce555b9ea3f565e2eb9a3/view>
- API：<https://api-open.data.gov.sg/v2/real-time/api/weather/flood-alerts>
- 许可：**Singapore Open Data Licence v1.0**，<https://data.gov.sg/open-data-licence>

此保存样本于 **2026-10-07 本地内容检查**。原型历史探测元数据 `experiments/service_probe.json` 中对应 `raw_path=data/research/pub-2025-05-22.json` 的记录，声明 `fetched_at=2026-10-04T17:13:02.198911+00:00`；其记录的 SHA-256 与本副本保留样本的原始字节一致。**这是历史探测声明的获取时间，不是本轮新获取，也不是独立时钟验证。** 该元数据保留在受控研究记录中，未随公开副本复制；未复制其他原始响应。样本内消息日期为 `2025-05-22`，含其自身更新字段；这些字段不能替代获取时间。此次检查只见公开道路警报，无个人信息或凭据。该样本用于回归，不能代表全时期覆盖或整体精度。

Open Data Licence 允许按其条件使用和分发数据；本说明保留 PUB / data.gov.sg 来源署名。该许可不自动授予个人信息、第三方权利、商标或标志的使用权。分发此样本不表示 PUB 或其他公共机构认可、担保本原型，也不将其警报点解释为已确认道路淹水几何。

## Leaflet

`web/vendor/leaflet.js`、`web/vendor/leaflet.css` 来自 **Leaflet 1.9.4**。许可为 **BSD 2-Clause**，Copyright (c) 2010–2023 Volodymyr Agafonkin；Copyright (c) 2010–2011 CloudMade。

- 官方网站：<https://leafletjs.com/>
- 1.9.4 许可来源：<https://github.com/Leaflet/Leaflet/blob/v1.9.4/LICENSE>
- 随附完整许可及免责条款：[`web/vendor/LEAFLET-LICENSE.txt`](web/vendor/LEAFLET-LICENSE.txt)

## OneMap / Singapore Land Authority

可选在线底图访问 **OneMap / Singapore Land Authority**，<https://www.onemap.gov.sg/>；路线 API 说明见 <https://www.onemap.gov.sg/apidocs/routing>。界面保留 OneMap 链接及 Singapore Land Authority 署名。地图瓦片仅在用户启用在线底图时通过网络获取，不随仓库分发；OneMap 标志图片已从公开副本排除。服务可用性、凭据和使用条款仍应在实际使用时核对。本项目没有机构认可或道路安全保证。

## 用户自有代码

本次公开副本未为用户自有代码选择或添加开源许可证。以上许可仅适用于对应第三方材料；仓库公开可见本身不授予第三方再利用用户代码的许可。
