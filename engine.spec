# PyInstaller build of the engine, as a folder (not one file: faster start, and one
# process for the desktop app to manage).
#
#   uv run pyinstaller engine.spec --noconfirm
#
# Output: dist/clipper-engine/, which the Tauri bundle ships as the app's "engine" resource.

from PyInstaller.utils.hooks import collect_data_files, collect_submodules, copy_metadata

hiddenimports = [
    *collect_submodules("keyring.backends"),  # found through entry points at runtime
    *collect_submodules("uvicorn"),           # loop/protocol implementations loaded by name
    *collect_submodules("alembic"),
    "sqlalchemy.dialects.sqlite",
    *collect_submodules("streamlink.plugins"),  # Twitch and Kick plugins, loaded by name
    # mcp.cli needs an optional package and exits when imported without it.
    *collect_submodules("mcp", filter=lambda name: not name.startswith("mcp.cli"), on_error="ignore"),
    *collect_submodules("anthropic", on_error="ignore"),
    "engine.mcp_server",
    "engine.selfcheck",
]

a = Analysis(
    ["scripts/engine_entry.py"],
    pathex=["."],
    datas=[
        # Alembic loads migration scripts from disk, so they ship as files.
        ("engine/db/migrations/env.py", "engine/db/migrations"),
        ("engine/db/migrations/versions/*.py", "engine/db/migrations/versions"),
        ("engine/policy/blocklist.txt", "engine/policy"),
        *copy_metadata("keyring"),
        *copy_metadata("streamlink"),
        *copy_metadata("mcp"),
        *copy_metadata("anthropic"),
        *collect_data_files("streamlink"),
        *collect_data_files("imageio_ffmpeg"),  # the bundled ffmpeg binary
    ],
    hiddenimports=hiddenimports,
    excludes=["tkinter", "pytest"],
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    exclude_binaries=True,
    name="clipper-engine",
    # Console subsystem so stdin/stdout work; the app starts it with no visible window.
    console=True,
    upx=False,
)
coll = COLLECT(exe, a.binaries, a.datas, name="clipper-engine", upx=False)
