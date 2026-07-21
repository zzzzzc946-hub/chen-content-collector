# 独立云端部署指南

本指南只适用于接收方自己的 Supabase、Cloudflare 和飞书资源。先完成[安装、独立部署与日常使用交接](INSTALL_AND_HANDOFF_ZH.md)的本地安装与凭据准备；真实密钥不可写进文件、命令参数、shell 历史、日志、截图或聊天。

## 准备条件

- 一个由接收方管理的 Supabase 项目，以及项目引用、数据库密码、项目 URL、匿名 key、service role key 和私有 Storage bucket 名称。
- 一个由接收方管理的 Cloudflare 账号，可部署 Worker；先确认套餐、费用和区域选择。
- 一个由接收方管理的飞书应用和专用多维表格；应用已取得表格访问权限。
- Node.js、npm、Supabase CLI 和 Wrangler CLI。只可在从公开仓库克隆的工作目录运行以下命令。

## 1. 获取和检查源码

```bash
git clone <PUBLIC_REPOSITORY_URL> chen-content-collector
cd chen-content-collector
git status --short
cat VERSION
cd max_daily_cloud
npm ci
npm test
npm run typecheck
```

上述测试失败时停止部署，先保留脱敏错误信息并修复；不要跳过失败测试继续发布。

## 2. 连接接收方自己的 Supabase 项目

先让接收方在浏览器完成 Supabase CLI 登录。以下命令会连接并写入目标项目，执行前必须确认项目引用与数据库密码属于接收方：

```bash
npx supabase login
npx supabase link --project-ref <YOUR_SUPABASE_PROJECT_REF> --password
npx supabase db push --linked --password
```

命令提示输入数据库密码时再输入，不要把密码放在命令行。迁移只能向前应用；已应用的 migration 不得回写或修改。

在 Supabase Dashboard 创建私有 Storage bucket。记录 bucket 名称和项目 URL，但不要公开 service role key。

## 3. 配置 Worker 与前端

1. 在 Cloudflare 登录接收方账号，并决定唯一的 Worker origin，例如 `https://<your-worker>.workers.dev`。
2. `APP_ORIGIN` 和前端的 `VITE_API_BASE_URL` 必须是同一个 origin；Worker 同时托管前端、`/api/*` 和 `/c/*`。
3. 对每个变量使用交互式 secret 输入。以下命令不会把值作为命令参数传递：

   ```bash
   cd max_daily_cloud
   npx wrangler secret put APP_ORIGIN --config apps/worker/wrangler.toml
   npx wrangler secret put MEDIA_SESSION_SECRET --config apps/worker/wrangler.toml
   npx wrangler secret put OWNER_EMAIL --config apps/worker/wrangler.toml
   npx wrangler secret put PUBLISHER_TOKEN_PEPPER --config apps/worker/wrangler.toml
   npx wrangler secret put SHARE_COOKIE_SECRET --config apps/worker/wrangler.toml
   npx wrangler secret put SUPABASE_ANON_KEY --config apps/worker/wrangler.toml
   npx wrangler secret put SUPABASE_SERVICE_ROLE_KEY --config apps/worker/wrangler.toml
   npx wrangler secret put SUPABASE_STORAGE_BUCKET --config apps/worker/wrangler.toml
   npx wrangler secret put SUPABASE_URL --config apps/worker/wrangler.toml
   npx wrangler secret put FEISHU_APP_ID --config apps/worker/wrangler.toml
   npx wrangler secret put FEISHU_APP_SECRET --config apps/worker/wrangler.toml
   npx wrangler secret put FEISHU_APP_TOKEN --config apps/worker/wrangler.toml
   npx wrangler secret put FEISHU_MOBILE_INBOX_TABLE_ID --config apps/worker/wrangler.toml
   ```

   `MEDIA_SESSION_SECRET`、`PUBLISHER_TOKEN_PEPPER` 和 `SHARE_COOKIE_SECRET` 使用接收方生成的高强度随机值。`FEISHU_MOBILE_INBOX_TABLE_ID` 必须是接收方专用“手机待扒取”表。
4. 构建前端时，接收方在交互式会话设置自己的 `VITE_API_BASE_URL`、`VITE_SUPABASE_URL` 与 `VITE_SUPABASE_ANON_KEY`。这三项会进入浏览器构建产物，因此只能使用公开的 URL 和匿名 key，绝不可使用 service role key。

## 4. 部署和验证

以下命令会部署 Worker，执行前必须让接收方确认目标 Cloudflare 账号和 Worker 名称：

```bash
cd max_daily_cloud
npm --workspace @max-daily-cloud/web run build
npx wrangler deploy --config apps/worker/wrangler.toml
```

部署后验证：

1. 打开 `https://<your-worker-origin>/collect`，确认页面与 API 同源。
2. 提交一个无敏感测试 URL；确认飞书专用表出现“待扒取”记录。
3. 启动桌面采集器，用同一飞书配置同步记录。手机提交只能写入，不能自动开始采集。
4. 在桌面端手动处理测试记录，确认状态回写。
5. 创建无敏感测试日报，验证 Owner 登录与协作入口。原始协作 token 仅私下发送，不写入日志或文档。

## 5. 回滚与维护

- Worker 部署异常时，在 Cloudflare 回退到上一可用版本；不要直接删除项目或数据库。
- Supabase migration 失败时不改写已应用迁移，只增加新的前进修复。
- 怀疑协作链接泄露时立即由 Owner 撤销并新建链接；原始 token 不可恢复。
- 本地 `config.json`、用户数据、SQLite、媒体、Cookie 与浏览器 profile 只由接收方备份和管理，不上传至公开仓库。
