from unittest.mock import AsyncMock
import pytest

from faults.docker_client import AsyncDockerClient
from faults.rabbitmq_block import RabbitMQFault


@pytest.fixture
def mock_docker_client():
    client = AsyncMock(spec=AsyncDockerClient)
    client.resolve_container = AsyncMock(return_value="robot-shop_rabbitmq_1")
    client.get_container_status = AsyncMock(return_value="running")
    client.exec_command = AsyncMock()
    return client


@pytest.mark.asyncio
async def test_inject_unsupported_target(mock_docker_client):
    fault = RabbitMQFault(docker_client=mock_docker_client)

    with pytest.raises(ValueError) as exc_info:
        await fault.inject("cart")
    assert "Unsupported target 'cart' for RabbitMQ fault" in str(exc_info.value)
    mock_docker_client.exec_command.assert_not_called()


@pytest.mark.asyncio
async def test_inject_container_not_running(mock_docker_client):
    mock_docker_client.get_container_status.return_value = "exited"
    fault = RabbitMQFault(docker_client=mock_docker_client)

    with pytest.raises(RuntimeError) as exc_info:
        await fault.inject("rabbitmq")
    assert "container 'robot-shop_rabbitmq_1' is not running" in str(exc_info.value)
    mock_docker_client.exec_command.assert_not_called()


@pytest.mark.asyncio
async def test_inject_success(mock_docker_client):
    mock_docker_client.exec_command.return_value = (0, "Stopping node rabbit@rabbitmq ...\n")
    fault = RabbitMQFault(docker_client=mock_docker_client)

    res = await fault.inject("rabbitmq")

    mock_docker_client.resolve_container.assert_awaited_once_with("rabbitmq")
    mock_docker_client.get_container_status.assert_awaited_once_with("robot-shop_rabbitmq_1")
    mock_docker_client.exec_command.assert_awaited_once_with(
        "robot-shop_rabbitmq_1", ["rabbitmqctl", "stop_app"]
    )
    assert res["fault_type"] == "rabbitmq_block"
    assert res["target"] == "rabbitmq"
    assert res["container"] == "robot-shop_rabbitmq_1"
    assert res["action"] == "stopped_app"
    assert res["rollback_command"] == "docker exec robot-shop_rabbitmq_1 rabbitmqctl start_app"


@pytest.mark.asyncio
async def test_inject_command_failure(mock_docker_client):
    mock_docker_client.exec_command.return_value = (1, "Error: node unreachable\n")
    fault = RabbitMQFault(docker_client=mock_docker_client)

    with pytest.raises(RuntimeError) as exc_info:
        await fault.inject("rabbitmq")
    assert "rabbitmqctl stop_app failed" in str(exc_info.value)
    assert "node unreachable" in str(exc_info.value)


@pytest.mark.asyncio
async def test_rollback_success(mock_docker_client):
    mock_docker_client.exec_command.return_value = (0, "Starting node rabbit@rabbitmq ...\ncompleted.\n")
    fault = RabbitMQFault(docker_client=mock_docker_client)

    res = await fault.rollback("rabbitmq")

    mock_docker_client.resolve_container.assert_awaited_once_with("rabbitmq")
    mock_docker_client.exec_command.assert_awaited_once_with(
        "robot-shop_rabbitmq_1", ["rabbitmqctl", "start_app"]
    )
    assert res["fault_type"] == "rabbitmq_block"
    assert res["target"] == "rabbitmq"
    assert res["container"] == "robot-shop_rabbitmq_1"
    assert res["action"] == "started_app"


@pytest.mark.asyncio
async def test_rollback_failure(mock_docker_client):
    mock_docker_client.exec_command.return_value = (1, "Error: timeout waiting for application\n")
    fault = RabbitMQFault(docker_client=mock_docker_client)

    with pytest.raises(RuntimeError) as exc_info:
        await fault.rollback("rabbitmq")
    assert "rabbitmqctl start_app failed" in str(exc_info.value)
    assert "timeout waiting for application" in str(exc_info.value)


@pytest.mark.asyncio
async def test_verify_rollback_true_when_running(mock_docker_client):
    mock_docker_client.exec_command.return_value = (0, "Health check passed\n")
    fault = RabbitMQFault(docker_client=mock_docker_client)

    verified = await fault.verify_rollback("rabbitmq")

    assert verified is True
    mock_docker_client.exec_command.assert_awaited_once_with(
        "robot-shop_rabbitmq_1", ["rabbitmq-diagnostics", "check_running"]
    )


@pytest.mark.asyncio
async def test_verify_rollback_false_when_not_running(mock_docker_client):
    mock_docker_client.exec_command.return_value = (1, "Application is not running\n")
    fault = RabbitMQFault(docker_client=mock_docker_client)

    verified = await fault.verify_rollback("rabbitmq")

    assert verified is False
    mock_docker_client.exec_command.assert_awaited_once_with(
        "robot-shop_rabbitmq_1", ["rabbitmq-diagnostics", "check_running"]
    )


@pytest.mark.asyncio
async def test_verify_rollback_false_on_exception(mock_docker_client):
    mock_docker_client.exec_command.side_effect = RuntimeError("Container connection lost")
    fault = RabbitMQFault(docker_client=mock_docker_client)

    verified = await fault.verify_rollback("rabbitmq")

    assert verified is False
