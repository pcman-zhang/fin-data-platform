# FinDataPlatform 控制台 · UI/UX 设计原则

> 本文是管理控制台的设计基准（dark-first、表格密集型、信息密度优先）。
> 变更 UI 前先改本文，再改实现。

## 0. 定位

- **用途**：平台治理（数据集字典 / 实体注册表 / 任务与水位 / 同步触发），不承担研究分析。
- **形态**：桌面优先（≥1440px 为最佳视口），**全宽利用**；表格是主体，详情不挤压列表。
- **技术**：Ant Design `6.6.4`（含 `@ant-design/icons`）+ React 19；主题经 AntD token 定制，
  不再叠加第二套 CSS 体系（Tailwind 退出）。

## 1. 布局与空间

采用 AntD **侧边布局（固定侧边栏）** 变体，全宽利用、仅内容区滚动（**不使用 Footer**）：

```
Layout hasSider
├─ Sider  固定（sticky, 100vh, scrollbar-thin）— Menu mode="inline" theme="dark"
└─ Layout
   ├─ Header 高度 64（产品名 / 当前页 / 右侧：健康状态 + 主题切换 + 折叠按钮）
   └─ Content 全宽（padding 16 24，无 max-width 居中）
```

**尺寸（对齐 AntD Layout 规范）**

| 项 | 取值 | 依据 |
|---|---|---|
| Sider 宽度 | `200`（范围 `200+8n`） | 官方公式 |
| Sider 折叠宽度 | `80`（默认值；不用 0 特殊 trigger） | 官方默认 |
| Header 高度 | `64`（范围 `48+8n`） | 官方公式 |
| 内容区 padding | `16 24`（紧凑页可 16） | 4px 栅格 |
| 断点 | `breakpoint="lg"`（<992 自动折叠） | 官方断点 |

**导航交互（官方规则落地）**

- 当前项优先级最高：dark 底色下用**大色块强调**（选中项 `colorPrimary` 底色 + 高亮文字），
  **禁止**在 dark 下改用浅色系专用的"高亮火柴棍"；
- 折叠时，当前项高亮自动上移到其父级（Menu 默认行为，不额外定制）；
- 菜单为**手风琴**模式（`openKeys` 单开），数据集/任务等分组打开互斥；
- 折叠按钮置于 Header 左侧（`MenuFoldOutlined / MenuUnfoldOutlined`，`type="text"`）。

**Layout 组件 Token 定制（dark）**：`siderBg=#171a21`、`triggerBg=#1f232b`、`headerBg=#171a21`、
`bodyBg=#0f1115`；菜单由 `ConfigProvider.theme`（darkAlgorithm + 上述 token）统一。

**其余空间规则**

| 规则 | 约定 |
|---|---|
| 密度 | `ConfigProvider.componentSize="middle"`（表单控件）；**Table `size="medium"`**（AntD 6 取值为 large/medium/small，默认 large；medium 行高 ≈ 44） |
| 主从布局 | 列表页：表格占满；**详情用 Drawer（宽 480/560）从右侧滑出**，不压缩列表列宽 |
| 数据集页 | 左 `Tree`（域 → 数据集，宽 280）+ 右全宽详情（`Splitter` 可拖动） |
| 间距 | 4px 基准；面板/卡片间距 16；面板内 padding 16 |
| 列宽策略 | 关键列固定宽（ID/代码/状态/时间），长文本列 `ellipsis` + Tooltip |
| 滚动 | 仅 Content 滚动（Sider/Header 固定）；表格分页 20/50/100（默认 50，本地记忆） |

## 2. 主题与色彩（dark-first）

- 默认 **dark**（`theme.darkAlgorithm`）；预留 light 切换（右上角，持久化到 localStorage）；
- **v6 默认 CSS 变量模式**：主题经 `ConfigProvider.theme`（algorithm + token）装配，
  组件样式不写覆盖 CSS；Provider 树见 §9.16。
- Token 基准（暗色）：
  - 背景层级：`bg-base #0f1115` → `bg-container #171a21` → `bg-elevated #1f232b`；
  - 文本：主 `#e5e7eb`、次 `#9ba3af`、禁用 `#5b6472`；
  - 主色 `#4c8dff`；分隔线 `#2a2f3a`；
- **状态语义色**（全局唯一映射，禁止各页自定义）：
  | 语义 | 色 | 用于 |
  |---|---|---|
  | success `#3fb950` | 绿 | succeeded / 存续 / 通过 |
  | processing `#4c8dff` | 蓝 | running / retrying |
  | warning `#d29922` | 黄 | queued / 待发布 / warn 级质量 |
  | error `#f85149` | 红 | failed / dead / error 级质量 |
  | default `#8b949e` | 灰 | cancelled / interrupted / 已关闭 / 未知 |
- 对比度 ≥ 4.5:1；图表只使用上表语义色（后续引入图表时）。

## 3. 字体与层级

- 字体族：`Inter, "PingFang SC", "Microsoft YaHei", system-ui`；
  **等宽**：`JetBrains Mono, ui-monospace` —— 用于代码 / entity_id / job_id / 时间戳 / SQL / 错误文本。
