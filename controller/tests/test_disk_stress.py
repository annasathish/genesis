from unittest.mock import AsyncMock
import pytest

from faults.disk_stress import DiskStressFault
from faults.docker_client import AsyncDockerClient


@pytest.fixture
def mock_docker_client():
    client = AsyncMock(spec=AsyncDockerClient)
    client.resolve_container = AsyncMock(return_value="robot-shop_mysql_1")
    client.exec_command = AsyncMock()
    return client


@pytest.mark.asyncio
async def test_inject_bounds_rejection(mock_docker_client):
    fault = DiskStressFault(docker_client=mock_docker_client)

    # Below minimum (1 MB)
    with pytest.raises(ValueError) as exc_info:
        await fault.inject("mysql", {"fill_mb": 0})
    assert "fill_mb must be between 1 and 120 MB" in str(exc_info.value)

    # Above maximum (120 MB)
    with pytest.raises(ValueError) as exc_info:
        await fault.inject("mysql", {"fill_mb": 121})
    assert "fill_mb must be between 1 and 120 MB" in str(exc_info.value)

    mock_docker_client.exec_command.assert_not_called()


@pytest.mark.asyncio
async def test_inject_unsupported_target(mock_docker_client):
    fault = DiskStressFault(docker_client=mock_docker_client)

    with pytest.raises(ValueError) as exc_info:
        await fault.inject("cart")
    assert "Unsupported target 'cart' for disk stress" in str(exc_info.value)
    mock_docker_client.exec_command.assert_not_called()


@pytest.mark.asyncio
async def test_inject_aborts_on_non_tmpfs(mock_docker_client):
    mock_docker_client.exec_command.return_value = (0, "overlay\n")
    fault = DiskStressFault(docker_client=mock_docker_client)

    with pytest.raises(RuntimeError) as exc_info:
        await fault.inject("mysql")
    assert "Refusing injection to protect host filesystem" in str(exc_info.value)

    # Ensure dd was never called
    assert mock_docker_client.exec_command.await_count == 1
    mock_docker_client.exec_command.assert_awaited_once_with(
        "robot-shop_mysql_1", ["stat", "-f", "-c", "%T", "/var/lib/mysql"]
    )


@pytest.mark.asyncio
async def test_inject_tmpfs_success_exit_zero(mock_docker_client):
    # Call 1: stat check -> tmpfs
    # Call 2: dd command -> exit 0
    mock_docker_client.exec_command.side_effect = [
        (0, "tmpfs\n"),
        (0, "120+0 records in\n120+0 records out\n125829120 bytes (126 MB) copied\n"),
    ]
    fault = DiskStressFault(docker_client=mock_docker_client)
    res = await fault.inject("mysql", {"fill_mb": 100})

    assert res["fault_type"] == "disk_fill"
    assert res["target"] == "mysql"
    assert res["container"] == "robot-shop_mysql_1"
    assert res["mount_path"] == "/var/lib/mysql"
    assert res["fill_file"] == "/var/lib/mysql/chaos_fill"
    assert res["fill_mb"] == 100
    assert res["action"] == "injected"
    assert res["rollback_command"] == "rm -f /var/lib/mysql/chaos_fill"

    mock_docker_client.exec_command.assert_has_awaits([
        (("robot-shop_mysql_1", ["stat", "-f", "-c", "%T", "/var/lib/mysql"]),),
        (("robot-shop_mysql_1", ["dd", "if=/dev/zero", "of=/var/lib/mysql/chaos_fill", "bs=1M", "count=100"]),),
    ])


@pytest.mark.asyncio
async def test_inject_tmpfs_success_disk_full(mock_docker_client):
    # dd exits 1 when disk fills up completely
    mock_docker_client.exec_command.side_effect = [
        (0, "tmpfs\n"),
        (1, "dd: error writing '/var/lib/mysql/chaos_fill': No space left on device\n"),
    ]
    fault = DiskStressFault(docker_client=mock_docker_client)
    res = await fault.inject("mysql", {"fill_mb": 120})

    assert res["action"] == "injected"


@pytest.mark.asyncio
async def test_inject_tmpfs_unrelated_dd_failure(mock_docker_client):
    mock_docker_client.exec_command.side_effect = [
        (0, "tmpfs\n"),
        (1, "dd: failed to open '/var/lib/mysql/chaos_fill': Permission denied\n"),
    ]
    fault = DiskStressFault(docker_client=mock_docker_client)

    with pytest.raises(RuntimeError) as exc_info:
        await fault.inject("mysql")
    assert "dd command failed" in str(exc_info.value)
    assert "Permission denied" in str(exc_info.value)


@pytest.mark.asyncio
async def test_rollback_removes_file(mock_docker_client):
    mock_docker_client.exec_command.return_value = (0, "")
    fault = DiskStressFault(docker_client=mock_docker_client)
    res = await fault.rollback("mongodb")

    mock_docker_client.resolve_container.assert_awaited_once_with("mongodb")
    mock_docker_client.exec_command.assert_awaited_once_with(
        "robot-shop_mysql_1", ["rm", "-f", "/data/db/chaos_fill"]
    )
    assert res["fault_type"] == "disk_fill"
    assert res["target"] == "mongodb"
    assert res["fill_file"] == "/data/db/chaos_fill"
    assert res["action"] == "removed"


@pytest.mark.asyncio
async def test_verify_rollback_true_when_file_absent(mock_docker_client):
    # test -f returns 1 when file does NOT exist
    mock_docker_client.exec_command.return_value = (1, "")
    fault = DiskStressFault(docker_client=mock_docker_client)

    verified = await fault.verify_rollback("mysql")
    assert verified is True
    mock_docker_client.exec_command.assert_awaited_once_with(
        "robot-shop_mysql_1", ["test", "-f", "/var/lib/mysql/chaos_fill"]
    )


@pytest.mark.asyncio
async def test_verify_rollback_false_when_file_present(mock_docker_client):
    # test -f returns 0 when file DOES exist
    mock_docker_client.exec_command.return_value = (0, "")
    fault = DiskStressFault(docker_client=mock_docker_client)

    verified = await fault.verify_rollback("mysql")
    assert verified is False


@pytest.mark.asyncio
async def test_verify_rollback_false_on_exception(mock_docker_client):
    mock_docker_client.exec_command.side_effect = RuntimeError("Container unreachable")
    fault = DiskStressFault(docker_client=mock_docker_client)

    verified = await fault.verify_rollback("mysql")
    assert verified is False
