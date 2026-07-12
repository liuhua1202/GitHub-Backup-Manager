#!/usr/bin/env python3
"""
GitHub Backup Manager - 桌面应用版
===================================
基于 ttkbootstrap 的现代化桌面 GUI，管理 GitHub 仓库的定期镜像备份。

启动: python app.py
"""

from __future__ import annotations

__version__ = "1.2.1"
__author__ = "liuhua"
__license__ = "MIT"

import os
import sys
import json
import shutil   # 磁盘空间预检（_on_backup_all）
import queue
import threading
import datetime
import subprocess
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from tkinter.constants import PIESLICE  # Canvas 圆角辅助用
from pathlib import Path
from typing import Any

import logging as _logging
import ttkbootstrap as ttkb
from ttkbootstrap.constants import *
from ttkbootstrap.widgets.scrolled import ScrolledText
from apscheduler.schedulers.background import BackgroundScheduler

# 导入备份引擎
from backup_engine import (
    BASE_DIR, LOG_FILE,
    DEFAULT_CONFIG, DEFAULT_STATE,
    load_config, save_config,
    load_state, save_state,
    log_buffer, add_log,
    LOG_SUCCESS,
    mask_token, format_size, get_dir_size, get_disk_free,
    check_git, verify_token,
    get_all_repos, github_api_request,
    run_full_backup, backup_single_repo, refresh_repos_list,
    get_schedule_trigger, validate_cron,
    parse_extra_repos, run_git_command,
)
from window_state import load_window_state, save_window_state


def _get_text_widget(scrolled_text) -> "tk.Text":
    """获取 ScrolledText 内部的 Text 组件（用于插入 / 配置 / 状态切换）。

    ── 多个类（BackupApp、RepoFileManager）共用，所以放模块级 ──
    """
    for child in scrolled_text.winfo_children():
        if child.winfo_class() == "Text":
            return child
    return scrolled_text


def _clamp_int(value: Any, default: int, lo: int, hi: int) -> int:
    """鲁棒地拿到一个合法 int：用 default 兜底，clamp 到 [lo, hi]。

    旧版 config.json 里新加的字段可能是 None（老版本写入了 key 占位但值是 None），
    或者字符串 / 浮点。用户切到 yearly / monthly 后存盘 → start_scheduler 用这些值
    调 CronTrigger(month='0', day='0', ...) → APScheduler 抛 ValueError → 保存流程
    中断，messagebox.showinfo 没显示 → 看上去「闪退」。

    修法：在 UI 读取和保存两处都用这个 helper 把 None / 越界值规范化。
    """
    try:
        n = int(value)
    except (TypeError, ValueError):
        return default
    if n < lo:
        return lo
    if n > hi:
        return hi
    return n


# ──────────────────────────────────────────────────────────────────────
# 全局调度器
# ──────────────────────────────────────────────────────────────────────

_scheduler: BackgroundScheduler | None = None
_scheduler_lock = threading.Lock()


def start_scheduler(config: dict[str, Any]) -> bool:
    """启动或重启后台调度器。

    返回 True 表示成功启动；False 表示已暂停 / 启动失败（保持关闭状态）。

    修法：旧版 config.json 里新加的字段（monthly_day_of_month / yearly_*）可能是 None，
    int(None) → TypeError 直接拖垮整个启动。这里用 _clamp_int 把 None / 越界值兜底；
    万一 APScheduler 自身仍抛 ValueError（极端无效 cron 等），catch 后记 log、降级不启动，
    不再让上层 UI 闪退。
    """
    global _scheduler
    with _scheduler_lock:
        if _scheduler is not None:
            try:
                _scheduler.shutdown(wait=False)
            except Exception:
                pass
            _scheduler = None

        schedule_type = config.get("schedule_type", "daily")
        cron_expr = config.get("cron_expression", "0 2 * * *")
        daily_hour = _clamp_int(config.get("daily_hour"), 0, 0, 23) or 2
        daily_minute = _clamp_int(config.get("daily_minute"), 0, 0, 59)
        weekly_dow = str(config.get("weekly_day_of_week") or "sun")
        monthly_dom = _clamp_int(config.get("monthly_day_of_month"), 1, 1, 31)
        yearly_month = _clamp_int(config.get("yearly_month"), 1, 1, 12)
        yearly_dom = _clamp_int(config.get("yearly_day_of_month"), 1, 1, 31)
        paused = bool(config.get("scheduler_paused", False))

        try:
            trigger = get_schedule_trigger(
                schedule_type, cron_expr,
                daily_hour=daily_hour, daily_minute=daily_minute,
                weekly_day_of_week=weekly_dow,
                monthly_day_of_month=monthly_dom,
                yearly_month=yearly_month, yearly_day_of_month=yearly_dom,
                scheduler_paused=paused,
            )
        except Exception as e:
            # 极端情况：cron 字段损坏 / APScheduler 拒绝。降级到 daily 不再让 UI 崩
            add_log(f"调度器启动失败：{type(e).__name__}: {e} — 降级为 daily",
                    _logging.ERROR)
            try:
                trigger = get_schedule_trigger("daily", "",
                                               daily_hour=2, daily_minute=0)
            except Exception:
                return False

        if trigger is None:
            # 暂停状态：调度器被关闭，不启动
            add_log("调度器已暂停（scheduler_paused=True）", _logging.WARNING)
            return False

        _scheduler = BackgroundScheduler(daemon=True)

        def backup_job():
            cfg = load_config()
            st = load_state()
            run_full_backup(cfg, st)

        _scheduler.add_job(backup_job, trigger=trigger, id="scheduled_backup")
        _scheduler.start()
        summary = f"调度器已启动 (类型: {schedule_type}"
        if schedule_type in ("daily", "weekly", "monthly", "yearly"):
            summary += f", {daily_hour:02d}:{daily_minute:02d}"
            if schedule_type == "weekly":
                summary += f", {weekly_dow}"
            elif schedule_type == "monthly":
                summary += f", 每月 {monthly_dom} 号"
            elif schedule_type == "yearly":
                summary += f", 每年 {yearly_month} 月 {yearly_dom} 号"
        else:
            summary += f", cron: {cron_expr}"
        summary += ")"
        add_log(summary)
        return True


def stop_scheduler() -> None:
    """停止调度器。"""
    global _scheduler
    with _scheduler_lock:
        if _scheduler is not None:
            try:
                _scheduler.shutdown(wait=False)
            except Exception:
                pass
            _scheduler = None


def get_next_run_time() -> str:
    """取得调度器下一次触发的本地化时间字符串。无任务时返回 '无调度'。"""
    with _scheduler_lock:
        sched = _scheduler
    if sched is None:
        return "无调度"
    try:
        job = sched.get_job("scheduled_backup")
    except Exception:
        job = None
    if job is None or job.next_run_time is None:
        return "无调度"
    return job.next_run_time.strftime("%Y-%m-%d %H:%M")


def system_notify(title: str, message: str) -> bool:
    """跨平台系统通知。

    Windows: 调用 PowerShell 用 WinRT ToastNotificationManager 弹 toast（无需第三方包）
    macOS / Linux: 暂不实现 —— 留给后续（osascript / notify-send）

    失败全部静默，不阻塞调用方。
    """
    if sys.platform == "win32":
        # PowerShell 用单引号边界，内部字符串里的单引号要 escape 成两个
        def esc(s: str) -> str:
            return s.replace("'", "''")

        # ToastText02 模板：标题 + 正文两行
        ps = (
            "[Windows.UI.Notifications.ToastNotificationManager, "
            "Windows.UI.Notifications, ContentType=WindowsRuntime] | Out-Null; "
            "$t = [Windows.UI.Notifications.ToastNotificationManager]"
            "::GetTemplateContent([Windows.UI.Notifications.ToastTemplateType]::ToastText02); "
            "$ns = $t.GetElementsByTagName('text'); "
            f"$ns.Item(0).AppendChild($t.CreateTextNode('{esc(title)}')) | Out-Null; "
            f"$ns.Item(1).AppendChild($t.CreateTextNode('{esc(message)}')) | Out-Null; "
            "$x = [Windows.UI.Notifications.ToastNotification]::new($t); "
            "[Windows.UI.Notifications.ToastNotificationManager]"
            "::CreateToastNotifier('GitHub Backup Manager').Show($x)"
        )
        try:
            r = subprocess.run(
                ["powershell", "-NoProfile", "-Command", ps],
                capture_output=True, timeout=10, text=True,
            )
            return r.returncode == 0
        except Exception:
            return False
    return False  # macOS / Linux no-op


# ════════════════════════════════════════════════════════════════════
# 工具函数
# ════════════════════════════════════════════════════════════════════

def set_light_titlebar(window) -> None:
    """Windows 下设置窗口标题栏为浅色模式。"""
    if sys.platform != "win32":
        return
    try:
        import ctypes
        from ctypes import wintypes

        DWMWA_USE_IMMERSIVE_DARK_MODE = 20
        hwnd = wintypes.HWND(window.winfo_id())
        # 0 = 浅色模式, 1 = 深色模式
        value = wintypes.BOOL(0)
        ctypes.windll.dwmapi.DwmSetWindowAttribute(
            hwnd,
            DWMWA_USE_IMMERSIVE_DARK_MODE,
            ctypes.byref(value),
            ctypes.sizeof(value)
        )
    except Exception:
        pass  # 非 Windows 或 API 调用失败则忽略


def apply_icon(window) -> None:
    """设置应用图标（窗口装饰 + Windows 任务栏）。

    单一资产来源：logo.ico (含 256/128/64/48/32/16 多分辨率)

    三层覆盖，按强度递进：
    1. iconphoto —— 跨平台标准做法（macOS / Linux 唯一靠谱；Windows 也兼容）
    2. iconbitmap —— Tk 内置 Windows 任务栏兜底
    3. Win32 API 直接 LoadImage + WM_SETICON + SetClassLongPtr
       —— Tk 的 iconbitmap 在 Win10/11 上有已知坑：只设 ICON_SMALL
       (16×16)，任务栏用的 ICON_BIG (32×32) 仍走 WNDCLASSEX，遇上
       Windows icon cache 残留旧图标就会出怪事。这里用 Win32 API
       强制把 16/32 两份 HICON 写到窗口 + 窗口类，绕开 Tk 与 icon cache。

    Tk 在 Toplevel 第一次 WM_PAINT 之后会重新设置 WNDCLASSEX.HICON 为
    Tk 默认图标（这是 Tk 维护 class 资源生命周期的副作用）—— 我们用
    `after_idle` + 短延迟再设一次，确保 class icon 是我们的 octocat。

    Pillow 缺失时整段静默失败——窗口用 Tk 默认图标，不阻塞启动。
    Win32 调用失败也不阻塞启动（iconbitmap / iconphoto 仍生效）。
    """
    logo_ico = BASE_DIR / "logo.ico"
    if not logo_ico.exists():
        return

    # ── 跨平台：PIL → ImageTk.PhotoImage → iconphoto ──
    try:
        from PIL import Image, ImageTk
        pil_img = Image.open(str(logo_ico))
        if pil_img.mode == "RGBA":
            bg = Image.new("RGB", pil_img.size, (255, 255, 255))
            bg.paste(pil_img, mask=pil_img.split()[3])
            pil_img = bg
        elif pil_img.mode != "RGB":
            pil_img = pil_img.convert("RGB")
        window.iconphoto(False, ImageTk.PhotoImage(pil_img))
    except Exception as e:
        print(f"[apply_icon] iconphoto 失败: {e}", file=sys.stderr)

    # ── Windows 任务栏：iconbitmap（标题栏 / grouped taskbar） + Win32 强制 ──
    if sys.platform == "win32":
        try:
            window.iconbitmap(str(logo_ico))
        except Exception as e:
            print(f"[apply_icon] iconbitmap 失败: {e}", file=sys.stderr)

        try:
            _apply_icon_win32(window, str(logo_ico))
        except Exception as e:
            print(f"[apply_icon] win32 强制失败: {e}", file=sys.stderr)

        # Tk 在 Toplevel 首次 paint 后会用 SetClassLongPtr 把 class icon
        # 改回 Tk 默认（每次新 Toplevel 创建都会发生）—— 用 after() 链
        # 持续重设 60 秒，确保 WNDCLASSEX 最终锁定到 octocat。
        # 实测：7 次（5 秒）的 retry 会被 Tk 在第 6 秒改回去，需要 ≥60s 覆盖
        # 任何 Toplevel 创建时机。
        def _retry():
            try:
                _apply_icon_win32(window, str(logo_ico))
            except Exception:
                pass
        try:
            # 60 秒内 12 次重试，覆盖 Tk 的所有 class icon 重设时机
            for ms in (50, 150, 300, 600, 1200, 2500, 5000, 10000, 20000, 35000, 50000, 60000):
                window.after(ms, _retry)
        except Exception:
            pass


def _apply_icon_win32(window, ico_path: str) -> None:
    """Windows 任务栏图标强制覆盖：Win32 API 直接 LoadImage + WM_SETICON +
    SetClassLongPtr，绕开 Tk 内置 iconbitmap 在 Win10/11 上的坑 + icon cache。

    - LoadImageW(LR_LOADFROMFILE): 从 .ico 直接加载 16/32 两个 HICON
    - WM_SETICON (ICON_SMALL/BIG): 设置窗口实例图标（标题栏 + grouped 任务栏）
    - SetClassLongPtrW(GCLP_HICON/HICONSM): 设置窗口类图标
      （ungrouped 任务栏用这个；任务栏按钮的『主图标』也走这个）
    """
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)

    LR_LOADFROMFILE = 0x00000010
    IMAGE_ICON = 1
    WM_SETICON = 0x0080
    ICON_BIG = 1
    ICON_SMALL = 0
    GCLP_HICON = -14
    GCLP_HICONSM = -34

    try:
        # 关键：`winfo_id()` 返回的是 Tk 内部子窗口的 HWND（class=TkChild），
        # 对它调 SetClassLongPtr 会 ERROR_INVALID_HANDLE。要拿真正的
        # top-level frame HWND（class=TkTopLevel）必须用 wm frame。
        # Tk 9.x 之前 wm frame 返回十进制字符串；9.x+ 加了 0x 前缀。
        try:
            frame_id = window.tk.call("wm", "frame", window._w)
        except Exception:
            frame_id = window.winfo_id()
        s = str(frame_id).strip()
        if s.startswith(("0x", "0X")):
            hwnd_int = int(s, 16)
        else:
            hwnd_int = int(s)
        hwnd = wintypes.HWND(hwnd_int)
    except Exception:
        return

    # 验证 class 是 TkTopLevel（不是 TkChild 之类），不是的话直接返回
    user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPCWSTR, ctypes.c_int]
    user32.GetClassNameW.restype = ctypes.c_int
    class_name = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(hwnd, class_name, 256)
    if class_name.value != "TkTopLevel":
        return

    # ── 配 ctypes 签名（64-bit 上不配会 crash 在 stdcall 栈错位） ──
    LoadImageW = user32.LoadImageW
    LoadImageW.argtypes = [
        wintypes.HINSTANCE, wintypes.LPCWSTR, wintypes.UINT,
        ctypes.c_int, ctypes.c_int, wintypes.UINT,
    ]
    LoadImageW.restype = wintypes.HANDLE

    SendMessageW = user32.SendMessageW
    SendMessageW.argtypes = [wintypes.HWND, wintypes.UINT,
                             wintypes.WPARAM, wintypes.LPARAM]
    SendMessageW.restype = ctypes.c_ssize_t  # LRESULT = signed size_t, wintypes 没提供

    SetClassLongPtrW = user32.SetClassLongPtrW
    SetClassLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_ssize_t]
    SetClassLongPtrW.restype = ctypes.c_ssize_t  # LONG_PTR = pointer-sized signed int

    # ── 加载 16/32 两个 HICON ──
    hicon_small = LoadImageW(
        None, ico_path, IMAGE_ICON, 16, 16, LR_LOADFROMFILE,
    )
    hicon_big = LoadImageW(
        None, ico_path, IMAGE_ICON, 32, 32, LR_LOADFROMFILE,
    )

    if hicon_small:
        SendMessageW(hwnd, WM_SETICON, ICON_SMALL, hicon_small)
    if hicon_big:
        SendMessageW(hwnd, WM_SETICON, ICON_BIG, hicon_big)

    # ── 改写 WNDCLASSEX，让任务栏主图标也跟上 ──
    if hicon_big:
        try:
            SetClassLongPtrW(hwnd, GCLP_HICON, hicon_big)
        except Exception:
            pass
    if hicon_small:
        try:
            SetClassLongPtrW(hwnd, GCLP_HICONSM, hicon_small)
        except Exception:
            pass


# ════════════════════════════════════════════════════════════════════
# 浅色系配色方案
# ════════════════════════════════════════════════════════════════════

class Palette:
    """统一的浅色系配色方案，提升视觉识别度。"""

    # 背景色系
    BG_APP      = "#f5f7fa"   # 应用主背景
    BG_CARD     = "#ffffff"   # 卡片背景
    BG_HOVER    = "#eef2f7"   # 悬停背景
    BG_STRIPE   = "#f8fafc"   # 表格隔行

    # 文字色系
    TEXT_PRIMARY   = "#1e293b"  # 主文字
    TEXT_SECONDARY = "#64748b"  # 次要文字
    TEXT_MUTED     = "#94a3b8"  # 弱化文字

    # 语义色系
    PRIMARY  = "#2563eb"   # 主色（蓝）
    SUCCESS  = "#16a34a"   # 成功（绿）
    DANGER   = "#dc2626"   # 危险（红）
    WARNING  = "#f59e0b"   # 警告（橙）
    INFO     = "#0891b2"   # 信息（青）

    # 语义色 — 浅底（Tailwind 100，仪表盘卡背景用）
    PRIMARY_LIGHT = "#dbeafe"  # blue-100
    SUCCESS_LIGHT = "#dcfce7"  # green-100
    DANGER_LIGHT  = "#fee2e2"  # red-100
    INFO_LIGHT    = "#cffafe"  # cyan-100

    # 边框
    BORDER      = "#cbd5e1"   # slate-300：仪表盘卡片描边
    BORDER_LIGHT = "#e2e8f0"  # 浅一档，给 Treeview 等细线用

    # Treeview 状态色
    TV_SUCCESS = "#16a34a"
    TV_FAILED  = "#dc2626"
    TV_PENDING = "#94a3b8"

    # 卡片强调色（左侧色条）
    ACCENTS = [PRIMARY, SUCCESS, DANGER, INFO]


# ════════════════════════════════════════════════════════════════════
# 首次配置向导对话框
# ════════════════════════════════════════════════════════════════════

