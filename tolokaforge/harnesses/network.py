"""Default-deny network topology for BYOH agent containers."""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from pathlib import Path

from tolokaforge.core.models import AgentNetworkConfig
from tolokaforge.docker.container import Container
from tolokaforge.docker.image import Image
from tolokaforge.docker.network import Network
from tolokaforge.docker.policy import Capability, ResourcePolicy
from tolokaforge.harnesses.registry import HarnessAdapterSpec


def effective_network_policy(
    configured: AgentNetworkConfig, spec: HarnessAdapterSpec
) -> AgentNetworkConfig:
    """Derive the default harness allowlist unless an operator set a policy."""

    if not configured.model_fields_set:
        entries = list(dict.fromkeys((*spec.provider_hosts, *spec.install_hosts)))
        return AgentNetworkConfig(mode="allowlist", entries=entries)
    return configured


@dataclass
class AgentNetworkRuntime:
    network: Network
    proxy_url: str | None = None
    proxy: Container | None = None
    runner_container: str | None = None
    owned_network: bool = False

    @classmethod
    def create(
        cls,
        *,
        policy: AgentNetworkConfig,
        external_network: Network,
        runner_container: str,
    ) -> AgentNetworkRuntime:
        if policy.mode == "public":
            return cls(network=external_network)

        internal = Network.create(f"tolokaforge-agent-{uuid.uuid4().hex[:10]}", internal=True)
        internal.attach(runner_container, aliases=["runner"])
        runtime = cls(
            network=internal,
            runner_container=runner_container,
            owned_network=True,
        )
        if policy.mode == "no-network":
            return runtime

        repo_root = Path(__file__).resolve().parents[2]
        image = Image.build(
            dockerfile=str(repo_root / "tolokaforge/docker/dockerfiles/agent_proxy.Dockerfile"),
            context=str(repo_root),
            name="tolokaforge-agent-proxy",
        )
        proxy = Container.create(
            image=image,
            name=f"tolokaforge-agent-proxy-{uuid.uuid4().hex[:10]}",
            network=internal,
            environment={"TOLOKAFORGE_PROXY_ALLOWLIST": json.dumps(policy.entries)},
            resources=ResourcePolicy(
                cpu_limit=0.5,
                memory_limit="128m",
                cap_drop=[Capability.ALL],
                no_new_privileges=True,
            ),
        )
        proxy.start()
        external_network.attach(proxy.container_id)
        runtime.proxy = proxy
        runtime.proxy_url = f"http://{proxy.name}:8080"
        return runtime

    def close(self) -> None:
        if self.proxy is not None:
            self.proxy.destroy()
        if self.owned_network:
            if self.runner_container:
                self.network.detach(self.runner_container)
            self.network.destroy()
