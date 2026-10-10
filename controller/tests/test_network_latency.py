from unittest.mock import AsyncMock, MagicMock
import pytest

from faults.network_latency import NetworkLatencyFault


@pytest.fixture
def mock_http_client():
    client = AsyncMock()
    return client


@pytest.mark.asyncio
async def test_inject_latency_success(mock_http_client):
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_http_client.post.return_value = mock_resp

    fault = NetworkLatencyFault(client=mock_http_client)
    res = await fault.inject("shipping_to_rabbitmq", {"latency_ms": 300, "jitter_ms": 50})

    mock_http_client.post.assert_awaited_once_with(
        "/proxies/shipping_to_rabbitmq/toxics",
        json={
            "name": "chaos_shipping_to_rabbitmq",
            "type": "latency",
            "toxicity": 1.0,
            "attributes": {"latency": 300, "jitter": 50},
        },
    )
    assert res["fault_type"] == "network_latency"
    assert res["target"] == "shipping_to_rabbitmq"
    assert res["toxic_name"] == "chaos_shipping_to_rabbitmq"
    assert res["action"] == "injected"
    assert "DELETE" in res["rollback_command"]


@pytest.mark.asyncio
async def test_inject_loss_success(mock_http_client):
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_http_client.post.return_value = mock_resp

    fault = NetworkLatencyFault(client=mock_http_client)
    res = await fault.inject("ratings", {"toxic_type": "loss", "percentage": 25.0})

    mock_http_client.post.assert_awaited_once_with(
        "/proxies/ratings/toxics",
        json={
            "name": "chaos_ratings",
            "type": "loss",
            "toxicity": 1.0,
            "attributes": {"percentage": 25.0},
        },
    )
    assert res["toxic_name"] == "chaos_ratings"


@pytest.mark.asyncio
async def test_inject_unsupported_toxic_type(mock_http_client):
    fault = NetworkLatencyFault(client=mock_http_client)
    with pytest.raises(ValueError) as exc_info:
        await fault.inject("ratings", {"toxic_type": "bandwidth"})
    assert "Unsupported toxic_type 'bandwidth'" in str(exc_info.value)
    mock_http_client.post.assert_not_called()


@pytest.mark.asyncio
async def test_inject_proxy_not_found(mock_http_client):
    mock_resp = MagicMock()
    mock_resp.status_code = 404
    mock_resp.text = "proxy not found"
    mock_http_client.post.return_value = mock_resp

    fault = NetworkLatencyFault(client=mock_http_client)
    with pytest.raises(RuntimeError) as exc_info:
        await fault.inject("nonexistent_proxy")
    assert "Toxiproxy injection failed on 'nonexistent_proxy' [404]" in str(exc_info.value)


@pytest.mark.asyncio
async def test_rollback_success(mock_http_client):
    mock_resp = MagicMock()
    mock_resp.status_code = 204
    mock_http_client.delete.return_value = mock_resp

    fault = NetworkLatencyFault(client=mock_http_client)
    res = await fault.rollback("shipping_to_rabbitmq")

    mock_http_client.delete.assert_awaited_once_with("/proxies/shipping_to_rabbitmq/toxics/chaos_shipping_to_rabbitmq")
    assert res["action"] == "removed"
    assert res["toxic_name"] == "chaos_shipping_to_rabbitmq"


@pytest.mark.asyncio
async def test_rollback_idempotent_404(mock_http_client):
    mock_resp = MagicMock()
    mock_resp.status_code = 404
    mock_http_client.delete.return_value = mock_resp

    fault = NetworkLatencyFault(client=mock_http_client)
    res = await fault.rollback("shipping_to_rabbitmq")

    assert res["action"] == "removed"


@pytest.mark.asyncio
async def test_verify_rollback_true_when_toxic_absent_from_existing_proxy(mock_http_client):
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = [{"name": "other_toxic"}]
    mock_http_client.get.return_value = mock_resp

    fault = NetworkLatencyFault(client=mock_http_client)
    verified = await fault.verify_rollback("shipping_to_rabbitmq")

    assert verified is True
    mock_http_client.get.assert_awaited_once_with("/proxies/shipping_to_rabbitmq/toxics")


@pytest.mark.asyncio
async def test_verify_rollback_false_when_proxy_does_not_exist(mock_http_client):
    mock_resp = MagicMock()
    mock_resp.status_code = 404
    mock_http_client.get.return_value = mock_resp

    fault = NetworkLatencyFault(client=mock_http_client)
    verified = await fault.verify_rollback("nonexistent_proxy")

    assert verified is False
    mock_http_client.get.assert_awaited_once_with("/proxies/nonexistent_proxy/toxics")


@pytest.mark.asyncio
async def test_verify_rollback_false_when_toxic_still_present(mock_http_client):
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = [{"name": "chaos_shipping_to_rabbitmq"}]
    mock_http_client.get.return_value = mock_resp

    fault = NetworkLatencyFault(client=mock_http_client)
    verified = await fault.verify_rollback("shipping_to_rabbitmq")

    assert verified is False


@pytest.mark.asyncio
async def test_verify_rollback_false_on_network_or_api_error(mock_http_client):
    mock_http_client.get.side_effect = RuntimeError("Connection refused")

    fault = NetworkLatencyFault(client=mock_http_client)
    verified = await fault.verify_rollback("shipping_to_rabbitmq")

    assert verified is False
