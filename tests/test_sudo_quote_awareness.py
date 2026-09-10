"""sudo 敏感操作检测的引号感知测试。

背景：sshpass/ssh 的远端命令串里的 sudo 在远端执行，不该触发本机授权弹窗
（生产实证：sshpass ssh host "...; sudo docker ps" 误弹本机 sudo 授权）。
"""
import pytest

from tools.permissions import check_command_permission, _sudo_at_top_level


class TestSudoTopLevel:
    """本机命令位置的 sudo 依然拦截"""

    @pytest.mark.parametrize("cmd", [
        "sudo apt-get update",
        "  sudo docker ps",
        "echo hi; sudo docker ps",
        "echo hi && sudo systemctl restart x",
        "echo hi | sudo tee /etc/x",
    ])
    def test_local_sudo_blocked(self, cmd):
        allowed, reason, category, _ = check_command_permission(cmd)
        assert not allowed
        assert category == 'sudo'

    """引号内的 sudo（远端/嵌套上下文）不弹本机授权"""

    @pytest.mark.parametrize("cmd", [
        # 用户的实际场景：sshpass + ssh 远端命令串里的 sudo
        'sshpass -p \'{{secret:aiserver.password}}\' ssh -o StrictHostKeyChecking=no '
        '{{secret:aiserver.username}}@192.168.148.200 "curl -s http://127.0.0.1:8001/health; '
        'sudo docker ps --format \'{{.Names}}\'"',
        # 单引号远端命令
        "ssh host 'sudo systemctl status nginx'",
        # 引号只是普通字符串
        'echo "sudo make me a sandwich"',
        # 双引号内的转义引号不提前终止字符串
        'ssh host "echo \\"; sudo docker ps"',
    ])
    def test_quoted_sudo_allowed(self, cmd):
        allowed, reason, category, _ = check_command_permission(cmd)
        assert allowed, f"quoted sudo should not trigger local auth: {reason}"

    def test_quoted_sudo_not_sudo_category(self):
        """引号内 sudo 绝不归 sudo 类（即便被 rm 等其他规则误拦——那是另一类
        既有行为，不在本次范围内）。"""
        cmd = "echo '; sudo rm -rf /tmp/x'"
        allowed, _, category, _ = check_command_permission(cmd)
        assert category != 'sudo'

    def test_scanner_directly(self):
        assert _sudo_at_top_level("sudo ls")
        assert _sudo_at_top_level("true && sudo ls")
        assert not _sudo_at_top_level("ssh h 'sudo ls'")
        assert not _sudo_at_top_level('printf "%s" "sudo"')