- 字号层级（仅 5 级，不允许新增）：

  | 级别 | 字号/字重 | 用途 |
  |---|---|---|
  | Display | 24 / 600 | 页面唯一主标题（Drawer 标题同规格下调 20） |
  | Title | 16 / 600 | 面板标题 |
  | Body | 14 / 400 | 表格与正文 |
  | Caption | 12 / 400 | 次要说明、时间、单位 |
  | Mono | 12 / 400 mono | 代码/ID/时间戳 |
- 数字统一 `tabular-nums` 右对齐（行数、尝试次数、计数）。
- 时间统一 `YYYY-MM-DD HH:mm:ss`（本地时区），日期窗口 `YYYY-MM-DD`。

## 4. 信息展示范式（何时用什么）

| 场景 | 范式 | 说明 |
|---|---|---|
| 列表数据（任务/实体/水位） | **Table** | 固定列 + 分页；行点击开详情；筛选器统一右上 |
| 对象详情 | **Drawer** | 右侧滑出，深链（`?run= ?entity= ?dataset=`），Esc 关闭 |
| 需要确认的写操作 | **`App.useApp().modal.confirm`** | 触发同步 / 重试；列出影响对象 + 窗口；焦点默认取消 |
| 短字段集合（键值/SLA） | **Descriptions** | Drawer 内 vertical+column 1–2；页面 horizontal+column 2；空值统一 — |
| 版本/属性历史 | **Timeline** | 节点=版本；右侧显示名称 + 版本号 + 有效期 + 知识时间 |
| 血缘/域结构 | **Tree / TreeSelect** | dataset 级 DAG 用 Tree + 节点 Tag（上游/派生） |
| 关键计数 | **Statistic** | 总览卡片：数据集 / 实体 / 水位 / 异常任务 |
| 枚举与状态 | **Tag / Badge** | 状态用 `Badge status`（色点+文案）；类型用 Tag |
| 错误全文与堆栈 | Drawer 内 **`<pre>` + 复制按钮** | 不在表格里展开 |
| 长文本截断 | `Typography.Text ellipsis` + Tooltip | 表格错误列固定此范式 |
| 空态 / 失败 | **Empty / Result** | 空态给出下一步动作；接口失败用 `Alert` 内联，不用弹窗 |
| 轻提示 | **message**（App 上下文） | 提交成功/失败反馈；不用于需要确认的动作 |

## 5. 图标语义表（仅用 `@ant-design/icons`）

| 概念 | 图标 | 说明 |
|---|---|---|
| 总览 / 数据集 / 实体 / 任务 | `DashboardOutlined` / `TableOutlined` / `ApartmentOutlined` / `ScheduleOutlined` | 导航固定 |
| 上市实体（equity/etf/lof） | `RiseOutlined` | entity_type 含 listing 类 |
| 发行主体（issuer） | `BankOutlined` | |
| 指数 / 基金 / 债券 | `FundOutlined` / `PieChartOutlined` / `FileProtectOutlined` | |
| 上下文：dataset | `DatabaseOutlined` | |
| 触发同步 / 重试 / 刷新 | `PlayCircleOutlined` / `RedoOutlined` / `ReloadOutlined` | 动作按钮统一带图标 |
| 关系方向 | `ArrowRightOutlined`（out）/ `ArrowLeftOutlined`（in） | 关系列表前缀 |
| 外部标识 | `SafetyCertificateOutlined` | ISIN/LEI 等 |

禁止用图标表达状态（状态一律色点 + 文案，避免歧义）。

## 6. 状态与反馈

1. **加载**：首屏 `Skeleton`；表格 `loading`；不做全屏遮罩。
2. **轮询**：健康状态 30s；任务列表手动刷新（`ReloadOutlined`）+ 详情自动 5s 刷新（运行中）。
3. **成功/失败**：`message` 轻提示；接口 4xx/5xx 在动作处内联 `Alert` 展示 detail。
4. **写操作确认**：仅「触发同步」「重试/取消」需要 `App.useApp().modal.confirm` 二次确认，且默认焦点在取消。
5. **一致性**：所有请求错误展示后端 `detail` 原文（便于排障），不吞错；
6. **渲染兜底**：每个路由页面由 `Alert.ErrorBoundary` 包裹，异常时展示错误信息 + 刷新入口（不让白屏）。

## 7. 交互一致性

- 筛选器位置：面板右上（状态 Select + 关键字 Input + 刷新），顺序固定。
- 行点击 = 打开详情 Drawer；行内无“查看”按钮（避免重复入口）。
- 深链：`/datasets?dataset=`、`/entities?entity=`、`/jobs?run=`；Drawer 关闭即清除参数。
- 分页与筛选状态可回退（浏览器后退保持筛选）。
- 键盘：Esc 关闭 Drawer / Modal；表格行支持 Enter 打开详情。

## 8. 响应式与可访问性

- ≥1440：全宽，详情 Drawer 并排；1280–1440：Drawer 覆盖（不禁用列表）；
  <1024：导航折叠为图标，表格横向滚动（保留首列与状态列粘性）。
