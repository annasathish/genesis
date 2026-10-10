from typing import Any, Dict, Optional, Set

from .base import BaseFault
from .docker_client import AsyncDockerClient


class RabbitMQFault(BaseFault):
    """Injects message-queue block into RabbitMQ by pausing the AMQP broker application."""

    fault_type: str = "rabbitmq_block"

    APPROVED_TARGETS: Set[str] = {"rabbitmq"}

    def __init__(self, docker_client: Optional[AsyncDockerClient] = None) -> None:
        super().__init__(docker_client=docker_client or AsyncDockerClient())

    def _validate_target(self, target: str) -> None:
        if target not in self.APPROVED_TARGETS:
            raise ValueError(
                f"Unsupported target '{target}' for RabbitMQ fault. "
                f"Supported targets: {list(self.APPROVED_TARGETS)}"
            )

    async def inject(
        self, target: str, params: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        assert self.docker_client is not None
        self._validate_target(target)

        container = await self.docker_client.resolve_container(target)
        status = await self.docker_client.get_container_status(container)
        if status != "running":
            raise RuntimeError(
                f"Cannot inject RabbitMQ fault: container '{container}' is not running (status: '{status}')"
            )

        exit_code, output = await self.docker_client.exec_command(
            container, ["rabbitmqctl", "stop_app"]
        )
        if exit_code != 0:
            raise RuntimeError(
                f"rabbitmqctl stop_app failed inside '{container}' with exit code {exit_code}: {output}"
            )

        return {
            "fault_type": self.fault_type,
            "target": target,
            "container": container,
            "action": "stopped_app",
            "rollback_command": f"docker exec {container} rabbitmqctl start_app",
        }

    async def rollback(self, target: str) -> Dict[str, Any]:
        assert self.docker_client is not None
        self._validate_target(target)

        container = await self.docker_client.resolve_container(target)
        exit_code, output = await self.docker_client.exec_command(
            container, ["rabbitmqctl", "start_app"]
        )
        if exit_code != 0:
            raise RuntimeError(
                f"rabbitmqctl start_app failed inside '{container}' with exit code {exit_code}: {output}"
            )

        return {
            "fault_type": self.fault_type,
            "target": target,
            "container": container,
            "action": "started_app",
        }

    async def verify_rollback(self, target: str) -> bool:
        assert self.docker_client is not None
        try:
            self._validate_target(target)
            container = await self.docker_client.resolve_container(target)
            exit_code, _ = await self.docker_client.exec_command(
                container, ["rabbitmq-diagnostics", "check_running"]
            )
            return exit_code == 0
        except Exception:
            return False
