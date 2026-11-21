"""关键词库部署包的离线一致性检查。"""

from __future__ import annotations

import argparse
import ipaddress
import json
import os
import re
import subprocess
from pathlib import Path
from urllib.parse import urlsplit


def _read(path: Path, errors: list[str]) -> str:
    if not path.is_file():
        errors.append(f"缺少文件：{path}")
        return ""
    return path.read_text(encoding="utf-8")


def _require(text: str, needles: tuple[str, ...], source: str, errors: list[str]) -> None:
    for needle in needles:
        if needle not in text:
            errors.append(f"{source} 缺少配置：{needle}")


def validate_frontend(root: Path) -> list[str]:
    """返回前端生产部署缺口；空列表表示检查通过。"""
    errors: list[str] = []
    dockerfile = _read(root / "frontend" / "Dockerfile", errors)
    nginx = _read(root / "deploy" / "nginx" / "http.conf.template", errors)
    entrypoint = _read(root / "deploy" / "nginx" / "40-configure-site.sh", errors)
    _read(root / ".dockerignore", errors)
    _require(
        dockerfile,
        (
            "FROM node:22-alpine AS builder",
            "npm ci",
            "npm run build",
            "FROM nginx:1.27-alpine AS runtime",
            "COPY --from=builder /build/dist/ /usr/share/nginx/html/",
        ),
        "frontend/Dockerfile",
        errors,
    )
    _require(
        nginx,
        (
            "client_max_body_size 25m",
            "location /api/",
            "proxy_pass ${BACKEND_UPSTREAM}",
            "try_files $uri $uri/ /index.html",
        ),
        "deploy/nginx/http.conf.template",
        errors,
    )
    _require(
        entrypoint,
        ("envsubst '${BACKEND_UPSTREAM} ${SERVER_NAME}'",),
        "Nginx 启动脚本",
        errors,
    )
    return errors


