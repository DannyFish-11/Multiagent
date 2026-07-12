# PyInstaller 打包规格(M31)——把 memory-agent 打成**单个可执行文件**,双击即跑。
# 用:  uv run --with pyinstaller pyinstaller packaging/memory-agent.spec  (在仓库根目录)
# 产物:dist/memory-agent(Linux)/ dist/memory-agent.exe(Windows)。
# 入口 core/launch.py:零配置即 demo 档 + 自动开浏览器。重的本地嵌入依赖(torch 等)排除,
# demo/api 模式不需要;真实大模型走 api(外部 OpenAI 兼容端点),binary 里放个 .env 即可。
import os

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

ROOT = os.path.abspath(os.getcwd())

# 本项目三包 + 动态导入(插件/懒加载)静态分析易漏,整包收集最稳。
hidden = []
for pkg in ("core", "adapters", "services"):
    hidden += collect_submodules(pkg)
hidden += collect_submodules("uvicorn")          # uvicorn 的 loop/protocol 动态选择
hidden += ["anyio", "click", "h11"]

datas = [(os.path.join(ROOT, "config.yaml"), ".")]   # 随 binary 带默认配置(PROJECT_ROOT 找得到)
datas += collect_data_files("qdrant_client")         # 内存向量库(:memory:)所需

a = Analysis(
    [os.path.join(ROOT, "core", "launch.py")],
    pathex=[ROOT],
    binaries=[],
    datas=datas,
    hiddenimports=hidden,
    hookspath=[],
    runtime_hooks=[],
    # 重依赖:本地嵌入/评测/云,不进 binary(demo/api 用不到,省几百 MB)
    excludes=["torch", "transformers", "sentence_transformers", "soundfile",
              "ray", "inspect_ai", "litellm", "opentelemetry"],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="memory-agent",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,                                 # 控制台窗口显示"聊天页 URL / 按 Ctrl+C 停止"
    disable_windowed_traceback=False,
)