- Focus 可见（AntD 默认），不使用纯色块代替文字状态。
- 文案用词：任务状态/字段名保留英文原文（与库口径一致），说明性文案中文。

## 9. 组件使用细则（随官方规范逐组件固化）

### 9.1 Divider（分割线）

| 场景 | 用法 | 说明 |
|---|---|---|
| Drawer / 页面内区块分段 | `<Divider titlePlacement="start" plain size="small">` | 区块标题用正文样式（`plain`），不用大字号 |
| 页头并列元信息 | `<Divider vertical />` | 如「市场 / 币种 / 交易所」行内分隔 |
| 表格操作列内的并列动作 | `<Divider vertical />` | 链接之间分隔（列内不放按钮组时） |
| 纯视觉留白分段 | `<Divider size="small" />` | 优先 `size="small"`（紧凑） |

约束：

- **变体只用 `solid`**（不引 dashed/dotted；分隔线是结构，不是装饰）；
- 颜色由 token `colorSplit` 统一，**禁止**自定义 `borderColor`；
- 不滥用：面板/卡片已有边框时不重复加分线；能用「间距 + 标题层级」表达的分组不画线；
- 深链/节奏：同一屏内分割线颜色与粗细必须完全一致（禁止混用 1px/2px）。

### 9.2 Listy（虚拟列表，antd ≥6.6）

| 场景 | 用法 | 说明 |
|---|---|---|
| 任务运行流 / 事件流（可能数百条且需持续滚动） | `virtual + height` | 仅渲染视口行；配合 `onScroll` 做无限加载 |
| 属性时间轴 / 代码履历（长历史） | `group`（按年份或类型分组）+ `sticky` | 分组标题吸顶；`scrollTo({groupKey})` 定位 |
| 需定位到某条记录 | `ref.scrollTo({ key, align })` | 深链（`?run=`）打开后滚动定位 |
| 表格类多列对齐数据 | **不用 Listy**，用 Table（分页/排序） | Listy 是列表不是表格，禁止用网格模拟列对齐 |

约束：

- 仅当「条目可能 >200 或容器高度受限」时启用 `virtual`（需配 `height`）；小列表用普通渲染（关虚拟避免无谓复杂度）；
- 行内点击行为与表格一致：打开右侧 Drawer（深链），**禁用行内“查看”按钮**；
- 行内复杂内容允许不等高，但同屏内上下 padding 一致（保留组件 token `itemPaddingBlock=12`，不逐行定制）；
- 颜色只用主题 token（`colorBgContainer` / `colorSplit` / `colorTextDescription`），不自定义分组底色；
- 分组键按**稳定维度**（域 / 年份 / 类别），禁止按易变字段分组。

### 9.3 Statistic（统计数值）

| 场景 | 用法 | 说明 |
|---|---|---|
| 总览关键计数（数据集 / 实体 / 水位 / 异常任务） | `Row gutter={16}` + `Col span={6}` + `Card variant="borderless"` | 一屏 4 个，不堆更多 |
| 运行指标（行数 / 尝试 / 耗时） | `precision=0`（计数）、耗时用 `suffix` 标单位 | 行数带千分位（默认 `,`） |
| 运行中任务实时时长 | `Statistic.Timer type="countup" value={started_at}` | 仅用于真实进度；**禁止**倒计时用于业务 |
| 加载态 | `loading` 属性 | 不自行拼 Skeleton |

约束：

- 数值一律 `font-variant-numeric: tabular-nums`（经 `classNames.content` 或全局 CSS），保证跳动不抖；
- 颜色只允许主题语义色（`colorSuccess` 上升 / `colorError` 下降，见 §2），**禁止硬编码色值**；
- 百分比统一 `precision=1~2` + `suffix="%"`；金额 `precision=2`；
- `title` 保留默认 14 号并弱化为 `colorTextDescription`（与 §3 层级一致，不额外加粗）；
- 不引 `react-countup`（管理台无需数字动画，减少依赖）；
- 已废弃 API 不用（`valueStyle` → `styles.content`；`Statistic.Countdown` → `Statistic.Timer`）。

### 9.4 Table（表格）

**总原则**：分页 + 服务端过滤优先；行点击开 Drawer；**不用** rowSelection / expandable / 可编辑行 / 拖拽排序
（平台治理无批量与编辑语义，详情统一 Drawer）。

| 参数 | 约定 |
|---|---|
| `rowKey` | 必填且稳定（`run_id` / `entity_id` / `dataset`） |
| `size` | `medium`；紧凑抽屉内可用 `small` |
| `scroll` | 宽表 `{ x: 'max-content' }`；列表页 `{ y: 'calc(100vh - 260px)' }`（仅内容区滚动） |
| `sticky` | 需要页面级滚动时启用（`offsetHeader` 与 Header 64 对齐） |
| `pagination` | `pageSize` 50（20/50/100，localStorage 记忆）、`showSizeChanger`、`showTotal`、`placement: ['bottomEnd']` |
| 过滤 | 已有 API 参数的列走服务端（状态/job_id）；纯展示列（行数/耗时）允许前端 `sorter` |
| 空态 | `locale.emptyText` 用 `Empty` + 下一步动作（如「前往任务页触发同步」） |
| 虚拟滚动 | 仅当 >1000 行且行高固定时用 `virtual`（`scroll.x/y` 必须为**数字**） |
| 禁止 | 行内「查看」按钮（行点击）；列拖拽；可编辑单元格；`rowSelection`（无批量操作） |

