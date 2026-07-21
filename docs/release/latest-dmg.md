# latest-DMG Release 规范

## 版本与标签

- Release 版本必须等于根目录 `VERSION`。
- 稳定 Release 使用 Git tag `v<version>`，例如 `v1.0.0`。
- 只有一个稳定 Release 可标记为 GitHub 的 `Latest`；预发布不得标记 Latest。

## 必需资产

每个稳定 Release 必须同时上传以下同一 commit 生成的资产：

1. `CHEN-内容采集助手-<version>-universal.dmg`
2. 同名 `.dmg.sha256`
3. 同版本 build manifest JSON
4. `chen-content-collector-public-source-<version>.tar.gz`

DMG 不进入 Git。公开源码包必须由 `scripts/export_public_source.py` 生成，且包内 manifest 的扫描结果必须通过复审。

## 发布前核对

1. 确认目标 commit、`VERSION`、CHANGELOG 和 tag 完全一致。
2. 运行完整测试、类型检查、依赖安全审计和公开源码导出检查。
3. 从干净的正式源码构建 DMG；验证 macOS 最低版本、universal2 架构、签名状态、SHA-256 和 build manifest。
4. 确认 DMG、源码包和 manifest 中没有凭证、本机配置、用户数据、个人网络端点或个人绝对路径。用于本地安全边界的通用回环地址可保留。
5. 在 GitHub Release 页面上传资产后，再次下载并核验 SHA-256。

## Release 说明

Release 正文至少列出版本、commit、兼容的 macOS 版本、架构、签名/公证状态、已知限制、SHA-256 核验命令和回滚版本。不得把生产端点、密钥名称的值、个人路径或用户数据写入正文。

## 回滚

若资产校验、签名、公开源码扫描或发布后核验失败：立即撤销 Latest 标记并恢复上一稳定 Release 的 Latest 状态；保留失败资产的审计记录，但不得继续分发。修复后使用新的 commit 和版本重新构建，不覆盖已发布资产。
