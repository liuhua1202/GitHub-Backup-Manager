"""窗口几何状态（位置 + 大小）持久化，跨启动保留。"""
import json
import os
from pathlib import Path
from typing import Any

DEFAULT_W, DEFAULT_H = 1100, 720
MIN_W, MIN_H = 900, 600

# 状态文件：跟 base_dir 同级放 .gbm_ui_state.json（不进仓库，git 友好）
import tempfile


def state_path(base_dir: Path) -> Path:
    """UI state 文件路径：用户配置目录 ~/.github_backup_manager/ui_state.json。"""
    state_dir = Path.home() / ".github_backup_manager"
    try:
        state_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        # 用户主目录不可写时退到临时目录
        state_dir = Path(tempfile.gettempdir()) / "github_backup_manager"
        state_dir.mkdir(parents=True, exist_ok=True)
    return state_dir / "ui_state.json"


def load_window_state(base_dir: Path) -> dict[str, Any]:
    """读取窗口几何状态；缺失或失败返回默认。"""
    path = state_path(base_dir)
    if not path.exists():
        return {"geometry": f"{DEFAULT_W}x{DEFAULT_H}"}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        # sanity check
        geom = str(data.get("geometry", f"{DEFAULT_W}x{DEFAULT_H}"))
        if "x" not in geom or "+" not in geom:
            geom = f"{DEFAULT_W}x{DEFAULT_H}"
        return {"geometry": geom}
    except (OSError, json.JSONDecodeError):
        return {"geometry": f"{DEFAULT_W}x{DEFAULT_H}"}


def save_window_state(base_dir: Path, window) -> None:
    """保存窗口几何状态（geometry 字符串，含位置和大小），原子写。"""
    path = state_path(base_dir)
    try:
        geom = window.winfo_geometry()  # "1100x720+100+50" 形式
        data = {"geometry": geom}
        tmp = path.with_suffix(".json.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, path)
    except OSError:
        pass  # 静默失败，不阻塞关闭
