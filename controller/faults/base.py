from abc import ABC, abstractmethod
from typing import Any, Dict, Optional

from .docker_client import AsyncDockerClient


class BaseFault(ABC):
    """Abstract base class for chaos fault injectors."""

    fault_type: str = "base"

    def __init__(self, docker_client: Optional[AsyncDockerClient] = None) -> None:
        self.docker_client = docker_client

    @abstractmethod
    async def inject(
        self, target: str, params: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """Inject the fault into the target service or container.

        Args:
            target: Service name or container identifier.
            params: Optional fault parameters (e.g. latency_ms, fill_mb).

        Returns:
            Dict containing details of the injection action.
        """
        raise NotImplementedError

    @abstractmethod
    async def rollback(self, target: str) -> Dict[str, Any]:
        """Revert the injected fault to restore steady state.

        Args:
            target: Service name or container identifier.

        Returns:
            Dict containing details of the rollback action.
        """
        raise NotImplementedError

    @abstractmethod
    async def verify_rollback(self, target: str) -> bool:
        """Verify that the rollback succeeded and the target is healthy.

        Args:
            target: Service name or container identifier.

        Returns:
            True if verification succeeded, False otherwise.
        """
        raise NotImplementedError
