# 贡献指南 (Contributing Guide)

感谢你对 **ViewDesk** 项目的关注与支持！我们欢迎各类贡献，包括但不限于报告 Bug、提出新功能建议、改进文档以及提交代码。

---

## 1. 提交规范

### 1.1 分支管理
- `main`：稳定主分支，只接收经过验证的 Pull Request。
- `feature/*`：新功能开发分支。
- `fix/*`：问题修复分支。

### 1.2 Commit Message 格式
推荐遵循 Conventional Commits 规范：
```text
<type>(<scope>): <subject>

<body>
```
例如：
- `feat(auth): add TOTP two-factor authentication support`
- `fix(daemon): fix PID detection logic on Ubuntu 24.04`
- `docs(readme): update installation instructions`

---

## 2. 本地开发与测试流程

1. **Fork 本仓库** 到个人 GitHub 账号。
2. **克隆分支**：
   ```bash
   git clone https://github.com/<your-username>/view_desk.git
   cd view_desk
   ```
3. **创建特性分支**：
   ```bash
   git checkout -b feature/your-feature-name
   ```
4. **验证代码**：
   - 检查代码风格与逻辑。
   - 确保启动脚本与保活脚本运行正常。
   - 确保没有提交私有密码、真实密钥或 `.env` 文件。
5. **提交 Pull Request**：
   - 详细描述你的修改内容及测试结果。

---

## 3. 安全漏洞报告

如发现涉及安全漏洞的问题（如未授权访问、提权隐患等），请勿直接在公开 Issue 中讨论，请联系仓库维护人员进行负责任的漏洞披露。
