from typing import Any, Dict, Optional
from unittest.mock import MagicMock
import pytest

from faults.base import BaseFault
from faults.docker_client import AsyncDockerClient


class DummyFault(BaseFault):
    """Concrete implementation of BaseFault for contract testing."""

    fault_type = "dummy_fault"

    def __init__(self, docker_client: Optional[AsyncDockerClient] = None, should_verify: bool = True) -> None:
        super().__init__(docker_client)
        self.should_verify = should_verify
        self.injected = False
        self.rolled_back = False

    async def inject(self, target: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        self.injected = True
        return {"action": "injected", "target": target, "params": params or {}}

    async def rollback(self, target: str) -> Dict[str, Any]:
        self.rolled_back = True
        return {"action": "rolled_back", "target": target}

    async def verify_rollback(self, target: str) -> bool:
        return self.should_verify and self.rolled_back


def test_base_fault_cannot_be_instantiated_directly():
    with pytest.raises(TypeError) as excinfo:
        BaseFault()  # type: ignore[abstract]
    assert "Can't instantiate abstract class BaseFault" in str(excinfo.value)


@pytest.mark.asyncio
async def test_dummy_fault_lifecycle():
    mock_docker = MagicMock(spec=AsyncDockerClient)
    fault = DummyFault(docker_client=mock_docker)

    assert fault.fault_type == "dummy_fault"
    assert fault.docker_client is mock_docker
    assert not fault.injected
    assert not fault.rolled_back

    # 1. Test inject
    inject_res = await fault.inject("cart", {"intensity": "high"})
    assert inject_res == {"action": "injected", "target": "cart", "params": {"intensity": "high"}}
    assert fault.injected is True

    # 2. Test rollback
    rollback_res = await fault.rollback("cart")
    assert rollback_res == {"action": "rolled_back", "target": "cart"}
    assert fault.rolled_back is True

    # 3. Test verify_rollback
    verified = await fault.verify_rollback("cart")
    assert verified is True


@pytest.mark.asyncio
async def test_dummy_fault_verify_failure():
    fault = DummyFault(should_verify=False)
    await fault.inject("ratings")
    await fault.rollback("ratings")

    verified = await fault.verify_rollback("ratings")
    assert verified is False
