from typing import Any, Dict, Optional

from .base import BaseFault
from .docker_client import AsyncDockerClient


class DiskStressFault(BaseFault):
    """Injects bounded disk stress into approved tmpfs database mounts."""

    fault_type: str = "disk_fill"

    APPROVED_MOUNTS: Dict[str, str] = {
        "mysql": "/var/lib/mysql",
        "mongodb": "/data/db",
    }

    def __init__(self, docker_client: Optional[AsyncDockerClient] = None) -> None:
        super().__init__(docker_client=docker_client or AsyncDockerClient())

    def _get_mount_info(self, target: str) -> tuple[str, str]:
        if target not in self.APPROVED_MOUNTS:
            raise ValueError(
                f"Unsupported target '{target}' for disk stress. "
                f"Supported targets: {list(self.APPROVED_MOUNTS.keys())}"
            )
        mount_path = self.APPROVED_MOUNTS[target]
        fill_file = f"{mount_path}/chaos_fill"
        return mount_path, fill_file

    async def inject(
        self, target: str, params: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        assert self.docker_client is not None
        params = params or {}

        # 1. Enforce hard fill-size bounds (1–120 MB)
        fill_mb = int(params.get("fill_mb", 120))
        if not (1 <= fill_mb <= 120):
            raise ValueError(
                f"fill_mb must be between 1 and 120 MB, got {fill_mb}"
            )

        # 2. Resolve target mount and fill file
        mount_path, fill_file = self._get_mount_info(target)
        container = await self.docker_client.resolve_container(target)

        # 3. Verify filesystem type is strictly tmpfs
        fs_code, fs_type = await self.docker_client.exec_command(
            container, ["stat", "-f", "-c", "%T", mount_path]
        )
        fs_type_clean = fs_type.strip().lower()
        if fs_code != 0 or fs_type_clean != "tmpfs":
            raise RuntimeError(
                f"Target mount '{mount_path}' is '{fs_type_clean}', not 'tmpfs'. "
                "Refusing injection to protect host filesystem."
            )

        # 4. Execute dd to fill the tmpfs mount
        dd_cmd = [
            "dd",
            "if=/dev/zero",
            f"of={fill_file}",
            "bs=1M",
            f"count={fill_mb}",
        ]
        exit_code, output = await self.docker_client.exec_command(
            container, dd_cmd
        )

        # 5. Validate dd outcome
        if exit_code != 0 and "no space left on device" not in output.lower():
            raise RuntimeError(
                f"dd command failed inside '{container}' with exit code {exit_code}: {output}"
            )

        return {
            "fault_type": self.fault_type,
            "target": target,
            "container": container,
            "mount_path": mount_path,
            "fill_file": fill_file,
            "fill_mb": fill_mb,
            "action": "injected",
            "rollback_command": f"rm -f {fill_file}",
        }

    async def rollback(self, target: str) -> Dict[str, Any]:
        assert self.docker_client is not None
        _, fill_file = self._get_mount_info(target)
        container = await self.docker_client.resolve_container(target)

        await self.docker_client.exec_command(
            container, ["rm", "-f", fill_file]
        )

        return {
            "fault_type": self.fault_type,
            "target": target,
            "container": container,
            "fill_file": fill_file,
            "action": "removed",
        }

    async def verify_rollback(self, target: str) -> bool:
        assert self.docker_client is not None
        try:
            _, fill_file = self._get_mount_info(target)
            container = await self.docker_client.resolve_container(target)
            exit_code, _ = await self.docker_client.exec_command(
                container, ["test", "-f", fill_file]
            )
            # test -f returns non-zero when the file does not exist
            return exit_code != 0
        except Exception:
            return False
