from typing import Any, Dict, Optional

from .base import BaseFault
from .docker_client import AsyncDockerClient


class ContainerCrashFault(BaseFault):
    """Fault injector that simulates a container crash by stopping the target container."""

    fault_type: str = "container_crash"

    def __init__(self, docker_client: Optional[AsyncDockerClient] = None) -> None:
        super().__init__(docker_client=docker_client or AsyncDockerClient())

    async def inject(
        self, target: str, params: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """Safely stops the target container.

        Args:
            target: Docker Compose service name or container name (e.g. 'cart', 'ratings').
            params: Optional parameters (e.g. {'timeout': 5}).

        Returns:
            Dict containing injection outcome and the rollback command.
        """
        assert self.docker_client is not None
        params = params or {}
        timeout = int(params.get("timeout", 10))

        container_name = await self.docker_client.resolve_container(target)
        await self.docker_client.stop_container(container_name, timeout=timeout)

        return {
            "fault_type": self.fault_type,
            "target": target,
            "container": container_name,
            "action": "stopped",
            "rollback_command": f"docker start {container_name}",
        }

    async def rollback(self, target: str) -> Dict[str, Any]:
        """Restarts the target container.

        Args:
            target: Docker Compose service name or container name.

        Returns:
            Dict containing rollback outcome.
        """
        assert self.docker_client is not None
        container_name = await self.docker_client.resolve_container(target)
        await self.docker_client.start_container(container_name)

        return {
            "fault_type": self.fault_type,
            "target": target,
            "container": container_name,
            "action": "started",
        }

    async def verify_rollback(self, target: str) -> bool:
        """Verifies that the target container is running after rollback.

        Args:
            target: Docker Compose service name or container name.

        Returns:
            True if container status is 'running', False otherwise.
        """
        assert self.docker_client is not None
        try:
            container_name = await self.docker_client.resolve_container(target)
            status = await self.docker_client.get_container_status(container_name)
            return status == "running"
        except Exception:
            return False