class SetupDialog(ttkb.Toplevel):
    """首次使用配置向导对话框。"""

    def __init__(self, parent, config: dict[str, Any], git_ok: bool, git_version: str):
        super().__init__(parent)
        self.title("初始配置向导")
        self.geometry("560x720")
        self.resizable(False, False)
        self.grab_set()  # 模态

        # 应用图标（标题栏左上角 + 任务栏）—— 不调就用 Tk 默认羽毛笔
        apply_icon(self)
        # Windows 下设置标题栏为浅色模式
        set_light_titlebar(self)

        self.config_data = dict(config)
        self.git_ok = git_ok
        self.git_version = git_version
        self.result: dict[str, Any] | None = None

        self._build_ui()

    def _build_ui(self) -> None:
        container = ttkb.Frame(self, padding=30)
        container.pack(fill=BOTH, expand=YES)

        # 标题
        ttkb.Label(
            container, text="GitHub Backup Manager",
            font=("Segoe UI", 18, "bold"), bootstyle=PRIMARY,
        ).pack(anchor=W, pady=(0, 5))
        ttkb.Label(
            container, text="欢迎使用！请完成以下配置以开始备份。",
            font=("Segoe UI", 10), bootstyle=SECONDARY,
        ).pack(anchor=W, pady=(0, 20))

        # Git 状态
        if not self.git_ok:
            ttkb.Label(
                container,
                text="⚠ 未检测到 Git！请先安装 Git：apt install git / brew install git",
                bootstyle=DANGER,
                wraplength=480,
            ).pack(fill=X, pady=(0, 15))

        # ── 表单 ──
        form = ttkb.Frame(container)
        form.pack(fill=X)

        # 目标
        ttkb.Label(form, text="GitHub 用户名或组织名", font=("Segoe UI", 10, "bold")).pack(anchor=W)
        self.target_var = tk.StringVar(value=self.config_data.get("target", ""))
        ttkb.Entry(form, textvariable=self.target_var, bootstyle=PRIMARY).pack(fill=X, pady=(2, 12))

        # Token
        ttkb.Label(form, text="GitHub Personal Access Token", font=("Segoe UI", 10, "bold")).pack(anchor=W)
        token_frame = ttkb.Frame(form)
        token_frame.pack(fill=X, pady=(2, 2))
        self.token_var = tk.StringVar(value=self.config_data.get("token", ""))
        self.token_entry = ttkb.Entry(token_frame, textvariable=self.token_var, show="*", bootstyle=PRIMARY)
        self.token_entry.pack(side=LEFT, fill=X, expand=YES)
        ttkb.Button(token_frame, text="👁", width=4, command=self._toggle_token, bootstyle=SECONDARY).pack(side=LEFT, padx=(5, 0))
        ttkb.Button(token_frame, text="验证", width=6, command=self._verify_token, bootstyle=INFO).pack(side=LEFT, padx=(5, 0))
        self.token_result_var = tk.StringVar()
        ttkb.Label(form, textvariable=self.token_result_var, font=("Segoe UI", 9)).pack(anchor=W, pady=(2, 4))
        ttkb.Label(
            form,
            text="Token 需要 repo 权限。在 GitHub Settings → Tokens 创建。仅存储在本地。",
            font=("Segoe UI", 8), bootstyle=SECONDARY, wraplength=480,
        ).pack(anchor=W, pady=(0, 12))

        # 备份目录
        ttkb.Label(form, text="备份存储根目录", font=("Segoe UI", 10, "bold")).pack(anchor=W)
        dir_frame = ttkb.Frame(form)
        dir_frame.pack(fill=X, pady=(2, 12))
        self.dir_var = tk.StringVar(value=self.config_data.get("backup_dir", str(Path.home() / "github-backups")))
        ttkb.Entry(dir_frame, textvariable=self.dir_var, bootstyle=PRIMARY).pack(side=LEFT, fill=X, expand=YES)
        ttkb.Button(dir_frame, text="浏览...", command=self._browse_dir, bootstyle=SECONDARY).pack(side=LEFT, padx=(5, 0))

        # 备份模式
        ttkb.Label(form, text="备份模式", font=("Segoe UI", 10, "bold")).pack(anchor=W)
        mode_frame = ttkb.Frame(form)
        mode_frame.pack(fill=X, pady=(2, 8))
        self.clone_mode_var = tk.StringVar(value=self.config_data.get("clone_mode", "mirror"))
        ttkb.Radiobutton(mode_frame, text="镜像模式 (git clone --mirror) — 完整备份所有引用",
                         variable=self.clone_mode_var, value="mirror", bootstyle=PRIMARY).pack(anchor=W)
        ttkb.Radiobutton(mode_frame, text="普通模式 (git clone) — 含工作目录，可直接浏览文件",
                         variable=self.clone_mode_var, value="normal", bootstyle=PRIMARY).pack(anchor=W)

        # 频率
        ttkb.Label(form, text="备份频率", font=("Segoe UI", 10, "bold")).pack(anchor=W)
        freq_frame = ttkb.Frame(form)
        freq_frame.pack(fill=X, pady=(2, 8))
        self.sched_var = tk.StringVar(value=self.config_data.get("schedule_type", "daily"))
        # 3 个选项：每天 / 每周 / 每月。yearly / custom 已删（旧 config 仍可读，schedule 层面保留分支）。
        for text, val in [
            ("每天", "daily"),
            ("每周", "weekly"),
            ("每月", "monthly"),
        ]:
            ttkb.Radiobutton(
                freq_frame, text=text, variable=self.sched_var,
                value=val, bootstyle=PRIMARY, command=self._toggle_cron,
            ).pack(side=LEFT, padx=(0, 12))

        # 同一行：执行时间 + 右边的 selector
        # - daily:   只显示 HH:MM
        # - weekly:  HH:MM + 周几 Combobox（同行右侧）
        # - monthly: HH:MM + 几号 Combobox（同行右侧）
        time_row = ttkb.Frame(form)
        time_row.pack(fill=X, pady=(0, 12))
        ttkb.Label(time_row, text="执行时间：", font=("Segoe UI", 10)).pack(side=LEFT)
        self.hour_var = tk.IntVar(value=_clamp_int(self.config_data.get("daily_hour"), 2, 0, 23))
        self.minute_var = tk.IntVar(value=_clamp_int(self.config_data.get("daily_minute"), 0, 0, 59))
        ttkb.Spinbox(time_row, from_=0, to=23, textvariable=self.hour_var,
                     width=4, bootstyle=PRIMARY, format="%02.0f").pack(side=LEFT, padx=(4, 2))
        ttkb.Label(time_row, text=":", font=("Segoe UI", 11, "bold")).pack(side=LEFT)
        ttkb.Spinbox(time_row, from_=0, to=59, textvariable=self.minute_var,
                     width=4, bootstyle=PRIMARY, format="%02.0f").pack(side=LEFT, padx=(2, 16))

        # 同行右侧的 selector — start with hidden, _toggle_cron reveals appropriate one
        # 周几 Combobox（weekly 时显示）
        self.dow_label = ttkb.Label(time_row, text="周几：")
        self.dow_var = tk.StringVar(value=self.config_data.get("weekly_day_of_week", "sun") or "sun")
        self.dow_combo = ttkb.Combobox(time_row, textvariable=self.dow_var,
                                       values=["mon", "tue", "wed", "thu", "fri", "sat", "sun"],
                                       width=6, state="readonly", bootstyle=PRIMARY)

        # 几号 Combobox（monthly 时显示）—— 用 "1"..."31" 字符串下拉
        self.dom_label = ttkb.Label(time_row, text="几号：")
        self.monthly_dom_var = tk.StringVar(
            value=str(_clamp_int(self.config_data.get("monthly_day_of_month"), 1, 1, 31))
        )
        self.dom_combo = ttkb.Combobox(
            time_row, textvariable=self.monthly_dom_var,
            values=[str(i) for i in range(1, 32)],
            width=4, state="readonly", bootstyle=PRIMARY,
        )

        self._toggle_cron()

        # 重试配置
        retry_frame = ttkb.Frame(form)
        retry_frame.pack(fill=X, pady=(0, 15))
        ttkb.Label(retry_frame, text="重试次数", font=("Segoe UI", 10, "bold")).grid(row=0, column=0, sticky=W, padx=(0, 20))
        ttkb.Label(retry_frame, text="基础重试间隔(秒)", font=("Segoe UI", 10, "bold")).grid(row=0, column=1, sticky=W)
        self.retry_var = tk.IntVar(value=self.config_data.get("retry_count", 3))
        self.delay_var = tk.IntVar(value=self.config_data.get("retry_delay_base", 10))
        ttkb.Spinbox(retry_frame, from_=1, to=10, textvariable=self.retry_var, width=10, bootstyle=PRIMARY).grid(row=1, column=0, sticky=W, padx=(0, 20))
        ttkb.Spinbox(retry_frame, from_=1, to=300, textvariable=self.delay_var, width=10, bootstyle=PRIMARY).grid(row=1, column=1, sticky=W)

        # 指定仓库
        ttkb.Label(form, text="指定额外备份仓库（可选）", font=("Segoe UI", 10, "bold")).pack(anchor=W)
        ttkb.Label(
            form,
            text="每行一个，格式 owner/repo。这些仓库会额外备份，不受上方目标限制。\n# 开头的行会被忽略。",
            font=("Segoe UI", 8), bootstyle=SECONDARY, wraplength=480,
        ).pack(anchor=W, pady=(2, 4))
        self.extra_repos_text = tk.Text(form, height=4, width=50, font=("Consolas", 10))
        self.extra_repos_text.pack(fill=X, pady=(2, 15))
        # 预填充已有配置
        existing_extra = self.config_data.get("extra_repos", [])
        if existing_extra:
            self.extra_repos_text.insert("1.0", "\n".join(existing_extra))

        # 保存按钮
        ttkb.Button(
            container, text="保存配置并开始使用", bootstyle=SUCCESS,
            command=self._save, padding=12,
        ).pack(fill=X, pady=(10, 0))

    def _toggle_token(self) -> None:
        if self.token_entry.cget("show") == "*":
            self.token_entry.config(show="")
        else:
            self.token_entry.config(show="*")

    def _verify_token(self) -> None:
        token = self.token_var.get().strip()
        if not token:
            self.token_result_var.set("请先输入 Token")
            return
        self.token_result_var.set("验证中...")
        self.update_idletasks()

        def do_verify():
            ok, msg = verify_token(token)
            self.after(0, lambda: self.token_result_var.set(
                f"✓ Token 有效，用户: {msg}" if ok else f"✗ {msg}"
            ))
        threading.Thread(target=do_verify, daemon=True).start()

    def _browse_dir(self) -> None:
        d = filedialog.askdirectory(initialdir=Path.home(), parent=self)
        if d:
            self.dir_var.set(d)

    def _toggle_cron(self) -> None:
        """按选中的 schedule_type 显示/隐藏 selector。

        ── daily   : 仅 HH:MM（不显示右侧 selector）
        ── weekly  : HH:MM + 周几 Combobox（同行右侧）
        ── monthly : HH:MM + 几号 Combobox（同行右侧）

        yearly / custom 已从 UI 移除。如果用户保存过老 config 仍是 yearly / custom，
        它们不会进新 UI，但 get_schedule_trigger 还认得这俩 value（向后兼容）。
        """
        mode = self.sched_var.get()
        # 先把右侧两个 selector 全隐
        for w in (self.dow_label, self.dow_combo, self.dom_label, self.dom_combo):
            try:
                w.pack_forget()
            except Exception:
                pass

        if mode == "weekly":
            self.dow_label.pack(side=LEFT, padx=(0, 4))
            self.dow_combo.pack(side=LEFT)
        elif mode == "monthly":
            self.dom_label.pack(side=LEFT, padx=(0, 4))
            self.dom_combo.pack(side=LEFT)
        # daily: 不 pack 任何右侧 selector

    def _save(self) -> None:
        target = self.target_var.get().strip()
        token = self.token_var.get().strip()
        backup_dir = self.dir_var.get().strip()

        if not target:
            messagebox.showwarning("提示", "请填写 GitHub 用户名或组织名", parent=self)
            return
        if not token:
            messagebox.showwarning("提示", "请填写 GitHub Token", parent=self)
            return
        if not backup_dir:
            backup_dir = str(Path.home() / "github-backups")

        # 验证备份目录
        try:
            Path(backup_dir).mkdir(parents=True, exist_ok=True)
        except OSError as e:
            messagebox.showerror("错误", f"无法创建备份目录: {e}", parent=self)
            return

        # 验证 Token
        ok, msg = verify_token(token)
        if not ok:
            if not messagebox.askyesno("Token 警告",
                f"Token 验证失败: {msg}\n是否仍要保存配置？", parent=self):
                return

        # 解析指定仓库
        extra_text = self.extra_repos_text.get("1.0", END).strip()
        extra_valid, extra_invalid = parse_extra_repos(extra_text)
        if extra_invalid:
            if not messagebox.askyesno("格式警告",
                f"以下行格式无效（应为 owner/repo），将被忽略：\n{chr(10).join(extra_invalid)}\n\n是否继续保存？",
                parent=self):
                return

        schedule_type = self.sched_var.get()
        self.result = {
            "target": target,
            "token": token,
            "backup_dir": backup_dir,
            "schedule_type": schedule_type,
            # cron_expression 字段保留（get_schedule_trigger 还会用到 legacy "custom"），
            # 但 UI 不再暴露 —— 默认写入一个有效 cron 以便老 config 结构不破
            "cron_expression": self.config_data.get("cron_expression") or "0 2 * * *",
            "daily_hour": _clamp_int(self.hour_var.get(), 2, 0, 23) if hasattr(self, "hour_var") else 2,
            "daily_minute": _clamp_int(self.minute_var.get(), 0, 0, 59) if hasattr(self, "minute_var") else 0,
            "weekly_day_of_week": (self.dow_var.get() if hasattr(self, "dow_var") else "sun") or "sun",
            "monthly_day_of_month": _clamp_int(self.monthly_dom_var.get(), 1, 1, 31) if hasattr(self, "monthly_dom_var") else 1,
            # yearly_* 字段保留（get_schedule_trigger 还会用到 legacy "yearly"），
            # 但 UI 不再暴露 —— 保留已有值或默认 1
            "yearly_month": int(self.config_data.get("yearly_month") or 1),
            "yearly_day_of_month": int(self.config_data.get("yearly_day_of_month") or 1),
            "scheduler_paused": False,
            "retry_count": self.retry_var.get(),
            "retry_delay_base": self.delay_var.get(),
            "extra_repos": extra_valid,
            "clone_mode": self.clone_mode_var.get(),
            "is_configured": True,
        }
        self.destroy()


# ════════════════════════════════════════════════════════════════════
# Git 未安装提示对话框
# ════════════════════════════════════════════════════════════════════

class GitMissingDialog(ttkb.Toplevel):
    """启动时检测到 git 未安装 → 弹出此对话框提示用户安装。

    - "🌐 访问 Git 官网"：一键跳 git-scm.com 下载页
    - "🔄 重试检测"：用户装好后点这个，不用重启 app；成功会自动更新 header 的
       Git 状态徽章 + 关对话框
    - "继续（暂不可备份）"：强行关弹窗，备份按钮被禁用但其他 UI 仍可用
    - "下次启动不再提醒"：勾上后下次启动不弹（直到用户主动改回，需手改 config.json）
    """

    GIT_HOMEPAGE_URL = "https://git-scm.com/downloads"

    def __init__(self, parent, git_diag_msg: str):
        super().__init__(parent)
        self.title("未检测到 Git")
        self.geometry("500x360")
        self.resizable(False, False)
        self.grab_set()  # 模态
        apply_icon(self)
        set_light_titlebar(self)

        container = ttkb.Frame(self, padding=30)
        container.pack(fill=BOTH, expand=YES)

        ttkb.Label(
            container, text="⚠ Git 未安装",
            font=("Segoe UI", 16, "bold"), bootstyle=DANGER,
        ).pack(anchor=W, pady=(0, 8))

        ttkb.Label(
            container,
            text="本应用依赖 Git 命令行执行镜像备份，请先安装 Git。",
            font=("Segoe UI", 10), bootstyle=SECONDARY,
        ).pack(anchor=W, pady=(0, 14))

        # 多平台安装提示
        hint = (
            "Windows  : 下载 git-scm.com 安装包\n"
            "macOS    : brew install git\n"
            "Linux    : sudo apt install git   (Debian/Ubuntu)\n"
            "            sudo yum install git   (CentOS/RHEL)"
        )
        ttkb.Label(container, text=hint, font=("Consolas", 9),
                   bootstyle=SECONDARY, justify="left").pack(anchor=W, pady=(0, 12))

        # 诊断信息（重试时会被替换更新）
        self._diag_label = ttkb.Label(
            container, text=f"诊断：{git_diag_msg}",
            font=("Segoe UI", 9), bootstyle=WARNING, wraplength=440,
        )
        self._diag_label.pack(anchor=W, pady=(0, 8))

        # "下次启动不再提醒"勾选框
        self._suppress_var = tk.BooleanVar(value=False)
        ttkb.Checkbutton(
            container,
            text="下次启动不再提醒",
            variable=self._suppress_var,
        ).pack(anchor=W, pady=(0, 12))

        # 按钮区
        btn_frame = ttkb.Frame(container)
        btn_frame.pack(fill=X, side=BOTTOM)

        ttkb.Button(
            btn_frame, text="🌐 访问 Git 官网", bootstyle=INFO,
            command=self._open_git_homepage, padding=(20, 10),
        ).pack(side=LEFT, padx=(0, 8))
        ttkb.Button(
            btn_frame, text="🔄 重试检测", bootstyle=WARNING,
            command=self._retry_check, padding=(20, 10),
        ).pack(side=LEFT, padx=(0, 8))
        ttkb.Button(
            btn_frame, text="继续（暂不可备份）", bootstyle=SECONDARY,
            command=self._on_dismiss, padding=(20, 10),
        ).pack(side=RIGHT)

        # Esc 关闭对话框，等同"继续"
        self.bind("<Escape>", lambda e: self._on_dismiss())

    def _open_git_homepage(self):
        """打开 git 官网。失败则降级到剪贴板。"""
        import webbrowser
        try:
            webbrowser.open(self.GIT_HOMEPAGE_URL)
        except Exception:
            try:
                self.clipboard_clear()
                self.clipboard_append(self.GIT_HOMEPAGE_URL)
            except Exception:
                pass
            messagebox.showinfo(
                "已复制 URL",
                f"无法启动浏览器，已复制地址到剪贴板：\n{self.GIT_HOMEPAGE_URL}",
                parent=self,
            )

    def _retry_check(self):
        """重试 git 检测：成功 → 通知父级 + 关对话框；失败 → 刷新诊断文案。"""
        from backup_engine import check_git
        ok, ver = check_git()
        if ok:
            # 通知父窗口更新 header 状态徽章（forward _refresh_git_status）
            parent = self.master
            if parent is not None and hasattr(parent, "_refresh_git_status"):
                parent._refresh_git_status(ok, ver)
            self.destroy()
        else:
            # 仍失败，把诊断文案刷出来
            self._diag_label.configure(text=f"诊断：{ver or 'Command failed'}")

    def _on_dismiss(self):
        """关闭对话框：若勾选了"下次不再提醒"，持久化到 config.json。"""
        if self._suppress_var.get():
            try:
                cfg = load_config()
                cfg["suppress_git_warning"] = True
                save_config(cfg)
            except Exception as e:
                print(f"[git dialog] 持久化 suppress 失败: {e}", file=sys.stderr)
        self.destroy()


