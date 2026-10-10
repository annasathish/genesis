import os
from typing import Any, Dict, Optional

import httpx

from .base import BaseFault
from .docker_client import AsyncDockerClient


class NetworkLatencyFault(BaseFault):
    """Injects network latency or packet loss via the Toxiproxy HTTP API."""

    fault_type: str = "network_latency"

    def __init__(
        self,
        docker_client: Optional[AsyncDockerClient] = None,
        base_url: Optional[str] = None,
        client: Optional[Any] = None,
    ) -> None:
        super().__init__(docker_client=docker_client or AsyncDockerClient())
        self.base_url = (
            base_url or os.environ.get("TOXIPROXY_URL", "http://localhost:8474")
        ).rstrip("/")
        self._client = client

    def _get_client(self) -> Any:
        if self._client is None:
            self._client = httpx.AsyncClient(base_url=self.base_url, timeout=10.0)
        return self._client

    async def inject(
        self, target: str, params: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        params = params or {}
        toxic_type = params.get("toxic_type", "latency")
        if toxic_type not in ("latency", "loss"):
            raise ValueError(
                f"Unsupported toxic_type '{toxic_type}'. Supported types: 'latency', 'loss'."
            )

        toxic_name = f"chaos_{target}"
        if toxic_type == "latency":
            attrs = {
                "latency": int(params.get("latency_ms", 200)),
                "jitter": int(params.get("jitter_ms", 0)),
            }
        else:
            attrs = {"percentage": float(params.get("percentage", 10.0))}

        payload = {
            "name": toxic_name,
            "type": toxic_type,
            "toxicity": 1.0,
            "attributes": attrs,
        }
        resp = await self._get_client().post(
            f"/proxies/{target}/toxics", json=payload
        )
        if resp.status_code not in (200, 201):
            raise RuntimeError(
                f"Toxiproxy injection failed on '{target}' [{resp.status_code}]: {resp.text}"
            )

        return {
            "fault_type": self.fault_type,
            "target": target,
            "toxic_name": toxic_name,
            "action": "injected",
            "rollback_command": f"DELETE {self.base_url}/proxies/{target}/toxics/{toxic_name}",
        }

    async def rollback(self, target: str) -> Dict[str, Any]:
        toxic_name = f"chaos_{target}"
        resp = await self._get_client().delete(
            f"/proxies/{target}/toxics/{toxic_name}"
        )
        if resp.status_code not in (200, 204, 404):
            raise RuntimeError(
                f"Toxiproxy rollback failed on '{target}' [{resp.status_code}]: {resp.text}"
            )

        return {
            "fault_type": self.fault_type,
            "target": target,
            "toxic_name": toxic_name,
            "action": "removed",
        }

    async def verify_rollback(self, target: str) -> bool:
        toxic_name = f"chaos_{target}"
        try:
            resp = await self._get_client().get(f"/proxies/{target}/toxics")
            if resp.status_code != 200:
                return False

            toxics = resp.json()
            if isinstance(toxics, list):
                active_names = [t.get("name") for t in toxics if isinstance(t, dict)]
            elif isinstance(toxics, dict):
                active_names = list(toxics.keys())
            else:
                active_names = []

            return toxic_name not in active_names
        except Exception:
            return False