def validate_compose(root: Path) -> list[str]:
    """返回 Compose 服务编排缺口。"""
    errors: list[str] = []
    try:
        completed = subprocess.run(
            ["docker", "compose", "config", "--format", "json"],
            cwd=root,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
    except FileNotFoundError:
        return ["找不到 docker compose，无法解析 compose.yaml"]
    if completed.returncode:
        return [f"docker compose config 失败：{completed.stderr.strip()}"]
    try:
        config = json.loads(completed.stdout)
    except json.JSONDecodeError as error:
        return [f"docker compose config 未返回有效 JSON：{error}"]
    services = config.get("services", {})
    expected = {"postgres", "migration", "backend", "frontend"}
    missing = expected.difference(services)
    if missing:
        errors.append(f"Compose 缺少服务：{', '.join(sorted(missing))}")
        return errors

    migration_deps = services["migration"].get("depends_on", {})
    backend_deps = services["backend"].get("depends_on", {})
    frontend_deps = services["frontend"].get("depends_on", {})
    if migration_deps.get("postgres", {}).get("condition") != "service_healthy":
        errors.append("migration 必须等待 postgres 健康")
    if backend_deps.get("migration", {}).get("condition") != "service_completed_successfully":
        errors.append("backend 必须等待 migration 成功完成")
    if frontend_deps.get("backend", {}).get("condition") != "service_healthy":
        errors.append("frontend 必须等待 backend 健康")
    if services["backend"].get("ports"):
        errors.append("backend 不得向宿主机发布端口")
    postgres_ports = services["postgres"].get("ports", [])
    if not postgres_ports or postgres_ports[0].get("host_ip") != "127.0.0.1":
        errors.append("本地 PostgreSQL 端口必须仅绑定 127.0.0.1")
    if not services["frontend"].get("ports"):
        errors.append("frontend 必须发布 HTTP 端口")
    backend_environment = services["backend"].get("environment", {})
    if not backend_environment.get("DASHSCOPE_MODEL"):
        errors.append("backend 不得用空 DASHSCOPE_MODEL 覆盖应用默认模型")
    return errors


def validate_persistence(root: Path) -> list[str]:
    """返回迁移与数据持久化缺口。"""
    errors: list[str] = []
    dockerfile = _read(root / "backend" / "Dockerfile", errors)
    _require(
        dockerfile,
        (
            "COPY migrations ./migrations",
            "COPY alembic.ini ./alembic.ini",
            "USER keyword",
            "ENV KEYWORD_STORAGE_ROOT=/app/data",
        ),
        "backend/Dockerfile",
        errors,
    )
    copy_app = dockerfile.find("COPY app ./app")
    install_match = re.search(r"RUN\s+pip\s+install\b[\s\S]*?(?:^|\s)\.\s*$", dockerfile, re.MULTILINE)
    install_package = install_match.start() if install_match else -1
    if copy_app < 0 or install_package < 0 or copy_app > install_package:
        errors.append("backend/Dockerfile 必须在 pip install . 前复制 app 包")
    try:
        completed = subprocess.run(
            ["docker", "compose", "config", "--format", "json"],
            cwd=root,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
    except FileNotFoundError:
        return errors + ["找不到 docker compose，无法检查持久卷"]
    if completed.returncode:
        return errors + [f"docker compose config 失败：{completed.stderr.strip()}"]
    config = json.loads(completed.stdout)
    services = config.get("services", {})
    volumes = config.get("volumes", {})
    if not {"pgdata", "backend_storage"}.issubset(volumes):
        errors.append("Compose 必须声明 pgdata 与 backend_storage 命名卷")
    migration = services.get("migration", {})
    if migration.get("command") != ["alembic", "upgrade", "head"]:
        errors.append("migration 命令必须是 alembic upgrade head")
    backend_mounts = services.get("backend", {}).get("volumes", [])
    if not any(
        mount.get("source") == "backend_storage" and mount.get("target") == "/app/data"
        for mount in backend_mounts
    ):
        errors.append("backend_storage 必须挂载到 /app/data")
    return errors


def validate_https(root: Path) -> list[str]:
    """返回 HTTPS 与生产反向代理缺口。"""
    errors: list[str] = []
    template = _read(root / "deploy" / "nginx" / "https.conf.template", errors)
    entrypoint = _read(root / "deploy" / "nginx" / "40-configure-site.sh", errors)
    production = _read(root / "compose.production.yaml", errors)
    _require(
        template,
        (
            "return 301 https://$host$request_uri",
            "ssl_protocols TLSv1.2 TLSv1.3",
            "ssl_certificate /etc/letsencrypt/live/${SERVER_NAME}/fullchain.pem",
            "ssl_certificate_key /etc/letsencrypt/live/${SERVER_NAME}/privkey.pem",
            "Strict-Transport-Security",
            'X-Content-Type-Options "nosniff"',
            "location /api/",
            "proxy_pass ${BACKEND_UPSTREAM}",
            "try_files $uri $uri/ /index.html",
        ),
        "deploy/nginx/https.conf.template",
        errors,
    )
    _require(
        entrypoint,
        (
            "[ -s \"$certificate\" ] && [ -s \"$private_key\" ]",
            'elif [ "${REQUIRE_TLS:-0}" = "1" ]',
            "https.conf.template",
        ),
        "Nginx 启动脚本",
        errors,
    )
    _require(
        production,
        (
            "ports: !reset []",
            "ports: !override",
            "TLS_CERT_DIR",
            "DASHSCOPE_SECRET_DIR",
            "REQUIRE_TLS",
            "read_only: true",
            '"${HTTPS_PORT:-443}:443"',
        ),
        "compose.production.yaml",
        errors,
    )
    environment = os.environ.copy()
    environment.update(
        {
            "SERVER_NAME": "keywords.example.test",
            "TLS_CERT_DIR": str(root),
            "DASHSCOPE_SECRET_DIR": str(root),
        }
    )
    completed = subprocess.run(
        [
            "docker",
            "compose",
            "-f",
            "compose.yaml",
            "-f",
            "compose.production.yaml",
            "config",
            "--format",
            "json",
        ],
        cwd=root,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    if completed.returncode:
        errors.append(f"生产 Compose 解析失败：{completed.stderr.strip()}")
        return errors
    config = json.loads(completed.stdout)
    services = config.get("services", {})
    if services.get("postgres", {}).get("ports"):
        errors.append("生产 Compose 不得发布 PostgreSQL 端口")
    published = {str(port.get("published")) for port in services.get("frontend", {}).get("ports", [])}
    if published != {"80", "443"}:
        errors.append(f"生产 frontend 必须发布 80/443，实际为 {sorted(published)}")
    backend = services.get("backend", {})
    backend_environment = backend.get("environment", {})
    if backend_environment.get("DASHSCOPE_API_KEY_FILE") != "/run/secrets/dashscope/api-key":
        errors.append("生产 backend 必须通过只读密钥文件配置 DashScope")
    if not any(
        mount.get("target") == "/run/secrets/dashscope" and mount.get("read_only") is True
        for mount in backend.get("volumes", [])
    ):
        errors.append("生产 backend 必须只读挂载 DashScope 密钥目录")
    return errors


def validate_server_assets(root: Path) -> list[str]:
    """Validate OZON-host tunnel and Windows Agent assets without reading secrets."""
    errors: list[str] = []
    checks = {
        "compose.ozon-server.yaml": (
            "keyword-agent-host:${KEYWORD_AGENT_GATEWAY_IP",
            "http://keyword-agent-host:17100",
            "KEYWORD_BROWSER_AGENT_TOKEN",
        ),
        "deploy/ozon-server/keyword-agent-sshd.conf.template": (
            "AllowTcpForwarding remote",
            "PermitListen __GATEWAY__:17100",
            "PermitTTY no",
            "ForceCommand /usr/local/libexec/keyword-agent-tunnel-command",
        ),
        "deploy/ozon-server/keyword-agent-command.sh.template": (
            "check-network __NETWORK_ID__",
            "/sys/class/net/__BRIDGE__",
        ),
        "deploy/ozon-server/keyword-agent-firewall.service.template": (
            "-s __SUBNET__",
            "--dport 17100",
        ),
        "deploy/ozon-server/keyword-library.nginx.conf": (
            "auth_basic",
            "keyword-library.htpasswd",
        ),
        "deploy/ozon-server/keyword-library-certbot-renew.service": (
            "certbot",
            "nginx -t",
            "reload nginx",
        ),
        "deploy/windows-agent/install.ps1": (
            "SupportsShouldProcess",
            "KeywordLibrary-Ziniao-Agent",
            "Register-ScheduledTask",
        ),
        "deploy/windows-agent/run-supervisor.ps1": (
            "127.0.0.1:7100",
            "17100:127.0.0.1:7100",
            "ExitOnForwardFailure=yes",
        ),
        "deploy/windows-agent/KeywordAgent.psm1": (
            "Get-NetTCPConnection",
            "AgentToken.Length -lt 32",
        ),
    }
    for relative, required in checks.items():
        source = _read(root / relative, errors)
        _require(source, required, relative, errors)
    if "host-gateway" in _read(root / "compose.ozon-server.yaml", errors):
        errors.append("compose.ozon-server.yaml 禁止使用默认 host-gateway")
    completed = subprocess.run(
        [
            "docker",
            "compose",
            "-p",
            "keyword-library",
            "-f",
            "compose.yaml",
            "-f",
            "compose.ozon-server.yaml",
            "--env-file",
            ".env.production.example",
            "config",
            "--format",
            "json",
        ],
        cwd=root,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    if completed.returncode:
        errors.append(f"服务器 Compose 解析失败：{completed.stderr.strip()}")
    else:
        config = json.loads(completed.stdout)
        backend = config.get("services", {}).get("backend", {})
        environment = backend.get("environment", {})
        if environment.get("KEYWORD_BROWSER_AGENT_URL") != "http://keyword-agent-host:17100":
            errors.append("服务器 backend 必须通过 keyword-agent-host:17100 访问 Agent")
        if backend.get("ports"):
            errors.append("服务器 backend 不得发布宿主机端口")
        if "61001" not in {str(group) for group in backend.get("group_add", [])}:
            errors.append("服务器 backend 缺少 DashScope 密钥补充组 61001")
        extra_hosts = backend.get("extra_hosts", [])
        if not any(
            str(item).startswith("keyword-agent-host=")
            for item in (
                extra_hosts.keys() if isinstance(extra_hosts, dict) else extra_hosts
            )
        ):
            errors.append("服务器 backend 缺少显式 keyword-agent-host Gateway 映射")
    return errors


def _agent_host_is_private(url: str) -> bool:
    hostname = (urlsplit(url).hostname or "").lower()
    if hostname in {
        "localhost",
        "host.docker.internal",
        "keyword-agent-host",
    } or hostname.endswith(".ts.net"):
        return True
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        return False
    return not address.is_global


def validate_environment(env_file: Path, *, template_mode: bool = False) -> list[str]:
    """返回生产环境变量中的安全缺口。"""
    root = Path(__file__).resolve().parents[1]
    if not env_file.is_file():
        return [f"环境文件不存在：{env_file}"]
    completed = subprocess.run(
        [
            "docker",
            "compose",
            "-f",
            "compose.yaml",
            "-f",
            "compose.production.yaml",
            "--env-file",
            str(env_file.resolve()),
            "config",
            "--format",
            "json",
        ],
        cwd=root,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    if completed.returncode:
        return [f"生产环境无法渲染 Compose：{completed.stderr.strip()}"]
    config = json.loads(completed.stdout)
    services = config.get("services", {})
    postgres = services.get("postgres", {})
    backend = services.get("backend", {})
    frontend = services.get("frontend", {})
    postgres_environment = postgres.get("environment", {})
    backend_environment = backend.get("environment", {})
    frontend_environment = frontend.get("environment", {})
    tls_mount = next(
        (mount for mount in frontend.get("volumes", []) if mount.get("target") == "/etc/letsencrypt"),
        {},
    )
    model_mount = next(
        (
            mount
            for mount in backend.get("volumes", [])
            if mount.get("target") == "/run/secrets/dashscope"
        ),
        {},
    )
    values = {
        "POSTGRES_USER": str(postgres_environment.get("POSTGRES_USER", "")),
        "POSTGRES_PASSWORD": str(postgres_environment.get("POSTGRES_PASSWORD", "")),
        "POSTGRES_DB": str(postgres_environment.get("POSTGRES_DB", "")),
        "SERVER_NAME": str(frontend_environment.get("SERVER_NAME", "")),
        "TLS_CERT_DIR": str(tls_mount.get("source", "")),
        "KEYWORD_BROWSER_AGENT_URL": str(
            backend_environment.get("KEYWORD_BROWSER_AGENT_URL", "")
        ),
        "KEYWORD_BROWSER_AGENT_TOKEN": str(
            backend_environment.get("KEYWORD_BROWSER_AGENT_TOKEN", "")
        ),
        "DASHSCOPE_SECRET_DIR": str(model_mount.get("source", "")),
    }
    errors: list[str] = []
    required = {
        "POSTGRES_USER",
        "POSTGRES_PASSWORD",
        "POSTGRES_DB",
        "SERVER_NAME",
        "TLS_CERT_DIR",
        "KEYWORD_BROWSER_AGENT_URL",
        "KEYWORD_BROWSER_AGENT_TOKEN",
        "DASHSCOPE_SECRET_DIR",
    }
    missing = sorted(key for key in required if not values.get(key))
    if missing:
        errors.append(f"缺少生产环境变量：{', '.join(missing)}")
    if template_mode:
        return errors

    placeholders = sorted(key for key, value in values.items() if "CHANGE_ME" in value)
    if placeholders:
        errors.append(f"仍包含 CHANGE_ME 占位值：{', '.join(placeholders)}")

    password = values.get("POSTGRES_PASSWORD", "")
    if password.lower() in {"keyword", "postgres", "password", "changeme"}:
        errors.append("不得使用默认数据库密码")
    if password and len(password) < 16:
        errors.append("数据库密码至少 16 个字符")
    token = values.get("KEYWORD_BROWSER_AGENT_TOKEN", "")
    if token and len(token) < 32:
        errors.append("浏览器 Agent Token 至少 32 个字符")
    agent_url = values.get("KEYWORD_BROWSER_AGENT_URL", "")
    if agent_url and not _agent_host_is_private(agent_url):
        errors.append("禁止使用公网 Agent 地址；请使用 VPN/隧道私网地址")
    certificate_directory = (
        Path(values.get("TLS_CERT_DIR", "")) / "live" / values.get("SERVER_NAME", "")
    )
    if not (certificate_directory / "fullchain.pem").is_file():
        errors.append("缺少 TLS 证书 fullchain.pem")
    if not (certificate_directory / "privkey.pem").is_file():
        errors.append("缺少 TLS 证书私钥 privkey.pem")
    # The OZON-host overlay deliberately preserves the existing DashScope CSV
    # export filename, while the generic production overlay uses a plain key.
    model_key_name = (
        "api-key.csv"
        if agent_url == "http://keyword-agent-host:17100"
        else "api-key"
    )
    model_key = Path(values.get("DASHSCOPE_SECRET_DIR", "")) / model_key_name
    if not model_key.is_file():
        errors.append(f"缺少 DashScope 密钥文件 {model_key_name}")
    return errors


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--section",
        choices=("frontend", "compose", "persistence", "https", "server", "environment", "all"),
        default="frontend",
    )
    parser.add_argument("--env-file", type=Path)
    parser.add_argument("--template-mode", action="store_true")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    validators = {
        "frontend": validate_frontend,
        "compose": validate_compose,
        "persistence": validate_persistence,
        "https": validate_https,
        "server": validate_server_assets,
    }
    if args.section == "environment":
        if args.env_file is None:
            parser.error("--section environment 需要 --env-file")
        errors = validate_environment(args.env_file, template_mode=args.template_mode)
    elif args.section == "all":
        errors = []
        for validator in validators.values():
            errors.extend(validator(root))
        if args.env_file is None:
            errors.append("--section all 需要 --env-file")
        else:
            errors.extend(validate_environment(args.env_file, template_mode=args.template_mode))
    else:
        errors = validators[args.section](root)
    if errors:
        for error in errors:
            print(f"ERROR: {error}")
        return 1
    print(f"OK: {args.section}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
