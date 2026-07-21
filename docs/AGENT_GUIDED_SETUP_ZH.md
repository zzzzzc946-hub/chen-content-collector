# 给 Codex 的独立安装与部署指令

接收方可把下面代码块完整复制到一个新的 Codex 任务中，并把两个占位 URL 替换为公开仓库和对应 Release。一个任务只处理这一个项目；如需实现新的功能，应另开 Codex 窗口并使用单功能 Git worktree。

```text
你要帮我在我自己的 macOS 和我自己的云端账号中安装并部署 CHEN 内容采集助手。项目公开仓库：<PUBLIC_REPOSITORY_URL>
发布版本与安装包：<GITHUB_RELEASE_URL>

目标：让我获得独立、可用的本地桌面采集器、云端日报和手机链接收集箱；绝不访问、猜测、请求或复用原作者的生产数据、飞书表、Supabase 项目、Cloudflare Worker、浏览器登录态、协作链接或密钥。

按 docs/INSTALL_AND_HANDOFF_ZH.md 和 docs/INDEPENDENT_CLOUD_DEPLOYMENT_ZH.md 逐阶段执行。先只读审计：确认 macOS 版本、磁盘空间、Git、Python、Node、npm、GitHub CLI、Cloudflare Wrangler 和 Supabase CLI 的可用性；核验 Release 文件 SHA-256。给出缺失项和预计的外部服务/费用影响。

除非我明确确认，不得：创建或删除云端资源、启用付费套餐、运行数据库 migration、部署或回退 Worker、修改 DNS、上传媒体、发布日报、创建固定协作链接、写入系统 Keychain、修改 LaunchAgent，或执行任何不可逆操作。

配置凭据时，逐项说明凭据由哪个接收方账号创建、存在哪里、用于什么验证；只让我在服务商页面或交互式 secret 输入提示中输入真实值。绝不要求我把真实密钥粘贴到聊天、源文件、命令行参数、shell 历史、日志或截图。输出中只能显示变量名、资源 ID 和脱敏状态。

先安装并验证 DMG 的本地桌面采集功能。然后用我的账号创建独立 Supabase、Cloudflare 和飞书资源，配置本地 config.json 和 Cloudflare Worker secrets，执行公开文档规定的部署流程。每个外部写操作前暂停，说明目标、影响、回滚方式并请求我确认。

最终按交接手册第 5 节执行端到端验收：本地采集、Worker 同源、/collect 提交、Mac 同步、飞书状态回写、测试日报和协作入口。输出一份脱敏交接结果，包含版本、SHA-256、系统版本、通过项、失败项、资源 ID、回滚位置和下一步；不要输出任何秘密或原始 token。
```

## Agent 执行规则

### 阶段 0：建立边界

1. 读取仓库中的 `README.md`、`SECURITY.md`、`docs/INSTALL_AND_HANDOFF_ZH.md` 与 `docs/INDEPENDENT_CLOUD_DEPLOYMENT_ZH.md`，不扫描或上传接收方的个人目录。
2. 确认当前任务只处理该项目。实施新功能时，先提醒接收方使用独立窗口与单功能 worktree。
3. 先列出计划操作。对任何外部创建、付费、部署、迁移、上传、删除或权限变更，必须等待明确确认。

### 阶段 1：下载、校验和安装

1. 从指定 Release 下载同一版本的 DMG 和 `.sha256`，只比较 SHA-256，不绕过 macOS 安全机制。
2. 确认系统为 macOS 12+；说明 universal2、ad-hoc 签名、未公证和 Finder 首次打开方法。
3. 安装后验证桌面应用能启动且服务仅对本机回环地址提供服务。

### 阶段 2：本地凭据

1. 仅从 `config.example.json` 创建未跟踪的 `config.json`。
2. 引导接收方在自己的飞书账号创建应用和专用多维表格，逐项填写 `app_id`、`app_secret`、`app_token`、`table_id` 与 `mobile_inbox_table_id`。
3. 只验证字段是否存在和 API 是否成功；不显示实际值，不把 `config.json` 纳入 Git。

### 阶段 3：独立云端环境

1. 说明 Cloudflare 和 Supabase 可能产生费用，先让接收方确认其套餐、项目和区域选择。
2. 引导接收方创建自己的 Supabase 项目、私有 Storage bucket 与 Cloudflare Worker；不能连接其他项目。
3. 使用服务商 secret store 配置项目文档列出的变量。每次交互式写入一个 secret 后只读取“已配置”状态。
4. 在接收方确认后执行数据库 migration 与 Worker 部署。若失败，停止并给出脱敏错误、当前版本和安全的回滚入口；不得强行重试同一失败操作两次以上。

### 阶段 4：端到端验收与交付

1. 用无敏感测试内容验证本地采集、`/collect`、桌面同步、飞书“待扒取”记录与状态回写。
2. 验证 Worker 同源配置、测试日报、协作入口和私有媒体约束。
3. 输出脱敏报告：仓库与 Release URL、版本、DMG/SHA-256、系统版本、已验证功能、资源 ID、失败项、回滚位置、后续维护命令。
4. 明确说明：原始协作 token、Cookie、密钥、数据库密码和用户数据从未显示、保存或上传。

## 不可跨越的安全边界

- 不读取或上传浏览器 profile、Keychain、Cookie、SQLite、下载视频、聊天记录或其他个人文件。
- 不将 secret 以环境变量命令行参数、文件内容、Git commit、日志、截图或聊天文本传递。
- 不修改原作者的仓库、云端账号或生产系统。
- 不把“待扒取”记录放进自动采集队列。
- 不声称 Intel 实机验收已经完成，除非接收方确实在 Intel Mac 上完成并记录验收。