**列型规范（列 = 固定语义，禁止每页自定义渲染风格）**

| 列型 | 渲染 | 宽度 | 对齐 | 备注 |
|---|---|---|---|---|
| 主键 / Run | `<Typography.Text code>` | 88 | left | `fixed: 'start'` |
| 代码 / job_id | `code`（mono） | 120 | left | 实体表 `fixed: 'start'` |
| 名称 | `ellipsis` + Tooltip | min 160 | left | 禁换行撑高行 |
| 状态 | `Badge status`（§2 语义色） | 96 | left | 色点 + 文案 |
| 时间戳 | mono `YYYY-MM-DD HH:mm:ss` | 168 | left | 统一格式 |
| 日期窗口 | mono `start ~ end` | 180 | left | 空窗口显示 — |
| 数值（行数/尝试） | `tabular-nums` | 88 | **right** | 千分位 |
| 耗时 | mono，`formatDuration` | 88 | **right** | ms/s/m |
| 枚举/类型 | `Tag` | auto | left | 非状态枚举 |
| 错误摘要 | `ellipsis: {showTitle:false}` + Tooltip | min 200 | left | 全文在 Drawer |

### 9.5 Tree（树形控件）

**用途**：数据集页左侧「域 → 数据集」两级导航（宽 280，配合 Splitter）。

| 参数 | 约定 |
|---|---|
| 数据 | `treeData`，key 全局唯一：域 `domain:cn_equity`、数据集 `dataset:cn_equity.daily_bar` |
| 展开 | 受控 `expandedKeys` + localStorage 记忆；`autoExpandParent` 在搜索时置 true |
| 选中 | 受控 `selectedKeys`，与深链 `?dataset=` 双向同步（URL 为准） |
| `blockNode` | 开启（整行可点，点击区域 ≥ 32px 高） |
| `showIcon` | 开启：域 `FolderOutlined`、数据集 `TableOutlined`（与导航图标语义一致，§5） |
| 搜索 | 顶部 `Input.Search`：过滤 + 自动展开匹配路径（`Tree.useTree().getPath`）；<200 节点不做 `filterTreeNode` 高亮 |
| 虚拟滚动 | 节点 >500 时设 `height`（虚拟滚动下不支持横向滚动、超长标题请用 ellipsis） |
| 定位 | `scrollTo({ key, align: 'top' })`（深链打开且节点在折叠区时展开后定位） |

**禁止**：`checkable`（无批量选择语义）、`draggable`（无重排需求）、`DirectoryTree`（文件目录形态，不符治理语义）、右键菜单（暂无）。

### 9.6 Alert（警告提示）

**用途**：页面/卡片内联的静态提示（非浮层）；接口错误、降级、危险操作结果。

| 场景 | 用法 |
|---|---|
| 接口 4xx/5xx | `<Alert type="error" showIcon title={detail 首行} description={截断全文} action={<Button size="small">查看详情</Button>} />`，详情在 Drawer 内 `<pre>` 全文 |
| 触发同步「跳过」结果 | `type="warning"`，`description` 列出 skipped 项（代码 + note） |
| 部分成功 | 同一位置错误与成功各一条，**不合并**文案（避免歧义） |
| 全局降级（服务不可达 / 只读模式） | `banner` 置顶全宽（仅此场景使用 banner） |
| 渲染异常 | `<Alert.ErrorBoundary>` 包裹每个路由页面（§6） |

**约束**

- `variant`：页面级默认 `outlined`；嵌在卡片/Drawer 内用 `filled`（避免与容器边框叠层）；
- `showIcon` 必开；**不自定义 icon**（语义色与图标由 type 决定，§2/§5）；
- `closable` 只用于「可消解的临时状态」（如本次跳过汇总）；**接口错误不设 closable**（须持续可见直至重试成功）；
- 不用 Alert 替代 `message`（轻提示）或表单校验（`Form.Item` 自带）；
- 不堆叠：同一位置若已有一条错误，更新其内容而不是再插一条。

### 9.7 Descriptions（描述列表）

**用途**：只读键值区——Drawer 详情头部、数据集页头、任务/实体元信息。

| 场景 | 用法 |
|---|---|
| Drawer 详情（480/560 宽） | `layout="vertical"` + `column={1或2}` + `size="small"`，**不加 `bordered`**（Drawer 内边框显重） |
| 页面宽区块（数据集页头） | `layout="horizontal"` + `column={2}` + `size="medium"` + `bordered` |
| 长文本字段（错误、键列表） | `span="filled"` 独占整行 |

**值渲染规范（与 §3/§4 一致）**

