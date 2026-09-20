---
id: TASK-3.28
title: 首个因子落地：MA20（端到端验证）
status: In Progress
assignee:
  - '@freeman'
created_date: '2026-09-20 05:34'
updated_date: '2026-09-20 07:48'
labels: []
milestone: m-0
dependencies:
  - TASK-3.24
  - TASK-3.25
parent_task_id: TASK-3
ordinal: 67000
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
在字典登记 daily_bar.ma20（@register 因子实现，输入显式后复权 close@hfq），materialize=latest；通过单元测试（数值对账 / 窗口不足不输出 / as-of 防前视）与栈内实测（物化 → Factor API 读取 → WebUI 算法页/代次可见）验证全链路正确。
<!-- SECTION:DESCRIPTION:END -->

## Implementation Plan

<!-- SECTION:PLAN:BEGIN -->
1. derived/factors.py：@register ma20_v1（DuckDB 窗口：20 个非空收盘价；不足 20 日不输出；输入经访问面显式 hfq）
2. 字典：daily_bar 登记 derived.ma20（inputs=[close@hfq]，materialize=latest，refresh=on_demand）+ CI 通过
3. 口径：daily_bar 默认复权 qfq → hfq（历史值不随新除权漂移；qfq 保留显式可选）
<!-- SECTION:PLAN:END -->

## Implementation Notes

<!-- SECTION:NOTES:BEGIN -->
实现与验证（未提交）：① derived/factors.py：@register ma20_v1（DuckDB 窗口：20 个非空收盘价，窗口不足不输出；输入经访问面，默认口径）；② 字典登记 daily_bar.derived.ma20（materialize=latest、refresh=on_demand）；CI 与三方一致性通过；③ **默认口径调整为 hfq**（daily_bar adjust.default: qfq→hfq）——因子物化值不随新除权事件漂移（PIT/append-only 友好）、与存储累计因子同构、增量更新友好；qfq 仍可显式请求（展示/与现价对比）；文档同步（doc-11 §3.7、doc-21 §3、docs/sdk.md、docs/components.md）；④ 测试：tests/test_platform_factor_ma20.py 4 项（pandas rolling 独立对账 / 窗口不足不输出 / as-of 防前视 / 物化 + FactorAPI）；更新因 shipped 字典新增派生而失效的 4 处旧断言（shipped-无派生假设）与 access 默认口径期望；⑤ 栈内实测：离线镜像重建；runtime 装配 derive.cn_equity.daily_bar.ma20；沙箱 tushare 不可达（真实窗口查询返回 0 行）→ 用确定性演示数据（600519.SH，40 交易日 2026-07-20~09-11，第 31 日起因子 1.0→1.1 模拟除权）；物化 21 行（40−19）、代次 20260920T054448Z、上游指纹 e3b0c44…（空集）；独立 pandas rolling(20) 复算最大差 0；FactorAPI 读取 materialized=True 且代次一致；/v1/algorithms 与 /generations 可见、WebUI 算法页展示 ma20 与投影代次。验证：501 单测 + ruff/mypy 通过。

真实数据复核（回应用户质疑）：① 根因——不是 tushare 不响应，而是**容器出网被切断**（api.tushare.pro TLS: UNEXPECTED_EOF，与 daemon/Docker Hub/PyPI 同类限制），tushare SDK 吞异常返回空表 → 同步『成功 0 行』；宿主机出网正常（真实数据已更新至 2026-09-18）。② 宿主机侧真实数据同步：600519.SH 79 个交易日（2026-06-01~09-18）日线入库；③ **发现缺口**：ingestion 无复权因子采集通道（adj_factor 长期为 0），已建 TASK-3.29（adj_factor 同步/任务注册/水位）；本次用宿主侧一次性补入 79 行真实 adj_factor 以完成验证；④ 真实数据 MA20：物化 60 行（79−19）、代次 20260920T055612Z；独立复算（tushare raw × adj_factor，不经平台存储与访问面）60 行、**最大差 0.0000000000**；样本 2026-06-29=10596.135、2026-09-18=11169.2514；/v1/algorithms/generations 与 WebUI 均显示新代次。

