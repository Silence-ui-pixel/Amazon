import shutil
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

from scripts.validate_deployment import (
    _agent_host_is_private,
    validate_compose,
    validate_environment,
    validate_frontend,
    validate_https,
    validate_persistence,
    validate_server_assets,
)

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


class FrontendDeploymentTests(TestCase):
    """防止前端镜像退化为开发服务器或丢失同源代理。"""

    def test_frontend_production_artifacts_are_complete(self) -> None:
        self.assertEqual(validate_frontend(REPOSITORY_ROOT), [])


class ComposeDeploymentTests(TestCase):
    """防止服务依赖退化为仅启动数据库或把内部端口发布到公网。"""

    def test_compose_orchestrates_all_services_in_order(self) -> None:
        self.assertEqual(validate_compose(REPOSITORY_ROOT), [])


class ServerAgentAssetsTests(TestCase):
    def test_tunnel_and_windows_assets_are_complete(self) -> None:
        self.assertEqual(validate_server_assets(REPOSITORY_ROOT), [])

    def test_keyword_agent_alias_is_private(self) -> None:
        self.assertTrue(_agent_host_is_private("http://keyword-agent-host:17100"))

    def test_server_overlay_rejects_backend_without_dashscope_secret_group(self) -> None:
        """删除补充组后，即使 Compose 可解析也必须阻止部署。"""
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            for relative in (
                "compose.yaml",
                "compose.ozon-server.yaml",
                ".env.production.example",
            ):
                shutil.copy2(REPOSITORY_ROOT / relative, root / relative)
            shutil.copytree(REPOSITORY_ROOT / "deploy", root / "deploy")

            overlay = root / "compose.ozon-server.yaml"
            source = overlay.read_text(encoding="utf-8")
            source = source.replace(
                '    group_add:\n      - "${DASHSCOPE_SECRET_GID:-61001}"\n',
                "",
                1,
            )
            overlay.write_text(source, encoding="utf-8")

            self.assertIn(
                "DashScope 密钥补充组",
                "\n".join(validate_server_assets(root)),
            )


class PersistenceDeploymentTests(TestCase):
    """防止镜像漏带迁移或容器重建后丢失业务文件。"""

    def test_backend_image_and_volumes_preserve_data_and_migrate(self) -> None:
        self.assertEqual(validate_persistence(REPOSITORY_ROOT), [])

    def test_backend_install_check_accepts_build_time_pip_options(self) -> None:
        """服务器构建可增加镜像、重试和超时参数，但仍必须安装当前 app 包。"""
        dockerfile = (REPOSITORY_ROOT / "backend" / "Dockerfile").read_text(encoding="utf-8")
        self.assertIn('pip install --no-cache-dir --retries 10 --timeout "${PIP_DEFAULT_TIMEOUT}"', dockerfile)
        self.assertIn('--index-url "${PIP_INDEX_URL}" .', dockerfile)
        self.assertEqual(validate_persistence(REPOSITORY_ROOT), [])


class HttpsDeploymentTests(TestCase):
    """防止生产入口降级为明文或丢失 SPA/API 代理规则。"""

    def test_https_proxy_and_production_override_are_complete(self) -> None:
        self.assertEqual(validate_https(REPOSITORY_ROOT), [])