- ID / 代码 / job_key：`Typography.Text code`；时间戳统一 `YYYY-MM-DD HH:mm:ss`；
- 状态：`Badge status`（不用纯文字）；枚举：`Tag`；
- 空值统一 `—`（`Typography.Text type="secondary"`），不写 `null` / `None` / 空字符串。

**约束**

- 不在 Descriptions 里重复面板标题（`title` 不用），`extra` 只放状态类小元素；
- 值里不放操作按钮（操作归面板 `extra` 或 Drawer 底部）；
- 列数固定 2（Drawer 内 1–2），不逐屏变化；禁用 `labelStyle`/`contentStyle`（用 `styles`）。

### 9.8 Progress（进度条）

**第一原则：没有真实分母，就不画进度。** 管理台的数据大多没有可计算百分比
（任务耗时不可预估），编造进度比没有进度更糟。

| 场景（分母真实） | 用法 |
|---|---|
| 窗口同步进度（已完成天数 / 窗口总天数） | `type="line"` `size="small"` + `format={() => `${done}/${total} 天`}`，仅在有逐日阶段回报时使用 |
| 队列构成（succeeded / running / failed 占比） | `success={{ percent }}` 双段：成功段 + 活动段 |
| 水位滞后 vs 容忍天数 | `type="line"` `status` 随阈值切换（normal / exception） |
| 覆盖率 / 质量通过率（已知分子分母） | `type="circle" size="small"`，与 `Statistic` 并排 |

**禁止 / 约束**

- 任务运行中**不显示百分比**（无分母）→ 用状态徽标 + 已用时 + 时间线表达（§4/§9.2）；
- `strokeColor` 只用语义色（禁渐变色/彩色装饰）；`railColor` 保持默认；
- 不用 `type="dashboard"`（仪表盘为装饰形态，无业务语义）；
- 不用 `steps`（我们无步骤语义）与 `showInfo` 自定义图标；
- `format` 只在单位明确时改写文案（如「3/5 天」），否则保持 `%`。

### 9.9 Drawer（抽屉）

**定位**：**主从详情的唯一形态**（任务 / 实体详情）；不属于「附加面板」用法（无多层、无左侧/顶部抽屉）。

| 参数 | 约定 |
|---|---|
| `placement` | 固定 `right`（详情统一右侧，不提供其它方向） |
| `size` | 默认 **560**（含错误全文场景）；`resizable` 开启 + `maxSize={840}`，宽度 localStorage 记忆 |
| `destroyOnHidden` | **开启**（切换记录不残留旧数据与滚动位置；运行中轮询随关闭停止） |
| `loading` | 首次打开用内置骨架屏；切换记录用 body 内局部 Skeleton（避免整面板跳动） |
| `mask` | 默认 dim（`mask: true`）；**不自定义** mask 颜色/模糊 |
| `keyboard` | `true`（Esc 关闭，§7） |
| `title` / `extra` | title = 对象标识（code / run_id）；extra 放状态 `Badge` + 刷新按钮 |
| `footer` | 仅在有写操作时使用（v1 不在 Drawer 内写；触发同步在主面板） |
| 打开/关闭 | 由深链派生（`?run=` / `?entity=`）；关闭即清除 URL 参数（§7） |

**禁止**

- 多层 Drawer（嵌套详情）——错误全文/附加信息在同一 Drawer 内用折叠区或 `<pre>` 呈现；
- `push`（无多层）、`getContainer=false`（不需要内联渲染）、自定义语义样式；
- 非 right 方向、`closable={false}`（必须保留右上角关闭与 Esc）。

### 9.10 DatePicker（日期选择）

**只用一个形态**：触发同步的窗口选择 = **`DatePicker.RangePicker`**（起止一体，避免两个单日选择器各择一段）。

| 参数 | 约定 |
|---|---|
| `picker` / `showTime` | 固定 `date`；**不用** `showTime`（窗口是日期粒度，时间戳展示用 Typography+mono） |
| `format` | 统一 `YYYY-MM-DD`（提交前 `format('YYYY-MM-DD')` 转字符串给 API） |
| `disabledDate` | **禁止晚于今天**（与后端 422 校验一致）：`current > dayjs().endOf('day')`；不设历史下限（越早的窗口交给后端跳过逻辑） |
| `presets` | 常用窗口：`近 7 日` / `近 20 日` / `本月至今`（用函数返回值实现"至今"动态求值） |
| `allowClear` | 允许清空；清空 = 缺省语义（起点=水位+1、终点=今日），表单项下方以 `Caption` 说明 |
| `needConfirm` | 保持默认（不显式设置），避免半截窗口被提交 |
| `status` | 由 `Form.Item` 校验接管，**不手写** `status` |

**禁止**：`multiple` / `picker` 其他类型（week/month/quarter/year）/ `cellRender` / `components` 自定义面板 /
`suffixIcon`·`prefix` 自定义 / 非默认 `variant` / 手写 `locale`（统一走全局 `ConfigProvider` zh_CN + `dayjs.locale('zh-cn')`）。

### 9.11 Input（输入框）

