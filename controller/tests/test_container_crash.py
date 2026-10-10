from unittest.mock import AsyncMock
import pytest

from faults.container_crash import ContainerCrashFault
from faults.docker_client import AsyncDockerClient


@pytest.fixture
def mock_docker_client():
    client = AsyncMock(spec=AsyncDockerClient)
    client.resolve_container = AsyncMock(return_value="robot-shop_cart_1")
    client.stop_container = AsyncMock()
    client.start_container = AsyncMock()
    client.get_container_status = AsyncMock(return_value="running")
    return client


@pytest.mark.asyncio
async def test_container_crash_inject_default_timeout(mock_docker_client):
    fault = ContainerCrashFault(docker_client=mock_docker_client)
    res = await fault.inject("cart")

    mock_docker_client.resolve_container.assert_awaited_once_with("cart")
    mock_docker_client.stop_container.assert_awaited_once_with("robot-shop_cart_1", timeout=10)

    assert res["fault_type"] == "container_crash"
    assert res["target"] == "cart"
    assert res["container"] == "robot-shop_cart_1"
    assert res["action"] == "stopped"
    assert res["rollback_command"] == "docker start robot-shop_cart_1"


@pytest.mark.asyncio
async def test_container_crash_inject_custom_timeout(mock_docker_client):
    fault = ContainerCrashFault(docker_client=mock_docker_client)
    res = await fault.inject("cart", params={"timeout": 3})

    mock_docker_client.stop_container.assert_awaited_once_with("robot-shop_cart_1", timeout=3)
    assert res["action"] == "stopped"


@pytest.mark.asyncio
async def test_container_crash_rollback(mock_docker_client):
    fault = ContainerCrashFault(docker_client=mock_docker_client)
    res = await fault.rollback("cart")

    mock_docker_client.resolve_container.assert_awaited_once_with("cart")
    mock_docker_client.start_container.assert_awaited_once_with("robot-shop_cart_1")

    assert res["fault_type"] == "container_crash"
    assert res["target"] == "cart"
    assert res["container"] == "robot-shop_cart_1"
    assert res["action"] == "started"


@pytest.mark.asyncio
async def test_container_crash_verify_rollback_success(mock_docker_client):
    mock_docker_client.get_container_status.return_value = "running"
    fault = ContainerCrashFault(docker_client=mock_docker_client)

    verified = await fault.verify_rollback("cart")
    assert verified is True
    mock_docker_client.resolve_container.assert_awaited_once_with("cart")
    mock_docker_client.get_container_status.assert_awaited_once_with("robot-shop_cart_1")


@pytest.mark.asyncio
async def test_container_crash_verify_rollback_failure(mock_docker_client):
    mock_docker_client.get_container_status.return_value = "exited"
    fault = ContainerCrashFault(docker_client=mock_docker_client)

    verified = await fault.verify_rollback("cart")
    assert verified is False


@pytest.mark.asyncio
async def test_container_crash_verify_rollback_exception(mock_docker_client):
    mock_docker_client.get_container_status.side_effect = RuntimeError("Docker API error")
    fault = ContainerCrashFault(docker_client=mock_docker_client)

    verified = await fault.verify_rollback("cart")
    assert verified is False
