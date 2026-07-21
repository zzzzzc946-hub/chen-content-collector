# 设计与验收记录

## 2026-07-18 手机链接收集箱生产验收

本节记录手机链接收集箱的真实生产部署和端到端验收。飞书凭证只保存在本机配置和 Cloudflare secret store，未写入仓库、构建产物或验收记录。

### 生产环境记录

| 项目 | 证据 |
| --- | --- |
| 执行日期 | `2026-07-18`（Asia/Shanghai） |
| Worker origin | `https://max-daily-cloud-worker.1643434181.workers.dev` |
| Worker Version ID | `aca268e7-56a7-4dc4-9ece-bf929654872b` |
| Worker API | `GET /api/mobile-inbox` 返回 `200`；空表快照 `pendingCount=0` |
| Web 入口 | `/collect` 返回 `200`，生产构建资源已上传 |
| 飞书专用表 | `手机待扒取`，七字段，桌面固定系统表同名 |
| 桌面代码提交 | `b87947d`（状态回写字段与混合平台选择修复） |

### 自动化验证

- [x] 根 Python：`221` tests passed
- [x] Publisher：`47` tests passed
- [x] 云端 Vitest：`407` tests passed
- [x] `npm run typecheck`
- [x] `npm audit --audit-level=high`：0 vulnerabilities
- [x] 生产 Web build
- [x] Worker dry-run
- [x] 独立代码审查：`APPROVED`

### 真实端到端验收

- [x] 手机尺寸真实浏览器打开 `/collect`
- [x] 一次提交 YouTube 与 B站链接：新增 `2` 条，待扒取 `2`
- [x] 重复提交 YouTube：新增 `0` 条，已存在 `1`
- [x] Mac App 启动后约 10 秒内同步到固定表 `手机待扒取`
- [x] 固定表保留完整现有操作：采集选中、选中+逐字稿、批量下载、网页日报、录入日报、下载视频
- [x] B站行按自身平台采集，桌面结果为 `基础信息成功`，手机端同步显示 `基础信息成功`
- [x] YouTube 行真实处理结果为 `需登录`；飞书/手机端规范化显示为 `等待登录`
- [x] 最终手机端待扒取数量为 `0`
- [x] 本地首页与 `/daily`、生产 `/collect` 与 `/daily` 均返回 `200`

### 运行归属

- [x] `/api/version.commit` 与正式 `master` 一致
- [x] 源码与安装脚本 SHA-256 一致
- [x] 原生 App bundle identifier 为 `com.chen.content-collector.native`
- [x] Python 服务由原生 App 直接派生
- [x] 旧 `com.chen.content-link-collector.desktop-app` LaunchAgent 保持禁用

## 2026-07-15 固定协作生产验收

本节记录真实生产迁移、部署和浏览器验收。原始 token、Cookie、Authorization header 和 service-role key 均未写入仓库。

**当前状态**

- 状态：MVP 生产验收通过；当天视频 `206` 因 2026-07-15 没有已发布日报，留待下一份真实当天日报复验
- 对应功能：Owner 管理固定协作入口，协作者通过 raw `/c/<token>` 进入同源 `/daily`
- 固定日常地址：`https://max-daily-cloud-worker.1643434181.workers.dev/daily`

### 生产环境记录

| 项目 | 证据 |
| --- | --- |
| 执行日期 | `2026-07-15`（Asia/Shanghai） |
| 执行人 | Codex，在用户授权的本机 Cloudflare / Supabase 会话中执行 |
| 部署前 `master` 提交 | `90e631c` |
| Worker origin | `https://max-daily-cloud-worker.1643434181.workers.dev` |
| Worker Version ID | `1e24e76b-52e8-4c27-9093-9693cd2499c6` |
| Supabase project ref | `yfhoplwxyqlnxfnyiknp` |
| 已应用迁移 | `001-019`，远端迁移列表连续 |
| 最终固定链接记录 | 仅记录完整 URL 的 SHA-256 前 12 位：`98f0f2ff3f4f` |

### 自动化验证

- [x] 根 Python：`205` tests passed
- [x] Publisher：`45` tests passed
- [x] 云端 Vitest：`351` tests passed
- [x] 可复用 Skill：`3` tests passed，6 个 Skill 均通过 `quick_validate.py`
- [x] `npm run typecheck`
- [x] `npm audit --audit-level=high`：0 vulnerabilities
- [x] 真实生产环境变量 Web build
- [x] Worker dry-run
- [x] 合并前后 `git diff --check` 与干净 `master` 核对

