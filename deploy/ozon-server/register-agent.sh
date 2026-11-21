#!/usr/bin/env bash
# 注册一台新的浏览器 Agent（每个使用者一个，端口 17101-17199）
# 用法: register-agent.sh <名字> <ssh公钥> [端口]
set -eu

name="${1:?用法: register-agent.sh <名字> <ssh公钥> [端口]}"
pubkey="${2:?缺少 ssh 公钥}"
port="${3:-}"

[[ "$name" =~ ^[a-z0-9][a-z0-9-]{1,28}$ ]] || { echo "名字只能是小写字母/数字/连字符" >&2; exit 1; }
[[ "$pubkey" == ssh-* ]] || { echo "公钥格式不对，应以 ssh- 开头" >&2; exit 1; }

user="keyword-agent-$name"
home="/var/lib/$user"
gateway="172.19.0.1"
network_id="f5d9976e189c8549ac5fed60a23c76a3d7b1f059d213c0fe72c93871bf0f8b0d"

if [[ -z "$port" ]]; then
  for p in $(seq 17101 17199); do
    ss -H -ltnp "sport = :$p" 2>/dev/null | grep -q . || { port=$p; break; }
  done
fi
[[ -n "$port" ]] || { echo "17101-17199 已占满" >&2; exit 1; }

if ! id "$user" >/dev/null 2>&1; then
  useradd --system --create-home --home-dir "$home" --shell /bin/sh "$user"
fi
usermod -aG keyword-agents "$user" 2>/dev/null || true

install -d -o "$user" -g "$user" -m 0700 "$home/.ssh"
printf %s keyword-agent-%sn "${pubkey%% keyword-library-agent*}" "$name" > "$home/.ssh/authorized_keys"
chown "$user:$user" "$home/.ssh/authorized_keys"
chmod 0600 "$home/.ssh/authorized_keys"

conf="/etc/ssh/sshd_config.d/keyword-agent-$name.conf"
cat > "$conf" <<EOF
Match User $user
    AuthenticationMethods publickey
    PasswordAuthentication no
    AllowTcpForwarding remote
    GatewayPorts clientspecified
    PermitListen $gateway:$port
    PermitTTY no
    X11Forwarding no
    AllowAgentForwarding no
    ForceCommand /usr/local/libexec/keyword-agent-tunnel-command
EOF

sshd -t
systemctl reload ssh

cat > "/etc/keyword-library-agent-$name.marker" <<EOF
KEYWORD_AGENT_GATEWAY_IP=$gateway
KEYWORD_AGENT_NETWORK_ID=$network_id
KEYWORD_AGENT_TUNNEL_PORT=$port
KEYWORD_AGENT_SSH_USER=$user
EOF
chmod 0600 "/etc/keyword-library-agent-$name.marker"

echo "已注册: $user -> $gateway:$port"
echo "Windows 侧 config.psd1 里 ServerGateway=$gateway"
