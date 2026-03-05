# Git 工作流技能
当用户需要 Git 操作帮助时：
1. **查看状态**：
   ```bash
   git status
   git log --oneline -10
   git branch -a
   ```
2. **分支管理**：
   - 创建功能分支：`git checkout -b feature/xxx`
   - 合并分支前先更新：`git fetch origin && git rebase origin/main`
   - 删除已合并分支：`git branch -d xxx`
3. **提交规范**（Angular 风格）：
   - `feat: 新功能`
   - `fix: 修复bug`
   - `docs: 文档更新`
   - `refactor: 重构`
   - `chore: 构建/工具`
4. **冲突解决**：
   - 使用 `git diff` 查看冲突
   - 编辑文件解决冲突标记（<<<< ==== >>>>）
   - 执行 `git add` 和 `git commit`
5. **回退操作**：
   - 撤销暂存：`git reset HEAD file`
   - 撤销修改：`git checkout -- file`
   - 回退提交：`git revert HEAD`（安全）或 `git reset --hard HEAD~1`（危险）

⚠️ 执行 `git reset --hard` 或 `git push --force` 前务必确认！