| 场景 | 形态 | 约定 |
|---|---|---|
| 代码清单（触发同步） | **`Input.TextArea`** | `autoSize={{minRows: 2, maxRows: 6}}` + `allowClear`；mono 字体；placeholder `600519.SH, 000001.SZ`；下方 Caption 显示「已解析 N 个代码」（**不用 `showCount`**——那是字符计数） |
| 搜索（数据集树 / 实体 / job_id） | **`Input.Search`** | `allowClear` + `onSearch`（回车/点击触发）；**不做输入即搜**，避免频繁请求；表格右上的 job_id 过滤同此形态 |
| 单行文本（request_id 等） | **`Input`** | `maxLength` 限定（与 API 上限一致） |

**统一约定**

- 尺寸统一 `medium`（表单内默认）；`variant` 保持 `outlined`（含工具条内筛选，不混用 filled/borderless）；
- `status` 由 `Form.Item` 校验接管，**不手写**；
- Form.Item 内自动接管 `value`/`id`，不手动传 `value`；
- 校验承担格式：代码格式（canonical 正则）与数量上限（≤200）在表单规则内实现，与 API 一致。

**禁止**：`addonBefore` / `addonAfter`（已废弃，用 `Space.Compact`）、`Input.Password` / `Input.OTP`（无场景）、
`type="textarea"`（用 `TextArea`）、动态切换 `prefix/suffix/showCount`（会导致失焦）。

### 9.12 Badge（徽标数）

**首选形态：状态点 + 文案**（`<Badge status text />`），与 §2 语义色一一映射，全站唯一：

| `status` | 语义（§2） | 我们的取值示例 |
|---|---|---|
| `success` | 成功 / 存续 / 通过 | succeeded / 存续 / 质量 pass |
| `processing` | 进行中 | running / retrying |
| `warning` | 待处理 / 待发布 | queued / warn 级质量 |
| `error` | 失败 | failed / dead / error 级质量 |
| `default` | 中性 / 已终止 | cancelled / interrupted / 已关闭 |

| 场景 | 用法 |
|---|---|
| 表格状态列 / 详情头 | `Badge status` + `text`；表格内 `size="small"`，详情头默认尺寸 |
| 导航/标题旁的**可关注计数**（如异常任务数） | `count`（红色冒泡）；`overflowCount={99}` |
| 数量为 0 | 默认隐藏；仅在「0 也是事实」时用 `showZero` |

**禁止**：`Badge.Ribbon`（装饰性缎带，无场景）、`dot`（无语义的点）、自定义 `color` 与多彩徽标
（必须走 status 五值，禁止第二套颜色体系）、`count` 内放图标/文字（计数只放数字）。

### 9.13 Form（表单）

**全站唯一表单**：触发同步（任务页右栏）。原则：**校验前置、二次确认、提交后不改状态**。

| 参数 | 约定 |
|---|---|
| 布局 | `layout="vertical"`（380 宽右栏紧凑）；不用 `labelCol/wrapperCol` |
| `requiredMark` | 保留默认（必填可见，不隐藏） |
| 提交 | `onFinish` + `onFinishFailed` + `scrollToFirstError`；按钮 `htmlType="submit"` + `type="primary"` + `block` + `loading`（mutation pending） |
| 重置 | `htmlType="button"` + `form.resetFields()`，次要 `type="text"` |
| 初始值 | `initialValues`：codes 默认 `600519.SH`；window 留空（缺省语义：起点=水位+1、终点=今日）；priority 固定 100（不暴露） |
| 提交失败 | API 错误用 §9.6 `Alert` 内联在按钮上方；表单错误由 `Form.Item` 自带，不额外套 Alert |
| 二次确认 | `onFinish` 校验通过后先开 `App.useApp().modal.confirm`（展示解析后的代码清单与窗口），确认后 `mutate`；**表单不直接发请求** |

**校验规则（与 API 契约逐条对齐）**

| 字段 | 规则 |
|---|---|
| `codes` | `required` + 自定义 validator：canonical 正则（`^\d{6}\.(SH|SZ|BJ|OF)$`）、解析去重、数量 ≤200；`validateFirst` 短路 |
| `window` | `RangePicker` `disabledDate` 拦未来（前端）+ 自定义 validator 保证 `start ≤ end`（防清空/手输异常） |
| `request_id` | 可选；`maxLength=64` |

**禁止**：`Form.List` / 嵌套动态字段、`Form.Provider`、`onFieldsChange` 外部托管表单状态、
表单级 `disabled`、校验文案混用英文或省略可操作提示（消息格式：`{字段}：{具体问题}，{怎么改}`）。

### 9.14 Select（选择器）

**用途**：表格筛选（任务状态 / 实体类型 / 数据集域）。**只做单选筛选，不做多选、不做表单选择**（表单当前无选择型字段）。

