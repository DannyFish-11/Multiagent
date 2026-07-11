# memory-agent 任务入口(BUILD_SPEC §1)
.PHONY: install setup quickstart chat plugins doctor up up-gpu down run-embed run-api run-mcp test lint demo \
        verify-m1 verify-m2 verify-m3 verify-m4 verify-m5 verify-m6 verify-m7 verify-m8 verify fixtures

install:
	uv sync --group dev

# M21:列出所有已注册插件(内置 + 第三方 entry_points 发现)
plugins:
	uv run python scripts/list_plugins.py

# 启动前体检:配置/依赖/目录一次看清(有 ❌ 退出非零)
doctor:
	uv run memory-agent doctor

# 首次运行向导:交互式生成 .env(选 LLM/嵌入/预算,密钥只落 .env)
setup:
	uv run python scripts/setup.py

# 一键上手:检查环境 → 向导(若无 .env)→ 起 Docker 全栈 → 等健康检查通过
quickstart:
	./scripts/install.sh

# 终端里和已在跑的 agent 对话(无需 GUI)
chat:
	uv run python scripts/chat.py

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
