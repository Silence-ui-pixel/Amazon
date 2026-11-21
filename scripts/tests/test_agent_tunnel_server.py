from __future__ import annotations

import importlib.util
from pathlib import Path
from unittest import TestCase

ROOT = Path(__file__).resolve().parents[2]
PREFLIGHT = ROOT / "deploy" / "ozon-server" / "preflight.py"
SPEC = importlib.util.spec_from_file_location("agent_tunnel_preflight", PREFLIGHT)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError("cannot load server preflight")
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class AgentTunnelServerTests(TestCase):
    def test_accepts_only_project_gateway_listener(self) -> None:
        self.assertEqual(MODULE.agent_tunnel_conflicts("172.28.0.1", {"172.28.0.1:17100"}, True), [])
        self.assertTrue(MODULE.agent_tunnel_conflicts("172.28.0.1", {"0.0.0.0:17100"}, True))
        self.assertTrue(MODULE.agent_tunnel_conflicts("172.28.0.1", {"172.29.0.1:17100"}, True))
        self.assertTrue(MODULE.agent_tunnel_conflicts("172.28.0.1", set(), False))

    def test_compose_uses_explicit_gateway_not_host_gateway(self) -> None:
        compose = (ROOT / "compose.ozon-server.yaml").read_text(encoding="utf-8")
        self.assertIn("keyword-agent-host:${KEYWORD_AGENT_GATEWAY_IP", compose)
        self.assertIn("http://keyword-agent-host:17100", compose)
        self.assertIn('"${DASHSCOPE_SECRET_GID:-61001}"', compose)
        self.assertNotIn("host-gateway", compose)

    def test_sshd_and_firewall_templates_are_narrow(self) -> None:
        sshd = (ROOT / "deploy/ozon-server/keyword-agent-sshd.conf.template").read_text(encoding="utf-8")
        firewall = (ROOT / "deploy/ozon-server/keyword-agent-firewall.service.template").read_text(encoding="utf-8")
        for clause in ["AllowTcpForwarding remote", "GatewayPorts clientspecified", "PermitListen __GATEWAY__:17100", "PermitTTY no", "ForceCommand /usr/local/libexec/keyword-agent-tunnel-command"]:
            self.assertIn(clause, sshd)
        command = (ROOT / "deploy/ozon-server/keyword-agent-command.sh.template").read_text(encoding="utf-8")
        self.assertIn("check-network __NETWORK_ID__", command)
        self.assertIn("/sys/class/net/__BRIDGE__", command)
        self.assertIn("-s __SUBNET__", firewall)
        self.assertIn("--dport 17100", firewall)
        self.assertIn("KEYWORD_AGENT_17100", firewall)
        self.assertIn("-I INPUT 1", firewall)
        self.assertNotIn("-A INPUT -p tcp --dport 17100", firewall)

    def test_installer_validates_identity_candidate_and_rolls_back(self) -> None:
        source = (ROOT / "deploy/ozon-server/install-agent-tunnel.sh").read_text(
            encoding="utf-8"
        )
        for clause in (
            "authorized_keys",
            "ssh-keygen -lf",
            "useradd",
            "sshd -t",
            "sshd -T -C",
            "trap rollback",
            "systemctl restart keyword-library-agent-firewall.service",
            "KEYWORD_AGENT_NETWORK_ID",
        ):
            self.assertIn(clause, source)
