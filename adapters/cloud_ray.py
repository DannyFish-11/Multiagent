"""Ray 云供应商(M21):把 conductor 的"一次性 VM"映射到 Ray 任务,做单集群大规模并发。

装了可选依赖(`uv sync --extra ray`)后,`cloud.provider=ray` 即用——把实验分发到
Ray 集群/本机多核并发跑(create=提交 task,status=轮询,destroy=取消)。适合"大规模
Agent 并发"场景:Ray 负责调度/容错/伸缩,conductor 的双层熔断与状态机不变。

红线:ray 只在本文件接触(lazy import);实现 CloudProvider 协议
(create_vm / get_status / destroy_vm)。
"""

from __future__ import annotations

from typing import Any

from core.errors import LayerError


def _experiment_entrypoint(name: str, cloud_init: str, secrets: dict) -> str:
    """在 Ray worker 上跑实验的入口(占位:真实场景解析 cloud_init 里的实验 YAML
    并调 core.experiment_run)。这里返回名字以示完成,便于协议/调度层单测。"""
    return name


class RayProvider:
    """Ray 后端:每个实验 = 一个 Ray remote 任务。VM 生命周期映射到任务生命周期。"""

    def __init__(self, config=None, address: str | None = None) -> None:
        try:
            import ray
        except ImportError as exc:
            raise LayerError("L17", "cloud-ray",
                             "缺 ray 依赖:uv sync --extra ray") from exc
        addr = address
        if config is not None and getattr(config, "cloud", None) is not None:
            addr = config.cloud.base_url or None   # 复用 cloud.base_url 作为 Ray 集群地址(留空=本机)
        if not ray.is_initialized():
            ray.init(address=addr, ignore_reinit_error=True, log_to_driver=False)
        self._remote = ray.remote(_experiment_entrypoint)
        self._tasks: dict[str, Any] = {}
        self._seq = 0

    async def create_vm(self, name: str, cloud_init: str, secrets: dict[str, str]) -> str:
        self._seq += 1
        vm_id = f"ray-{name}-{self._seq}"
        self._tasks[vm_id] = self._remote.remote(name, cloud_init, secrets)
        return vm_id

    async def get_status(self, vm_id: str) -> str:
        import ray

        ref = self._tasks.get(vm_id)
        if ref is None:
            return "terminated"
        ready, _ = ray.wait([ref], timeout=0)      # 非阻塞:完成→stopped,否则 running
        return "stopped" if ready else "running"

    async def destroy_vm(self, vm_id: str) -> None:
        import ray

        ref = self._tasks.pop(vm_id, None)
        if ref is not None:
            try:
                ray.cancel(ref, force=True)
            except Exception:
                pass   # 已结束的任务取消报错无害
