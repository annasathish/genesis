import asyncio
from datetime import datetime, timedelta
import json
import logging
from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session, sessionmaker

try:
    from models import ExperimentIntent
except ImportError:
    from ..models import ExperimentIntent

from .base import BaseFault
from .container_crash import ContainerCrashFault
from .disk_stress import DiskStressFault
from .docker_client import AsyncDockerClient
from .network_latency import NetworkLatencyFault
from .rabbitmq_block import RabbitMQFault

logger = logging.getLogger(__name__)


class ChaosManager:
    """Orchestrates chaos fault injection, intent logging, TTL enforcement, and verified rollbacks."""

    def __init__(
        self,
        session_factory: sessionmaker,
        docker_client: Optional[AsyncDockerClient] = None,
        injectors: Optional[Dict[str, BaseFault]] = None,
    ) -> None:
        self.session_factory = session_factory
        self.docker_client = docker_client or AsyncDockerClient()
        self.active_ttl_tasks: Dict[int, asyncio.Task] = {}

        if injectors is not None:
            self._injectors = injectors
        else:
            self._injectors = {
                "container_crash": ContainerCrashFault(docker_client=self.docker_client),
                "network_latency": NetworkLatencyFault(docker_client=self.docker_client),
                "disk_fill": DiskStressFault(docker_client=self.docker_client),
                "rabbitmq_block": RabbitMQFault(docker_client=self.docker_client),
            }

    async def execute_fault(
        self,
        target: str,
        fault_type: str,
        params: Optional[Dict[str, Any]] = None,
        ttl_seconds: int = 30,
    ) -> Dict[str, Any]:
        """Validates pre-flight, logs intent to SQLite as 'pending', executes

        injection, arms the TTL auto-rollback timer, updates status to
        'running', and returns structured metadata.
        """
        # 1. Validate fault_type
        if fault_type not in self._injectors:
            raise ValueError(
                f"Unsupported fault_type '{fault_type}'. Supported: {list(self._injectors.keys())}"
            )

        # 2. Validate TTL
        if not (1 <= ttl_seconds <= 300):
            raise ValueError(f"ttl_seconds must be between 1 and 300, got {ttl_seconds}")

        # 3. Check for active running fault on same target
        db: Session = self.session_factory()
        try:
            active = (
                db.query(ExperimentIntent)
                .filter(
                    ExperimentIntent.target_service == target,
                    ExperimentIntent.status == "running",
                )
                .first()
            )
            if active:
                raise RuntimeError(
                    f"Target '{target}' already has an active running fault (intent #{active.id})"
                )

            # 4. Intent logging before injection (status="pending")
            created_at = datetime.utcnow()
            expires_at = created_at + timedelta(seconds=ttl_seconds)
            intent = ExperimentIntent(
                fault_type=fault_type,
                target_service=target,
                fault_params=json.dumps(params or {}),
                rollback_command="pending",
                status="pending",
                created_at=created_at,
                expires_at=expires_at,
            )
            db.add(intent)
            db.commit()
            db.refresh(intent)
            intent_id = intent.id
        finally:
            db.close()

        # 5. Inject
        injector = self._injectors[fault_type]
        try:
            inject_res = await injector.inject(target, params)
            rollback_cmd = inject_res.get("rollback_command", "unknown")

            # 6. Update status to running
            db = self.session_factory()
            try:
                db_intent = db.query(ExperimentIntent).filter(ExperimentIntent.id == intent_id).first()
                if db_intent:
                    db_intent.rollback_command = rollback_cmd
                    db_intent.status = "running"
                    db.commit()
            finally:
                db.close()

            # 7. Register and launch TTL watchdog
            ttl_task = asyncio.create_task(self._ttl_watchdog(intent_id))
            self.active_ttl_tasks[intent_id] = ttl_task

            return {
                "intent_id": intent_id,
                "fault_type": fault_type,
                "target": target,
                "params": params or {},
                "status": "running",
                "created_at": created_at.isoformat(),
                "expires_at": expires_at.isoformat(),
                "ttl_seconds": ttl_seconds,
                "rollback_command": rollback_cmd,
            }

        except Exception as err:
            db = self.session_factory()
            try:
                db_intent = db.query(ExperimentIntent).filter(ExperimentIntent.id == intent_id).first()
                if db_intent:
                    db_intent.status = "aborted"
                    db.commit()
            finally:
                db.close()
            raise err

    async def rollback(self, intent_id: int) -> Dict[str, Any]:
        """Explicitly rolls back a specific experiment by intent_id, verifies

        restoration, updates DB status ('success' or 'aborted'), cancels active
        TTL, and returns structured result.
        """
        ttl_task = self.active_ttl_tasks.pop(intent_id, None)
        if ttl_task and not ttl_task.done():
            ttl_task.cancel()

        db: Session = self.session_factory()
        try:
            intent = db.query(ExperimentIntent).filter(ExperimentIntent.id == intent_id).first()
            if not intent:
                raise ValueError(f"Experiment intent #{intent_id} not found")

            fault_type = intent.fault_type
            target = intent.target_service
            current_status = intent.status

            if current_status not in ("running", "pending"):
                return {
                    "intent_id": intent_id,
                    "fault_type": fault_type,
                    "target": target,
                    "status": current_status,
                    "rollback_verified": True,
                    "rolled_back_at": datetime.utcnow().isoformat(),
                    "error": None,
                    "details": f"Intent already in '{current_status}' state",
                }

            injector = self._injectors.get(fault_type)
            if not injector:
                raise ValueError(f"No injector registered for fault_type '{fault_type}'")

            error_msg = None
            details = None
            verified = False

            try:
                details = await injector.rollback(target)
                verified = await injector.verify_rollback(target)
            except Exception as e:
                error_msg = str(e)
                verified = False

            final_status = "success" if verified else "aborted"
            intent.status = final_status
            
            # Create ExperimentResult so reporter sees it
            try:
                from models import ExperimentResult
                res = ExperimentResult(
                    intent_id=intent_id,
                    target_service=target,
                    # We can't know exact metrics here without passing telemetry, 
                    # but we create the row. The telemetry loop can update it, 
                    # or it just records that it finished.
                    details=details or {"error": error_msg} if error_msg else {}
                )
                db.add(res)
            except ImportError:
                pass
                
            db.commit()

            return {
                "intent_id": intent_id,
                "fault_type": fault_type,
                "target": target,
                "status": final_status,
                "rollback_verified": verified,
                "rolled_back_at": datetime.utcnow().isoformat(),
                "error": error_msg,
                "details": details or {},
            }
        finally:
            db.close()

    async def emergency_rollback_all(self) -> List[Dict[str, Any]]:
        """Invoked by M4's Kill Switch or Dead-man Watchdog.

        Finds all currently 'running' intents in SQLite and executes verified
        rollbacks on each independently.
        """
        db: Session = self.session_factory()
        try:
            active_intents = (
                db.query(ExperimentIntent)
                .filter(ExperimentIntent.status == "running")
                .all()
            )
            intent_ids = [i.id for i in active_intents]
        finally:
            db.close()

        results = []
        for i_id in intent_ids:
            try:
                res = await self.rollback(i_id)
                results.append(res)
            except Exception as err:
                results.append({
                    "intent_id": i_id,
                    "status": "aborted",
                    "rollback_verified": False,
                    "rolled_back_at": datetime.utcnow().isoformat(),
                    "error": str(err),
                    "details": {},
                })
        return results

    async def get_active_intents(self) -> List[Dict[str, Any]]:
        """Returns all intents currently recorded in SQLite with status 'running'."""
        db: Session = self.session_factory()
        try:
            active = (
                db.query(ExperimentIntent)
                .filter(ExperimentIntent.status == "running")
                .all()
            )
            return [
                {
                    "intent_id": i.id,
                    "fault_type": i.fault_type,
                    "target": i.target_service,
                    "status": i.status,
                    "created_at": i.created_at.isoformat() if i.created_at else None,
                    "expires_at": i.expires_at.isoformat() if i.expires_at else None,
                    "rollback_command": i.rollback_command,
                }
                for i in active
            ]
        finally:
            db.close()

    async def _ttl_watchdog(self, intent_id: int) -> None:
        """Background watchdog task that sleeps until persisted expires_at,

        confirms running status, and rolls back the fault.
        """
        try:
            # 1. Fetch expires_at from DB
            db: Session = self.session_factory()
            try:
                intent = db.query(ExperimentIntent).filter(ExperimentIntent.id == intent_id).first()
                if not intent or intent.status != "running":
                    return
                expires_at = intent.expires_at
            finally:
                db.close()

            # 2. Calculate sleep
            sleep_sec = max(0.0, (expires_at - datetime.utcnow()).total_seconds())
            await asyncio.sleep(sleep_sec)

            # 3. Re-check intent status before rolling back
            db = self.session_factory()
            try:
                intent = db.query(ExperimentIntent).filter(ExperimentIntent.id == intent_id).first()
                if not intent or intent.status != "running":
                    return
            finally:
                db.close()

            # 4. Trigger rollback
            await self.rollback(intent_id)

        except asyncio.CancelledError:
            pass
        except Exception as err:
            logger.error(f"TTL watchdog error for intent #{intent_id}: {err}")
            try:
                db = self.session_factory()
                try:
                    intent = db.query(ExperimentIntent).filter(ExperimentIntent.id == intent_id).first()
                    if intent and intent.status == "running":
                        intent.status = "aborted"
                        db.commit()
                finally:
                    db.close()
            except Exception:
                pass
        finally:
            self.active_ttl_tasks.pop(intent_id, None)
