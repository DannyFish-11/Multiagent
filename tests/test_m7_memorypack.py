"""Milestone 7 验收:MemoryPack 导出/导入/合并/遗传。"""

from __future__ import annotations

from pathlib import Path

import pytest

from adapters.embedder import build_embedder
from adapters.memory import QdrantMemoryStore
from adapters.vectordb import QdrantAdapter
from core.errors import LayerError
from core.identity import AgentIdentity
from core.memorypack import MemoryPackManager, read_pack, verify_manifest
from core.schemas import MultimodalInput
from tests.conftest import FIXTURES, ScriptedLLM, make_fake_config


def build_store(cfg, dim: int = 64):
    embedder = build_embedder(cfg.embedder)
    db = QdrantAdapter(cfg.vectordb, dim=dim)
    return QdrantMemoryStore(embedder, ScriptedLLM(), db, cfg)


def make_manager(store, tmp_path, name="a", model="fake-deterministic", dim=64):
    ident = AgentIdentity.load_or_create(Path(tmp_path) / f"identity-{name}")
    return MemoryPackManager(store, ident, model, dim), ident


async def seed(store):
    await store.add(MultimodalInput.text("用户的猫叫 Benjamin"), {})
    await store.add(MultimodalInput.text("用户喜欢喝手冲咖啡"), {})
    img = MultimodalInput.from_file(FIXTURES / "white_cat.png", "image", "image/png")
    await store.add(img, {"caption": "一只白色的猫"})


# ---------------------------------------------------------------- export

async def test_export_manifest_and_structure(tmp_path):
    cfg = make_fake_config(tmp_path)
    store = build_store(cfg)
    await seed(store)
    mgr, ident = make_manager(store, tmp_path)

    pack = tmp_path / "out" / "pack.tar.zst"
    manifest = await mgr.export(pack)

    assert pack.exists()
    m2, entries, blobs = read_pack(pack)
    assert verify_manifest(m2), "manifest 签名必须可验"
    assert m2["exporter"]["agent_id"] == ident.agent_id
    assert m2["memory_count"] == len(entries) == 3  # 2 条文本 + 1 图像(caption 派生点不导出)
    assert m2["embedding"] == {"model": "fake-deterministic", "dim": 64}
    assert m2["modality_stats"] == {"text": 2, "image": 1}
    # 条目不含向量;blob 以内容哈希命名
    assert all("vector" not in e for e in entries)
    assert len(blobs) == 1
    del manifest


async def test_manifest_tamper_detected(tmp_path):
    cfg = make_fake_config(tmp_path)
    store = build_store(cfg)
    await seed(store)
    mgr, _ = make_manager(store, tmp_path)
    pack = tmp_path / "pack.tar.zst"
    await mgr.export(pack)
    manifest, _, _ = read_pack(pack)
    manifest["memory_count"] = 9999  # 篡改
    assert not verify_manifest(manifest)


# ---------------------------------------------------------------- import(重算向量)

async def test_import_reembeds_with_current_embedder(tmp_path):
    cfg = make_fake_config(tmp_path)
    src_store = build_store(cfg)
    await seed(src_store)
    mgr, _ = make_manager(src_store, tmp_path, "src")
    pack = tmp_path / "pack.tar.zst"
    await mgr.export(pack)

    # 新实例换了嵌入维度(模拟换嵌入模型/换脑)—— 导入方用当前 Embedder 重算
    cfg2 = make_fake_config(tmp_path)
    cfg2.embedder.dim = 32
    dst_store = build_store(cfg2, dim=32)
    mgr2, _ = make_manager(dst_store, tmp_path, "dst", dim=32)
    count = await mgr2.import_pack(pack)
    assert count == 3

    hits = await dst_store.search(MultimodalInput.text("我的猫叫什么"), k=3)
    assert hits and "Benjamin" in hits[0].content
    # 跨模态记忆(blob)也完整迁移
    img_hits = [h for h in await dst_store.search(MultimodalInput.text("白色的猫"), k=5)
                if h.modality == "image"]
    assert img_hits, "图像记忆未随包迁移"


# ---------------------------------------------------------------- merge(去重+并存)

async def test_merge_dedup_and_conflict_coexist(tmp_path):
    cfg = make_fake_config(tmp_path)
    store_a = build_store(cfg)
    await store_a.add(MultimodalInput.text("用户的猫叫 Benjamin"), {})
    await store_a.add(MultimodalInput.text("用户住在上海"), {})

    cfg_b = make_fake_config(tmp_path)
    store_b = build_store(cfg_b)
    await store_b.add(MultimodalInput.text("用户的猫叫 Benjamin"), {})   # 与 A 完全相同 → 去重
    await store_b.add(MultimodalInput.text("用户住在北京"), {})          # 矛盾事实 → 并存+标注
    mgr_b, ident_b = make_manager(store_b, tmp_path, "b")
    pack_b = tmp_path / "b.tar.zst"
    await mgr_b.export(pack_b)

    mgr_a, _ = make_manager(store_a, tmp_path, "a")
    report = await mgr_a.merge(pack_b)
    assert report["deduplicated"] == 1
    assert report["imported"] == 1

    hits = await store_a.search(MultimodalInput.text("用户住在哪里"), k=5)
    contents = {h.content for h in hits}
    assert "用户住在上海" in contents and "用户住在北京" in contents, "矛盾事实应并存"
    beijing = next(h for h in hits if h.content == "用户住在北京")
    assert beijing.meta.get("merged_from") == ident_b.agent_id, "并存事实须标注来源 agent_id"
    assert beijing.meta.get("merged_source_time"), "并存事实须标注来源时间"


