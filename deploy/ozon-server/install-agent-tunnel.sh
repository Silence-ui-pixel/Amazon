#!/usr/bin/env bash
set -euo pipefail

project_name=keyword-library
network_name="${project_name}_default"
ssh_user=keyword-agent-tunnel
config_dir=/etc/keyword-library
marker="$config_dir/agent-tunnel.env"
sshd_target=/etc/ssh/sshd_config.d/keyword-agent-tunnel.conf
firewall_target=/etc/systemd/system/keyword-library-agent-firewall.service
command_target=/usr/local/libexec/keyword-agent-tunnel-command
home_dir=/var/lib/keyword-agent-tunnel

gateway="$(docker network inspect "$network_name" --format '{{(index .IPAM.Config 0).Gateway}}')"
subnet="$(docker network inspect "$network_name" --format '{{(index .IPAM.Config 0).Subnet}}')"
network_id="$(docker network inspect "$network_name" --format '{{.Id}}')"
bridge="br-${network_id:0:12}"
[[ -n "$gateway" && -n "$subnet" && -n "$network_id" ]] || {
  echo '无法探测关键词库 Docker 网络' >&2; exit 2;
}

public_key="${KEYWORD_AGENT_PUBLIC_KEY:?KEYWORD_AGENT_PUBLIC_KEY is required}"
expected_fingerprint="${KEYWORD_AGENT_KEY_FINGERPRINT:?KEYWORD_AGENT_KEY_FINGERPRINT is required}"
pub_tmp="$(mktemp)"
printf '%s\n' "$public_key" > "$pub_tmp"
actual_fingerprint="$(ssh-keygen -lf "$pub_tmp" -E sha256 | awk '{print $2}')"
rm -f "$pub_tmp"
[[ "$actual_fingerprint" == "$expected_fingerprint" ]] || {
  echo '专用 SSH 公钥指纹不匹配' >&2; exit 3;
}

# All ownership checks happen before user/config/firewall mutations.
if [[ -f "$marker" ]]; then
  # shellcheck disable=SC1090
  source "$marker"
  [[ "${KEYWORD_AGENT_GATEWAY_IP:-}" == "$gateway" \
    && "${KEYWORD_AGENT_NETWORK_ID:-}" == "$network_id" \
    && "${KEYWORD_AGENT_KEY_FINGERPRINT:-}" == "$expected_fingerprint" ]] || {
      echo '现有隧道标记与当前 Gateway/network/key 不一致，请先人工核对并卸载' >&2; exit 4;
    }
fi

listener_output="$(ss -H -ltnp 'sport = :17100' || true)"
if [[ -n "$listener_output" ]]; then
  [[ -f "$marker" ]] || { echo '17100 已被未知监听占用' >&2; exit 5; }
  [[ "$(printf '%s\n' "$listener_output" | wc -l)" -eq 1 \
    && "$listener_output" == *"$gateway:17100"* \
    && "$listener_output" == *'sshd'* ]] || {
      echo '17100 的监听地址或进程不属于关键词库专用 SSH 隧道' >&2; exit 5;
    }
fi

if id "$ssh_user" >/dev/null 2>&1; then
  current_home="$(getent passwd "$ssh_user" | cut -d: -f6)"
  current_shell="$(getent passwd "$ssh_user" | cut -d: -f7)"
  [[ -f "$marker" && "$current_home" == "$home_dir" && "$current_shell" == /bin/sh ]] || {
    echo '同名 SSH 用户不是关键词库已登记资源' >&2; exit 6;
  }
fi

backup_dir="$(mktemp -d)"
installed_user=0
committed=0
rollback() {
  status=$?
  if [[ "$committed" -eq 0 ]]; then
    if [[ -f "$backup_dir/sshd.conf" ]]; then cp "$backup_dir/sshd.conf" "$sshd_target"; else rm -f "$sshd_target"; fi
    if [[ -f "$backup_dir/firewall.service" ]]; then cp "$backup_dir/firewall.service" "$firewall_target"; else rm -f "$firewall_target"; fi
    if [[ -f "$backup_dir/marker" ]]; then cp "$backup_dir/marker" "$marker"; else rm -f "$marker"; fi
    if [[ -f "$backup_dir/command" ]]; then cp "$backup_dir/command" "$command_target"; else rm -f "$command_target"; fi
    systemctl daemon-reload >/dev/null 2>&1 || true
    sshd -t >/dev/null 2>&1 && systemctl reload ssh >/dev/null 2>&1 || true
    if [[ "$installed_user" -eq 1 ]]; then userdel -r "$ssh_user" >/dev/null 2>&1 || true; fi
  fi
  rm -rf "$backup_dir"
  exit "$status"
}
trap rollback EXIT

install -d -m 0700 "$config_dir"
[[ -f "$sshd_target" ]] && cp "$sshd_target" "$backup_dir/sshd.conf"
[[ -f "$firewall_target" ]] && cp "$firewall_target" "$backup_dir/firewall.service"
[[ -f "$marker" ]] && cp "$marker" "$backup_dir/marker"
[[ -f "$command_target" ]] && cp "$command_target" "$backup_dir/command"

if ! id "$ssh_user" >/dev/null 2>&1; then
  useradd --system --create-home --home-dir "$home_dir" --shell /bin/sh "$ssh_user"
  installed_user=1
fi
install -d -o "$ssh_user" -g "$ssh_user" -m 0700 "$home_dir/.ssh"
printf '%s keyword-library-agent\n' "${public_key%% keyword-library-agent}" > "$home_dir/.ssh/authorized_keys"
chown "$ssh_user:$ssh_user" "$home_dir/.ssh/authorized_keys"
chmod 0600 "$home_dir/.ssh/authorized_keys"

sed "s/__GATEWAY__/$gateway/g" deploy/ozon-server/keyword-agent-sshd.conf.template > "$sshd_target"
sed -e "s|__GATEWAY__|$gateway|g" -e "s|__SUBNET__|$subnet|g" -e "s|__BRIDGE__|$bridge|g" \
  deploy/ozon-server/keyword-agent-firewall.service.template > "$firewall_target"
install -d -m 0755 /usr/local/libexec
sed -e "s|__NETWORK_ID__|$network_id|g" -e "s|__BRIDGE__|$bridge|g" \
  deploy/ozon-server/keyword-agent-command.sh.template > "$command_target"
chown root:root "$command_target"
chmod 0755 "$command_target"
cat > "$marker" <<EOF
KEYWORD_AGENT_GATEWAY_IP=$gateway
KEYWORD_AGENT_NETWORK_ID=$network_id
KEYWORD_AGENT_TUNNEL_PORT=17100
KEYWORD_AGENT_SSH_USER=$ssh_user
KEYWORD_AGENT_KEY_FINGERPRINT=$expected_fingerprint
EOF
chmod 0600 "$marker"

# Validate the candidate at its final included path before any reload.
sshd -t
sshd -T -C "user=$ssh_user,host=localhost,addr=127.0.0.1" | grep -Eq '^permitlisten[[:space:]]+'"$gateway"':17100$'
systemctl daemon-reload
systemctl reload ssh
systemctl enable keyword-library-agent-firewall.service
systemctl restart keyword-library-agent-firewall.service

committed=1
echo "Agent tunnel installed on $gateway:17100 (network $network_id)"