### 生产部署

- [x] Supabase 迁移 `017`：固定协作入口与三字段原子更新
- [x] Supabase 迁移 `018`：回填迁移前 Owner 编辑造成的当前发布快照漂移
- [x] Supabase 迁移 `019`：把 stale conflict 从可重试 `40001` 改为立即返回的非重试冲突
- [x] `APP_ORIGIN` 与 `VITE_API_BASE_URL` 均指向同一个 `workers.dev` origin
- [x] Worker 同源托管 Web、`/c/*` 与 `/api/*`
- [x] 每小时媒体清理 cron 保持 `0 * * * *`

### 真实浏览器验收

#### 固定入口与无登录

- [x] Owner 身份通过生产 API 读取、创建和撤销固定链接；Owner 面板交互由自动化组件测试覆盖
- [x] 全新浏览器上下文打开 raw `/c/<token>`，直接进入 `/daily`
- [x] 首次交换后地址栏不再保留 raw token
- [x] 页面不出现 OTP、邮箱登录、退出登录或权限管理入口
- [x] 刷新 `/daily` 仍可继续使用，无需再次登录

#### 日报索引、日期切换与字段权限

- [x] 默认打开最新已发布日报，共读到 3 个发布日期
- [x] 日期导航可切换所有已发布日报
- [x] 地址栏始终保持 `/daily`
- [x] 仅 `max_daily_card`、`max_feedback`、`review_status` 三个字段可编辑
- [x] 标题、正文、来源链接、发布操作、邀请/分享管理均不可编辑或不可见
- [x] 生产并发实测：同版本双写分别在约 1.51 秒和 2.00 秒返回 `200`、`409`，内容未被覆盖

#### 视频与历史日报

- [ ] 当前日视频 `206 Partial Content` 与拖动播放：2026-07-15 无当天已发布日报，下一份真实当天日报复验
- [x] 历史日报不渲染播放器；实测 `videoCount=0`
- [x] 历史日报保留来源链接；实测原视频链接数量为 1
- [x] 72 小时清理只影响媒体副本，不影响日报数据和来源链接

#### 撤销语义

- [x] Owner 撤销返回 `204`，旧 `/c/<token>` 返回 `410`
- [x] 旧 Cookie 在下一次日报索引请求返回 `410`
- [x] 重建新链接后，新 raw token 交换返回 `302`，新 Cookie 读取日报返回 `200`

#### 响应式与控制台

- [x] 桌面 `1440x1000` 三栏布局正常，无不连贯重叠
- [x] 移动 `390x844`：`scrollWidth=390`，无页面级横向溢出
- [x] 最终新链接浏览器会话控制台：0 errors，0 warnings
- [x] 固定入口、日报读取和日期切换无意外 4xx/5xx；预期撤销与并发冲突除外

### 证据文件

- 桌面截图：`output/playwright/fixed-daily-desktop.png`
- 移动截图：`output/playwright/fixed-daily-mobile.png`
- raw token 交换：HTTP `302`，最终地址 `/daily`
- 历史媒体策略：`videoCount=0`，来源链接数量 `1`
- 撤销语义：旧 raw `410`，旧 Cookie `410`
- 最终浏览器会话：刷新后仍为 `/daily`，无 OTP，控制台 0 errors / 0 warnings

### 最终结论

- final result: passed for fixed-link MVP
- data-dependent follow-up: 下一份真实当天日报发布后补验视频 `206` 和拖动播放

## 说明

以下历史段落是 2026-07-14 的本地/预发视觉对照记录，用于保留 UI 演进证据；它们不等于固定协作入口的生产验收结论。

**对照目标**

- Source visual truth: `/var/folders/d7/_4vthhmx29l0n9sprdf6jh0c0000gn/T/codex-clipboard-f8ed6c4d-641d-45d4-ada8-bb8e5b0bc895.png`
- Desktop implementation: `output/playwright/cloud-workbench-reference-size.png`
- Mobile implementation: `output/playwright/cloud-workbench-mobile-final.png`
- Route: `/r/7c33d6ae-95ae-43b2-87b7-fa83e73130b1`
- State: Owner 已登录，真实 2026-07-14 日报，1 条素材，真实视频、文稿与 MAX 卡片。
- Viewports: desktop 1930 x 1280; mobile 390 x 844.

**完整画面对照**

