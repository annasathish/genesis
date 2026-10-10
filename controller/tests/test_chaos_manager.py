import asyncio
from datetime import datetime, timedelta
from unittest.mock import AsyncMock
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from faults.base import BaseFault
from faults.docker_client import AsyncDockerClient
from faults.manager import ChaosManager
from models import Base, ExperimentIntent


@pytest.fixture
def db_session_factory():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    session_factory = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    return session_factory


@pytest.fixture
def mock_injector():
    injector = AsyncMock(spec=BaseFault)
    injector.inject.return_value = {
        "action": "stopped",
        "rollback_command": "docker start test_c",
    }
    injector.rollback.return_value = {"action": "started"}
    injector.verify_rollback.return_value = True
    return injector


@pytest.mark.asyncio
async def test_intent_logging_before_injection(db_session_factory):
    inject_started_event = asyncio.Event()
    inject_can_finish_event = asyncio.Event()

    async def mock_inject(target, params=None):
        inject_started_event.set()
        await inject_can_finish_event.wait()
        return {"rollback_command": "docker start cart_container"}

    slow_injector = AsyncMock(spec=BaseFault)
    slow_injector.inject.side_effect = mock_inject
    slow_injector.rollback.return_value = {}
    slow_injector.verify_rollback.return_value = True

    manager = ChaosManager(
        session_factory=db_session_factory,
        injectors={"container_crash": slow_injector},
    )

    # 1. Start execution in background task
    task = asyncio.create_task(
        manager.execute_fault("cart", "container_crash", ttl_seconds=30)
    )

    # 2. Wait until injection has started
    await inject_started_event.wait()

    # 3. Query DB to prove that intent is committed as 'pending' BEFORE injection completes
    db = db_session_factory()
    try:
        intent = db.query(ExperimentIntent).filter(ExperimentIntent.target_service == "cart").first()
        assert intent is not None
        assert intent.status == "pending"
        assert intent.rollback_command == "pending"
    finally:
        db.close()

    # 4. Release injection to finish
    inject_can_finish_event.set()
    res = await task

    assert res["status"] == "running"
    assert res["rollback_command"] == "docker start cart_container"

    # 5. Confirm DB status updated to 'running'
    db = db_session_factory()
    try:
        intent = db.query(ExperimentIntent).filter(ExperimentIntent.id == res["intent_id"]).first()
        assert intent.status == "running"
        assert intent.rollback_command == "docker start cart_container"
    finally:
        db.close()

    # Cleanup background TTL task
    await manager.rollback(res["intent_id"])


@pytest.mark.asyncio
async def test_preflight_unregistered_fault(db_session_factory):
    manager = ChaosManager(session_factory=db_session_factory)
    with pytest.raises(ValueError) as exc_info:
        await manager.execute_fault("cart", "unknown_fault")
    assert "Unsupported fault_type 'unknown_fault'" in str(exc_info.value)


@pytest.mark.asyncio
async def test_ttl_bounds_validation(db_session_factory, mock_injector):
    manager = ChaosManager(
        session_factory=db_session_factory,
        injectors={"container_crash": mock_injector},
    )
    with pytest.raises(ValueError) as exc_info:
        await manager.execute_fault("cart", "container_crash", ttl_seconds=0)
    assert "ttl_seconds must be between 1 and 300" in str(exc_info.value)

    with pytest.raises(ValueError) as exc_info:
        await manager.execute_fault("cart", "container_crash", ttl_seconds=301)
    assert "ttl_seconds must be between 1 and 300" in str(exc_info.value)


@pytest.mark.asyncio
async def test_prevent_concurrent_fault_on_same_target(db_session_factory, mock_injector):
    manager = ChaosManager(
        session_factory=db_session_factory,
        injectors={"container_crash": mock_injector},
    )
    res = await manager.execute_fault("cart", "container_crash", ttl_seconds=60)
    assert res["status"] == "running"

    # Second execution on same target must be rejected
    with pytest.raises(RuntimeError) as exc_info:
        await manager.execute_fault("cart", "container_crash", ttl_seconds=60)
    assert "already has an active pending or running fault" in str(exc_info.value)

    await manager.rollback(res["intent_id"])


@pytest.mark.asyncio
async def test_injection_failure_aborts_intent(db_session_factory):
    failing_injector = AsyncMock(spec=BaseFault)
    failing_injector.inject.side_effect = RuntimeError("Docker API unreachable")

    manager = ChaosManager(
        session_factory=db_session_factory,
        injectors={"container_crash": failing_injector},
    )

    with pytest.raises(RuntimeError):
        await manager.execute_fault("cart", "container_crash")

    db = db_session_factory()
    try:
        intent = db.query(ExperimentIntent).filter(ExperimentIntent.target_service == "cart").first()
        assert intent is not None
        assert intent.status == "aborted"
    finally:
        db.close()


