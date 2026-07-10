"""MemoryPack:记忆资产化(M7)。可携带、可继承的单一 tar.zst 归档。

归档结构:
  manifest.json   格式版本、导出方 agent_id + 签名、条数、模态统计、嵌入模型标识/维度
  memories.jsonl  记忆条目(原文+元数据+可见性),不含向量(导入方用当前 Embedder 重算)
  blobs/<sha256>  多模态原始文件,内容哈希命名

CLI(均为手动命令):
  python -m core.memorypack export  --out pack.tar.zst
  python -m core.memorypack import  --pack pack.tar.zst
  python -m core.memorypack merge   --pack other.tar.zst
  python -m core.memorypack inherit --pack parent.tar.zst   # LLM 蒸馏后导入,lineage 追加

merge 冲突策略:同内容哈希去重;矛盾事实并存,标注来源 agent_id 与导出时间,
交由检索时 LLM 裁决。
"""

from __future__ import annotations

import hashlib
import io
import json
import tarfile
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

import zstandard

from core.errors import LayerError
from core.identity import AgentIdentity, verify_signature
from core.schemas import Message, MultimodalInput

if TYPE_CHECKING:
    from adapters.llm import LLMClient
    from adapters.memory import QdrantMemoryStore

FORMAT_VERSION = 1

DISTILL_SYSTEM = """\
你是记忆蒸馏器(代际遗传)。输入是一个 agent 的全部记忆条目,输出压缩后的
结构化认知:合并流水账、去除一次性琐事、保留身份/偏好/关系/关键事实/经验教训。
输出严格 JSON 数组,每个元素是一条自包含的中文陈述句。不要输出其他字符。"""


def _content_hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# ---------------------------------------------------------------- 打包/解包