- 本地参考图与桌面实现已在同一轮视觉输入中共同打开并对照。
- 信息层级一致：橙色品牌头、日期与素材数、工作台入口、左侧素材列表、中间视频和文稿、右侧 MAX 口喷卡片。
- 云端账号、刷新、权限和退出控制保留在右上角，这是云端产品的必要差异。
- 本地版尚未接入云端的多个工具页没有伪装成可用按钮；当前只显示真实可用的工作台。

**重点区域对照**

- Typography: 标题、素材标题和面板标题均使用与本地版相同的高对比粗体层级；小字保持清晰，无负字距。
- Spacing and layout: 桌面三栏比例、16px 栏间距、8px 圆角和橙色选中边框与参考一致；手机变为单栏。
- Colors: 深色中性背景、冷灰蓝面板、暖橙品牌和选中态已映射到参考图；没有蓝色单一主题残留。
- Image quality: 使用真实上传视频，没有占位图或伪造素材；视频保持 16:9、`object-fit: contain`，不裁掉内容。
- Copy: 日期、素材数、标题、来源、文稿、卡片及审核状态全部来自真实日报数据。

**对照历史**

- Pass 1, P2: 超长文稿把手机页面拉成极长页面；顶部暖色区域过于生硬。
- Fix: 文稿区域改为有上限的内部滚动；顶部暖色改为横向自然过渡。
- Pass 2 evidence: `output/playwright/cloud-workbench-desktop-final.png` and `output/playwright/cloud-workbench-mobile-final.png`。
- Pass 2 result: 手机 `clientWidth=390`, `scrollWidth=390`；按钮内容均未溢出。无剩余 P0/P1/P2 视觉问题。

**交互与控制台**

- 已验证真实日报读取、Owner 权限入口、素材选中态、视频渲染、编辑表单和保存按钮。
- 本地预览通过 Playwright 路由转发正式 API；初次加载产生的 401 来自测试路由建立前的请求，路由建立后真实日报正常载入。正式生产构建不使用该转发层。

**Follow-up Polish**

- P3: 后续实现日期索引和其他真实工具视图后，可逐步补齐本地版顶部工具条；当前不展示无功能控件。

final result: passed

## 2026-07-14 云端日报九按钮工具条

**对照目标**

- Source visual truth: `/var/folders/d7/_4vthhmx29l0n9sprdf6jh0c0000gn/T/codex-clipboard-cb41226c-bc45-4e35-a973-dc0127bd1e6e.png`
- Desktop workbench: `output/playwright/toolbar-workbench-desktop.png`
- Desktop fields panel: `output/playwright/toolbar-fields-desktop.png`
- Desktop table: `output/playwright/toolbar-table-desktop-final.png`
- Mobile workbench: `output/playwright/toolbar-workbench-mobile-final.png`
- Route: `/r/7c33d6ae-95ae-43b2-87b7-fa83e73130b1`
- State: Editor 已登录，使用真实生产日报数据。

**功能验收**

- 四个视图入口均为真实视图：工作台、视频专注、文稿阅读、表格总览。
- 五个设置入口均可展开且互斥：字段配置、筛选、排序、行高、调整空间。
- 字段显示、关键词和状态筛选、排序字段和方向、三档行高、左右栏宽均可操作并持久化 UI 偏好。
- 切换到视频专注后隐藏协同栏；切换到文稿阅读后不渲染播放器；表格总览使用完整宽度；返回工作台恢复三栏。
- Viewer 权限不会因视图切换或筛选而获得编辑能力。

**响应式与视觉**

- 桌面工具条按参考图分成左右两组，选中视图和展开设置使用暖橙描边，延续本地日报视觉。
- Pass 1, P2: 窄屏仍被桌面属性选择器覆盖，内容出现狭窄第二列。
- Fix: 移动端对四种 `data-view` 布局使用同等或更高选择器优先级，统一回落为单列。
- Pass 2: 页面 `clientWidth=390`, `scrollWidth=390`；工具条在自身容器内横向滚动，不造成页面横向溢出。
- 固定尺寸控件、文字换行和面板宽度均正常，无剩余 P0/P1/P2 视觉问题。

**交互与控制台**

- Playwright 已逐项点击九个按钮并验证对应视图或设置面板出现。
- 本地预览的 favicon 404 和路由建立前 401 属于预览噪声；真实数据加载后未发现功能 JavaScript 错误。

final result: passed