# ---------------------------------------------------------------- inherit(蒸馏遗传)

async def test_inherit_distills_and_appends_lineage(tmp_path):
    cfg = make_fake_config(tmp_path)
    parent_store = build_store(cfg)
    await parent_store.add(MultimodalInput.text("3 月 1 日买了猫粮"), {})
    await parent_store.add(MultimodalInput.text("3 月 8 日又买了猫粮"), {})
    await parent_store.add(MultimodalInput.text("用户的猫叫 Benjamin"), {})
    parent_mgr, parent_ident = make_manager(parent_store, tmp_path, "parent")
    pack = tmp_path / "parent.tar.zst"
    await parent_mgr.export(pack)

    child_store = build_store(make_fake_config(tmp_path))
    child_mgr, child_ident = make_manager(child_store, tmp_path, "child")
    distiller = ScriptedLLM(replies=['["用户的猫叫 Benjamin", "用户定期为猫购买猫粮(约每周一次)"]'])
    result = await child_mgr.inherit(pack, distiller)

    # lineage 追加源 agent_id(为拟生学代际机制打地基)
    assert parent_ident.agent_id in child_ident.lineage
    assert result["lineage"] == child_ident.lineage
    # 蒸馏后的结构化认知入库,而非流水账原文
    hits = await child_store.search(MultimodalInput.text("猫粮"), k=5)
    contents = {h.content for h in hits}
    assert "用户定期为猫购买猫粮(约每周一次)" in contents
    assert "3 月 1 日买了猫粮" not in contents, "流水账应被蒸馏压缩"
    inherited = next(iter(hits))
    assert inherited.meta.get("inherited_from") == parent_ident.agent_id


async def test_import_rejects_bad_signature(tmp_path):
    """负向:manifest 验签失败的包必须拒绝导入。"""
    import json
    import tarfile
    import io
    import zstandard

    cfg = make_fake_config(tmp_path)
    store = build_store(cfg)
    await seed(store)
    mgr, _ = make_manager(store, tmp_path)
    pack = tmp_path / "pack.tar.zst"
    await mgr.export(pack)

    # 重打包并篡改 manifest
    raw = zstandard.ZstdDecompressor().decompress(pack.read_bytes(), max_output_size=1 << 30)
    out = io.BytesIO()
    with tarfile.open(fileobj=io.BytesIO(raw)) as src, tarfile.open(fileobj=out, mode="w") as dst:
        for m in src.getmembers():
            data = src.extractfile(m).read()
            if m.name == "manifest.json":
                mani = json.loads(data)
                mani["memory_count"] = 1
                data = json.dumps(mani).encode()
            info = tarfile.TarInfo(m.name)
            info.size = len(data)
            dst.addfile(info, io.BytesIO(data))
    bad_pack = tmp_path / "bad.tar.zst"
    bad_pack.write_bytes(zstandard.ZstdCompressor().compress(out.getvalue()))

    with pytest.raises(LayerError) as exc:
        await mgr.import_pack(bad_pack)
    assert "验签失败" in str(exc.value)


async def test_import_rejects_tampered_entries(tmp_path):
    """负向:篡改 memories.jsonl(manifest 原样)必须被摘要校验拒绝——防记忆投毒。"""
    import io
    import tarfile
    import zstandard

    cfg = make_fake_config(tmp_path)
    store = build_store(cfg)
    await seed(store)
    mgr, _ = make_manager(store, tmp_path)
    pack = tmp_path / "pack.tar.zst"
    await mgr.export(pack)

    raw = zstandard.ZstdDecompressor().decompress(pack.read_bytes(), max_output_size=1 << 30)
    out = io.BytesIO()
    with tarfile.open(fileobj=io.BytesIO(raw)) as src, tarfile.open(fileobj=out, mode="w") as dst:
        for m in src.getmembers():
            data = src.extractfile(m).read()
            if m.name == "memories.jsonl":
                data = data.replace("Benjamin".encode(), "EVIL_INJECT".encode())
            info = tarfile.TarInfo(m.name)
            info.size = len(data)
            dst.addfile(info, io.BytesIO(data))
    evil = tmp_path / "evil.tar.zst"
    evil.write_bytes(zstandard.ZstdCompressor().compress(out.getvalue()))

    with pytest.raises(LayerError) as exc:
        read_pack(evil)
    assert "篡改" in str(exc.value)
