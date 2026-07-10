# memory-agent 任务入口(BUILD_SPEC §1)
.PHONY: install up up-gpu down run-embed run-api run-mcp test \
        verify-m1 verify-m2 verify-m3 verify-m4 verify-m5 verify-m6 verify-m7 verify-m8 verify fixtures

install:
	uv sync --group dev

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
