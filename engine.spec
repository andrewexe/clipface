# PyInstaller build of the engine, as a folder (not one file: faster start, and one
# process for the desktop app to manage).
#
#   uv run pyinstaller engine.spec --noconfirm
#
# Output: dist/clipper-engine/, which the Tauri bundle ships as the app's "engine" resource.

from PyInstaller.utils.hooks import collect_submodules, copy_metadata

hiddenimports = [
    *collect_submodules("keyring.backends"),  # found through entry points at runtime
    *collect_submodules("uvicorn"),           # loop/protocol implementations loaded by name
    *collect_submodules("alembic"),
    "sqlalchemy.dialects.sqlite",
]

a = Analysis(
    ["scripts/engine_entry.py"],
    pathex=["."],
    datas=[
        # Alembic loads migration scripts from disk, so they ship as files.
        ("engine/db/migrations/env.py", "engine/db/migrations"),
        ("engine/db/migrations/versions/*.py", "engine/db/migrations/versions"),
        *copy_metadata("keyring"),
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