class EnvironmentDeploymentTests(TestCase):
    """生产检查必须在容器启动前拦截弱秘密和裸公网 Agent。"""

    def _validate(
        self,
        content: str,
        *,
        certificate_files: bool = True,
        dashscope_key_name: str = "api-key",
    ) -> list[str]:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            certs = root / "certs"
            certificate_live = certs / "live" / "keywords.example.com"
            certificate_live.mkdir(parents=True)
            if certificate_files:
                (certificate_live / "fullchain.pem").write_text("certificate", encoding="utf-8")
                (certificate_live / "privkey.pem").write_text("private-key", encoding="utf-8")
            (certs / dashscope_key_name).write_text("test-secret", encoding="utf-8")
            env_file = root / ".env.production"
            env_file.write_text(content.replace("<CERT_DIR>", str(certs)), encoding="utf-8")
            return validate_environment(env_file)

    def test_safe_production_environment_passes(self) -> None:
        errors = self._validate(
            "\n".join(
                (
                    "POSTGRES_USER=keyword_prod",
                    "POSTGRES_PASSWORD=correct-horse-battery-staple",
                    "POSTGRES_DB=keyword_library",
                    "SERVER_NAME=keywords.example.com",
                    "TLS_CERT_DIR=<CERT_DIR>",
                    "KEYWORD_BROWSER_AGENT_URL=http://100.64.0.8:7100",
                    "KEYWORD_BROWSER_AGENT_TOKEN=0123456789abcdef0123456789abcdef",
                    "DASHSCOPE_SECRET_DIR=<CERT_DIR>",
                )
            )
        )
        self.assertEqual(errors, [])

    def test_server_overlay_accepts_the_csv_dashscope_key(self) -> None:
        errors = self._validate(
            "\n".join(
                (
                    "POSTGRES_USER=keyword_prod",
                    "POSTGRES_PASSWORD=correct-horse-battery-staple",
                    "POSTGRES_DB=keyword_library",
                    "SERVER_NAME=keywords.example.com",
                    "TLS_CERT_DIR=<CERT_DIR>",
                    "KEYWORD_BROWSER_AGENT_URL=http://keyword-agent-host:17100",
                    "KEYWORD_BROWSER_AGENT_TOKEN=0123456789abcdef0123456789abcdef",
                    "DASHSCOPE_SECRET_DIR=<CERT_DIR>",
                )
            ),
            dashscope_key_name="api-key.csv",
        )
        self.assertEqual(errors, [])

    def test_unsafe_production_values_are_rejected(self) -> None:
        errors = self._validate(
            "\n".join(
                (
                    "POSTGRES_USER=keyword",
                    "POSTGRES_PASSWORD=keyword",
                    "POSTGRES_DB=keyword_library",
                    "SERVER_NAME=example.com",
                    "TLS_CERT_DIR=<CERT_DIR>",
                    "KEYWORD_BROWSER_AGENT_URL=http://8.8.8.8:7100",
                    "KEYWORD_BROWSER_AGENT_TOKEN=short",
                    "DASHSCOPE_SECRET_DIR=<CERT_DIR>",
                )
            ),
            certificate_files=False,
        )
        combined = "\n".join(errors)
        self.assertIn("默认数据库密码", combined)
        self.assertIn("至少 32", combined)
        self.assertIn("公网 Agent", combined)
        self.assertIn("证书", combined)

    def test_change_me_placeholders_are_rejected_outside_template_mode(self) -> None:
        errors = self._validate(
            "\n".join(
                (
                    "POSTGRES_USER=keyword_prod",
                    "POSTGRES_PASSWORD=CHANGE_ME_USE_A_LONG_RANDOM_DATABASE_PASSWORD",
                    "POSTGRES_DB=keyword_library",
                    "SERVER_NAME=keywords.example.com",
                    "TLS_CERT_DIR=<CERT_DIR>",
                    "KEYWORD_BROWSER_AGENT_URL=http://100.64.0.8:7100",
                    "KEYWORD_BROWSER_AGENT_TOKEN=CHANGE_ME_USE_AT_LEAST_32_RANDOM_CHARACTERS",
                    "DASHSCOPE_SECRET_DIR=<CERT_DIR>",
                )
            )
        )
        self.assertIn("CHANGE_ME", "\n".join(errors))

    def test_compose_parsing_handles_inline_comments_and_interpolation(self) -> None:
        errors = self._validate(
            "\n".join(
                (
                    "POSTGRES_USER=keyword_prod",
                    "POSTGRES_PASSWORD=password # production database password",
                    "POSTGRES_DB=keyword_library",
                    "SERVER_NAME=keywords.example.com",
                    'TLS_CERT_DIR="<CERT_DIR>"',
                    "KEYWORD_BROWSER_AGENT_URL=http://100.64.0.8:7100",
                    "KEYWORD_BROWSER_AGENT_TOKEN=123456789012345678901234567890 # token",
                    'DASHSCOPE_SECRET_DIR="<CERT_DIR>"',
                )
            )
        )
        combined = "\n".join(errors)
        self.assertIn("默认数据库密码", combined)
        self.assertIn("至少 32", combined)

    def test_unset_variable_does_not_count_as_a_long_token(self) -> None:
        errors = self._validate(
            "\n".join(
                (
                    "POSTGRES_USER=keyword_prod",
                    "POSTGRES_PASSWORD=correct-horse-battery-staple",
                    "POSTGRES_DB=keyword_library",
                    "SERVER_NAME=keywords.example.com",
                    "TLS_CERT_DIR=<CERT_DIR>",
                    "KEYWORD_BROWSER_AGENT_URL=http://100.64.0.8:7100",
                    "KEYWORD_BROWSER_AGENT_TOKEN=${MISSING_LONG_TOKEN_VARIABLE_NAME}",
                    "DASHSCOPE_SECRET_DIR=<CERT_DIR>",
                )
            )
        )
        self.assertIn("KEYWORD_BROWSER_AGENT_TOKEN", "\n".join(errors))