容器出网根因探查与修复（用户关注点）：① 根因——宿主 shell 有 HTTP(S)_PROXY=http://127.0.0.1:7897，容器无代理变量且直连 TLS 被截断（UNEXPECTED_EOF），tushare SDK 吞异常返回空表；容器可经 Docker Desktop 网关 host.docker.internal:7897 访问宿主代理（172.17.0.1:7897 不可达）；② 修复固化：docker-compose x-app-env 注入 HTTP_PROXY/HTTPS_PROXY/NO_PROXY（默认空；NO_PROXY 含 timescaledb,redis,localhost,127.0.0.1），.env.example 增 FDP_CONTAINER_PROXY/FDP_CONTAINER_NO_PROXY 模板，docs/configuration.md 增「容器出网代理」排查小节；③ 实测：容器内经代理 tushare 直调成功；重置水位后容器调度自动同步 600519.SH **run 11 succeeded rows_written=79**（真实数据 2026-06-01~09-18），MA20 重物化 60 行、代次 20260920T060250Z，样本与宿主侧一致（10596.135 / 11169.2514）；④ 运维提示：手动提交窗口终点晚于最近已收盘交易日会把水位推到未来，导致调度器无窗口可投（本次已重置水位修复），建议后续在 API/意图层做交易日钳制。

合并后复核（本分支 feat/ma20-factor，基于 main=25de612，含 TASK-3.29 因子通道）：宿主直连栈库重跑物化——materialize 60 行 / 代次 20260920T070130Z / algorithm ma20_v1；FactorAPI.read 60 行 materialized=True；独立复算（access hfq 收盘 × pandas rolling(20)）最大差 5e-12（浮点噪声级）；样本 首行 2026-06-29 10596.134997 → 末行 2026-09-18 11169.251432，与 3.28 首轮验证数值一致。说明：upstream_fingerprint=e3b0c44298fc1c14 为「空上游集合」SHA-256 前缀——按设计指纹仅聚合上游**因子**的 algorithm_id，ma20 上游是原始数据集故为空集（非缺陷）。运行栈重建后 /v1/algorithms 返回 ma20_v1（inputs/描述正确），/v1/algorithms/generations?output=ma20 指向本次代次。检查：ruff + mypy(src 109 文件) 全绿，全量单测通过。

代码审查修复：① ma20 输入改为**显式口径** cn_equity.daily_bar.close@hfq（数值语义锚定在声明上，不再隐式依赖 adjust.default）；factors.py 按声明引用取输入、字典追溯用例改用 parse_ref 校验（含口径后缀合法性）。② 补 qfq 正向数值断言（raw×f/f_anchor → [5,11]），默认集不再只覆盖 hfq。③ 文案同步（ma20/hfq 注释、字典用例 docstring）。④ docs/components.md 注明 materialize=latest 首次物化前无投影可读、缺复权因子行（NULL）不参与计算。复验：全量单测通过 + ruff/mypy 全绿；真实数据复核（显式 @hfq）60 行 / 代次 20260920T071439Z / 独立复算差 ≤5e-12，数值与首轮一致；镜像重建后 /v1/algorithms 显示 inputs=[close@hfq]。

算法身份调整（按用户建议，同分支）：algorithm_id 稳定为 ma20，版本由 version=1 承载（不再嵌入名字）。实现：注册表按 (id, version) 并存（get 缺省取最高版本；兼容旧 _vN 后缀写法）；审计身份 id@vN 贯通——物化投影新增 algorithm_version 列、read_factor_projection_meta/ProjectionAudit 返回版本、graph 指纹按身份哈希、derive version_dimension 写身份；FactorAPI 读路径新增版本漂移守卫（投影版本 ≠ 当前登记 → FactorError 不静默复用）。字典/文档同步：daily_bar.yaml（algorithm_id: ma20、description 显式 close@hfq）、doc-11 §4 与示例、doc-20 版本维度表、doc-21 SDK 示例、docs/sdk.md 响应元数据。复验：全量单测 + ruff/mypy 全绿；真实数据复核 60 行 / 代次 20260920T072931Z / 独立复算差 ≤5e-12；镜像重建后 /v1/algorithms 返回 ma20 + version=1 + inputs=[close@hfq]。遗留项已建 **TASK-6**（DB 级多版本主键/迁移、id@vN pin、孤儿行治理、升级台账自动联动）；演示库旧行 ma20_v1 已清理。

第二轮审查修复：① 读路径守卫补全——显式 pin 未注册（含 id@vN 写法/旧 id/笔误）时抛 FactorError（不静默复用投影；与按需路径对未知 id 的报错对齐），新增用例 test_materialized_read_rejects_unknown_pin；② graph 注册表参数改精确类型（AlgorithmRegistry | None，TYPE_CHECKING），去掉 Any；③ 文案统一：registry docstring 注明 DB 级多版本留存/按版本 pin 属 TASK-6（当前 algorithm_registry 仍按 id 单行），doc-11 规则 1「旧写法兼容」措辞修正 + 194/200/399 行改为「身份 (id, version) 唯一 / 升级升 version」，doc-21 §3 响应元数据与幂等版本维度补 algorithm_version 与 id@vN。复验：全量单测 + ruff + mypy 全绿。
<!-- SECTION:NOTES:END -->
