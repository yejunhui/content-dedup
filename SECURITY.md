# Security Policy

## Supported versions

Security fixes are applied to the latest `main` branch and the latest tagged release.

## Reporting a vulnerability

请不要在公开 Issue 中披露可利用细节。优先使用 GitHub 仓库的 **Security → Report a vulnerability** 私密报告；如果不可用，请通过仓库主页公开的联系方式联系维护者，并附上：

- 受影响版本/提交
- 复现步骤
- 影响范围
- 建议修复方向（如有）

## Deployment notes

ContentDedup 的 Web 控制台是**面向本机/可信局域网的自托管工具**，当前版本没有登录认证：

- 默认只监听 `127.0.0.1`。不要直接暴露到公网。
- 如果使用 `--host 0.0.0.0`，请配合防火墙、VPN 或反向代理认证。
- 建议始终设置 `--root`，把浏览、扫描和报告访问限制在媒体目录。
- Web API 可以触发文件移动到 `.dedup-trash`，会重新校验文件内容，但不会永久删除。

## Secrets

- 不要把 GitHub/Gitee Token、SSH 私钥或 `.env` 提交到仓库。
- 如果凭据误入提交历史，请立即撤销并轮换，而不只是删除文件。
