# 系统监控技能
当用户询问系统状态、性能或异常时：
1. **CPU/内存/磁盘**使用情况：
   ```bash
   # macOS
   top -l 1 | head -10
   vm_stat
   df -h
   # Linux
   top -bn1 | head -10
   free -h
   df -h
   ```
2. **异常进程**检测：
   ```bash
   ps aux --sort=-%cpu | head -15   # CPU占用最高
   ps aux --sort=-%mem | head -15   # 内存占用最高
   ```
3. **网络状态**：
   ```bash
   netstat -an | grep LISTEN        # 开放端口
   lsof -i -P | head -20           # 网络连接
   ```
4. **磁盘空间**分析：
   ```bash
   du -sh * | sort -rh | head -20   # 大文件/目录
   ```
5. 将监控结果整理为易读的报告，标记异常值（🔴 高 / 🟡 中 / 🟢 正常）
6. 对异常情况给出处理建议
