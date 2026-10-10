from unittest.mock import MagicMock
import pytest

from faults.docker_client import AsyncDockerClient, NotFound


@pytest.fixture
def mock_docker_client():
    client = MagicMock()
    return client


@pytest.mark.asyncio
async def test_no_docker_calls_on_init():
    client = AsyncDockerClient()
    assert client._client is None


@pytest.mark.asyncio
async def test_resolve_container_by_compose_label(mock_docker_client):
    mock_container = MagicMock()
    mock_container.name = "robot-shop_cart_1"
    mock_docker_client.containers.list.return_value = [mock_container]

    async_client = AsyncDockerClient(client=mock_docker_client)
    resolved = await async_client.resolve_container("cart")

    assert resolved == "robot-shop_cart_1"
    mock_docker_client.containers.list.assert_called_once_with(
        all=True,
        filters={
            "label": [
                "com.docker.compose.service=cart",
                "com.docker.compose.project=robotshop",
                "com.chaospilot.role=target"
            ]
        }
    )


@pytest.mark.asyncio
async def test_resolve_container_not_found(mock_docker_client):
    mock_docker_client.containers.list.return_value = []
    mock_docker_client.containers.get.side_effect = NotFound("Container not found")

    async_client = AsyncDockerClient(client=mock_docker_client)
    with pytest.raises(NotFound):
        await async_client.resolve_container("nonexistent")


@pytest.mark.asyncio
async def test_stop_container(mock_docker_client):
    mock_container = MagicMock()
    mock_docker_client.containers.get.return_value = mock_container

    async_client = AsyncDockerClient(client=mock_docker_client)
    await async_client.stop_container("cart", timeout=5)

    mock_docker_client.containers.get.assert_called_once_with("cart")
    mock_container.stop.assert_called_once_with(timeout=5)


@pytest.mark.asyncio
async def test_start_container(mock_docker_client):
    mock_container = MagicMock()
    mock_docker_client.containers.get.return_value = mock_container

    async_client = AsyncDockerClient(client=mock_docker_client)
    await async_client.start_container("cart")

    mock_docker_client.containers.get.assert_called_once_with("cart")
    mock_container.start.assert_called_once()


@pytest.mark.asyncio
async def test_get_container_status(mock_docker_client):
    mock_container = MagicMock()
    mock_container.status = "running"
    mock_docker_client.containers.get.return_value = mock_container

    async_client = AsyncDockerClient(client=mock_docker_client)
    status = await async_client.get_container_status("cart")

    assert status == "running"
    mock_container.reload.assert_called_once()


@pytest.mark.asyncio
async def test_exec_command(mock_docker_client):
    mock_container = MagicMock()
    mock_container.exec_run.return_value = (0, b"total 128M\n")
    mock_docker_client.containers.get.return_value = mock_container

    async_client = AsyncDockerClient(client=mock_docker_client)
    exit_code, output = await async_client.exec_command("cart", "ls -l")

    assert exit_code == 0
    assert output == "total 128M"
    mock_container.exec_run.assert_called_once_with("ls -l")