@pytest.mark.asyncio
async def test_rollback_by_intent_id_success(db_session_factory, mock_injector):
    manager = ChaosManager(
        session_factory=db_session_factory,
        injectors={"container_crash": mock_injector},
    )
    res = await manager.execute_fault("ratings", "container_crash", ttl_seconds=60)
    intent_id = res["intent_id"]

    assert intent_id in manager.active_ttl_tasks

    rollback_res = await manager.rollback(intent_id)

    assert rollback_res["status"] == "success"
    assert rollback_res["rollback_verified"] is True
    assert rollback_res["error"] is None
    assert intent_id not in manager.active_ttl_tasks

    mock_injector.rollback.assert_awaited_once_with("ratings")
    mock_injector.verify_rollback.assert_awaited_once_with("ratings")

    db = db_session_factory()
    try:
        intent = db.query(ExperimentIntent).filter(ExperimentIntent.id == intent_id).first()
        assert intent.status == "success"
    finally:
        db.close()


@pytest.mark.asyncio
async def test_rollback_verification_failure(db_session_factory):
    unverifiable_injector = AsyncMock(spec=BaseFault)
    unverifiable_injector.inject.return_value = {"rollback_command": "start"}
    unverifiable_injector.rollback.return_value = {}
    unverifiable_injector.verify_rollback.return_value = False

    manager = ChaosManager(
        session_factory=db_session_factory,
        injectors={"container_crash": unverifiable_injector},
    )
    res = await manager.execute_fault("cart", "container_crash", ttl_seconds=60)
    intent_id = res["intent_id"]

    rollback_res = await manager.rollback(intent_id)

    assert rollback_res["status"] == "aborted"
    assert rollback_res["rollback_verified"] is False

    db = db_session_factory()
    try:
        intent = db.query(ExperimentIntent).filter(ExperimentIntent.id == intent_id).first()
        assert intent.status == "aborted"
    finally:
        db.close()


@pytest.mark.asyncio
async def test_ttl_auto_rollback_expiration(db_session_factory, mock_injector):
    manager = ChaosManager(
        session_factory=db_session_factory,
        injectors={"container_crash": mock_injector},
    )
    # Inject with 1s TTL
    res = await manager.execute_fault("cart", "container_crash", ttl_seconds=1)
    intent_id = res["intent_id"]

    # Wait for TTL to expire and watchdog to process
    await asyncio.sleep(1.2)

    db = db_session_factory()
    try:
        intent = db.query(ExperimentIntent).filter(ExperimentIntent.id == intent_id).first()
        assert intent.status == "success"
    finally:
        db.close()

    assert intent_id not in manager.active_ttl_tasks
    mock_injector.rollback.assert_awaited_once_with("cart")
    mock_injector.verify_rollback.assert_awaited_once_with("cart")


@pytest.mark.asyncio
async def test_ttl_watchdog_exception_handling(db_session_factory):
    exploding_injector = AsyncMock(spec=BaseFault)
    exploding_injector.inject.return_value = {"rollback_command": "start"}
    exploding_injector.rollback.side_effect = RuntimeError("Fatal daemon crash during rollback")
    exploding_injector.verify_rollback.return_value = False

    manager = ChaosManager(
        session_factory=db_session_factory,
        injectors={"container_crash": exploding_injector},
    )
    res = await manager.execute_fault("cart", "container_crash", ttl_seconds=1)
    intent_id = res["intent_id"]

    await asyncio.sleep(1.2)

    db = db_session_factory()
    try:
        intent = db.query(ExperimentIntent).filter(ExperimentIntent.id == intent_id).first()
        assert intent.status == "aborted"
    finally:
        db.close()

    assert intent_id not in manager.active_ttl_tasks


@pytest.mark.asyncio
async def test_emergency_rollback_all(db_session_factory):
    good_injector = AsyncMock(spec=BaseFault)
    good_injector.inject.return_value = {"rollback_command": "start"}
    good_injector.rollback.return_value = {}
    good_injector.verify_rollback.return_value = True

    bad_injector = AsyncMock(spec=BaseFault)
    bad_injector.inject.return_value = {"rollback_command": "start"}
    bad_injector.rollback.side_effect = RuntimeError("Failed bad rollback")
    bad_injector.verify_rollback.return_value = False

    manager = ChaosManager(
        session_factory=db_session_factory,
        injectors={"container_crash": good_injector, "disk_fill": bad_injector},
    )

    res1 = await manager.execute_fault("cart", "container_crash", ttl_seconds=60)
    res2 = await manager.execute_fault("mysql", "disk_fill", ttl_seconds=60)

    emergency_results = await manager.emergency_rollback_all()

    assert len(emergency_results) == 2
    statuses = {r["target"]: r["status"] for r in emergency_results}
    assert statuses["cart"] == "success"
    assert statuses["mysql"] == "aborted"


@pytest.mark.asyncio
async def test_get_active_intents(db_session_factory, mock_injector):
    manager = ChaosManager(
        session_factory=db_session_factory,
        injectors={"container_crash": mock_injector},
    )
    res = await manager.execute_fault("cart", "container_crash", ttl_seconds=60)

    active = await manager.get_active_intents()
    assert len(active) == 1
    assert active[0]["target"] == "cart"
    assert active[0]["status"] == "running"

    await manager.rollback(res["intent_id"])
    active_after = await manager.get_active_intents()
    assert len(active_after) == 0