def _write_pack(path: Path, manifest: dict, entries: list[dict], blobs: dict[str, bytes]) -> None:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        def add_bytes(name: str, data: bytes) -> None:
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mtime = int(time.time())
            tar.addfile(info, io.BytesIO(data))

        add_bytes("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2).encode())
        add_bytes("memories.jsonl",
                  "".join(json.dumps(e, ensure_ascii=False) + "\n" for e in entries).encode())
        for digest, data in blobs.items():
            add_bytes(f"blobs/{digest}", data)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(zstandard.ZstdCompressor(level=9).compress(buf.getvalue()))


def read_pack(path: Path) -> tuple[dict, list[dict], dict[str, bytes]]:
    raw = zstandard.ZstdDecompressor().decompress(path.read_bytes(), max_output_size=1 << 32)
    manifest: dict = {}
    entries: list[dict] = []
    blobs: dict[str, bytes] = {}
    with tarfile.open(fileobj=io.BytesIO(raw)) as tar:
        for member in tar.getmembers():
            f = tar.extractfile(member)
            if f is None:
                continue
            data = f.read()
            if member.name == "manifest.json":
                manifest = json.loads(data)
            elif member.name == "memories.jsonl":
                entries = [json.loads(line) for line in data.decode().splitlines() if line.strip()]
            elif member.name.startswith("blobs/"):
                digest = member.name.split("/", 1)[1]
                if _content_hash(data) != digest:
                    raise LayerError("L7", "memorypack", f"blob 哈希不符: {digest}")
                blobs[digest] = data
    if not manifest:
        raise LayerError("L7", "memorypack", f"{path} 缺少 manifest.json")
    return manifest, entries, blobs


def verify_manifest(manifest: dict) -> bool:
    sig = manifest.get("signature") or {}
    payload = {k: v for k, v in manifest.items() if k != "signature"}
    return verify_signature(payload, sig.get("protected", ""), sig.get("signature", ""),
                            manifest.get("exporter", {}).get("public_key", ""))


# ---------------------------------------------------------------- 核心操作

class MemoryPackManager:
    def __init__(self, store: "QdrantMemoryStore", identity: AgentIdentity,
                 embedder_model: str, embedder_dim: int) -> None:
        self._store = store
        self._identity = identity
        self._embedder_model = embedder_model
        self._embedder_dim = embedder_dim

    async def export(self, out_path: Path) -> dict:
        points = await self._store.dump_all()
        entries: list[dict] = []
        blobs: dict[str, bytes] = {}
        modality_stats: dict[str, int] = {}
        for p in points:
            payload = p["payload"]
            # caption 派生点不导出:import 时由图像条目的 meta.caption 自动重建
            if payload.get("meta", {}).get("kind") == "caption":
                continue
            modality = payload.get("modality", "text")
            modality_stats[modality] = modality_stats.get(modality, 0) + 1
            entry = {
                "content": payload.get("content", ""),
                "modality": modality,
                "meta": payload.get("meta", {}),
                "visibility": payload.get("meta", {}).get("visibility", "private"),
                "created_at": payload.get("created_at"),
                "content_hash": _content_hash(str(payload.get("content", "")).encode()),
            }
            raw_b64 = payload.get("raw_base64")
            if raw_b64:
                import base64

                data = base64.b64decode(raw_b64)
                digest = _content_hash(data)
                blobs[digest] = data
                entry["blob"] = digest
                entry["mime"] = payload.get("mime")
            entries.append(entry)

        manifest: dict[str, Any] = {
            "format_version": FORMAT_VERSION,
            "exporter": {
                "agent_id": self._identity.agent_id,
                "public_key": self._identity.public_key_b64(),
                "lineage": self._identity.lineage,
            },
            "exported_at": time.time(),
            "memory_count": len(entries),
            "modality_stats": modality_stats,
            "embedding": {"model": self._embedder_model, "dim": self._embedder_dim},
        }
        manifest["signature"] = self._identity.sign(manifest)
        _write_pack(out_path, manifest, entries, blobs)
        return manifest

    async def import_pack(self, pack_path: Path, entries_override: list[dict] | None = None,
                          extra_meta: dict | None = None) -> int:
        """导入:用当前 Embedder 重算全部向量(换嵌入模型/换脑后无损迁移)。"""
        manifest, entries, blobs = read_pack(pack_path)
        if not verify_manifest(manifest):
            raise LayerError("L7", "memorypack", f"manifest 签名验签失败: {pack_path}")
        if entries_override is not None:
            entries = entries_override
        count = 0
        for e in entries:
            meta = dict(e.get("meta", {}))
            meta.update(extra_meta or {})
            meta.setdefault("imported_from", manifest["exporter"]["agent_id"])
            meta.setdefault("visibility", e.get("visibility", "private"))
            if e.get("blob") and e["blob"] in blobs:
                import base64

                content = MultimodalInput(
                    type=e["modality"], content=base64.b64encode(blobs[e["blob"]]).decode(),
                    mime=e.get("mime"),
                )
                meta.setdefault("caption", e.get("content", ""))
            else:
                content = MultimodalInput.text(e.get("content", ""))
            await self._store.add(content, meta)  # add() 内部经当前 Embedder 重算向量
            count += 1
        return count

    async def merge(self, pack_path: Path) -> dict:
        """合并他人 MemoryPack:同内容哈希去重;矛盾事实并存并标注来源。"""
        manifest, entries, _ = read_pack(pack_path)
        if not verify_manifest(manifest):
            raise LayerError("L7", "memorypack", f"manifest 签名验签失败: {pack_path}")
        existing = await self._store.dump_all()
        existing_hashes = {
            _content_hash(str(p["payload"].get("content", "")).encode()) for p in existing
        }
        fresh = [e for e in entries if e.get("content_hash") not in existing_hashes]
        skipped = len(entries) - len(fresh)
        imported = await self.import_pack(
            pack_path, entries_override=fresh,
            extra_meta={
                "merged_from": manifest["exporter"]["agent_id"],
                "merged_source_time": manifest.get("exported_at"),
            },
        )
        return {"imported": imported, "deduplicated": skipped}

    async def inherit(self, pack_path: Path, llm: "LLMClient") -> dict:
        """遗传:LLM 蒸馏源包(压缩流水账、保留结构化认知)后导入;lineage 追加源 agent_id。"""
        manifest, entries, _ = read_pack(pack_path)
        if not verify_manifest(manifest):
            raise LayerError("L7", "memorypack", f"manifest 签名验签失败: {pack_path}")
        source_agent = manifest["exporter"]["agent_id"]

        corpus = "\n".join(f"- {e.get('content', '')}" for e in entries if e.get("modality") == "text")
        raw = await llm.chat(
            [Message(role="system", content=DISTILL_SYSTEM),
             Message(role="user", content=corpus or "(空)")],
            temperature=0.0,
        )
        cleaned = raw.strip().strip("`")
        cleaned = cleaned[4:] if cleaned.startswith("json") else cleaned
        try:
            distilled = json.loads(cleaned)
            assert isinstance(distilled, list)
        except (json.JSONDecodeError, AssertionError) as exc:
            raise LayerError("L7", "memorypack-inherit", f"蒸馏输出非 JSON 数组: {raw[:200]}") from exc

        distilled_entries = [
            {"content": str(item), "modality": "text",
             "meta": {"kind": "inherited", "distilled": True},
             "visibility": "private",
             "content_hash": _content_hash(str(item).encode())}
            for item in distilled
        ]
        # 多模态记忆不蒸馏,原样继承
        media_entries = [e for e in entries if e.get("modality") != "text"]
        imported = await self.import_pack(
            pack_path, entries_override=distilled_entries + media_entries,
            extra_meta={"inherited_from": source_agent},
        )
        if source_agent not in self._identity.lineage:
            self._identity.lineage.append(source_agent)
        return {"imported": imported, "distilled_from": len(entries),
                "lineage": list(self._identity.lineage)}


# ---------------------------------------------------------------- CLI

def main() -> int:  # pragma: no cover - 组装层,逻辑均有单测
    import argparse
    import asyncio

    from adapters.embedder import build_embedder
    from core.factory import build_llm, get_config, get_identity, get_shared_memory_store

    parser = argparse.ArgumentParser(prog="memorypack")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_exp = sub.add_parser("export")
    p_exp.add_argument("--out", required=True)
    for name in ("import", "merge", "inherit"):
        p = sub.add_parser(name)
        p.add_argument("--pack", required=True)
    args = parser.parse_args()

    cfg = get_config()
    identity = get_identity(cfg)
    store = get_shared_memory_store(cfg)
    mgr = MemoryPackManager(store, identity,
                            cfg.embedder.model_name, cfg.embedder.effective_dim)

    async def run():
        if args.cmd == "export":
            m = await mgr.export(Path(args.out))
            print(json.dumps(m, ensure_ascii=False, indent=2))
        elif args.cmd == "import":
            print(await mgr.import_pack(Path(args.pack)), "memories imported")
        elif args.cmd == "merge":
            print(json.dumps(await mgr.merge(Path(args.pack)), ensure_ascii=False))
        elif args.cmd == "inherit":
            result = await mgr.inherit(Path(args.pack), build_llm(cfg))
            identity.save(cfg.identity.dir)  # 持久化 lineage 变更
            print(json.dumps(result, ensure_ascii=False))

    asyncio.run(run())
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
