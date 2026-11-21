"""Reject OZON-server deployment collisions before Docker mutates anything."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from collections.abc import Iterable, Mapping
from pathlib import Path


PROJECT_NAME = "keyword-library"
NAMED_RESOURCES = (
    ("volume", "keyword-library_pgdata"),
    ("volume", "keyword-library_backend_storage"),
    ("network", "keyword-library_default"),
)


def agent_tunnel_conflicts(
    gateway: str, listeners: set[str], owned_key: bool
) -> list[str]:
    """Validate the tunnel listener without changing sshd, firewall, or Docker."""
    expected = f"{gateway}:17100"
    conflicts: list[str] = []
    if not gateway or gateway in {"0.0.0.0", "127.0.0.1"}:
        conflicts.append("关键词库 Docker Gateway 无效")
    if listeners and listeners != {expected}:
        conflicts.append("17100 未仅绑定关键词库 Docker Gateway")
    if not owned_key:
        conflicts.append("SSH key marker 不属于关键词库 Agent")
    return conflicts


def _normal_path(value: str) -> str:
    return os.path.normcase(os.path.realpath(value))


def find_conflicts(
    *,
    project_dir: str,
    listeners: set[int],
    published_containers: Mapping[int, set[str]],
    project_containers: set[str],
    project_workdirs: Mapping[str, str],
    named_resource_owners: Mapping[str, str],
) -> list[str]:
    """Return collisions; existing resources are allowed only when owned by this project."""

    conflicts: list[str] = []
    expected_dir = _normal_path(project_dir)

    for container_id in sorted(project_containers):
        actual_dir = project_workdirs.get(container_id, "")
        if not actual_dir or _normal_path(actual_dir) != expected_dir:
            conflicts.append(
                f"Compose 项目 {PROJECT_NAME} 的容器 {container_id} 来自其他工作目录"
            )

    for port in sorted(listeners):
        publishers = published_containers.get(port, set())
        if not publishers or not publishers.issubset(project_containers):
            conflicts.append(f"端口 {port} 已被非 {PROJECT_NAME} 项目占用")

    for resource, owner in named_resource_owners.items():
        if owner != PROJECT_NAME:
            conflicts.append(f"同名资源 {resource} 不属于 {PROJECT_NAME} 项目")

    return conflicts


def _run(arguments: Iterable[str]) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            list(arguments),
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as exc:
        raise RuntimeError(f"无法执行命令：{arguments}") from exc


def _lines(result: subprocess.CompletedProcess[str]) -> set[str]:
    return {line.strip() for line in result.stdout.splitlines() if line.strip()}


def _require_success(result: subprocess.CompletedProcess[str], command: str) -> None:
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "unknown error"
        raise RuntimeError(f"{command} 失败：{detail}")


def _resource_owner(kind: str, name: str) -> str | None:
    result = _run(
        (
            "docker",
            kind,
            "inspect",
            name,
            "--format",
            '{{ index .Labels "com.docker.compose.project" }}',
        )
    )
    if result.returncode != 0:
        return None
    return result.stdout.strip() or "<unmanaged>"


def collect_snapshot(
    *, project_dir: str, ports: set[int]
) -> tuple[set[int], dict[int, set[str]], set[str], dict[str, str], dict[str, str]]:
    project_result = _run(
        (
            "docker",
            "ps",
            "-aq",
            "--filter",
            f"label=com.docker.compose.project={PROJECT_NAME}",
        )
    )
    _require_success(project_result, "docker ps")
    project_containers = _lines(project_result)

    project_workdirs: dict[str, str] = {}
    for container_id in project_containers:
        result = _run(
            (
                "docker",
                "inspect",
                container_id,
                "--format",
                '{{ index .Config.Labels "com.docker.compose.project.working_dir" }}',
            )
        )
        _require_success(result, f"docker inspect {container_id}")
        project_workdirs[container_id] = result.stdout.strip()

    listeners: set[int] = set()
    published_containers: dict[int, set[str]] = {}
    for port in ports:
        listener_result = _run(("ss", "-H", "-ltn", f"sport = :{port}"))
        _require_success(listener_result, f"ss port {port}")
        if listener_result.stdout.strip():
            listeners.add(port)
            published_result = _run(
                ("docker", "ps", "-q", "--filter", f"publish={port}")
            )
            _require_success(published_result, f"docker ps publish={port}")
            published_containers[port] = _lines(published_result)

    owners: dict[str, str] = {}
    for kind, name in NAMED_RESOURCES:
        owner = _resource_owner(kind, name)
        if owner is not None:
            owners[f"{kind}/{name}"] = owner

    return listeners, published_containers, project_containers, project_workdirs, owners


def _read_ports(env_file: Path) -> set[int]:
    values: dict[str, str] = {}
    for raw_line in env_file.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")

    ports: set[int] = set()
    for key, default in (("POSTGRES_PORT", "15433"), ("HTTP_PORT", "18080")):
        try:
            port = int(values.get(key, default))
        except ValueError as exc:
            raise RuntimeError(f"{key} 不是有效端口") from exc
        if not 1 <= port <= 65535:
            raise RuntimeError(f"{key} 超出有效端口范围")
        ports.add(port)
    return ports


def collect_agent_tunnel_conflicts() -> list[str]:
    marker = Path("/etc/keyword-library/agent-tunnel.env")
    listener_result = _run(("ss", "-H", "-ltn", "sport = :17100"))
    _require_success(listener_result, "ss port 17100")
    listeners = {
        fields[3]
        for line in listener_result.stdout.splitlines()
        if len(fields := line.split()) >= 4
    }
    if not listeners and not marker.is_file():
        return []
    network = _run(
        (
            "docker",
            "network",
            "inspect",
            "keyword-library_default",
            "--format",
            "{{(index .IPAM.Config 0).Gateway}}|{{.Id}}",
        )
    )
    _require_success(network, "docker network inspect keyword-library_default")
    gateway, network_id = network.stdout.strip().split("|", 1)
    values: dict[str, str] = {}
    if marker.is_file():
        for line in marker.read_text(encoding="utf-8").splitlines():
            if "=" in line:
                key, value = line.split("=", 1)
                values[key] = value
    owned = (
        values.get("KEYWORD_AGENT_GATEWAY_IP") == gateway
        and values.get("KEYWORD_AGENT_NETWORK_ID") == network_id
        and bool(values.get("KEYWORD_AGENT_KEY_FINGERPRINT"))
    )
    return agent_tunnel_conflicts(gateway, listeners, owned)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project-dir", required=True)
    parser.add_argument("--env-file", type=Path, required=True)
    arguments = parser.parse_args()

    try:
        ports = _read_ports(arguments.env_file)
        snapshot = collect_snapshot(project_dir=arguments.project_dir, ports=ports)
        conflicts = find_conflicts(
            project_dir=arguments.project_dir,
            listeners=snapshot[0],
            published_containers=snapshot[1],
            project_containers=snapshot[2],
            project_workdirs=snapshot[3],
            named_resource_owners=snapshot[4],
        )
        conflicts.extend(collect_agent_tunnel_conflicts())
    except (OSError, RuntimeError) as exc:
        print(f"部署预检失败：{exc}", file=sys.stderr)
        return 2

    if conflicts:
        for conflict in conflicts:
            print(f"部署预检失败：{conflict}", file=sys.stderr)
        return 1

    print(f"部署预检通过：端口 {', '.join(map(str, sorted(ports)))} 和命名资源归属安全")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