| 参数 | 约定 |
|---|---|
| `options` | 数据化数组（禁止 JSX `Option` 子元素）；状态选项直接来自 §9.12 的取值枚举 |
| `allowClear` | 开启；`placeholder` 写明"全部状态 / 全部类型"；清空 = `undefined` = 不过滤 |
| 宽度 | 固定：状态 148、类型 160、域 180（不随内容伸缩） |
| `size` / `variant` | `medium`；恒 `outlined`（与工具条其它控件一致，不混用 filled/borderless） |
| `showSearch` | 选项 ≤10 **不开**（状态 8 / 类型 6 / 域 ~10）；未来 >10 再开 `showSearch={{ optionFilterProp: 'label' }}` |
| `mode` | **不设置**（单选）；`multiple` / `tags` 一律禁用（筛选语义必须单值） |
| 弹层容器 | 在 Drawer / Modal 内使用时设 `getPopupContainer={(t) => t.parentElement!}`（防被浮层遮挡） |

**禁止**：`mode="multiple"` / `tags`、`labelInValue`、`tagRender`、`popupRender`、`optionRender`（保持原生列表）、
`status`（非表单场景不传）、`showArrow={false}`（用 `suffixIcon={null}` 替代的旧写法同样不用）。

### 9.15 Modal（对话框）

**唯一用途**：写操作二次确认（触发同步；未来的重试/取消）。**必须使用 `App.useApp().modal`**（hooks 版本）——
静态 `Modal.confirm` 无法读取 `ConfigProvider` 上下文，dark 主题与 zh_CN 会失效，**禁止使用**。

| 参数 | 约定 |
|---|---|
| `title` | 具体动作：`确认触发同步` / `确认重试`（不用"提示/确认"泛化标题） |
| `content` | **影响对象清单**：数据集、代码列表、窗口（`start ~ end`）、幂等键；用列表渲染，不用长段落 |
| `okText` / `cancelText` | `确认提交` / `取消`（动词具体化，禁用"确定"） |
| `okButtonProps` | 写数据（触发同步）= 默认 primary；**丢弃/中断类**（取消运行中任务）= `danger: true` |
| 焦点 | `focusable={{ autoFocusButton: 'cancel' }}`（默认焦点在取消，防误触） |
| 遮罩/关闭 | `mask={{ enabled: true, closable: false }}` + `closable: false`（强制二选一）；Esc 仍可取消 |
| `centered` / `width` | `centered: true`；`width: 480`（容纳清单） |
| 提交 | `onOk` 返回 Promise：成功 resolve 关闭；**失败 reject 保持打开** + `message.error(detail)`，可重试或取消 |
| `icon` | 保留默认，不自定义 |

**禁止**：静态 `Modal.confirm`；`Modal.info/success/warning/error`（反馈用 `message`、内联用 §9.6 `Alert`）；
受控 `<Modal>` 组件（当前无表单弹窗场景）；多层 Modal；自定义 `mask` 颜色/模糊与响应式 `width`；
把 Modal 当信息展示用（详情一律 Drawer）。

### 9.16 App（包裹组件）与 Provider 装配

**入口树顺序（固定）**：

```
<ConfigProvider
  theme={darkAlgorithm + token}        ← 主题
  locale={zh_CN}                       ← 语言
  tooltip={{ unique: true }}           ← 全站同一时间仅一个 Tooltip
>
  <App message={{ maxCount: 3 }}>      ← 上下文（message/modal）
    <BrowserRouter> … 页面 … </BrowserRouter>
  </App>
</ConfigProvider>
```

| 规则 | 约定 |
|---|---|
| 获取实例 | 一律 `const { message, modal } = App.useApp()`；**禁用** `message.xxx` / `Modal.xxx` 静态方法 |
| `App` 根节点 | **保留默认 div**（不使用 `component={false}`）：AntD v6 默认 CSS 变量，需要一个 DOM 节点承载变量类名，去掉会丢失主题变量 |
| `message` | `maxCount: 3`（防堆叠）；成功/失败用 `message.success/error`，文案含动作与对象 |
| `notification` | 暂不使用（通知渠道不在本期；预留 doc-16） |
| 全局样式 | 只允许一处全局 CSS（高度/字体/reset），组件样式一律走 AntD token |

### 9.17 Menu（导航菜单）

**用途**：左侧主导航（总览 / 数据集 / 实体注册表 / 任务），四项平铺、无二级。

| 参数 | 约定 |
|---|---|
| `mode` / `theme` | `inline`；`theme` 与 Sider 一致（`dark`），**不混用** |
| `items` | 数据化数组；`key` = 路由 path（`/`、`/datasets`、`/entities`、`/jobs`）；`icon` 按 §5 图标语义（Dashboard / Table / Apartment / Schedule） |
| `selectedKeys` | **受控**，由 `useLocation().pathname` 派生；`onClick` → `navigate(key)`（不用 `defaultSelectedKeys`） |
| 折叠 | `inlineCollapsed` 由 Sider 折叠状态驱动；`tooltip` 保持开启（折叠时悬浮显示名称，位置 `left`） |
| 二级菜单 | 当前无；未来新增时 `openKeys` 受控 + **手风琴**（§1），且需先登记图标语义 |
| `inlineIndent` | 默认 24（与 §1 空间规则一致） |

