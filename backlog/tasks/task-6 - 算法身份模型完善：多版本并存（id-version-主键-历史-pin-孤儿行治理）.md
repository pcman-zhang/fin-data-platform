---
id: TASK-6
title: 算法身份模型完善：多版本并存、历史 pin 与孤儿行治理
status: To Do
assignee: []
created_date: '2026-09-20 07:31'
updated_date: '2026-09-20 07:32'
labels: []
dependencies: []
priority: medium
ordinal: 69000
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
承接 TASK-3.28 的算法身份调整（algorithm_id 稳定 + version 独立）。现状：注册表支持同 id 多版本并存（get 取最高版本），投影审计、上游指纹与 derive 任务键均含版本（审计身份 id@vN）。仍缺：

1. DB 级多版本留存：meta.algorithm_registry 主键仍是 algorithm_id（单行），同 id 多版本互相覆盖；需复合主键 (algorithm_id, version) + 迁移，并保持 active/deprecated 分类。
2. 历史 pin：控制面/SDK 目前只接受稳定 id（取最新版本）；需支持按 id@vN pin 旧版本（含按需子图求值路径）。
3. 孤儿行治理：sync 只对代码注册表内的算法判 active/deprecated；改名/删除后 DB 孤儿行会永久停留在 active（本次 ma20_v1→ma20 已手工清理）。
4. 升级台账联动：version 递增应自动写 meta.algorithm_events（effective_from/reason），当前需人工传参。

参考：doc-11 §4（算法登记）、doc-20（derive 版本维度 = 算法身份 id@vN）。
<!-- SECTION:DESCRIPTION:END -->
