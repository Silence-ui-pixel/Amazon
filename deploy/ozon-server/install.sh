#!/usr/bin/env bash
set -Eeuo pipefail

project_dir="${KEYWORD_LIBRARY_PROJECT_DIR:-/home/kill_project/keyword_library}"
compose=(docker compose -p keyword-library -f compose.yaml -f compose.ozon-server.yaml --env-file .env.production)

cd "$project_dir"
umask 077

if [[ ! -f .env.production ]]; then
  db_password="$(openssl rand -hex 24)"
  agent_token="$(openssl rand -hex 32)"
  cat > .env.production <<EOF
POSTGRES_USER=keyword_prod
POSTGRES_PASSWORD=$db_password
POSTGRES_DB=keyword_library
POSTGRES_PORT=15433
HTTP_PORT=18080
DASHSCOPE_SECRET_DIR=/etc/keyword-library/secrets/dashscope
DASHSCOPE_MODEL=openai/mimo-v2.6-pro
DASHSCOPE_VISION_MODEL=openai/mimo-v2.6-pro
KEYWORD_BROWSER_AGENT_URL=
KEYWORD_BROWSER_AGENT_TOKEN=
KEYWORD_AGENT_RESERVED_TOKEN=$agent_token
EOF
fi
chmod 600 .env.production

# backend 镜像以非 root 用户运行。宿主机密钥仍归 root 所有，只通过一个
# 固定的只读补充组授予目录 traverse 和单文件 read，禁止 other 访问。
DASHSCOPE_SECRET_GID="${DASHSCOPE_SECRET_GID:-61001}"
[[ "$DASHSCOPE_SECRET_GID" =~ ^[0-9]+$ ]] || { echo 'DASHSCOPE_SECRET_GID 必须为数字' >&2; exit 1; }
dashscope_secret_dir="${DASHSCOPE_SECRET_DIR:-$(sed -n 's/^DASHSCOPE_SECRET_DIR=//p' .env.production | tail -n 1)}"
dashscope_key="$dashscope_secret_dir/api-key.csv"
[[ -d "$dashscope_secret_dir" ]] || { echo "缺少 DashScope 密钥目录：$dashscope_secret_dir" >&2; exit 1; }
[[ -f "$dashscope_key" ]] || { echo "缺少 DashScope 密钥文件：$dashscope_key" >&2; exit 1; }
chown root:"$DASHSCOPE_SECRET_GID" "$dashscope_secret_dir"
chmod 0710 "$dashscope_secret_dir"
chown root:"$DASHSCOPE_SECRET_GID" "$dashscope_key"
chmod 0640 "$dashscope_key"

# 在任何镜像标签、构建、迁移或启动动作前确认端口与同名 Docker 资源归属。
# 已属于本项目且工作目录一致的资源允许复用，其他占用一律立即停止。
python3 deploy/ozon-server/preflight.py \
  --project-dir "$project_dir" \
  --env-file "$project_dir/.env.production"

# Docker Hub 在本服务器不可达；给已从 DaoCloud 拉取的同内容镜像补本地标准标签，
# 让 Dockerfile 在禁用 pull 的情况下完全使用本地缓存。
docker tag docker.m.daocloud.io/library/python:3.12-slim python:3.12-slim
docker tag docker.m.daocloud.io/library/node:22-alpine node:22-alpine
docker tag docker.m.daocloud.io/library/nginx:1.27-alpine nginx:1.27-alpine

"${compose[@]}" config --quiet

# legacy builder 只读取本地基础镜像，避免 BuildKit 再访问 Docker Hub 元数据。
DOCKER_BUILDKIT=0 COMPOSE_DOCKER_CLI_BUILD=0 "${compose[@]}" build --pull=false migration backend frontend

"${compose[@]}" up -d postgres migration
"${compose[@]}" wait migration
"${compose[@]}" up -d backend frontend

for _ in $(seq 1 60); do
  if curl -fsS --max-time 3 http://127.0.0.1:18080/healthz >/dev/null \
    && curl -fsS --max-time 3 http://127.0.0.1:18080/api/v1/health/live >/dev/null; then
    break
  fi
  sleep 5
done

curl -fsS --max-time 5 http://127.0.0.1:18080/healthz >/dev/null
curl -fsS --max-time 5 http://127.0.0.1:18080/api/v1/health/live >/dev/null
"${compose[@]}" ps

