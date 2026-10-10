import asyncio
from typing import Any, List, Optional, Tuple, Union

try:
    import docker
    from docker.errors import APIError, DockerException, NotFound
except ImportError:
    docker = None

    class DockerException(Exception):
        pass

    class NotFound(DockerException):
        pass

    class APIError(DockerException):
        pass


class AsyncDockerClient:
    """Minimal async wrapper around the synchronous Python Docker SDK."""

    def __init__(self, client: Optional[Any] = None) -> None:
        """Allow injecting a mock/sync client for unit tests. No Docker calls on init."""
        self._client = client

    def _get_client(self) -> Any:
        """Lazy access to Docker client."""
        if self._client is None:
            if docker is None:
                raise DockerException(
                    "Docker SDK not installed. Please install docker>=7.0.0."
                )
            self._client = docker.from_env()
        return self._client

    async def resolve_container(self, service_name: str) -> str:
        """Resolve a Docker Compose service name (e.g. 'cart') using strict labels."""
        def _resolve() -> str:
            client = self._get_client()
            containers = client.containers.list(
                all=True,
                filters={
                    "label": [
                        f"com.docker.compose.service={service_name}",
                        "com.docker.compose.project=robotshop",
                        "com.chaospilot.role=target"
                    ]
                },
            )
            if containers:
                return containers[0].name
            raise NotFound(f"No container found for service: '{service_name}' in project 'robotshop' with target label")

        return await asyncio.to_thread(_resolve)

    async def stop_container(self, container: str, timeout: int = 10) -> None:
        """Stop container asynchronously."""
        def _stop() -> None:
            c = self._get_client().containers.get(container)
            c.stop(timeout=timeout)

        await asyncio.to_thread(_stop)

    async def start_container(self, container: str) -> None:
        """Start container asynchronously."""
        def _start() -> None:
            c = self._get_client().containers.get(container)
            c.start()

        await asyncio.to_thread(_start)

    async def get_container_status(self, container: str) -> str:
        """Get container status (e.g. 'running', 'exited') asynchronously."""
        def _status() -> str:
            c = self._get_client().containers.get(container)
            c.reload()
            return c.status

        return await asyncio.to_thread(_status)

    async def exec_command(
        self, container: str, command: Union[str, List[str]]
    ) -> Tuple[int, str]:
        """Execute command in container asynchronously, returning (exit_code, output)."""
        def _exec() -> Tuple[int, str]:
            c = self._get_client().containers.get(container)
            exit_code, output = c.exec_run(command)
            output_str = (
                output.decode("utf-8", errors="replace").strip()
                if isinstance(output, bytes)
                else str(output).strip()
            )
            return exit_code, output_str

        return await asyncio.to_thread(_exec)
