# CHEN 内容采集助手

一个面向本地内容采集、整理、日报协作与 macOS 桌面运行的源码项目。

本公开源码不包含 Git 历史、生产凭证、用户数据、浏览器登录态、数据库、媒体文件或本机路径。云端服务、第三方平台账号与本地配置均须由部署者自行创建和管理。

## 开始使用

1. 安装项目所需的 Python 与 Node.js 运行环境。
2. 从 `config.example.json` 创建本地 `config.json`，只填入自己拥有的服务配置；该文件不得提交。
3. 在修改前运行最小相关测试；发布前执行完整验证矩阵。

## 从安装包到独立环境

- 只需本地桌面采集器：下载 GitHub Release 的 DMG，并按[安装、独立部署与日常使用交接](docs/INSTALL_AND_HANDOFF_ZH.md)完成 SHA-256 核验和安装。
- 需要云端日报与手机收集箱：接收方必须部署自己的 Cloudflare、Supabase 和飞书环境。把[给 Codex 的独立安装与部署指令](docs/AGENT_GUIDED_SETUP_ZH.md)连同公开仓库 URL 和 Release URL 交给 Codex，即可获得逐步、确认式的配置与验收协助。

公开仓库与 Release 从不包含原作者的生产凭据、数据、登录态或协作链接；接收方只能使用自己创建和管理的服务资源。

常用检查：

```bash
PYTHONPYCACHEPREFIX=/tmp/chen-collector-pycache python3 -m unittest -v

cd max_daily_cloud
npm ci
npm test
npm run typecheck
```

## 公开源码导出

公开分发包必须通过 `scripts/export_public_source.py` 从指定 Git commit 创建。该工具不复制 `.git`，并排除内部任务资料、运行数据、个人绝对路径与私有配置；`127.0.0.1` 等用于本地安全边界的通用地址会保留。包内的 `PUBLIC-SOURCE-MANIFEST.json` 会记录排除的相对路径和原因。

```bash
python3 scripts/export_public_source.py \
  --source . \
  --ref HEAD \
  --output dist/chen-content-collector-public-source.tar.gz
```

发布 DMG 的规范见 [docs/release/latest-dmg.md](docs/release/latest-dmg.md)。版本记录见 [docs/版本记录.md](docs/版本记录.md)。

## 安全与贡献

- 提交安全问题前，请阅读 [SECURITY.md](SECURITY.md)。
- 参与开发请阅读 [CONTRIBUTING.md](CONTRIBUTING.md)。
- 变更记录维护在 [CHANGELOG.md](CHANGELOG.md)。

## 许可证

项目自有源码采用 [MIT License](LICENSE)。第三方依赖分别受其自身许可证约束；每次 Release 必须重新生成并核验第三方许可证清单。