# ════════════════════════════════════════════════════════════════════
# 备份失败详情对话框
# ════════════════════════════════════════════════════════════════════

class FailuresDialog(ttkb.Toplevel):
    """展示当前 state 里所有 status=failed 的仓库，支持选中后单独重试。"""

    def __init__(self, parent, failures: list[tuple[str, dict]]):
        super().__init__(parent)
        self._parent_app = parent
        self._failures = failures
        self.title(f"备份失败详情 - {len(failures)} 个")
        self.geometry("780x440")
        self.grab_set()
        apply_icon(self)
        set_light_titlebar(self)

        container = ttkb.Frame(self, padding=15)
        container.pack(fill=BOTH, expand=YES)

        ttkb.Label(
            container, text=f"以下 {len(failures)} 个仓库备份失败：",
            font=("Segoe UI", 11, "bold"),
        ).pack(anchor=W, pady=(0, 10))

        # Treeview 列：仓库名 / 上次成功时间 / 错误摘要 / 来源
        cols = ("name", "last", "error", "source")
        tree = ttkb.Treeview(container, columns=cols, show="headings", height=14,
                             selectmode="extended")
        tree.heading("name", text="仓库", anchor=W)
        tree.heading("last", text="上次成功", anchor=W)
        tree.heading("error", text="错误摘要", anchor=W)
        tree.heading("source", text="来源", anchor=CENTER)
        tree.column("name", width=200)
        tree.column("last", width=140)
        tree.column("error", width=320)
        tree.column("source", width=70, anchor=CENTER)

        for full_name, info in failures:
            err = (info.get("error") or "").splitlines()[0][:200]  # 第一行 200 字
            tree.insert("", END, iid=full_name,
                        values=(full_name,
                               self._fmt_dt(info.get("last_backup")),
                               err,
                               info.get("source", "all")))
        tree.pack(fill=BOTH, expand=YES, pady=(0, 10))

        # 错误详情（选中一行后底部 Text 显示完整错误）
        self._detail = ttkb.ScrolledText(container, height=6, autohide=True,
                                        padding=8)
        self._detail.pack(fill=BOTH, expand=NO, pady=(0, 10))
        self._detail.configure(state=DISABLED)
        tree.bind("<<TreeviewSelect>>",
                  lambda e: self._on_select(tree, dict(tree.selection())))

        # 按钮区
        btn_frame = ttkb.Frame(container)
        btn_frame.pack(fill=X)
        ttkb.Button(
            btn_frame, text="🔁 重试选中", bootstyle=INFO,
            command=lambda: self._retry_selected(tree, dict(tree.selection())),
            padding=(15, 8),
        ).pack(side=LEFT, padx=(0, 8))
        ttkb.Button(
            btn_frame, text="关闭", bootstyle=SECONDARY,
            command=self.destroy, padding=(15, 8),
        ).pack(side=RIGHT)

    @staticmethod
    def _fmt_dt(iso: str | None) -> str:
        if not iso:
            return "从未"
        from datetime import datetime as _dt
        try:
            return _dt.fromisoformat(iso).strftime("%Y-%m-%d %H:%M")
        except Exception:
            return str(iso)

    def _on_select(self, tree, sel: dict) -> None:
        # 显示完整错误（多行）
        full_lines: list[str] = []
        for full_name in sel:
            info = tree.set(full_name)
            full_lines.append(f"── {full_name} ──")
            err = tree.item(full_name, "values")
            full_lines.append(str(err[2]) if len(err) > 2 else "")
        text = "\n".join(full_lines).strip() or "(无)"
        tw = self._detail.winfo_children()[0]
        tw.configure(state=NORMAL)
        tw.delete("1.0", END)
        tw.insert("1.0", text)
        tw.configure(state=DISABLED)

    def _retry_selected(self, tree, sel: dict) -> None:
        if not sel:
            messagebox.showwarning("提示", "请先选择仓库。", parent=self)
            return
        # 通过父级 BackupApp 的 _on_backup_selected 路径（重用逻辑）
        parent = self._parent_app
        repo_full_names = list(sel.keys())
        self.destroy()
        # BackupApp 当前没有"批量重试"接口——逐个走 backup_single_repo
        # 简单做法：起 worker thread，逐个串行重试
        def worker():
            cfg = load_config()
            st = load_state()
            for full in repo_full_names:
                if not hasattr(parent, "_last_progress_msg"):
                    pass
                backup_single_repo(full, cfg, st, progress_callback=lambda c, t, m:
                                    parent.msg_queue.put(("progress", (c, t, m))))
            parent.msg_queue.put(("backup_done", True))
        import threading
        threading.Thread(target=worker, daemon=True).start()
        messagebox.showinfo("重试已启动",
                            f"已对 {len(repo_full_names)} 个仓库启动重试，请查看实时日志。",
                            parent=parent)


# ════════════════════════════════════════════════════════════════════
# 仓库文件浏览器对话框
# ════════════════════════════════════════════════════════════════════

class RepoFileManager(ttkb.Frame):
    """仓库文件浏览器：嵌入式 IDE 风格，兼容 bare 镜像 (git clone --mirror) 和普通克隆两种格式。"""

    MAX_PREVIEW_SIZE = 5 * 1024 * 1024  # 5MB 预览限制

    def __init__(self, parent, repo_name: str, repo_path: Path):
        super().__init__(parent)
        self.repo_name = repo_name
        self.repo_path = repo_path
        self._node_names: dict[str, str] = {}  # node_id -> 实际文件名

        # 检测仓库类型
        self.is_bare = self._detect_bare()

        self._build_ui()
        self._load_root()
        self.pack(fill=BOTH, expand=YES)

    def _detect_bare(self) -> bool:
        """检测是否为 bare 仓库。"""
        ret, out, _ = run_git_command(
            ["git", "rev-parse", "--is-bare-repository"], cwd=self.repo_path
        )
        return ret == 0 and out.strip() == "true"

    def _build_ui(self) -> None:
        # 顶部信息栏
        header = ttkb.Frame(self, padding=(10, 8, 10, 4))
        header.pack(fill=X)
        ttkb.Label(header, text=f"📁 {self.repo_name}",
                   font=("Segoe UI", 12, "bold"), bootstyle=PRIMARY).pack(side=LEFT)
        mode_text = "镜像模式 (bare)" if self.is_bare else "普通模式"
        ttkb.Label(header, text=f"  [{mode_text}]",
                   font=("Segoe UI", 8), bootstyle=SECONDARY).pack(side=LEFT, padx=(5, 0))

        # 路径面包屑
        self.path_var = tk.StringVar(value="/")
        ttkb.Label(self, textvariable=self.path_var, font=("Consolas", 9),
                   bootstyle=SECONDARY).pack(fill=X, padx=10, pady=(0, 4))

        ttkb.Separator(self, bootstyle=SECONDARY).pack(fill=X, padx=8, pady=2)

        # 主区域：左文件树 + 右预览
        paned = ttkb.Panedwindow(self, orient=HORIZONTAL)
        paned.pack(fill=BOTH, expand=YES, padx=8, pady=(4, 8))

        # ── 左：文件树 ──
        tree_frame = ttkb.Frame(paned)
        paned.add(tree_frame, weight=3)

        self.file_tree = ttkb.Treeview(
            tree_frame, columns=("type", "size"),
            show="tree headings", height=28,
        )
        self.file_tree.heading("#0", text="文件名", anchor=W)
        self.file_tree.heading("type", text="类型", anchor=CENTER)
        self.file_tree.heading("size", text="大小", anchor=E)
        self.file_tree.column("#0", width=280)
        self.file_tree.column("type", width=50, anchor=CENTER)
        self.file_tree.column("size", width=70, anchor=E)

        tree_scroll = ttkb.Scrollbar(tree_frame, command=self.file_tree.yview)
        self.file_tree.configure(yscrollcommand=tree_scroll.set)
        self.file_tree.pack(side=LEFT, fill=BOTH, expand=YES)
        tree_scroll.pack(side=RIGHT, fill=Y)

        self.file_tree.bind("<<TreeviewOpen>>", self._on_expand)
        self.file_tree.bind("<<TreeviewSelect>>", self._on_select)

        # ── 右：文件预览 ──
        preview_frame = ttkb.Frame(paned)
        paned.add(preview_frame, weight=2)

        preview_header = ttkb.Frame(preview_frame)
        preview_header.pack(fill=X)
        self.preview_title = tk.StringVar(value="文件预览")
        ttkb.Label(preview_header, textvariable=self.preview_title,
                   font=("Segoe UI", 10, "bold")).pack(side=LEFT)
        self.preview_info = tk.StringVar(value="")
        ttkb.Label(preview_header, textvariable=self.preview_info,
                   font=("Segoe UI", 8), bootstyle=SECONDARY).pack(side=RIGHT)

        self.preview_text = ScrolledText(preview_frame, autohide=True, padding=8)
        self.preview_text.pack(fill=BOTH, expand=YES, pady=(5, 0))
        tw = _get_text_widget(self.preview_text)
        tw.configure(state=DISABLED, wrap="none", font=("Consolas", 10))

    # ── 文件树加载 ──

    def _load_root(self) -> None:
        """加载根目录。"""
        for item in self.file_tree.get_children():
            self.file_tree.delete(item)
        self._node_names.clear()
        entries = self._list_dir("")
        for entry in entries:
            self._insert_node("", entry)

    def _list_dir(self, dir_path: str) -> list[dict]:
        """列出目录内容。空字符串表示根目录。"""
        entries: list[dict] = []

        if self.is_bare:
            # bare 仓库：用 git ls-tree 遍历
            path_arg = f"HEAD:{dir_path}" if dir_path else "HEAD"
            ret, out, _ = run_git_command(
                ["git", "ls-tree", path_arg], cwd=self.repo_path
            )
            if ret == 0:
                for line in out.strip().splitlines():
                    if not line:
                        continue
                    parts = line.split("\t", 1)
                    if len(parts) != 2:
                        continue
                    meta, name = parts
                    meta_parts = meta.split()
                    if len(meta_parts) >= 2:
                        obj_type = meta_parts[1]
                        obj_hash = meta_parts[2] if len(meta_parts) > 2 else ""
                        if obj_type == "tree":
                            entries.append({"name": name, "type": "dir", "hash": obj_hash})
                        elif obj_type == "blob":
                            entries.append({"name": name, "type": "file", "hash": obj_hash})
        else:
            # 普通仓库：用文件系统 API 遍历
            full_path = self.repo_path / dir_path if dir_path else self.repo_path
            try:
                for item in full_path.iterdir():
                    if item.name == ".git":
                        continue
                    if item.is_dir():
                        entries.append({"name": item.name, "type": "dir"})
                    else:
                        try:
                            size = item.stat().st_size
                        except OSError:
                            size = 0
                        entries.append({"name": item.name, "type": "file", "size": size})
            except OSError:
                pass

        # 排序：目录在前，然后按名称
        entries.sort(key=lambda e: (e["type"] != "dir", e["name"].lower()))
        return entries

    def _insert_node(self, parent: str, entry: dict) -> str:
        """插入一个节点到文件树。"""
        name = entry["name"]
        if entry["type"] == "dir":
            node = self.file_tree.insert(
                parent, END, text=f"📁 {name}",
                values=("目录", ""), open=False,
            )
            # 添加虚拟子节点以显示展开箭头
            self.file_tree.insert(node, END, text="...", values=("", ""))
        else:
            icon = self._get_file_icon(name)
            size_str = ""
            if "size" in entry:
                size_str = format_size(entry["size"])
            elif entry.get("hash"):
                size_str = self._get_blob_size(entry["hash"])
            node = self.file_tree.insert(
                parent, END, text=f"{icon} {name}",
                values=("文件", size_str),
            )
        self._node_names[node] = name
        return node

    def _on_expand(self, event) -> None:
        """节点展开时懒加载子目录。"""
        node = self.file_tree.focus()
        if not node:
            return
        children = self.file_tree.get_children(node)
        # 只有一个虚拟子节点时才需要加载
        if len(children) == 1:
            child_text = self.file_tree.item(children[0], "text")
            if child_text in ("...", "loading..."):
                self.file_tree.delete(children[0])
            else:
                return
        elif len(children) > 1:
            return  # 已加载

        path = self._get_node_path(node)
        entries = self._list_dir(path)
        if not entries:
            self.file_tree.insert(node, END, text="(空目录)", values=("", ""))
        for entry in entries:
            self._insert_node(node, entry)

    def _get_node_path(self, node: str) -> str:
        """获取节点从根到当前的相对路径。"""
        parts: list[str] = []
        current = node
        while current:
            name = self._node_names.get(current, "")
            if name:
                parts.insert(0, name)
            current = self.file_tree.parent(current)
        return "/".join(parts)

    def _on_select(self, event) -> None:
        """选中文件时显示预览。"""
        node = self.file_tree.focus()
        if not node:
            return

        text = self.file_tree.item(node, "text")
        if "📁" in text:
            # 目录：更新路径显示
            path = self._get_node_path(node)
            self.path_var.set(f"/{path}/")
            return

        path = self._get_node_path(node)
        self.path_var.set(f"/{path}")
        name = self._node_names.get(node, "")

        # 获取文件大小
        size = self._get_file_size(path)
        if size > self.MAX_PREVIEW_SIZE:
            self.preview_title.set(f"📄 {name}")
            self.preview_info.set(format_size(size))
            self._set_preview(
                f"文件过大 ({format_size(size)})，超过预览限制 ({format_size(self.MAX_PREVIEW_SIZE)})。\n"
                f"请使用外部工具打开。"
            )
            return

        # 读取文件内容
        content = self._read_file(path)
        if content is None:
            self.preview_title.set(f"📄 {name}")
            self.preview_info.set(format_size(size) if size else "")
            self._set_preview("(无法读取文件内容)")
        elif "\x00" in content[:8192]:
            self.preview_title.set(f"📄 {name}")
            self.preview_info.set(format_size(size) if size else "")
            self._set_preview("(二进制文件，无法预览)")
        else:
            self.preview_title.set(f"📄 {name}")
            self.preview_info.set(format_size(size) if size else "")
            self._set_preview(content)

    # ── 文件读取 ──

    def _get_file_size(self, path: str) -> int:
        """获取文件大小（字节）。"""
        if self.is_bare:
            ret, out, _ = run_git_command(
                ["git", "cat-file", "-s", f"HEAD:{path}"], cwd=self.repo_path
            )
            if ret == 0:
                try:
                    return int(out.strip())
                except ValueError:
                    pass
            return 0
        else:
            try:
                return (self.repo_path / path).stat().st_size
            except OSError:
                return 0

    def _get_blob_size(self, obj_hash: str) -> str:
        """通过 git hash 获取 blob 大小。"""
        if not obj_hash:
            return ""
        ret, out, _ = run_git_command(
            ["git", "cat-file", "-s", obj_hash], cwd=self.repo_path
        )
        if ret == 0:
            try:
                return format_size(int(out.strip()))
            except ValueError:
                pass
        return ""

    def _read_file(self, path: str) -> str | None:
        """读取文件内容为字符串。"""
        if self.is_bare:
            # bare 仓库：用 git show 读取（使用 bytes 模式避免解码异常）
            try:
                result = subprocess.run(
                    ["git", "show", f"HEAD:{path}"],
                    capture_output=True, cwd=str(self.repo_path), timeout=30,
                    env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
                )
                if result.returncode == 0:
                    return result.stdout.decode("utf-8", errors="replace")
            except Exception:
                pass
            return None
        else:
            try:
                return (self.repo_path / path).read_text(
                    encoding="utf-8", errors="replace"
                )
            except OSError:
                return None

    # ── 辅助方法 ──
    # _get_text_widget 在模块级（见顶部）定义，避免在 BackupApp / RepoFileManager
    # 各自复制一份

    def _set_preview(self, content: str) -> None:
        """设置预览区内容。"""
        tw = _get_text_widget(self.preview_text)
        tw.configure(state=NORMAL)
        tw.delete("1.0", END)
        tw.insert("1.0", content)
        tw.configure(state=DISABLED)

    @staticmethod
    def _get_file_icon(name: str) -> str:
        """根据文件扩展名返回图标。"""
        ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
        icons = {
            "py": "🐍", "js": "📜", "ts": "📜", "jsx": "📜", "tsx": "📜",
            "html": "🌐", "css": "🎨", "json": "📋", "xml": "📋", "svg": "🖼️",
            "md": "📝", "txt": "📄", "rst": "📝",
            "yml": "⚙️", "yaml": "⚙️", "toml": "⚙️", "ini": "⚙️", "cfg": "⚙️",
            "sh": "🔧", "bat": "🔧", "ps1": "🔧",
            "png": "🖼️", "jpg": "🖼️", "jpeg": "🖼️", "gif": "🖼️", "ico": "🖼️",
            "pdf": "📕", "zip": "📦", "tar": "📦", "gz": "📦", "rar": "📦",
            "c": "🔧", "cpp": "🔧", "h": "🔧", "java": "☕", "go": "🐹",
            "rs": "🦀", "rb": "💎", "php": "🐘", "sql": "🗄️",
        }
        return icons.get(ext, "📄")


# ════════════════════════════════════════════════════════════════════
# 主应用窗口
# ════════════════════════════════════════════════════════════════════