**禁止**：`mode="horizontal"`（顶部导航不在设计内）、`multiple` / `selectable={false}`、
`danger` 菜单项（导航无删除语义）、`popupRender` / `expandIcon` 自定义、`Menu` 当下拉菜单用（用 Dropdown 场景再议）。

### 9.18 Tag（标签）

**用途**：仅作**枚举 / 分类标注**（domain、`pit_class`、`entity_class`、字段 `type`、`id_type`、`relation_type`）。
与 Badge 的边界：**状态 → Badge（§9.12）；枚举 → Tag**，两者不得互换。

| 参数 | 约定 |
|---|---|
| `variant` | 统一 `filled`（不混用 solid / outlined） |
| `color` | **一律不设**（默认中性）：语义由文字承载，颜色留给状态体系（§2 单一强调色原则） |
| 尺寸 | 默认；表格内与行高一致即可 |
| 内容 | 短枚举原文（英文 token 保持原文，如 `market` / `scd2` / `isin`），不加图标、不加前后缀 |

**禁止**：`color` 预设色与自定义色值（禁止第二套颜色体系）、`closable` / `closeIcon`（无输入型标签）、
`Tag.CheckableTag` / `CheckableTagGroup`（筛选一律 Select，§9.14）、`icon`（图标噪音）、
`href` / `target`（Tag 不做链接）、用 Tag 表达状态（那是 Badge）。

### 9.19 Tooltip（文字提示）

**用途**：截断文本的全文查看与图标按钮说明。**不承载操作**（那是 Popover；当前无场景，暂禁）。

| 场景 | 约定 |
|---|---|
| 表格 `ellipsis` 列（名称 / 错误摘要） | `ellipsis: { showTitle: false }` + `<Tooltip placement="topLeft" title={全文}>`（§9.4 列型规范） |
| 图标按钮（刷新 / 折叠 / 复制） | `placement="top"`，文案 = 动作名 |
| 截断的单行文本 | 仅当确实会被截断时才包 Tooltip（不滥用） |

**约束**

- `title` 为纯文本或简单多行；禁止放按钮 / 链接 / 表单（交互内容不属于 Tooltip）；
- 保持默认气泡样式：**不设置 `color`**（默认深色气泡 = `colorBgSpotlight`）；
- 全站开启 **`ConfigProvider.tooltip.unique: true`**（同一时间仅一个，平滑过渡；§9.16 已装配）；
- 子元素必须能接收 `onMouseEnter/Leave`、`onFocus`；`disabled` 元素外包一层 `<span>`；
- Menu 折叠时的菜单名提示由 Menu 自带（§9.17），不重复包裹。

**禁止**：Tooltip 内放操作、`color` 自定义、`open` 受控（除自动化测试）、Popover 化用法（未来需要时先在本文登记）。

### 9.20 组件选型总表（累计）

| 需求 | 组件 | 备注 |
|---|---|---|
| 列表 + 多列对齐 / 排序 / 分页 | **Table** | 任务、实体、水位的主形态 |
| 长流式列表 / 虚拟滚动 / 吸顶分组 | **Listy** | 任务流、时间轴、履历 |
| 详情（主从） | **Drawer** | right / 560 / resizable / destroyOnHidden；深链 + Esc；禁止多层 |
| 写操作确认 | **`App.useApp().modal`** | 禁静态方法（丢主题）；焦点默认取消；失败保持打开 |
| 轻提示 | **`App.useApp().message`** | maxCount 3；文案含动作与对象 |
| 左侧主导航 | **Menu** | inline + dark；selectedKeys 由路由派生；无二级 |
| 键值集合 | **Descriptions** | 2 列 |
| 状态 | **Badge** | `status` 五值 ↔ §2 语义色；禁自定义色/ribbon/dot |
| 区块分隔 | **Divider** | solid + token 色 |
| 域 → 数据集层级 | **Tree** | 左侧 280 + Splitter；域 Folder / 数据集 Table 图标；`blockNode`；深链双向同步 |
| 关键计数 / 运行指标 | **Statistic** | 总览 4 卡；tabnum；语义色 |
| 内联错误 / 降级 / 结果汇总 | **Alert** | showIcon；错误不可关闭；banner 仅全局降级 |
| 详情键值区 | **Descriptions** | Drawer vertical；页面 horizontal；空值 — |
| 真实分母的完成度 | **Progress** | 无分母不画进度；语义色；禁用 dashboard/steps |
| 日期窗口选择 | **DatePicker.RangePicker** | date 粒度；禁未来；presets；清空=缺省语义 |
| 代码清单输入 | **Input.TextArea** | autoSize；mono；解析计数 Caption |
| 写操作表单 | **Form** | vertical；校验对齐 API；提交前 Modal 确认；不直接发请求 |
| 表格筛选（单值） | **Select** | allowClear；禁 multiple；宽度固定；浮层内注意弹层容器 |
| 检索 / 过滤 | **Input.Search** | onSearch 触发；不做输入即搜 |
| 枚举 / 类型标注 | **Tag** | filled + 不设 color（默认中性）；状态一律用 Badge |
| 截断文本全文 / 图标说明 | **Tooltip** | 不承载操作；不设 color；全站 unique |
