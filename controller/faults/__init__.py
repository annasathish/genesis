from .base import BaseFault
from .container_crash import ContainerCrashFault
from .disk_stress import DiskStressFault
from .docker_client import (
    APIError,
    AsyncDockerClient,
    DockerException,
    NotFound,
)
from .network_latency import NetworkLatencyFault
from .rabbitmq_block import RabbitMQFault

__all__ = [
    "AsyncDockerClient",
    "BaseFault",
    "ChaosManager",
    "ContainerCrashFault",
    "DiskStressFault",
    "NetworkLatencyFault",
    "RabbitMQFault",
    "DockerException",
    "NotFound",
    "APIError",
]


def __getattr__(name: str):
    """Lazy import for ChaosManager to defer the sqlalchemy dependency."""
    if name == "ChaosManager":
        from .manager import ChaosManager

        return ChaosManager
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