class BackupApp(ttkb.Window):
    """主应用窗口。"""

    def __init__(self):
        super().__init__(themename="flatly")
        self.title("GitHub Backup Manager")
        # 窗口几何从持久化文件恢复，找不到则用默认
        ui_state = load_window_state(BASE_DIR)
        self.geometry(ui_state["geometry"])
        self.minsize(900, 600)

        # 应用图标（窗口装饰 + Windows 任务栏）
        apply_icon(self)

        # Windows 下设置标题栏为浅色模式
        set_light_titlebar(self)

        # 设置应用背景色
        self.configure(background=Palette.BG_APP)

        # 状态
        self.config_data: dict[str, Any] = load_config()
        self.state_data: dict[str, Any] = load_state()
        self.msg_queue: queue.Queue[tuple[str, Any]] = queue.Queue()
        # 异步任务结果队列（git 检测 / get_dir_size 等后台 thread 通知主线程刷新 UI 用）
        # —— Tk 的 after() 不是 thread-safe，子线程直接 self.after() 调度的事件主线程 update() 看不到。
        # queue.Queue 是 thread-safe 的，子线程 put + 主线程 get_nowait 是正确范式。
        self._async_results: queue.Queue[tuple] = queue.Queue()

        # git 检测：原同步 subprocess.run 要 ~149ms 阻塞启动。
        # 改成后台 thread 跑，结果回来在主线程统一处理（更新 header / 弹窗）。
        # 占位值让 header 立即可见"检测中…"，避免空白等待。
        # 注：Tk 的 after() 不是 thread-safe，子线程直接调 main thread 的 update() 看不到，
        # 所以走 self._async_results queue + 主线程 _drain_async 轮询。
        self.git_ok: bool = False
        self.git_version: str = "检测中…"

        def _bg_check_git() -> None:
            ok, ver = check_git()
            # queue.Queue 是 thread-safe 的，子线程 put 主线程 get_nowait 安全
            self._async_results.put(("git_check", ok, ver))
        threading.Thread(target=_bg_check_git, daemon=True).start()

        # git 未安装弹窗：移到 _on_git_checked 里，等检测完再决定弹不弹
        # （保留 suppress_git_warning 检查逻辑不变）

        # 首次配置检查
        if not self.config_data.get("is_configured", False):
            self.after(100, self._show_setup_wizard)

        # 自定义样式
        self._setup_styles()

        # 构建 UI
        self._build_ui()

        # 启动调度器
        if self.config_data.get("is_configured", False):
            start_scheduler(self.config_data)

        # 启动队列轮询
        self._poll_queue()

        # 异步任务结果轮询（git 检测 / get_dir_size 等后台 thread → 主线程 UI 刷新）
        # 50ms tick —— 启动期感知不到延迟，后台任务一般 100-300ms 完成
        self.after(50, self._drain_async_results)

        # ── Tk 全局回调异常钩子 ─────────────────────────────────
        # 默认 Tk 默默吞掉 command 回调里的所有异常，调试黑洞。
        # 改成：打印到 stderr + 写日志，让用户能看到『按钮没反应』的真实原因。
        self.report_callback_exception = self._tk_excepthook
        # 一些方法（button.invoke / widget.bind 触发）走 cmdline 路径，
        # 那个走不到 report_callback_exception；同时覆盖 TclError 显示行为
        # 让『无效命令名』之类的运行时错误也走到 excepthook 而不是静默忽略。
        try:
            self.tk.call_encoding = sys.stdout.encoding or "utf-8"
        except Exception:
            pass

        # 关闭事件
        self.protocol("WM_DELETE_WINDOW", self._on_close)

        # 全局快捷键
        # Ctrl+S: 保存设置（设置对话框开着时生效；通过 _save_settings_then_close）
        # F5: 刷新仓库列表
        # Ctrl+R: 立即备份所有仓库
        # Ctrl+L: 切到备份日志 tab
        # Ctrl+,: 打开系统配置对话框
        # F1: 关于
        self.bind_all("<Control-s>", lambda e: self._on_ctrl_s())
        self.bind_all("<F5>", lambda e: self._on_refresh_repos())
        self.bind_all("<Control-r>", lambda e: self._on_backup_all())
        self.bind_all("<Control-l>", lambda e: self.notebook.select(self.tab_logs))
        self.bind_all("<Control-comma>", lambda e: self._show_settings_dialog())
        self.bind_all("<F1>", lambda e: self._show_about())

    # ── 样式配置 ──

    def _setup_styles(self) -> None:
        """配置自定义 ttkbootstrap 样式，统一浅色系视觉风格。"""
        style = ttkb.Style()

        # ── Treeview 样式 ──
        style.configure("Treeview",
                        background=Palette.BG_CARD,
                        foreground=Palette.TEXT_PRIMARY,
                        fieldbackground=Palette.BG_CARD,
                        borderwidth=0,
                        font=("Segoe UI", 10))
        style.configure("Treeview.Heading",
                        font=("Segoe UI", 10, "bold"),
                        foreground=Palette.TEXT_PRIMARY,
                        background=Palette.BG_HOVER)
        style.map("Treeview",
                  background=[("selected", Palette.PRIMARY)],
                  foreground=[("selected", "#ffffff")])

        # ── 信息卡片：白色背景 + 细边框 ──
        # 注：指标卡片（_metric_card）改用 Canvas 自绘，不再依赖 ttk 样式
        style.configure("InfoCard.TFrame", background=Palette.BG_CARD, relief="solid",
                        borderwidth=1, bordercolor=Palette.BORDER)
        style.configure("InfoCardTitle.TLabel",
                        background=Palette.BG_CARD,
                        foreground=Palette.TEXT_SECONDARY, font=("Segoe UI", 10))
        style.configure("InfoCardValue.TLabel",
                        background=Palette.BG_CARD,
                        foreground=Palette.TEXT_PRIMARY, font=("Segoe UI", 14, "bold"))

        # ── Header 区域样式（白色背景，ttk 不支持直接 configure background） ──
        style.configure("Header.TFrame", background=Palette.BG_CARD)
        style.configure("HeaderTitle.TLabel", background=Palette.BG_CARD,
                        foreground=Palette.PRIMARY, font=("Segoe UI", 16, "bold"))
        style.configure("HeaderSub.TLabel", background=Palette.BG_CARD,
                        foreground=Palette.TEXT_SECONDARY, font=("Segoe UI", 10))
        style.configure("GitOK.TLabel", background=Palette.BG_CARD,
                        foreground=Palette.SUCCESS, font=("Segoe UI", 9))
        style.configure("GitFail.TLabel", background=Palette.BG_CARD,
                        foreground=Palette.DANGER, font=("Segoe UI", 9))

    # ── UI 构建 ──

    def _load_title_logo(self, target_size: int = 56) -> tk.PhotoImage | None:
        """加载 logo.ico 高质量缩到约 target_size×target_size。

        Pillow 默认从 .ico 取最大一帧（256×256），LANCZOS 缩到目标尺寸。
        调用方必须把返回值挂到 self 上（PhotoImage 没有自动引用保留，否则会被 GC）。

        Pillow 缺失或 .ico 不存在时返回 None —— 标题区只显示文字，不阻塞启动。
        """
        logo_ico = BASE_DIR / "logo.ico"
        if not logo_ico.exists():
            return None
        try:
            from PIL import Image, ImageTk
            pil_img = Image.open(str(logo_ico))
            # RGBA → 白底合成（Windows Tk 8.6 对 PNG alpha 渲染不稳，与 apply_icon 保持一致）
            if pil_img.mode == "RGBA":
                bg = Image.new("RGB", pil_img.size, (255, 255, 255))
                bg.paste(pil_img, mask=pil_img.split()[3])
                pil_img = bg
            elif pil_img.mode != "RGB":
                pil_img = pil_img.convert("RGB")
            pil_img = pil_img.resize((target_size, target_size), Image.LANCZOS)
            return ImageTk.PhotoImage(pil_img)
        except Exception as e:
            print(f"[logo] 加载失败: {e}", file=sys.stderr)
            return None

    def _create_menu_bar(self) -> None:
        """构建顶部菜单栏（文件 / 设置 / 帮助）。

        设计：
        - 文件：日常操作集中（备份、刷新、取消、目录、日志、退出）
        - 设置：原"设置"Tab 改成 Toplevel 对话框，由"系统配置…"打开；
          外加暂停调度器 / 重新初始化 / 重置备份状态
        - 帮助：原 ❓ header 按钮搬到这里（关于…）；外加系统定时任务参考

        Tk 下划线字母 + accelerator 一并显示在菜单里，符合 Windows 习惯。
        """
        menubar = ttkb.Menu(self)

        # ── 文件菜单 ────────────────────────────────────────────
        file_menu = ttkb.Menu(menubar, tearoff=0)
        file_menu.add_command(
            label="🚀 立即备份所有仓库",
            accelerator="Ctrl+R",
            command=self._on_backup_all,
        )
        file_menu.add_command(
            label="📦 备份选中的仓库",
            command=self._on_backup_selected_kb,
        )
        file_menu.add_separator()
        file_menu.add_command(
            label="🔄 刷新仓库列表",
            accelerator="F5",
            command=self._on_refresh_repos,
        )
        file_menu.add_command(
            label="⏹ 取消当前备份",
            command=self._on_cancel_backup,
        )
        file_menu.add_separator()
        file_menu.add_command(
            label="🗂 打开备份目录",
            command=self._on_open_backup_dir,
        )
        file_menu.add_command(
            label="📝 查看备份日志",
            accelerator="Ctrl+L",
            command=lambda: self.notebook.select(self.tab_logs),
        )
        file_menu.add_command(
            label="📋 失败详情",
            command=self._show_failures_panel,
        )
        file_menu.add_separator()
        file_menu.add_command(
            label="❌ 退出",
            accelerator="Alt+F4",
            command=self._on_close,
        )
        menubar.add_cascade(label="文件(F)", menu=file_menu, underline=3)

        # ── 设置菜单 ────────────────────────────────────────────
        self._settings_menu = ttkb.Menu(menubar, tearoff=0)
        self._settings_menu.add_command(
            label="⚙ 系统配置…",
            accelerator="Ctrl+,",
            command=self._show_settings_dialog,
        )
        self._settings_menu.add_separator()
        # 暂停调度器：菜单文字 + ✓/空 checkbox 一目了然
        self._scheduler_paused_var = tk.BooleanVar(
            value=bool(self.config_data.get("scheduler_paused", False))
        )
        self._settings_menu.add_checkbutton(
            label="⏸ 暂停调度器（勾选后不自动备份）",
            variable=self._scheduler_paused_var,
            command=self._on_toggle_scheduler_paused_menu,
        )
        self._settings_menu.add_separator()
        self._settings_menu.add_command(
            label="🔧 重新初始化（首次设置向导）",
            command=self._show_setup_wizard,
        )
        self._settings_menu.add_command(
            label="⚠ 重置备份状态（清除所有仓库记录）",
            command=self._reset_state,
        )
        menubar.add_cascade(label="设置(S)", menu=self._settings_menu, underline=0)

        # ── 帮助菜单 ────────────────────────────────────────────
        help_menu = ttkb.Menu(menubar, tearoff=0)
        help_menu.add_command(
            label="❓ 关于…",
            accelerator="F1",
            command=self._show_about,
        )
        help_menu.add_separator()
        help_menu.add_command(
            label="⏰ 系统定时任务参考 (Cron / Systemd Timer)",
            command=self._show_cron_reference,
        )
        help_menu.add_command(
            label="🗂 打开备份目录",
            command=self._on_open_backup_dir,
        )
        menubar.add_cascade(label="帮助(H)", menu=help_menu, underline=0)

        # 挂到窗口（Windows 默认显示在窗口顶部）
        self.config(menu=menubar)

    def _build_ui(self) -> None:
        # 顶部标题栏（ttk.Frame 不支持 configure background，须用 style）
        header = ttkb.Frame(self, padding=(20, 14, 20, 10), style="Header.TFrame")
        header.pack(fill=X)

        # 加载头部 logo（缩到 56px；引用挂 self 防 GC）
        self._title_logo = self._load_title_logo(target_size=56)

        # 图标 + (主标题 + 副标题两行) 横排布局
        title_box = ttkb.Frame(header, style="Header.TFrame")
        title_box.pack(side=LEFT, fill=Y)
        if self._title_logo is not None:
            # 图标在左
            ttkb.Label(
                title_box, image=self._title_logo, style="HeaderTitle.TLabel",
            ).pack(side=LEFT, padx=(0, 8))
        # 文字列在右：主标题 + 副标题垂直堆叠，全部位于图标正右方
        text_col = ttkb.Frame(title_box, style="Header.TFrame")
        text_col.pack(side=LEFT, fill=Y)
        ttkb.Label(
            text_col, text="GitHub Backup Manager", style="HeaderTitle.TLabel",
        ).pack(anchor=W)
        ttkb.Label(
            text_col, text="安全 · 自动 · 增量镜像备份", style="HeaderSub.TLabel",
        ).pack(anchor=W, pady=(1, 0))

        # Git 状态 + 下次执行时间指示徽章（保持引用，动态更新）
        status_frame = ttkb.Frame(header, style="Header.TFrame")
        status_frame.pack(side=RIGHT, fill=Y)
        git_text = f"✓ {self.git_version}" if self.git_ok else "✗ Git 未安装"
        git_style = "GitOK.TLabel" if self.git_ok else "GitFail.TLabel"
        self._git_status_label = ttkb.Label(
            status_frame, text=git_text, style=git_style,
        )
        self._git_status_label.pack(side=RIGHT, padx=(12, 0))
        # 下次执行时间（分隔 + 文字，标题布局用轻量提示）
        ttkb.Separator(status_frame, orient=VERTICAL, bootstyle=SECONDARY).pack(side=RIGHT, fill=Y, padx=8, pady=4)
        self._next_run_label = ttkb.Label(
            status_frame, text=self._next_run_text(),
            style="HeaderSub.TLabel",
        )
        self._next_run_label.pack(side=RIGHT)
        # ❓ About 按钮已搬到顶部菜单栏：帮助 → 关于…
        # 每 30 秒刷新一次"下次执行时间"
        self.after(30_000, self._refresh_next_run_label)

        ttkb.Separator(self, bootstyle=SECONDARY).pack(fill=X)

        # 选项卡（设置 tab 已删除——设置改由菜单"设置 → 系统配置…"打开 Toplevel）
        self.notebook = ttkb.Notebook(self)
        self.notebook.pack(fill=BOTH, expand=YES, padx=10, pady=10)

        self.tab_dashboard = ttkb.Frame(self.notebook, padding=15)
        self.tab_repos = ttkb.Frame(self.notebook, padding=15)
        self.tab_logs = ttkb.Frame(self.notebook, padding=15)
        # 不再有 tab_settings — 设置改为菜单驱动的 Toplevel

        self.notebook.add(self.tab_dashboard, text="  📊 仪表盘  ")
        self.notebook.add(self.tab_repos, text="  📦 仓库列表  ")
        self.notebook.add(self.tab_logs, text="  📝 备份日志  ")

        self._build_dashboard()
        self._build_repos()
        self._build_logs()
        # 注意：原 _build_settings 已删除——设置面板现在是 Toplevel 对话框，
        # 由菜单"设置 → 系统配置…"触发 _show_settings_dialog 按需创建。

        # 顶部菜单栏（必须在 build_ui 末尾，所有快捷键 / handler 都已存在）
        self._create_menu_bar()

        # 初始化仪表盘数据
        self._update_dashboard()

    def _build_dashboard(self) -> None:
        tab = self.tab_dashboard

        # 摘要卡片行
        cards = ttkb.Frame(tab)
        cards.pack(fill=X, pady=(0, 15))

        self.lbl_total = self._metric_card(cards, "仓库总数", "0", PRIMARY, 0)
        self.lbl_success = self._metric_card(cards, "备份成功", "0", SUCCESS, 1)
        self.lbl_failed = self._metric_card(cards, "备份失败", "0", DANGER, 2)
        self.lbl_disk = self._metric_card(cards, "磁盘可用", "N/A", INFO, 3)

        # 备份信息行
        info = ttkb.Frame(tab)
        info.pack(fill=X, pady=(0, 15))
        self.lbl_last_backup = self._info_card(info, "最近备份时间", "从未", 0)
        self.lbl_total_size = self._info_card(info, "备份总大小", "N/A", 1)
        self.lbl_target = self._info_card(info, "备份目标", "N/A", 2)
        self.lbl_extra = self._info_card(info, "指定额外仓库", "0 个", 3)

        # 操作按钮（仪表盘不再放"刷新"按钮——"仓库列表"页 toolbar 上有同功能入口，避免重复）
        btn_frame = ttkb.Frame(tab)
        btn_frame.pack(fill=X, pady=(0, 15))
        self.btn_backup_all = ttkb.Button(
            btn_frame, text="🚀 立即备份所有仓库",
            bootstyle=SUCCESS, command=self._on_backup_all, padding=(20, 10),
        )
        self.btn_backup_all.pack(side=LEFT)
        # 取消按钮：备份中显示，平时隐藏
        self.btn_cancel_backup = ttkb.Button(
            btn_frame, text="⏹ 停止备份",
            bootstyle=DANGER, command=self._on_cancel_backup, padding=(20, 10),
        )
        # 不立即 pack，靠 _on_backup_all / _poll_queue 控制
        # 打开备份目录 + 失败详情 两个常用入口
        ttkb.Button(
            btn_frame, text="🗂 打开备份目录", bootstyle=(INFO, OUTLINE),
            command=self._on_open_backup_dir, padding=(15, 10),
        ).pack(side=LEFT, padx=(8, 0))
        self.btn_show_failures = ttkb.Button(
            btn_frame, text="📋 失败详情", bootstyle=(DANGER, OUTLINE),
            command=self._show_failures_panel, padding=(15, 10),
        )
        self.btn_show_failures.pack(side=LEFT, padx=(8, 0))

        # 进度条
        self.progress_var = tk.DoubleVar(value=0)
        self.progress = ttkb.Progressbar(
            tab, variable=self.progress_var, maximum=100,
            bootstyle=(INFO, STRIPED),
        )
        self.progress.pack(fill=X, pady=(0, 5))
        self.progress_label = ttkb.Label(tab, text="就绪", font=("Segoe UI", 9),
                                         bootstyle=SECONDARY)
        self.progress_label.pack(anchor=W, pady=(0, 10))

        # 实时日志框
        ttkb.Label(tab, text="实时日志", font=("Segoe UI", 11, "bold")).pack(anchor=W)
        self.dash_log = ScrolledText(tab, height=12, autohide=True, padding=5)
        self.dash_log.pack(fill=BOTH, expand=YES)
        self._configure_log_tags(self.dash_log)
        self._set_text_state(self.dash_log, DISABLED)

    # ── ScrolledText 辅助方法 ──
    # 模块级 _get_text_widget 在文件顶部 —— 这里不再重复
    def _set_text_state(self, scrolled_text, state: str) -> None:
        """设置 ScrolledText 内部 Text 组件的状态。"""
        _get_text_widget(scrolled_text).configure(state=state)

    # ── 日志着色 ──────────────────────────────────────
    def _configure_log_tags(self, scrolled_text) -> None:
        """给日志 ScrolledText 配置 level → 颜色 tag（成功绿 / 错误红 / 警告橙）。"""
        tw = _get_text_widget(scrolled_text)
        tw.tag_configure("log_success", foreground=Palette.SUCCESS)
        tw.tag_configure("log_error", foreground=Palette.DANGER)
        tw.tag_configure("log_warning", foreground=Palette.WARNING)

    def _log_level_tag(self, level: int) -> str:
        """level 数值 → 颜色 tag 名称。无匹配返回空串（默认色）。"""
        if level >= _logging.ERROR:                # ERROR / CRITICAL
            return "log_error"
        if level == _logging.WARNING:
            return "log_warning"
        if level == LOG_SUCCESS:
            return "log_success"
        return ""

    def _metric_card(self, parent, title, value, style, col):
        """创建摘要指标卡片：Metro 磁贴风 —— 浅 accent 圆角底 + 10px 顶部 accent 描边。

        设计：参考 subnet-calculator 的 .tile 风格 + 自身浅色系配色方案
            - 顶部 10px accent 描边（粗，强调语义，圆角）
            - 卡片底色 = accent 浅色（Tailwind 100），四边 6px 圆角
            - 数值大号，字色 = accent（与顶部描边联动，强语义）
            - 标题小号次要文字色

        实现：Canvas 像素级自绘（绕开 ttkbootstrap Canvas bg 覆盖问题）。
        圆角通过 4 个 create_arc PIESLICE + 中心矩形组合。
        z 顺序：bg < stripe < text —— 用 tag 标记保证 resize 重绘后顺序不乱。
        """
        # style(ttkbootstrap 常量) → (style 名称, accent 颜色, accent 浅色)
        if style == SUCCESS:
            name, accent, accent_light = "Success", Palette.SUCCESS, Palette.SUCCESS_LIGHT
        elif style == DANGER:
            name, accent, accent_light = "Danger", Palette.DANGER, Palette.DANGER_LIGHT
        elif style == INFO:
            name, accent, accent_light = "Info", Palette.INFO, Palette.INFO_LIGHT
        else:
            name, accent, accent_light = "Primary", Palette.PRIMARY, Palette.PRIMARY_LIGHT

        RADIUS = 6     # 圆角半径
        STRIPE_H = 10  # 顶部 accent 描边高度
        CARD_H = 80    # 卡片总高

        # Canvas 卡框：固定高度，圆角自绘
        card = tk.Canvas(parent, highlightthickness=0, bd=0, height=CARD_H)
        card.grid(row=0, column=col, padx=10, sticky=NSEW)
        parent.grid_columnconfigure(col, weight=1)
        parent.grid_rowconfigure(0, minsize=CARD_H)
        card.grid_propagate(False)

        # ── 圆角矩形辅助：4 个角的 PIESLICE + 中心矩形 ──
        def _rounded_rect_all(x1, y1, x2, y2, r, **kw):
            """四边都圆角的矩形，返回 item id 列表"""
            r = max(0, min(r, (x2 - x1) / 2, (y2 - y1) / 2))
            if r <= 0:
                return [card.create_rectangle(x1, y1, x2, y2, **kw)]
            ids = [
                card.create_arc(x1, y1, x1 + 2*r, y1 + 2*r,
                                start=90, extent=90, style=PIESLICE, **kw),
                card.create_arc(x2 - 2*r, y1, x2, y1 + 2*r,
                                start=0, extent=90, style=PIESLICE, **kw),
                card.create_arc(x1, y2 - 2*r, x1 + 2*r, y2,
                                start=180, extent=90, style=PIESLICE, **kw),
                card.create_arc(x2 - 2*r, y2 - 2*r, x2, y2,
                                start=270, extent=90, style=PIESLICE, **kw),
                card.create_rectangle(x1 + r, y1, x2 - r, y2, **kw),
                card.create_rectangle(x1, y1 + r, x2, y2 - r, **kw),
                card.create_rectangle(x1 + r, y1 + r, x2 - r, y2 - r, **kw),
            ]
            return ids

        def _rounded_top_rect(x1, y1, x2, y2, r, **kw):
            """仅顶部圆角的矩形（底部直角），返回 item id 列表"""
            r = max(0, min(r, (x2 - x1) / 2, (y2 - y1) / 2))
            if r <= 0:
                return [card.create_rectangle(x1, y1, x2, y2, **kw)]
            ids = [
                card.create_arc(x1, y1, x1 + 2*r, y1 + 2*r,
                                start=90, extent=90, style=PIESLICE, **kw),
                card.create_arc(x2 - 2*r, y1, x2, y1 + 2*r,
                                start=0, extent=90, style=PIESLICE, **kw),
                card.create_rectangle(x1, y1, x2, y2, **kw),
            ]
            return ids

        # ── 绘制：bg + stripe + text，全部用 tag 标记方便 z 排序 ──
        _bg_items = _rounded_rect_all(0, 0, 100, CARD_H, RADIUS,
                                       fill=accent_light, outline="",
                                       tags=("bg",))
        _stripe_items = _rounded_top_rect(0, 0, 100, STRIPE_H, RADIUS,
                                           fill=accent, outline="",
                                           tags=("stripe",))
        _title_id = card.create_text(18, 24, text=title, anchor=NW,
                                      fill=Palette.TEXT_SECONDARY,
                                      font=("Segoe UI", 10, "bold"),
                                      tags=("text",))
        _value_id = card.create_text(18, 44, text=value, anchor=NW,
                                      fill=accent,
                                      font=("Segoe UI", 22, "bold"),
                                      tags=("text",))
        # 显式建立 z 顺序：bg < stripe < text
        card.tag_raise("stripe", "bg")
        card.tag_raise("text", "stripe")

        # ── resize：删除旧圆角 item，重绘后用 tag 重建 z 顺序 ──
        def _on_resize(event):
            for item in _bg_items + _stripe_items:
                card.delete(item)
            new_bg = _rounded_rect_all(0, 0, event.width, CARD_H, RADIUS,
                                        fill=accent_light, outline="",
                                        tags=("bg",))
            new_stripe = _rounded_top_rect(0, 0, event.width, STRIPE_H, RADIUS,
                                            fill=accent, outline="",
                                            tags=("stripe",))
            _bg_items.clear()
            _bg_items.extend(new_bg)
            _stripe_items.clear()
            _stripe_items.extend(new_stripe)
            # 关键：resize 后新 bg/stripe 在 text 之后创建，会盖住 text，
            # 必须重新建立 z 顺序
            card.tag_raise("stripe", "bg")
            card.tag_raise("text", "stripe")
        card.bind("<Configure>", _on_resize)

        # ── 句柄：模拟 ttkb.Label 接口，val.configure(text=...) 走 itemconfigure ──
        class _Handle:
            def __init__(self, initial):
                self._text = initial
            def configure(self, cnf=None, **kwargs):
                if "text" in kwargs:
                    self._text = kwargs["text"]
                    card.itemconfigure(_value_id, text=self._text)
                # 其他 configure 项（state、font 等）静默忽略
            config = configure
            def cget(self, key):
                if key == "text":
                    return self._text
                return None

        return _Handle(value)

    def _info_card(self, parent, title, value, col) -> ttkb.Label:
        """信息卡片：白色背景 + 标题 + 数值。"""
        card = ttkb.Frame(parent, padding=15, style="InfoCard.TFrame")
        card.grid(row=0, column=col, padx=5, sticky=NSEW)
        parent.grid_columnconfigure(col, weight=1)
        ttkb.Label(card, text=title, style="InfoCardTitle.TLabel").pack(anchor=W)
        val_label = ttkb.Label(card, text=value, style="InfoCardValue.TLabel")
        val_label.pack(anchor=W, pady=(4, 0))
        return val_label

    def _build_repos(self) -> None:
        tab = self.tab_repos

        # 工具栏
        toolbar = ttkb.Frame(tab)
        toolbar.pack(fill=X, pady=(0, 10))

        ttkb.Label(toolbar, text="搜索:", font=("Segoe UI", 10)).pack(side=LEFT)
        self.search_var = tk.StringVar()
        self.search_var.trace_add("write", self._filter_repos)
        ttkb.Entry(toolbar, textvariable=self.search_var, width=30,
                   bootstyle=PRIMARY).pack(side=LEFT, padx=(5, 15))

        ttkb.Label(toolbar, text="排序:", font=("Segoe UI", 10)).pack(side=LEFT)
        self.sort_var = tk.StringVar(value=self.config_data.get("sort_option", "名称"))
        sort_combo = ttkb.Combobox(toolbar, textvariable=self.sort_var, width=12,
                                   values=["名称", "最后备份", "状态", "大小", "来源"],
                                   state="readonly", bootstyle=PRIMARY)
        sort_combo.pack(side=LEFT, padx=(5, 15))
        sort_combo.bind("<<ComboboxSelected>>", lambda e: self._populate_repos())

        # 右侧操作按钮（备份选中放在刷新列表左侧，两个按钮高度一致）
        # side=RIGHT 时先 pack 的贴近右边，后 pack 的靠左 → 顺序写刷新、备份，视觉上得到 [备份][刷新][右边]
        ttkb.Button(toolbar, text="🔄 刷新列表", bootstyle=INFO,
                    command=self._on_refresh_repos, padding=(15, 8)).pack(side=RIGHT)
        ttkb.Button(toolbar, text="📦 备份选中仓库", bootstyle=SUCCESS,
                    command=self._on_backup_selected, padding=(15, 8)).pack(side=RIGHT, padx=(0, 5))

        # 主区域：上仓库列表 + 下文件浏览器（垂直分栏，保证预览空间）
        paned = ttkb.Panedwindow(tab, orient=VERTICAL)
        paned.pack(fill=BOTH, expand=YES)

        # ── 上：仓库列表 ──
        top = ttkb.Frame(paned)
        paned.add(top, weight=0)  # weight=0：不参与额外空间分配，按自然高度（toolbar + 3 行 tree）

        # 仓库表格容器
        tree_container = ttkb.Frame(top)
        tree_container.pack(fill=BOTH, expand=YES)

        # 仓库表格
        cols = ("name", "last_backup", "status", "size", "source")
        self.repo_tree = ttkb.Treeview(
            tree_container, columns=cols, show="headings",
            height=5,
        )
        self.repo_tree.heading("name", text="仓库名", anchor=W)
        self.repo_tree.heading("last_backup", text="最后备份", anchor=W)
        self.repo_tree.heading("status", text="状态", anchor=CENTER)
        self.repo_tree.heading("size", text="大小", anchor=E)
        self.repo_tree.heading("source", text="来源", anchor=CENTER)
        self.repo_tree.column("name", width=300)
        self.repo_tree.column("last_backup", width=160)
        self.repo_tree.column("status", width=80, anchor=CENTER)
        self.repo_tree.column("size", width=90, anchor=E)
        self.repo_tree.column("source", width=70, anchor=CENTER)

        # 标签样式映射（浅色系适配）
        self.repo_tree.tag_configure("success",
            foreground=Palette.TV_SUCCESS, background="#f0fdf4")
        self.repo_tree.tag_configure("failed",
            foreground=Palette.TV_FAILED, background="#fef2f2")
        self.repo_tree.tag_configure("pending",
            foreground=Palette.TV_PENDING, background=Palette.BG_HOVER)
        self.repo_tree.tag_configure("stripe", background=Palette.BG_STRIPE)
        # 空状态行：弱化色 + 斜体感（ttk 不直接支持 italic，靠 font）
        self.repo_tree.tag_configure("empty",
            foreground=Palette.TEXT_MUTED,
            font=("Segoe UI", 10, "italic"))

        scroll = ttkb.Scrollbar(tree_container, command=self.repo_tree.yview)
        self.repo_tree.configure(yscrollcommand=scroll.set)

        self.repo_tree.pack(side=LEFT, fill=BOTH, expand=YES)
        scroll.pack(side=RIGHT, fill=Y)

        # 选中仓库 → 自动加载文件浏览器
        self.repo_tree.bind("<<TreeviewSelect>>", self._on_repo_selected)
        # 双击仓库 = 备份该仓库
        self.repo_tree.bind("<Double-1>", self._on_repo_double_click)
        # 右键菜单（复制 full_name / 在文件管理器打开）
        self.repo_tree.bind("<Button-3>", self._on_repo_right_click)

        # ── 下：文件浏览器（自动加载，无需手动点击） ──
        bottom = ttkb.Frame(paned)
        paned.add(bottom, weight=1)  # 拿走全部剩余空间

        self.file_browser_container = ttkb.Frame(bottom)
        self.file_browser_container.pack(fill=BOTH, expand=YES)
        self._show_file_browser_placeholder()

        self._populate_repos()

    def _build_logs(self) -> None:
        tab = self.tab_logs

        toolbar = ttkb.Frame(tab)
        toolbar.pack(fill=X, pady=(0, 10))

        ttkb.Label(toolbar, text="搜索:", font=("Segoe UI", 10)).pack(side=LEFT)
        self.log_search_var = tk.StringVar()
        log_search_entry = ttkb.Entry(toolbar, textvariable=self.log_search_var, width=30,
                   bootstyle=PRIMARY)
        log_search_entry.pack(side=LEFT, padx=(5, 15))
        # 回车直接触发刷新；右侧 🔄 刷新按钮仍然可用
        log_search_entry.bind("<Return>", lambda e: self._refresh_logs())

        self.log_source_var = tk.StringVar(value="当前会话日志")
        ttkb.Combobox(toolbar, textvariable=self.log_source_var, width=14,
                      values=["当前会话日志", "完整日志文件"],
                      state="readonly", bootstyle=PRIMARY).pack(side=LEFT, padx=(0, 15))

        ttkb.Button(toolbar, text="🔄 刷新", bootstyle=INFO,
                    command=self._refresh_logs, padding=(15, 8)).pack(side=LEFT, padx=(0, 8))
        ttkb.Button(toolbar, text="📥 导出", bootstyle=PRIMARY,
                    command=self._export_logs, padding=(15, 8)).pack(side=LEFT, padx=(0, 8))
        ttkb.Button(toolbar, text="🗑 清空缓冲", bootstyle=DANGER,
                    command=self._clear_logs, padding=(15, 8)).pack(side=LEFT)

        # 日志文本框
        self.log_text = ScrolledText(tab, autohide=True, padding=8)
        self.log_text.pack(fill=BOTH, expand=YES)
        self._configure_log_tags(self.log_text)
        self._set_text_state(self.log_text, DISABLED)
        _get_text_widget(self.log_text).configure(wrap="none")

        # 自动刷新日志
        self._refresh_logs()

    def _build_settings_into(self, parent: tk.Widget) -> None:
        """把设置表单构建到指定的 parent widget 里。

        ── 入口：_show_settings_dialog（Toplevel）
        ── 表单里所有引用都挂在 self.s_* 上（tk.Var），关闭对话框后值依然保留，
           下次再开无需 re-init。

        把原 _build_settings 拆出来是为了支持菜单驱动的对话框模式；notebook
        Tab 已经移除。
        """
        # 顶部固定操作栏（在 dialog 里：保存 + 关闭）
        top_bar = ttkb.Frame(parent)
        top_bar.pack(fill=X, padx=15, pady=(10, 6))
        # 关店放进这个对话框的关闭函数
        close_btn = ttkb.Button(
            top_bar, text="❌ 关闭", bootstyle=SECONDARY,
            command=getattr(self, "_close_settings_dialog", None) or parent.destroy,
            padding=(15, 6),
        )
        # 动态绑定到 settings dialog 的关闭
        if hasattr(self, "_settings_dialog") and self._settings_dialog is parent:
            close_btn.configure(command=self._close_settings_dialog)
        close_btn.pack(side=RIGHT, padx=(5, 0))
        save_btn = ttkb.Button(
            top_bar, text="💾 保存设置", bootstyle=SUCCESS,
            command=self._save_settings_then_close, padding=(20, 6),
        )
        save_btn.pack(side=RIGHT)
        ttkb.Separator(parent, bootstyle=SECONDARY).pack(fill=X)

        # 中间滚动区
        scroll_frame = ttkb.Frame(parent)
        scroll_frame.pack(fill=BOTH, expand=YES, padx=10, pady=(10, 10))
        canvas = tk.Canvas(scroll_frame, highlightthickness=0, bg=Palette.BG_APP)
        scrollbar = ttkb.Scrollbar(scroll_frame, orient=VERTICAL, command=canvas.yview)
        self.settings_inner = ttkb.Frame(canvas)
        self.settings_inner.bind(
            "<Configure>",
            lambda e: canvas.configure(scrollregion=canvas.bbox("all"))
        )
        canvas.create_window((0, 0), window=self.settings_inner, anchor=NW, width=1040)
        canvas.configure(yscrollcommand=scrollbar.set)
        canvas.pack(side=LEFT, fill=BOTH, expand=YES)
        scrollbar.pack(side=RIGHT, fill=Y, padx=(5, 0))

        # 滚轮（dialog 模式下 bind 这个 dialog 自身，离开 dialog 自动失效）
        def _on_wheel(event):
            delta = getattr(event, "delta", 0)
            num = getattr(event, "num", 0)
            if delta > 0 or num == 4:
                canvas.yview_scroll(-3, "units")
            elif delta < 0 or num == 5:
                canvas.yview_scroll(3, "units")
            return "break"
        for seq in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
            try:
                parent.bind(seq, _on_wheel)
            except Exception:
                pass

        form = self.settings_inner

        # Git 状态（绿字已就绪的提示由顶部 header badge 显示，此处仅保留警告）
        if not self.git_ok:
            ttkb.Label(form, text="✗ Git 未安装！请先安装 Git。",
                       font=("Segoe UI", 10), bootstyle=DANGER).pack(anchor=W, pady=(0, 15))

        # 配置区
        cfg = self.config_data

        # 目标
        ttkb.Label(form, text="GitHub 用户名或组织名", font=("Segoe UI", 11, "bold")).pack(anchor=W)
        self.s_target = tk.StringVar(value=cfg.get("target", ""))
        ttkb.Entry(form, textvariable=self.s_target, bootstyle=PRIMARY, width=50).pack(fill=X, pady=(2, 15))

        # Token
        ttkb.Label(form, text="GitHub Personal Access Token", font=("Segoe UI", 11, "bold")).pack(anchor=W)
        tk_frame = ttkb.Frame(form)
        tk_frame.pack(fill=X, pady=(2, 2))
        self.s_token = tk.StringVar(value="")
        # Token 哨兵：用户输入任何字符都标记为 modified，StringVar.trace 会触发
        self._s_token_modified = False
        self.s_token.trace_add("write", lambda *_: self._on_token_typed())
        self.s_token_entry = ttkb.Entry(tk_frame, textvariable=self.s_token, show="*", bootstyle=PRIMARY)
        self.s_token_entry.pack(side=LEFT, fill=X, expand=YES)
        ttkb.Button(tk_frame, text="👁", width=4, command=self._toggle_settings_token, bootstyle=SECONDARY).pack(side=LEFT, padx=(5, 0))
        ttkb.Button(tk_frame, text="验证", width=6, command=self._verify_settings_token, bootstyle=INFO).pack(side=LEFT, padx=(5, 0))
        self.s_token_result = tk.StringVar(value=f"当前: {mask_token(cfg.get('token', ''))}")
        ttkb.Label(form, textvariable=self.s_token_result, font=("Segoe UI", 9)).pack(anchor=W, pady=(2, 15))

        # 备份目录
        ttkb.Label(form, text="备份存储根目录", font=("Segoe UI", 11, "bold")).pack(anchor=W)
        dir_frame = ttkb.Frame(form)
        dir_frame.pack(fill=X, pady=(2, 15))
        self.s_dir = tk.StringVar(value=cfg.get("backup_dir", ""))
        ttkb.Entry(dir_frame, textvariable=self.s_dir, bootstyle=PRIMARY).pack(side=LEFT, fill=X, expand=YES)
        ttkb.Button(dir_frame, text="浏览...", command=self._browse_settings_dir, bootstyle=SECONDARY).pack(side=LEFT, padx=(5, 0))

        # 备份模式
        ttkb.Label(form, text="备份模式", font=("Segoe UI", 11, "bold")).pack(anchor=W)
        mode_frame = ttkb.Frame(form)
        mode_frame.pack(fill=X, pady=(2, 8))
        self.s_clone_mode = tk.StringVar(value=cfg.get("clone_mode", "mirror"))
        ttkb.Radiobutton(mode_frame, text="镜像模式 (git clone --mirror) — 完整备份所有引用，bare 仓库格式",
                         variable=self.s_clone_mode, value="mirror", bootstyle=PRIMARY).pack(anchor=W)
        ttkb.Radiobutton(mode_frame, text="普通模式 (git clone) — 含工作目录，可直接浏览文件",
                         variable=self.s_clone_mode, value="normal", bootstyle=PRIMARY).pack(anchor=W)
        ttkb.Label(mode_frame, text="切换模式后，已有仓库保持原格式，新备份的仓库使用新模式。",
                   font=("Segoe UI", 8), bootstyle=SECONDARY).pack(anchor=W, pady=(2, 0))

        # 频率
        ttkb.Label(form, text="备份频率", font=("Segoe UI", 11, "bold")).pack(anchor=W)
        freq_frame = ttkb.Frame(form)
        freq_frame.pack(fill=X, pady=(2, 8))
        self.s_sched = tk.StringVar(value=cfg.get("schedule_type", "daily"))
        # 3 选项：每天 / 每周 / 每月。yearly / custom 已取消（但模型仍兼容老 config）。
        for text, val in [
            ("每天", "daily"),
            ("每周", "weekly"),
            ("每月", "monthly"),
        ]:
            ttkb.Radiobutton(freq_frame, text=text, variable=self.s_sched,
                             value=val, bootstyle=PRIMARY,
                             command=self._toggle_settings_cron).pack(side=LEFT, padx=(0, 12))

        # 同一行：执行时间 + 右侧 selector
        # - daily:   HH:MM
        # - weekly:  HH:MM + 周几 Combobox（同行右侧）
        # - monthly: HH:MM + 几号 Combobox（同行右侧）
        time_frame = ttkb.Frame(form)
        time_frame.pack(fill=X, pady=(0, 12))
        ttkb.Label(time_frame, text="执行时间：", font=("Segoe UI", 10)).pack(side=LEFT)
        self.s_daily_hour = tk.IntVar(value=_clamp_int(cfg.get("daily_hour"), 2, 0, 23))
        self.s_daily_minute = tk.IntVar(value=_clamp_int(cfg.get("daily_minute"), 0, 0, 59))
        ttkb.Spinbox(time_frame, from_=0, to=23, textvariable=self.s_daily_hour,
                     width=4, bootstyle=PRIMARY, format="%02.0f").pack(side=LEFT, padx=(4, 2))
        ttkb.Label(time_frame, text=":", font=("Segoe UI", 11, "bold")).pack(side=LEFT)
        ttkb.Spinbox(time_frame, from_=0, to=59, textvariable=self.s_daily_minute,
                     width=4, bootstyle=PRIMARY, format="%02.0f").pack(side=LEFT, padx=(2, 16))

        # 右侧 selector — start hidden, _toggle_settings_cron reveals
        self.s_dow_label = ttkb.Label(time_frame, text="周几：")
        self.s_weekly_dow = tk.StringVar(value=cfg.get("weekly_day_of_week", "sun") or "sun")
        self.s_dow_combo = ttkb.Combobox(
            time_frame, textvariable=self.s_weekly_dow,
            values=["mon", "tue", "wed", "thu", "fri", "sat", "sun"],
            width=6, state="readonly", bootstyle=PRIMARY,
        )

        self.s_dom_label = ttkb.Label(time_frame, text="几号：")
        self.s_monthly_dom = tk.StringVar(
            value=str(_clamp_int(cfg.get("monthly_day_of_month"), 1, 1, 31))
        )
        self.s_dom_combo = ttkb.Combobox(
            time_frame, textvariable=self.s_monthly_dom,
            values=[str(i) for i in range(1, 32)],
            width=4, state="readonly", bootstyle=PRIMARY,
        )

        # 暂停调度器开关
        self.s_paused_var = tk.BooleanVar(value=cfg.get("scheduler_paused", False))
        ttkb.Checkbutton(
            form, text="暂停调度器（勾选后不自动备份，配置保留）",
            variable=self.s_paused_var,
            bootstyle=PRIMARY,
        ).pack(anchor=W, pady=(0, 12))

        self._toggle_settings_cron()

        # 重试
        retry_frame = ttkb.Frame(form)
        retry_frame.pack(fill=X, pady=(0, 15))
        ttkb.Label(retry_frame, text="重试次数", font=("Segoe UI", 11, "bold")).grid(row=0, column=0, sticky=W, padx=(0, 20))
        ttkb.Label(retry_frame, text="基础重试间隔(秒)", font=("Segoe UI", 11, "bold")).grid(row=0, column=1, sticky=W)
        self.s_retry = tk.IntVar(value=cfg.get("retry_count", 3))
        self.s_delay = tk.IntVar(value=cfg.get("retry_delay_base", 10))
        ttkb.Spinbox(retry_frame, from_=1, to=10, textvariable=self.s_retry, width=12, bootstyle=PRIMARY).grid(row=1, column=0, sticky=W, padx=(0, 20))
        ttkb.Spinbox(retry_frame, from_=1, to=300, textvariable=self.s_delay, width=12, bootstyle=PRIMARY).grid(row=1, column=1, sticky=W)

        # 指定额外仓库
        ttkb.Separator(form, bootstyle=SECONDARY).pack(fill=X, pady=10)
        ttkb.Label(form, text="指定额外备份仓库", font=("Segoe UI", 11, "bold")).pack(anchor=W)
        ttkb.Label(
            form,
            text="每行一个，格式 owner/repo。这些仓库会额外备份，不受目标用户/组织限制。\n# 开头的行会被忽略，可添加行内注释。",
            font=("Segoe UI", 9), bootstyle=SECONDARY,
        ).pack(anchor=W, pady=(2, 6))
        self.s_extra_text = tk.Text(form, height=5, width=60, font=("Consolas", 10))
        self.s_extra_text.pack(fill=X, pady=(2, 4))
        # 行内语法校验状态：实时显示 valid / invalid 行数
        self._s_extra_status = tk.StringVar(value="")
        ttkb.Label(form, textvariable=self._s_extra_status,
                   font=("Segoe UI", 9), bootstyle=SECONDARY).pack(anchor=W, pady=(0, 12))
        # 用户输入时刷新
        self.s_extra_text.bind("<KeyRelease>",
                               lambda e: self._refresh_extra_status())
        # 预填充
        existing_extra = cfg.get("extra_repos", [])
        if existing_extra:
            self.s_extra_text.insert("1.0", "\n".join(existing_extra))
        self._refresh_extra_status()  # 初始化

        # 分隔线（保存按钮已移到顶部固定栏）
        ttkb.Separator(form, bootstyle=SECONDARY).pack(fill=X, pady=10)
        # 系统定时任务参考 已移到 帮助 → 系统定时任务参考 菜单（_show_cron_reference）
        # 重置备份状态 已移到 设置菜单（_reset_state 调用点）
        # 不在这界面显示过多内容，保持清爽

    # ── 首次向导 ──

    def _show_setup_wizard(self) -> None:
        dlg = SetupDialog(self, self.config_data, self.git_ok, self.git_version)
        self.wait_window(dlg)
        if dlg.result:
            save_config(dlg.result)
            self.config_data = dlg.result
            start_scheduler(self.config_data)
            self._refresh_all()
            messagebox.showinfo("成功", "配置已保存！调度器已启动。", parent=self)

    def _show_git_missing_dialog(self) -> None:
        """git 未安装时启动时弹窗。模态，挡住主窗口直到用户关闭。"""
        GitMissingDialog(self, git_diag_msg=self.git_version or "git 命令未在 PATH 中找到")

    def _on_git_checked(self, ok: bool, ver: str) -> None:
        """后台 git 检测完成回调（主线程）。

        1) 更新 self.git_ok / git_version（所有后续依赖此状态的代码拿到真值）
        2) 刷新 header 徽章
        3) 若未安装且未 suppress，schedule 弹窗（沿用原 300ms 延迟，
           等 build_ui 跑完、mainloop 激活再弹）
        """
        self._refresh_git_status(ok, ver)
        if not ok and not self.config_data.get("suppress_git_warning", False):
            self.after(300, self._show_git_missing_dialog)

    def _refresh_git_status(self, ok: bool, ver: str) -> None:
        """git 状态变了之后被对话框"重试检测"回调。

        - 更新 self.git_ok / self.git_version
        - 重新配置 header 的 Git 状态徽章文字 + 配色（绿/红）
        - 任何依赖 git_ok 的操作（如 _on_backup_all 入口判断）下次调用看到新值
        """
        self.git_ok = ok
        self.git_version = ver
        if hasattr(self, "_git_status_label") and self._git_status_label.winfo_exists():
            git_text = f"✓ {ver}" if ok else "✗ Git 未安装"
            git_style = "GitOK.TLabel" if ok else "GitFail.TLabel"
            self._git_status_label.configure(text=git_text, style=git_style)

    def _next_run_text(self) -> str:
        """header 右侧"下次执行"文案：'下次：YYYY-MM-DD HH:MM' 或 '调度暂停' 等。"""
        try:
            cfg = load_config()
            if cfg.get("scheduler_paused", False):
                return "调度暂停"
            from backup_engine import get_next_run_time
            nxt = get_next_run_time()
            if nxt == "无调度":
                return nxt
            return f"下次备份：{nxt}"
        except Exception:
            return ""

    def _refresh_next_run_label(self) -> None:
        """每 30 秒刷新"下次执行时间"文字。"""
        if hasattr(self, "_next_run_label"):
            try:
                self._next_run_label.configure(text=self._next_run_text())
            except Exception:
                pass
        self.after(30_000, self._refresh_next_run_label)

    # ── 仪表盘操作 ──

    def _on_backup_all(self) -> None:
        if self.state_data.get("backup_in_progress", False):
            messagebox.showwarning("提示", "备份正在进行中，请等待完成。", parent=self)
            return
        if not self.git_ok:
            messagebox.showerror("错误", "Git 未安装，无法执行备份。", parent=self)
            return

        # 磁盘预检：先估个保守下限 < 500 MB，给用户一个明确告警
        backup_dir = Path(self.config_data.get("backup_dir", str(Path.home() / "github-backups")))
        try:
            free = shutil.disk_usage(backup_dir).free
            if free < 500 * 1024 * 1024:
                if not messagebox.askyesno("磁盘空间不足",
                    f"备份目录所在磁盘仅剩 {free // (1024*1024)} MB（< 500 MB）。\n"
                    "完整镜像备份一个仓库可能占用数十 MB 至数 GB，备份中可能中断。\n"
                    "是否继续？", parent=self):
                    return
        except OSError:
            pass  # 目录不存在或无权限检查——交给后续 mkdir 报错

        self.btn_backup_all.configure(state=DISABLED, text="备份进行中...")
        self.btn_cancel_backup.pack(side=LEFT, padx=(8, 0))
        self.progress_var.set(0)
        self.progress_label.configure(text="正在启动备份任务...")

        # 取消信号：worker 持引用，每仓间检查
        self._backup_stop_event = threading.Event()
        self._backup_thread_ref = None  # 用于等待 thread 退出

        def progress_cb(current, total, msg):
            self.msg_queue.put(("progress", (current, total, msg)))

        def worker():
            cfg = load_config()
            st = load_state()
            completed = run_full_backup(cfg, st, progress_callback=progress_cb,
                                        stop_event=self._backup_stop_event)
            self.msg_queue.put(("backup_done", completed))  # True=正常, False=取消

        self._backup_thread_ref = threading.Thread(target=worker, daemon=True)
        self._backup_thread_ref.start()

    def _on_cancel_backup(self) -> None:
        """按 ⏹ 停止备份：set() 之后 backup_engine 的看门狗线程在 ≤200ms 内
        proc.terminate() 打断正在跑的 git 子进程（clone / fetch / pull / fsck）。
        之前是仓间粒度（要等当前仓库跑完），现在是真正打断当前子进程。
        """
        if not hasattr(self, "_backup_stop_event") or self._backup_stop_event is None:
            return
        self._backup_stop_event.set()
        self.btn_cancel_backup.configure(state=DISABLED, text="正在停止...")
        add_log("收到停止信号，正在终止当前 git 子进程...", _logging.WARNING)

    def _on_open_backup_dir(self) -> None:
        """用系统默认文件管理器打开备份目录。"""
        backup_dir = Path(self.config_data.get("backup_dir",
                                              str(Path.home() / "github-backups")))
        if not backup_dir.exists():
            messagebox.showwarning("提示", f"备份目录不存在：\n{backup_dir}", parent=self)
            return

        try:
            if sys.platform == "win32":
                os.startfile(str(backup_dir))  # noqa
            elif sys.platform == "darwin":
                subprocess.Popen(["open", str(backup_dir)])
            else:
                subprocess.Popen(["xdg-open", str(backup_dir)])
        except Exception as e:
            messagebox.showerror("打开失败", f"无法打开目录：\n{e}", parent=self)

    def _show_about(self) -> None:
        """About 弹窗：版本、作者、菜单 + 快捷键速查。"""
        info = (
            f"GitHub Backup Manager v{__version__}\n"
            f"作者: {__author__}\n"
            f"协议: {__license__}\n"
            "\n"
            "📋 顶部菜单：\n"
            "  文件    立即备份 / 刷新 / 取消 / 打开目录 / 日志 / 退出\n"
            "  设置    系统配置… / 暂停调度器 / 重新初始化 / 重置状态\n"
            "  帮助    关于… / 系统定时任务参考 / 打开备份目录\n"
            "\n"
            "⌨ 快捷键：\n"
            "  Ctrl+R    立即备份所有仓库\n"
            "  F5        刷新仓库列表\n"
            "  Ctrl+L    切换到日志\n"
            "  Ctrl+,    打开系统配置\n"
            "  F1        关于本程序\n"
            "\n"
            "📁 仓库存储位置：\n"
            f"  {self.config_data.get('backup_dir', '—')}\n"
            "\n"
            "💾 配置文件：\n"
            f"  {BASE_DIR / 'config.json'}"
        )
        messagebox.showinfo("关于 GitHub Backup Manager", info, parent=self)

    def _show_cron_reference(self) -> None:
        """弹出系统定时任务参考（Windows / macOS / Linux Cron / Systemd Timer）。"""
        # 三平台示例：路径与脚本名只是占位，请替换为实际安装位置
        ref_text = (
            # ── Windows ────────────────────────────────────────────
            "═══ 【Windows】 Task Scheduler（任务计划程序） ═══\n"
            "\n"
            "  1. 准备 .bat 包装脚本（绕开 pythonw 弹黑框）：\n"
            "     把下面的 batch 保存到 C:\\Scripts\\github-backup.bat：\n"
            "\n"
            "       @echo off\n"
            "       cd /d C:\\path\\to\\GitHub-Backup-Manager\n"
            "       C:\\Python314\\python.exe app.py --cron-job >> C:\\path\\to\\logs\\cron.log 2>&1\n"
            "\n"
            "  2. 任务计划程序 → 创建基本任务：\n"
            "     - 触发器：每天 / 选定时间（例如 02:00）\n"
            "     - 操作：启动程序 → 程序或脚本 = C:\\Scripts\\github-backup.bat\n"
            "     - 常规：勾选『不管用户是否登录都要运行』+\n"
            "            『使用最高权限运行』（避免访问被拒）\n"
            "\n"
            "  3. PowerShell 一键注册（可选）：\n"
            "     $A = New-ScheduledTaskAction -Execute 'C:\\Scripts\\github-backup.bat'\n"
            "     $T = New-ScheduledTaskTrigger -Daily -At '02:00'\n"
            "     Register-ScheduledTask -TaskName 'GitHubBackup' -Action $A -Trigger $T \\\n"
            "         -User 'SYSTEM' -RunLevel Highest\n"
            "\n"
            # ── macOS ──────────────────────────────────────────────
            "\n\n"
            "═══ 【macOS】 launchd（推荐） / cron ═══\n"
            "\n"
            "  ── launchd（推荐，比 cron 可靠） ──\n"
            "  1. 准备 launch agent plist（保到 ~/Library/LaunchAgents/）：\n"
            "\n"
            "       <?xml version=\"1.0\" encoding=\"UTF-8\"?>\n"
            "       <!DOCTYPE plist PUBLIC \"-//Apple//DTD PLIST 1.0//EN\"\n"
            "                            \"http://www.apple.com/DTDs/PropertyList-1.0.dtd\">\n"
            "       <plist version=\"1.0\">\n"
            "       <dict>\n"
            "           <key>Label</key>             <string>com.liuhua.github-backup</string>\n"
            "           <key>ProgramArguments</key>\n"
            "           <array>\n"
            "               <string>/usr/local/bin/python3</string>\n"
            "               <string>/path/to/app.py</string>\n"
            "               <string>--cron-job</string>\n"
            "           </array>\n"
            "           <key>StartCalendarInterval</key>\n"
            "           <dict>\n"
            "               <key>Hour</key>   <integer>2</integer>\n"
            "               <key>Minute</key> <integer>0</integer>\n"
            "           </dict>\n"
            "           <key>StandardOutPath</key>   <string>/path/to/logs/cron.log</string>\n"
            "           <key>StandardErrorPath</key>  <string>/path/to/logs/cron.log</string>\n"
            "           <key>WorkingDirectory</key>  <string>/path/to/GitHub-Backup-Manager</string>\n"
            "       </dict>\n"
            "       </plist>\n"
            "\n"
            "  2. 注册 / 取消：\n"
            "     launchctl load ~/Library/LaunchAgents/com.liuhua.github-backup.plist\n"
            "     launchctl unload ~/Library/LaunchAgents/com.liuhua.github-backup.plist\n"
            "\n"
            "  ── cron（保留方案，遗留系统用） ──\n"
            "     crontab -e\n"
            "     0 2 * * * cd /path/to/GitHub-Backup-Manager && "
            "/usr/local/bin/python3 app.py --cron-job "
            ">> /path/to/logs/cron.log 2>&1\n"
            "\n"
            # ── Linux ──────────────────────────────────────────────
            "\n\n"
            "═══ 【Linux】 cron / systemd ═══\n"
            "\n"
            "  ── Cron ──\n"
            "     crontab -e\n"
            "     0 2 * * * cd /path/to/GitHub-Backup-Manager && "
            "/usr/bin/python3 app.py --cron-job "
            ">> /path/to/logs/cron.log 2>&1\n"
            "\n"
            "  ── Systemd Timer ──\n"
            "     # /etc/systemd/system/github-backup.service\n"
            "     [Service]\n"
            "     ExecStart=/usr/bin/python3 /path/to/app.py --cron-job\n"
            "\n"
            "     # /etc/systemd/system/github-backup.timer\n"
            "     [Timer]\n"
            "     OnCalendar=daily\n"
            "     Persistent=true\n"
            "\n"
            "     # 启用: systemctl enable --now github-backup.timer\n"
            "\n"
            "────────────────────────────────────────────────────\n"
            "💡 提示：内置调度器（APScheduler）适合『有用户会话时』\n"
            "     跑，需要 24×7 可靠运行请用上面任一系统级方案。\n"
        )
        if hasattr(self, "_cron_ref_dialog") and self._cron_ref_dialog is not None \
                and self._cron_ref_dialog.winfo_exists():
            self._cron_ref_dialog.lift()
            return
        dlg = ttkb.Toplevel(self)
        dlg.title("系统定时任务参考（Windows / macOS / Linux）")
        dlg.geometry("820x640")
        dlg.transient(self)
        apply_icon(dlg)  # 标题栏左上角图标
        set_light_titlebar(dlg)
        self._cron_ref_dialog = dlg

        ttkb.Label(
            dlg, text="系统级定时任务参考（Windows / macOS / Linux）",
            font=("Segoe UI", 12, "bold"),
        ).pack(anchor=W, padx=15, pady=(10, 8))
        box = ScrolledText(dlg, autohide=True, padding=8)
        box.pack(fill=BOTH, expand=YES, padx=15, pady=(0, 10))
        box.insert("1.0", ref_text)
        self._set_text_state(box, DISABLED)

        def on_close():
            dlg.destroy()
            self._cron_ref_dialog = None
        dlg.protocol("WM_DELETE_WINDOW", on_close)

    def _show_settings_dialog(self) -> None:
        """弹出系统配置对话框（菜单：设置 → 系统配置…）。

        ── 只允许一个实例：若已开则前置窗口。
        ── 表单通过 _build_settings_into(parent) 构建到对话框的 scroll_inner。
        ── 保存按钮触发 _save_settings_then_close，立即写入配置并关闭对话框。
        """
        # 防多开
        if hasattr(self, "_settings_dialog") and self._settings_dialog is not None \
                and self._settings_dialog.winfo_exists():
            self._settings_dialog.lift()
            self._settings_dialog.focus_force()
            return

        dlg = ttkb.Toplevel(self)
        dlg.title("系统配置 — GitHub Backup Manager")
        dlg.geometry("1140x780")
        dlg.transient(self)
        apply_icon(dlg)  # 标题栏左上角图标
        set_light_titlebar(dlg)
        self._settings_dialog = dlg

        # 构建表单到对话框
        self._build_settings_into(dlg)

        # 关闭时清理 self.* 引用
        def on_close():
            if self._settings_dialog is dlg:
                self._settings_dialog = None
            dlg.destroy()
        dlg.protocol("WM_DELETE_WINDOW", on_close)

    def _close_settings_dialog(self) -> None:
        if hasattr(self, "_settings_dialog") and self._settings_dialog is not None \
                and self._settings_dialog.winfo_exists():
            if self._settings_dialog is not None:
                self._settings_dialog.destroy()
            self._settings_dialog = None

    def _save_settings_then_close(self) -> None:
        """设置保存路径 + 关闭对话框版本。

        复用 _save_settings 的写入逻辑（更新 self.config_data / save_config /
        start_scheduler / 刷新）—— 不破坏现有 messagebox.showinfo 提示框。
        """
        try:
            self._save_settings()
        finally:
            # 无论是否成功都关（用户已经点了保存）
            self._close_settings_dialog()

    def _on_ctrl_s(self) -> None:
        """全局 Ctrl+S 处理：仅当设置对话框开着时触发保存。"""
        dlg = getattr(self, "_settings_dialog", None)
        if dlg is not None and dlg.winfo_exists():
            self._save_settings_then_close()
        # 否则静默忽略——避免误触发保存空状态

    def _on_backup_selected_kb(self) -> None:
        """菜单「文件 → 备份选中的仓库」入口。

        若未选择任何仓库，给出 messagebox 提示；如果有备份正在跑，挡掉。
        """
        if not self.config_data.get("is_configured"):
            messagebox.showwarning("提示", "请先完成配置。", parent=self)
            return
        if self.state_data.get("backup_in_progress", False):
            messagebox.showwarning("提示", "备份正在进行中，请等待完成。", parent=self)
            return
        sel = self.repo_tree.selection() if hasattr(self, "repo_tree") else ()
        if not sel:
            messagebox.showwarning(
                "提示", "请先在『仓库列表』里选中一个仓库，然后使用此功能。", parent=self,
            )
            # 顺手把用户切到仓库列表 tab，让选择有地方做
            try:
                self.notebook.select(self.tab_repos)
            except Exception:
                pass
            return
        self._on_backup_selected()

    def _on_toggle_scheduler_paused_menu(self) -> None:
        """设置菜单中的「暂停调度器」勾选框点击处理。

        把勾选状态落地到 config，并重启调度器。如果 UI 里的 settings dialog
        已经开着，也要同步更新它的 s_paused_var。
        """
        cfg = load_config()
        cfg["scheduler_paused"] = bool(self._scheduler_paused_var.get())
        save_config(cfg)
        self.config_data = cfg
        # 同步设置对话框里的 s_paused_var（如果开着）
        if hasattr(self, "s_paused_var"):
            try:
                self.s_paused_var.set(cfg["scheduler_paused"])
            except Exception:
                pass
        # 重启调度器
        start_scheduler(cfg)
        if cfg["scheduler_paused"]:
            messagebox.showinfo("已暂停", "调度器已暂停，不会自动备份。", parent=self)
        else:
            messagebox.showinfo("已恢复", "调度器已恢复运行。", parent=self)

    def _show_failures_panel(self) -> None:
        """打开/刷新失败详情弹窗：所有 status=failed 的仓库 + 错误详情。"""
        repos = self.state_data.get("repos", {})
        failed = [(full, info) for full, info in repos.items()
                  if info.get("status") == "failed"]
        if not failed:
            messagebox.showinfo("失败详情", "当前没有失败仓库。", parent=self)
            return

        FailuresDialog(self, failed)

    def _on_refresh_repos(self) -> None:
        if not self.config_data.get("is_configured"):
            messagebox.showwarning("提示", "请先完成配置。", parent=self)
            return

        def worker():
            cfg = load_config()
            st = load_state()
            ok, count, msg = refresh_repos_list(cfg, st)
            self.msg_queue.put(("refresh_done", (ok, count, msg)))

        threading.Thread(target=worker, daemon=True).start()

    # ── 仓库列表 ──

    def _populate_repos(self) -> None:
        for item in self.repo_tree.get_children():
            self.repo_tree.delete(item)

        repos = self.state_data.get("repos", {})

        # 空状态：冷启动 / 没配 target / 没拉过库时给个引导文案，而不是空白
        if not repos:
            empty_msg = "仓库列表为空" + (
                "，点上方「🔄 刷新列表」拉取 GitHub 仓库" if self.config_data.get("is_configured")
                else "，请先在设置页完成配置"
            )
            self.repo_tree.insert(
                "", END,
                values=(empty_msg, "", "", "", ""),
                tags=("empty",),
            )
            return

        keyword = self.search_var.get().lower() if hasattr(self, "search_var") else ""

        items = []
        for full_name, repo in repos.items():
            if keyword and keyword not in full_name.lower():
                continue
            items.append((
                repo.get("last_backup") or "",
                repo.get("status", "pending"),
                repo.get("source", "all"),
                repo,
            ))

        # 排序
        sort_by = self.sort_var.get() if hasattr(self, "sort_var") else "名称"
        if sort_by == "最后备份":
            items.sort(key=lambda x: x[0], reverse=True)
        elif sort_by == "状态":
            items.sort(key=lambda x: x[1])
        elif sort_by == "大小":
            items.sort(key=lambda x: x[3].get("size_bytes", 0), reverse=True)
        elif sort_by == "来源":
            items.sort(key=lambda x: x[2])
        else:
            # 默认按名称排序
            items.sort(key=lambda x: x[3].get("full_name", "").lower())

        # 来源显示文字映射
        source_labels = {"all": "全量", "extra": "指定"}

        for idx, (_, status, source, repo) in enumerate(items):
            full_name = repo.get("full_name", repo.get("name", ""))
            last = repo.get("last_backup")
            last_str = self._format_time(last) if last else "从未"
            size = repo.get("size", "N/A")
            source_str = source_labels.get(source, source)
            # 隔行变色：偶数行用浅色背景
            tags = (status,)
            if idx % 2 == 1:
                tags = (status, "stripe")
            self.repo_tree.insert("", END,
                values=(full_name, last_str, status, size, source_str),
                tags=tags)

    def _filter_repos(self, *args) -> None:
        self._populate_repos()

    def _on_repo_double_click(self, event) -> None:
        self._on_backup_selected()

    def _on_backup_selected(self) -> None:
        sel = self.repo_tree.selection()
        if not sel:
            messagebox.showwarning("提示", "请先选择一个仓库。", parent=self)
            return
        if self.state_data.get("backup_in_progress", False):
            messagebox.showwarning("提示", "备份正在进行中，请等待完成。", parent=self)
            return

        item = self.repo_tree.item(sel[0])
        full_name = item["values"][0]

        self.btn_backup_all.configure(state=DISABLED)

        def progress_cb(current, total, msg):
            self.msg_queue.put(("progress", (current, total, msg)))

        def worker():
            cfg = load_config()
            st = load_state()
            backup_single_repo(str(full_name), cfg, st, progress_callback=progress_cb)
            self.msg_queue.put(("backup_done", None))

        threading.Thread(target=worker, daemon=True).start()

    def _show_file_browser_placeholder(self, text: str = None, subtext: str = None) -> None:
        """显示文件浏览器占位符。"""
        for child in self.file_browser_container.winfo_children():
            child.destroy()
        placeholder = ttkb.Frame(self.file_browser_container)
        placeholder.pack(fill=BOTH, expand=YES)
        center = ttkb.Frame(placeholder)
        center.place(relx=0.5, rely=0.5, anchor=CENTER)
        ttkb.Label(center, text=text or "📂 文件浏览器",
                   font=("Segoe UI", 14, "bold"), bootstyle=SECONDARY).pack()
        if subtext is not None:
            ttkb.Label(center, text=subtext,
                       font=("Segoe UI", 9), bootstyle=SECONDARY).pack(pady=(6, 0))
        else:
            ttkb.Label(center, text="选择上方仓库即可自动加载文件内容",
                       font=("Segoe UI", 9), bootstyle=SECONDARY).pack(pady=(6, 0))

    def _on_repo_right_click(self, event) -> None:
        """右键仓库表弹上下文菜单：复制 / 在文件管理器打开 / 备份。"""
        # 先选中当前右键击中的行
        row = self.repo_tree.identify_row(event.y)
        if row:
            self.repo_tree.selection_set(row)

        # 懒创建 menu（首次右键时构造，之后复用）
        if not hasattr(self, "_repo_context_menu") or self._repo_context_menu is None:
            m = tk.Menu(self, tearoff=0)
            m.add_command(label="📦 备份此仓库", command=self._on_backup_selected)
            m.add_separator()
            m.add_command(label="📋 复制 owner/repo", command=self._copy_selected_repo_name)
            m.add_command(label="🗂 在文件管理器打开备份", command=self._open_selected_repo_dir)
            m.add_separator()
            m.add_command(label="📋 查看失败详情", command=self._show_failures_panel)
            self._repo_context_menu = m
        else:
            m = self._repo_context_menu

        sel = self.repo_tree.selection()
        state = NORMAL if sel else DISABLED
        # 0 备份 / 1 分隔 / 2 复制 / 3 打开文件管理器 / 4 分隔 / 5 失败详情
        for idx in (0, 2, 3, 5):
            try:
                m.entryconfig(idx, state=state)
            except Exception:
                pass

        try:
            m.tk_popup(event.x_root, event.y_root)
        finally:
            m.grab_release()

    def _copy_selected_repo_name(self) -> None:
        """复制当前选中的仓库 owner/repo 到剪贴板。"""
        sel = self.repo_tree.selection()
        if not sel:
            return
        full_name = str(self.repo_tree.item(sel[0])["values"][0])
        self.clipboard_clear()
        self.clipboard_append(full_name)

    def _open_selected_repo_dir(self) -> None:
        """在系统文件管理器打开选中仓库的本地备份目录（.git 或裸目录）。"""
        sel = self.repo_tree.selection()
        if not sel:
            return
        full_name = str(self.repo_tree.item(sel[0])["values"][0])
        repo_name = full_name.split("/")[-1] if "/" in full_name else full_name
        backup_dir = Path(self.config_data.get("backup_dir",
                                              str(Path.home() / "github-backups")))
        # 兼容 mirror / normal 两种格式
        for candidate in (backup_dir / f"{repo_name}.git", backup_dir / repo_name):
            if candidate.exists():
                try:
                    if sys.platform == "win32":
                        os.startfile(str(candidate))  # noqa
                    elif sys.platform == "darwin":
                        subprocess.Popen(["open", str(candidate)])
                    else:
                        subprocess.Popen(["xdg-open", str(candidate)])
                except Exception as e:
                    messagebox.showerror("打开失败", f"无法打开目录：\n{e}", parent=self)
                return
        messagebox.showinfo("提示", "该仓库尚未备份，无法打开。", parent=self)

    def _on_repo_selected(self, event=None) -> None:
        """选中仓库后自动加载文件浏览器（无弹窗）。"""
        sel = self.repo_tree.selection()
        if not sel:
            return

        item = self.repo_tree.item(sel[0])
        full_name = str(item["values"][0])
        repo_name = full_name.split("/")[-1] if "/" in full_name else full_name

        backup_dir = Path(self.config_data.get("backup_dir", str(Path.home() / "github-backups")))
        mirror_path = backup_dir / f"{repo_name}.git"
        normal_path = backup_dir / repo_name

        if mirror_path.exists():
            repo_path = mirror_path
        elif normal_path.exists():
            repo_path = normal_path
        else:
            self._show_file_browser_placeholder(
                f"📂 {repo_name} 尚未备份",
                "请先备份该仓库后再浏览文件",
            )
            return

        ret, _, _ = run_git_command(["git", "rev-parse", "--git-dir"], cwd=repo_path)
        if ret != 0:
            self._show_file_browser_placeholder(
                "⚠️ 无效的 Git 仓库",
                str(repo_path),
            )
            return

        # 清空容器并嵌入文件浏览器
        for child in self.file_browser_container.winfo_children():
            child.destroy()
        RepoFileManager(self.file_browser_container, repo_name, repo_path)

    # ── 日志 ──

    def _refresh_logs(self) -> None:
        source = self.log_source_var.get() if hasattr(self, "log_source_var") else "当前会话日志"
        keyword = self.log_search_var.get() if hasattr(self, "log_search_var") else ""

        # items: list of (text, level)；会话缓冲自带 level，日志文件靠解析 [LEVEL] 标签
        items: list[tuple[str, int]] = []
        if source == "完整日志文件":
            items = self._load_log_file_with_levels()
        else:
            items = log_buffer.get_all_with_levels()

        # 关键词过滤
        if keyword:
            kw = keyword.lower()
            items = [(l, lvl) for l, lvl in items if kw in l.lower()]

        tw = _get_text_widget(self.log_text)
        tw.configure(state=NORMAL)
        tw.delete("1.0", END)
        for text, level in items:
            tag = self._log_level_tag(level)
            if tag:
                tw.insert(END, text + "\n", tag)
            else:
                tw.insert(END, text + "\n")
        tw.see(END)
        tw.configure(state=DISABLED)

    def _load_log_file_with_levels(self) -> list[tuple[str, int]]:
        """读取 backup.log，从日志行 `[LEVEL]` 字段还原 level。

        文件格式 `%(asctime)s [%(levelname)s] %(message)s`，例如：
        `2026-07-05 17:40:31 [ERROR] [markdoc] 克隆失败: ...`
        """
        import re
        pattern = re.compile(r'\[(ERROR|WARNING|INFO|DEBUG|CRITICAL)\]')
        out: list[tuple[str, int]] = []
        try:
            if LOG_FILE.exists():
                with open(LOG_FILE, "r", encoding="utf-8") as f:
                    for raw in f.readlines()[-2000:]:
                        line = raw.rstrip("\n")
                        m = pattern.search(line)
                        if m:
                            level = getattr(_logging, m.group(1), _logging.INFO)
                        else:
                            level = _logging.INFO
                        out.append((line, level))
        except OSError:
            pass
        return out

    def _export_logs(self) -> None:
        try:
            if LOG_FILE.exists():
                content = LOG_FILE.read_text(encoding="utf-8")
            else:
                content = ""
            filename = f"backup_log_{datetime.date.today()}.txt"
            path = filedialog.asksaveasfilename(
                initialfile=filename,
                defaultextension=".txt",
                filetypes=[("Text files", "*.txt"), ("All files", "*.*")],
                parent=self,
            )
            if path:
                Path(path).write_text(content, encoding="utf-8")
                messagebox.showinfo("成功", f"日志已导出到:\n{path}", parent=self)
        except Exception as e:
            messagebox.showerror("错误", f"导出失败: {e}", parent=self)

    def _clear_logs(self) -> None:
        if messagebox.askyesno("确认", "确定清空当前会话的日志缓冲区吗？（日志文件不受影响）", parent=self):
            log_buffer.clear()
            self._refresh_logs()

    # ── 设置 ──

    def _toggle_settings_token(self) -> None:
        if self.s_token_entry.cget("show") == "*":
            self.s_token_entry.config(show="")
        else:
            self.s_token_entry.config(show="*")

    def _verify_settings_token(self) -> None:
        token = self.s_token.get().strip()
        if not token:
            self.s_token_result.set("请输入要验证的 Token")
            return
        self.s_token_result.set("验证中...")

        def do_verify():
            ok, msg = verify_token(token)
            self.after(0, lambda: self.s_token_result.set(
                f"✓ Token 有效，用户: {msg}" if ok else f"✗ {msg}"
            ))
        threading.Thread(target=do_verify, daemon=True).start()

    def _browse_settings_dir(self) -> None:
        d = filedialog.askdirectory(initialdir=Path.home(), parent=self)
        if d:
            self.s_dir.set(d)

    def _toggle_settings_cron(self) -> None:
        """按选中的 schedule_type 在时间行右侧显示对应 selector。

        与 SetupDialog._toggle_cron 等价的 SettingsPanel 版本。
        """
        mode = self.s_sched.get()
        # 全隐
        for w in (self.s_dow_label, self.s_dow_combo,
                  self.s_dom_label, self.s_dom_combo):
            try:
                w.pack_forget()
            except Exception:
                pass

        if mode == "weekly":
            self.s_dow_label.pack(side=LEFT, padx=(0, 4))
            self.s_dow_combo.pack(side=LEFT)
        elif mode == "monthly":
            self.s_dom_label.pack(side=LEFT, padx=(0, 4))
            self.s_dom_combo.pack(side=LEFT)
        # daily: 右侧什么都不显示
        # 老 config 里如果是 yearly/custom：也不显示（UI 不暴露，
        # 但保存时会被切到 daily/weekly/monthly）


    def _on_token_typed(self) -> None:
        """用户敲了 token 输入框任意字符 → 标记为已修改。"""
        # _s_token_modified 默认 False；用户输入第一个字符就会被 trace 触发设为 True
        if self.s_token.get():
            self._s_token_modified = True

    def _refresh_extra_status(self) -> None:
        """实时显示 extra_repos 输入框的 valid / invalid 行数（仅当 Text widget 已初始化时）。"""
        if not hasattr(self, "_s_extra_status"):
            return
        try:
            text = self.s_extra_text.get("1.0", END)
        except Exception:
            return
        valid, invalid = parse_extra_repos(text)
        if not valid and not invalid:
            self._s_extra_status.set("")
        elif invalid:
            self._s_extra_status.set(
                f"✅ {len(valid)} 行有效    ⚠ {len(invalid)} 行格式无效（将忽略）"
            )
        else:
            self._s_extra_status.set(f"✅ {len(valid)} 行有效")

    def _save_settings(self) -> None:
        target = self.s_target.get().strip()
        token = self.s_token.get().strip()
        backup_dir = self.s_dir.get().strip()

        if not target:
            messagebox.showwarning("提示", "目标不能为空", parent=self)
            return
        if not backup_dir:
            backup_dir = str(Path.home() / "github-backups")

        try:
            Path(backup_dir).mkdir(parents=True, exist_ok=True)
        except OSError as e:
            messagebox.showerror("错误", f"无法创建备份目录: {e}", parent=self)
            return

        cfg = dict(self.config_data)
        cfg["target"] = target
        cfg["backup_dir"] = backup_dir
        cfg["schedule_type"] = self.s_sched.get()
        # cron_expression 不再写入新值（UI 不再暴露 raw cron）。
        # 保留字段以兼容老 config：用已有值或默认 daily cron
        cfg["cron_expression"] = (
            self.config_data.get("cron_expression") or "0 2 * * *"
        )
        cfg["retry_count"] = self.s_retry.get()
        cfg["retry_delay_base"] = self.s_delay.get()
        cfg["clone_mode"] = self.s_clone_mode.get()
        # Token：用 trace 哨兵判断是否真修改了，比星号启发可靠
        if self._s_token_modified and token:
            cfg["token"] = token

        if not cfg.get("token"):
            messagebox.showwarning("提示", "Token 不能为空", parent=self)
            return

        schedule_type = self.s_sched.get()
        cfg["schedule_type"] = schedule_type
        cfg["daily_hour"] = _clamp_int(self.s_daily_hour.get(), 0, 0, 23) or 2
        cfg["daily_minute"] = _clamp_int(self.s_daily_minute.get(), 0, 0, 59)
        cfg["weekly_day_of_week"] = str(self.s_weekly_dow.get()) or "sun"
        # monthly_dom 现在是 StringVar（Combobox 值 "1".."31"）；_clamp_int 处理空串
        cfg["monthly_day_of_month"] = _clamp_int(self.s_monthly_dom.get(), 1, 1, 31)
        # yearly_* 不再被新 UI 写入；保留老值让老 config 结构不破
        cfg["yearly_month"] = int(self.config_data.get("yearly_month") or 1)
        cfg["yearly_day_of_month"] = int(self.config_data.get("yearly_day_of_month") or 1)
        cfg["scheduler_paused"] = bool(self.s_paused_var.get())
        cfg["sort_option"] = self.sort_var.get() if hasattr(self, "sort_var") else "名称"

        # 解析指定仓库
        extra_text = self.s_extra_text.get("1.0", END).strip()
        extra_valid, extra_invalid = parse_extra_repos(extra_text)
        if extra_invalid:
            if not messagebox.askyesno("格式警告",
                f"以下行格式无效（应为 owner/repo），将被忽略：\n{chr(10).join(extra_invalid)}\n\n是否继续保存？",
                parent=self):
                return
        cfg["extra_repos"] = extra_valid

        save_config(cfg)
        self.config_data = cfg
        started = start_scheduler(cfg)
        # 清空 token 输入框 + 重置哨兵，避免下一次"误保存"
        self.s_token.set("")
        self._s_token_modified = False
        self.s_token_result.set(f"当前: {mask_token(cfg.get('token', ''))}")
        pause_msg = "" if not cfg.get("scheduler_paused") else "\n⚠ 调度器已暂停，不会自动备份"
        extra_count = len(extra_valid)
        started_msg = "调度器已自动重启" if started else "调度器状态变更"
        messagebox.showinfo("成功",
            f"设置已保存！{started_msg}。{pause_msg}\n指定额外仓库：{extra_count} 个",
            parent=self)

    def _reset_state(self) -> None:
        if messagebox.askyesno("确认", "确定要重置所有备份状态吗？此操作不可撤销。\n（不会删除实际备份文件）", parent=self):
            self.state_data = json.loads(json.dumps(DEFAULT_STATE))
            save_state(self.state_data)
            log_buffer.clear()
            self._refresh_all()
            messagebox.showinfo("成功", "状态已重置。", parent=self)

    # ── 队列轮询与刷新 ──

    def _animate_progress_to(self, target: float, duration_ms: int = 320) -> None:
        """将进度条从当前值平滑插值到 target。

        进度回调是离散的（每个仓库推送一次），直接 set 会"啪"地跳一下。这里用
        ~60fps 的 after 链做线性插值，duration 短到不会被察觉延迟，足够掩盖跳跃感。

        - target 已在 ±0.5px 范围内 → 直接 snap，不再起 tween
        - 已有进行中的 tween 被覆盖：以新 target 为终点的全新过渡
        """
        start = self.progress_var.get()
        delta = target - start
        if abs(delta) < 0.5:
            self.progress_var.set(target)
            self._progress_tween = None
            return
        steps = max(1, int(duration_ms / 16))  # 16ms ≈ 60fps
        self._progress_tween = {
            "delta": delta / steps,
            "left": steps,
            "target": target,
        }
        self._tick_progress_animation()

    def _tick_progress_animation(self) -> None:
        tween = getattr(self, "_progress_tween", None)
        if not tween:
            return
        if tween["left"] <= 0:
            self.progress_var.set(tween["target"])
            self._progress_tween = None
            return
        self.progress_var.set(self.progress_var.get() + tween["delta"])
        tween["left"] -= 1
        self.after(16, self._tick_progress_animation)

    def _poll_queue(self) -> None:
        # 每次 tick 都重新读 in_progress —— worker 写 state_data 后下次 poll
        # 立刻用新值，否则 backup 启动那一拍界面要 1s 才加速到 100ms 拉取
        in_progress = bool(self.state_data.get("backup_in_progress"))

        try:
            while True:
                msg_type, data = self.msg_queue.get_nowait()
                if msg_type == "progress":
                    current, total, msg = data
                    if total > 0:
                        pct = (current / total) * 100
                        self._animate_progress_to(pct)
                    self.progress_label.configure(text=msg)
                    self._last_progress_msg = msg  # 给 backup_done 系统通知用
                    self._update_dash_log()
                elif msg_type == "backup_done":
                    # data: True=正常完成, False=取消
                    cancelled = data is False
                    self.btn_backup_all.configure(
                        state=NORMAL,
                        text="🚀 立即备份所有仓库" if not cancelled
                        else "🚀 立即备份所有仓库")
                    self.btn_cancel_backup.pack_forget()
                    self.btn_cancel_backup.configure(state=NORMAL, text="⏹ 停止备份")
                    if not cancelled:
                        self._animate_progress_to(100, duration_ms=400)
                    self._refresh_all()
                    self._update_dash_log()
                    self._backup_stop_event = None  # 清空引用
                    # 系统通知（仅正常完成时；取消不发）
                    if not cancelled:
                        summary = getattr(self, "_last_progress_msg", "备份完成")
                        system_notify("GitHub Backup Manager", summary)
                elif msg_type == "refresh_done":
                    ok, count, msg = data
                    # 成功时静默刷新 UI 即可，只在失败时弹错误窗
                    if not ok:
                        messagebox.showerror("失败", msg, parent=self)
                    self._refresh_all()
        except queue.Empty:
            pass

        # 备份进行中 → 100ms 拉一次；空闲 → 回到 1s
        # 备份期实时日志 + worker 心跳事件能及时被吸到 UI
        if in_progress:
            self._update_dash_log()
            self.after(100, self._poll_queue)
        else:
            self.after(1000, self._poll_queue)

    def _drain_async_results(self) -> None:
        """轮询 self._async_results（后台 thread 写、主线程读）。

        为什么不用子线程直接 self.after(0, ...)：Tk 的 after() 不是 thread-safe，
        子线程 schedule 的事件主线程 update() 看不到。queue.Queue 是 thread-safe 的，
        所以走 put + 主线程 get_nowait 的范式。50ms tick 启动期感知不到延迟，
        后台任务一般 100-300ms 完成。
        """
        try:
            while True:
                # 队列里塞 (kind, *args) —— 比如 ("git_check", ok, ver)
                # 但 get_nowait 返回单值，要按 kind 长度灵活解包
                item = self._async_results.get_nowait()
                kind = item[0]
                if kind == "git_check":
                    _, ok, ver = item
                    self._on_git_checked(ok, ver)
                elif kind == "dir_size":
                    _, size_str = item
                    self.lbl_total_size.configure(text=size_str)
        except queue.Empty:
            pass
        # 持续 poll —— 即使现在没结果也不停（防止后续有任务）
        # 用 1s 间隔即可，UI 更新不敏感
        self.after(1000, self._drain_async_results)

    def _update_dash_log(self) -> None:
        items = log_buffer.get_all_with_levels()[-200:]
        tw = _get_text_widget(self.dash_log)
        tw.configure(state=NORMAL)
        tw.delete("1.0", END)
        for text, level in items:
            tag = self._log_level_tag(level)
            if tag:
                tw.insert(END, text + "\n", tag)
            else:
                tw.insert(END, text + "\n")
        tw.see(END)
        tw.configure(state=DISABLED)

    def _refresh_all(self) -> None:
        """刷新所有页面的数据。"""
        self.config_data = load_config()
        self.state_data = load_state()
        self._update_dashboard()
        self._populate_repos()
        self._refresh_logs()

    def _update_dashboard(self) -> None:
        repos = self.state_data.get("repos", {})
        total = len(repos)
        success = sum(1 for r in repos.values() if r.get("status") == "success")
        failed = sum(1 for r in repos.values() if r.get("status") == "failed")

        self.lbl_total.configure(text=str(total))
        self.lbl_success.configure(text=str(success))
        self.lbl_failed.configure(text=str(failed))

        backup_dir = Path(self.config_data.get("backup_dir", str(Path.home() / "github-backups")))
        self.lbl_disk.configure(text=get_disk_free(backup_dir))

        last = self.state_data.get("last_full_backup")
        if last:
            time_str = self._format_time(last)
            # 加上"距今多久"
            try:
                dt = datetime.datetime.fromisoformat(last)
                delta = datetime.datetime.now() - dt
                secs = int(delta.total_seconds())
                if secs < 60:
                    ago = "刚刚"
                elif secs < 3600:
                    ago = f"{secs // 60} 分钟前"
                elif secs < 86400:
                    ago = f"{secs // 3600} 小时前"
                else:
                    ago = f"{secs // 86400} 天前"
                self.lbl_last_backup.configure(text=f"{time_str}（{ago}）")
            except (ValueError, TypeError):
                self.lbl_last_backup.configure(text=time_str)
        else:
            self.lbl_last_backup.configure(text="从未")

        # 备份总大小：原同步 get_dir_size 在大仓库时阻塞 100ms+。
        # 改成后台 thread 算，主线程先显示"计算中…"，算完通过 queue 通知主线程刷新标签。
        if not backup_dir.exists():
            self.lbl_total_size.configure(text="N/A")
        else:
            self.lbl_total_size.configure(text="计算中…")

            def _bg_dir_size() -> None:
                size_str = get_dir_size(backup_dir)
                self._async_results.put(("dir_size", size_str))
            threading.Thread(target=_bg_dir_size, daemon=True).start()

        self.lbl_target.configure(text=self.config_data.get("target") or "未设置")

        # 指定额外仓库数
        extra_count = len(self.config_data.get("extra_repos", []))
        extra_backed = sum(1 for r in repos.values() if r.get("source") == "extra")
        self.lbl_extra.configure(text=f"{extra_count} 个（已备份 {extra_backed}）")

        if self.state_data.get("backup_in_progress"):
            self.btn_backup_all.configure(state=DISABLED, text="备份进行中...")
        else:
            self.btn_backup_all.configure(state=NORMAL, text="🚀 立即备份所有仓库")

    @staticmethod
    def _format_time(iso_str: str | None) -> str:
        if not iso_str:
            return "从未"
        try:
            dt = datetime.datetime.fromisoformat(iso_str)
            return dt.strftime("%Y-%m-%d %H:%M:%S")
        except (ValueError, TypeError):
            return str(iso_str)

    # ── 关闭 ──

    def _tk_excepthook(self, exc: type[BaseException], val: BaseException, tb) -> None:
        """Tk 全局回调异常钩子：button 回调里抛的异常原本会被 Tk 默默吞掉，
        现在打印到 stderr + 写到日志，让『按钮没反应』的情况有迹可循。
        同步用到标准库的 sys.excepthook 兜底（极少数路径会走到那里）。
        """
        try:
            import traceback as _tb
            msg = "".join(_tb.format_exception(exc, val, tb))
            print(msg, file=sys.stderr, flush=True)
            try:
                add_log(f"[UI callback] {exc.__name__}: {val}\n{msg}", logging.ERROR)
            except Exception:
                pass
        finally:
            # 顺手让 Python 本身也走一遍默认钩子（万一 excepthook 被嵌入了别的）
            sys.excepthook(exc, val, tb)

    def _on_close(self) -> None:
        """关闭主窗口。

        如果有备份正在跑：
        - 自动 `stop_event.set()` 让 backup_engine 看门狗在 ≤200ms 内 terminate 子进程
        - 等 1.5s 收尾（不阻塞太久）
        - 超过则强制退出（daemon thread 会被进程退出带走，用户已经点过确认了）
        """
        # ── 备份中提示（不静默关掉） ──
        in_progress = bool(self.state_data.get("backup_in_progress"))
        if in_progress:
            ok = messagebox.askyesno(
                "关闭确认",
                "当前有备份任务正在运行。\n\n"
                "关闭程序会立即中断正在执行的 git 子进程，"
                "部分仓库的状态可能不完整（下次会重新增量同步）。\n\n"
                "确认要关闭吗？",
                parent=self,
            )
            if not ok:
                return
            # 让 backup_engine 看门狗立即 terminate 子进程
            stop_event = getattr(self, "_backup_stop_event", None)
            if stop_event is not None:
                try:
                    stop_event.set()
                except Exception:
                    pass
            # 给 1.5s 让 worker 自己跑完取消路径
            worker = getattr(self, "_backup_thread_ref", None)
            if worker is not None and worker.is_alive():
                worker.join(timeout=1.5)

        save_window_state(BASE_DIR, self)
        stop_scheduler()
        self.destroy()


# ════════════════════════════════════════════════════════════════════
# 命令行备份模式（供 cron / systemd 调用）
# ════════════════════════════════════════════════════════════════════

def run_cli_backup() -> int:
    """命令行模式：读取已有配置执行一次备份，不启动 GUI。"""
    config = load_config()
    if not config.get("is_configured"):
        print("错误：尚未配置。请先运行 GUI 完成设置。", file=sys.stderr)
        return 1

    git_ok, _ = check_git()
    if not git_ok:
        print("错误：未检测到 git，请先安装。", file=sys.stderr)
        return 1

    state = load_state()
    run_full_backup(config, state)
    return 0


# ════════════════════════════════════════════════════════════════════
# 主入口
# ════════════════════════════════════════════════════════════════════

def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="GitHub Backup Manager - 桌面应用")
    parser.add_argument(
        "--cron-job", action="store_true",
        help="命令行模式：执行一次备份后退出（不启动 GUI，供 cron/systemd 调用）",
    )
    args = parser.parse_args()

    if args.cron_job:
        sys.exit(run_cli_backup())

    # GUI 模式
    app = BackupApp()
    app.mainloop()


if __name__ == "__main__":
    main()
