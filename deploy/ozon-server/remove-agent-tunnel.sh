#!/usr/bin/env bash
set -euo pipefail
marker=/etc/keyword-library/agent-tunnel.env
sshd_target=/etc/ssh/sshd_config.d/keyword-agent-tunnel.conf
firewall_target=/etc/systemd/system/keyword-library-agent-firewall.service
command_target=/usr/local/libexec/keyword-agent-tunnel-command
[[ -f "$marker" ]] || { echo '缺少关键词库隧道 marker，拒绝删除未知资源' >&2; exit 2; }
# shellcheck disable=SC1090
source "$marker"
[[ "${KEYWORD_AGENT_SSH_USER:-}" == keyword-agent-tunnel ]] || {
  echo '隧道 marker 用户不匹配' >&2; exit 3;
}
backup="$(mktemp)"
[[ -f "$sshd_target" ]] && cp "$sshd_target" "$backup"
systemctl disable --now keyword-library-agent-firewall.service 2>/dev/null || true
rm -f "$firewall_target"
rm -f "$command_target"
rm -f "$sshd_target"
if ! sshd -t; then
  [[ -s "$backup" ]] && cp "$backup" "$sshd_target"
  rm -f "$backup"
  echo '删除后的 sshd 配置无效，已恢复原配置' >&2
  exit 4
fi
systemctl reload ssh
pkill -u keyword-agent-tunnel 2>/dev/null || true
userdel -r keyword-agent-tunnel 2>/dev/null || true
rm -f "$marker" "$backup"
systemctl daemon-reload
