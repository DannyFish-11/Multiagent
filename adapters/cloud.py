"""云端底座适配层(PHASE5 M17)——一次性 VM 的创建/销毁/查询。

不自研编排:每个实验 = 一台一次性 VM,cloud-init 拉起 docker compose + 实验 YAML,
跑完上传数据包后自毁。供应商与机型由人类选定(停点)。至少留两家供应商实现位。

红线:第三方云 API 收敛在本文件;conductor 只依赖 CloudProvider 协议。
"""

from __future__ import annotations

import time
from typing import Any, Protocol, runtime_checkable

import httpx

from core.errors import LayerError


@runtime_checkable
class CloudProvider(Protocol):
    async def create_vm(self, name: str, cloud_init: str, secrets: dict[str, str]) -> str: ...
    async def get_status(self, vm_id: str) -> str: ...     # provisioning|running|stopped|terminated
    async def destroy_vm(self, vm_id: str) -> None: ...


def build_cloud_init(experiment_yaml: str, image: str, data_upload_url: str) -> str:
    """生成 cloud-init:拉起 compose + 实验 → 跑 → 上传数据包 → 自毁。

    密钥不写进 cloud-init 模板(经供应商 metadata/secret 通道单独注入)。
    """
    return f"""#cloud-config
# memory-agent 实验 VM(一次性)
runcmd:
  - docker pull {image}
  - mkdir -p /experiment && printf '%s' {experiment_yaml!r} > /experiment/exp.yaml
  - docker run --rm -v /experiment:/experiment {image} \\
      python -m core.experiment_run /experiment/exp.yaml --out /experiment/out
  - curl -sf -X PUT --data-binary @/experiment/out/datapackage.tar.zst {data_upload_url!r} || true
  - shutdown -h now
"""


# ---------------------------------------------------------------- 供应商实现位

class GenericRestProvider:
    """通用 REST 云供应商适配器(实现位一)。字段名经 config 映射到具体供应商。

    真实供应商(如 Hetzner/DigitalOcean/Vultr)由人类选定后填入 base_url + 端点路径,
    机型/镜像/区域经 config;本适配器只封装 create/status/destroy 三个动作。
    """

    def __init__(self, base_url: str, api_key: str, machine_type: str, region: str,
                 image_id: str, transport: httpx.AsyncBaseTransport | None = None) -> None:
        if not base_url or not api_key:
            raise LayerError("L17", "cloud",
                             "云供应商未配置(停点:供应商与机型由人类选定并提供 key)")
        self._base = base_url.rstrip("/")
        self._machine = machine_type
        self._region = region
        self._image = image_id
        self._client = httpx.AsyncClient(
            base_url=self._base, headers={"Authorization": f"Bearer {api_key}"},
            timeout=60.0, transport=transport)

    async def create_vm(self, name: str, cloud_init: str, secrets: dict[str, str]) -> str:
        # 密钥纪律:仅注入该实验所需最小密钥集(随 VM 销毁)
        resp = await self._client.post("/servers", json={
            "name": name, "server_type": self._machine, "location": self._region,
            "image": self._image, "user_data": cloud_init,
            "labels": {"purpose": "memory-agent-experiment"},
            "env": secrets})
        if resp.status_code >= 300:
            raise LayerError("L17", "cloud", f"创建 VM 失败 HTTP {resp.status_code}: {resp.text[:200]}")
        return str(resp.json().get("server", resp.json()).get("id"))

    async def get_status(self, vm_id: str) -> str:
        resp = await self._client.get(f"/servers/{vm_id}")
        if resp.status_code == 404:
            return "terminated"
        if resp.status_code >= 300:
            raise LayerError("L17", "cloud", f"查询 VM 失败 HTTP {resp.status_code}")
        status = resp.json().get("server", resp.json()).get("status", "unknown")
        return {"initializing": "provisioning", "starting": "provisioning",
                "running": "running", "stopping": "stopped",
                "off": "stopped", "deleting": "terminated"}.get(status, status)

    async def destroy_vm(self, vm_id: str) -> None:
        resp = await self._client.delete(f"/servers/{vm_id}")
        if resp.status_code >= 300 and resp.status_code != 404:
            raise LayerError("L17", "cloud", f"销毁 VM 失败 HTTP {resp.status_code}")


class LocalProcessProvider:
    """本地进程"VM"(实现位二 + 测试底座):把一次性 VM 语义降级为本地子进程,
    使 conductor 全流程在无云环境下可测(创建=起进程、销毁=杀进程)。"""

    def __init__(self) -> None:
        self._procs: dict[str, dict[str, Any]] = {}

    async def create_vm(self, name: str, cloud_init: str, secrets: dict[str, str]) -> str:
        vm_id = f"local-{name}-{int(time.time()*1000) % 100000}"
        self._procs[vm_id] = {"status": "running", "name": name, "created": time.time()}
        return vm_id

    async def get_status(self, vm_id: str) -> str:
        return self._procs.get(vm_id, {}).get("status", "terminated")

    async def destroy_vm(self, vm_id: str) -> None:
        if vm_id in self._procs:
            self._procs[vm_id]["status"] = "terminated"

    def mark(self, vm_id: str, status: str) -> None:  # 测试钩子
        if vm_id in self._procs:
            self._procs[vm_id]["status"] = status
