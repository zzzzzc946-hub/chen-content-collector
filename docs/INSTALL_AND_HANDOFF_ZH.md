# 安装、独立部署与日常使用交接

本手册帮助接收方在自己的 macOS 和自己的云端账号中运行 CHEN 内容采集助手。它不会转移原作者的飞书数据、Supabase 数据、Cloudflare Worker、登录态、协作链接或任何凭据。

## 先选用法

| 目标 | 需要的内容 | 结果 |
| --- | --- | --- |
| 本地采集与整理 | GitHub Release 的 DMG | 可在 Mac 上运行桌面采集器；不包含云端日报、手机收集箱或发布服务。 |
| 完整独立环境 | DMG、公开 GitHub 仓库及自己的 Cloudflare、Supabase、飞书账号 | 本地采集器、独立云端日报和手机收集箱。 |

DMG 是本地桌面应用，不会自动建立云端服务。要达到完整体验，必须部署自己拥有的云端环境。

## 1. 下载并验签

1. 在项目 GitHub Release 下载下列同版本文件：
   - `chen-content-collector-<version>-universal.dmg`
   - 对应的 `.dmg.sha256`
   - `chen-content-collector-public-source-<version>.tar.gz`
   - 对应的源码包 `.sha256`
2. 在下载目录核验 DMG：

   ```bash
   shasum -a 256 chen-content-collector-<version>-universal.dmg
   cat chen-content-collector-<version>-universal.dmg.sha256
   ```

   两个 SHA-256 值必须完全一致。源码包也按相同方式核验。
3. DMG 面向 macOS 12+，同时支持 Apple Silicon 和 Intel（universal2）。当前为 ad-hoc 签名，未经过 Apple notarization。

## 2. 安装桌面采集器

1. 双击 DMG，将 `CHEN 内容采集助手.app` 拖到 `Applications`。
2. 首次打开时，在 Finder 中按住 Control 点击应用，选择“打开”，再确认打开。不要从不明来源下载安装包，也不要用 `xattr` 绕过系统保护。
3. 启动应用后，确认桌面入口能打开本地页面。应用只应监听本机回环地址；不应把本地端口暴露到公网。

## 3. 配置本地采集器

1. 从公开源码中的 `config.example.json` 复制出本机未跟踪的 `config.json`。
2. 用自己创建的飞书应用填写 `app_id`、`app_secret`、`app_token`、`table_id` 与 `mobile_inbox_table_id`。示例值不是凭据。
3. `config.json`、Cookie、浏览器 profile、SQLite 数据库、下载媒体和日志都只保留在接收方本机，不提交 Git，不发送给 Agent，不上传 Issue 或聊天记录。
4. 重新启动应用，先用一个无敏感内容的测试链接验证本地采集、素材记录与状态回写。

## 4. 创建独立云端服务

完整云端体验需要接收方自己拥有下列服务。不要复用原作者的资源、密钥、项目引用或表格。

1. **Supabase**：新建项目，保存项目引用、数据库密码、URL、匿名 key、service role key 和私有 Storage bucket 名称。执行公开源码 `max_daily_cloud` 的 migration；已应用的 migration 只允许新增前进修复，不能回改历史。
2. **Cloudflare**：登录自己的账号，创建 Worker 部署目标。Web 前端、`/api/*` 与 `/c/*` 必须由同一个 Worker origin 托管。
3. **飞书**：创建自己的应用和多维表格，授权应用访问该表；为手机收集箱创建专用表并记下其 table ID。桌面端和 Worker 使用同一张专用表。
4. **Worker secrets**：通过 Cloudflare 的 secret store 配置 `APP_ORIGIN`、`MEDIA_SESSION_SECRET`、`OWNER_EMAIL`、`PUBLISHER_TOKEN_PEPPER`、`SHARE_COOKIE_SECRET`、`SUPABASE_ANON_KEY`、`SUPABASE_SERVICE_ROLE_KEY`、`SUPABASE_STORAGE_BUCKET`、`SUPABASE_URL`、`FEISHU_APP_ID`、`FEISHU_APP_SECRET`、`FEISHU_APP_TOKEN`、`FEISHU_MOBILE_INBOX_TABLE_ID`。

真实值只能在服务商的交互式输入框或接收方自己的安全终端会话中输入。不得写入 Git、`.dev.vars`、构建产物、截图、shell 历史、文档或 Codex 对话。

详细部署命令、同源要求、迁移和回滚规范见[独立云端部署指南](INDEPENDENT_CLOUD_DEPLOYMENT_ZH.md)。部署前要明确确认 Cloudflare 与 Supabase 的套餐、费用和地区设置。

## 5. 验收完整流程

按顺序完成，并仅记录通过/失败、资源 ID 和脱敏错误码：

1. 本地桌面端能启动；新建测试链接后能完成采集与本地记录。
2. Worker 健康检查可访问，`APP_ORIGIN` 与浏览器请求的 Worker origin 完全一致。
3. 浏览器访问 `https://<your-worker-origin>/collect`，提交一条测试 URL；手机端只提交，不能直接启动 Mac 采集。
4. Mac 在线时同步该记录；确认记录进入飞书专用表的“待扒取”，而不是自动采集队列。
5. 在桌面端手动选中并处理该条记录，确认飞书状态回写。
6. 发布一份无敏感内容的测试日报，验证 Owner 登录、固定协作链接、`/daily` 与私有媒体访问策略。
7. 在另一浏览器 profile 中做一次只读访问测试；不要在文档、截图或日志中保留原始协作 token。

## 6. 日常使用

- 手机端使用 `/collect` 快速粘贴链接并查看只读状态；它不会自动启动采集。
- Mac 桌面端负责同步、选择、采集、下载、整理与状态回写。
- 日报仅从本地明确选择的内容发布到接收方自己的云端环境。
- 定期备份本地数据；云端媒体副本遵循项目的清理策略，不应作为长期原始素材库。

## 7. 常见问题与恢复

- **首次应用无法打开**：确认 SHA-256 后，使用 Finder 的 Control-点击“打开”；当前发行包未公证。
- **手机提交后无同步**：先检查 Worker secrets、飞书应用权限和专用表 ID，再检查桌面端网络与同步状态；不要把“待扒取”加入自动采集队列。
- **Worker 或迁移失败**：不要修改已应用 migration；核对同源配置与 secret 名称，修复后新增 migration 或回退 Cloudflare 到上一可用版本。
- **协作链接疑似泄露**：立即在 Owner 页面撤销，重新创建并私下发送新链接。原始 token 不可恢复。
- **需要 Codex 协助**：打开 [`AGENT_GUIDED_SETUP_ZH.md`](AGENT_GUIDED_SETUP_ZH.md)，把其中的指令连同公开仓库 URL 和 Release URL 交给 Codex。
