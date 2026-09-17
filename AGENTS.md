# AGENTS.md

本文件是 `CLAUDE.md` 的 Codex 适配版本。主体内容（架构、开发命令、配置等）见 [CLAUDE.md](./CLAUDE.md)。

---

## 差异（与 CLAUDE.md 不同之处）

- 本文件面向 **Codex (Codex.ai)** 而非 **Claude Code**
- 关键规则和开发命令与 CLAUDE.md 完全一致

## 关键规则（与 CLAUDE.md 共享，同等重要）

- **NEVER push to the `release` branch unless the user explicitly and directly asks you to.** Pushing to `main` is fine. Pushing to `release` triggers CI builds and Docker image releases — only do it on explicit user request.
- **NEVER commit or push after making code changes until the user has tested and confirmed the changes work.** Write code, present it to the user, and wait for explicit approval before staging, committing, or pushing.
- **ALWAYS use UTF-8 encoding for all file operations.** PowerShell's `Get-Content` and `Set-Content` default to GBK/UTF-16 which corrupts Chinese characters. Use `open(path, 'w', encoding='utf-8')` or the Edit/Write tool for any file modifications involving non-ASCII text. If PowerShell is unavoidable, use `[System.IO.File]::ReadAllText()` and `Set-Content -Encoding utf8`.
- **分支映射：`zxs` 分支对应内网 GitLab（remote `jsb`，project `jsb/zxs-ai-agency`）的产品线，推送时必须同时更新 jsb 的 `main` 分支**——`git push jsb zxs` + `git push jsb zxs:main`（zxs 与 jsb/main 保持同源快进关系），CI 制品（win/deb 包）都从 jsb 构建。GitHub（remote `origin`）的 `main`/`zxs` 按同名分支推送。品牌定制（`build_data/`）只进 `zxs`，不 cherry-pick 进 GitHub `main`。
- **提交信息（commit message）中不得出现真实人名。**

---

> 完整文档请参阅 [CLAUDE.md](./CLAUDE.md)
