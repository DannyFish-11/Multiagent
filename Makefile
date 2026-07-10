# memory-agent 任务入口(BUILD_SPEC §1)
.PHONY: install up up-gpu down run-embed run-api run-mcp test lint demo \
        observability-up observability-down \
        verify-m1 verify-m2 verify-m3 verify-m4 verify-m5 verify-m6 verify-m7 verify-m8 verify fixtures

install:
	uv sync --group dev

# M20 A1:无 key demo(echo LLM + fake 嵌入 + 内存向量库),零密钥/GPU/docker
demo:
	MEMORY_AGENT_LLM__MODE=echo \
	MEMORY_AGENT_EMBEDDER__BACKEND=fake \
	MEMORY_AGENT_VECTORDB__MODE=memory \
	MEMORY_AGENT_MEMORY__EXTRACTION=verbatim \
	uv run python scripts/demo.py

# M20 A3:静态检查(与 test 一并进 CI)
lint:
	uv run ruff check core adapters services scripts

# M20 B:Langfuse 自托管可观测性栈(独立启停,目标机器需开放出网拉镜像)
# --env-file 让 compose 插值从 .env.observability 读取密钥(区别于主 .env)
observability-up:
	docker compose --env-file .env.observability -f docker-compose.observability.yaml up -d

observability-down:
	docker compose --env-file .env.observability -f docker-compose.observability.yaml down

up:
	docker compose up -d qdrant

up-gpu:
	docker compose --profile gpu up -d

down:
	docker compose --profile gpu down

run-embed:
	uv run python -m services.embed_service

run-api:
	uv run python -m services.api

run-mcp:
	uv run python -m services.mcp_server

fixtures:
	uv run python scripts/make_fixtures.py

test:
	uv run pytest -q

# 各里程碑验收(集成用例在对应服务不可达时会显式 SKIP 并注明原因)
verify-m1:
	uv run pytest tests/test_m1_llm.py -v

verify-m2:
	uv run pytest tests/test_m2_embed.py -v

verify-m3:
	uv run pytest tests/test_m3_memory.py -v

verify-m4:
	uv run pytest tests/test_m4_omnigent.py -v

verify-m5:
	uv run pytest tests/test_m5_identity.py -v

verify-m6:
	uv run pytest tests/test_m6_gmail.py -v

verify-m7:
	uv run pytest tests/test_m7_memorypack.py -v

verify-m8:
	uv run pytest tests/test_m8_metabolism.py -v

verify: verify-m1 verify-m2 verify-m3 verify-m4 verify-m5 verify-m6 verify-m7 verify-m8
	uv run pytest tests/test_e2e_scenario.py -v
