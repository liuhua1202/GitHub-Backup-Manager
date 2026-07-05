#!/usr/bin/env python3
"""诊断脚本：捕获所有启动错误"""
import sys
import traceback
from pathlib import Path

print("=" * 60)
print("GitHub Backup Manager 启动诊断")
print("=" * 60)
print(f"Python 版本: {sys.version}")
print(f"Python 路径: {sys.executable}")
print()


def _pkg_version(distribution_name: str, fallback_mod=None) -> str:
    """取一个包的版本号。优先 importlib.metadata（标准库），fallback mod.__version__。"""
    try:
        from importlib import metadata as _md
        return _md.version(distribution_name)
    except Exception:
        if fallback_mod is not None:
            v = getattr(fallback_mod, "__version__", None)
            if v:
                return v
        return "<未知>"


# 1. 检查依赖
print("[1/5] 检查依赖包...")
try:
    import tkinter
    print("  ✓ tkinter - 内置")
except ImportError as e:
    print(f"  ✗ tkinter - 缺失: {e}")

try:
    import ttkbootstrap
    print(f"  ✓ ttkbootstrap - {_pkg_version('ttkbootstrap', ttkbootstrap)}")
except ImportError as e:
    print(f"  ✗ ttkbootstrap - 缺失: {e}")
    print("    安装命令: pip install ttkbootstrap")

try:
    import apscheduler
    print(f"  ✓ APScheduler - {_pkg_version('APScheduler', apscheduler)}")
except ImportError as e:
    print(f"  ✗ APScheduler - 缺失: {e}")
    print("    安装命令: pip install apscheduler")

print()

# 2. 检查 backup_engine 导入
print("[2/5] 检查 backup_engine 模块...")
try:
    # 与该脚本同目录导入 backup_engine（不依赖任何机器特定路径）
    SCRIPT_DIR = Path(__file__).resolve().parent
    sys.path.insert(0, str(SCRIPT_DIR))
    from backup_engine import load_config, check_git
    print("  ✓ backup_engine 导入成功")

    # 检查 git
    git_ok, git_ver = check_git()
    if git_ok:
        print(f"  ✓ Git - {git_ver}")
    else:
        print(f"  ⚠ Git - 未检测到: {git_ver}")
except Exception as e:
    print(f"  ✗ backup_engine 导入失败: {e}")
    traceback.print_exc()

print()

# 3. 检查配置文件
print("[3/5] 检查配置文件...")
try:
    cfg = load_config()
    print(f"  ✓ 配置加载成功")
    print(f"    - 目标: {cfg.get('target', '(空)')}")
    print(f"    - Token: {'已设置' if cfg.get('token') else '(空)'}")
    print(f"    - 备份目录: {cfg.get('backup_dir')}")
    print(f"    - 是否已配置: {cfg.get('is_configured')}")
except Exception as e:
    print(f"  ✗ 配置加载失败: {e}")
    traceback.print_exc()

print()

# 4. 尝试创建主窗口（不启动事件循环）
print("[4/5] 测试 GUI 初始化...")
try:
    import tkinter as tk
    from ttkbootstrap.constants import *

    # 测试 ttkbootstrap Window（用主程序同款主题以校验主题加载）
    import ttkbootstrap as ttkb
    root = ttkb.Window(themename="flatly")
    root.title("测试窗口")
    root.geometry("300x200")

    # 添加一个标签测试
    ttkb.Label(root, text="GUI 初始化成功！", font=("Segoe UI", 14)).pack(pady=50)

    print("  ✓ GUI 窗口创建成功")
    print("  ✓ ttkbootstrap 主题加载成功")

    # 显示 2 秒后关闭
    root.after(2000, root.destroy)
    root.mainloop()
    print("  ✓ 事件循环正常退出")

except Exception as e:
    print(f"  ✗ GUI 初始化失败: {e}")
    traceback.print_exc()

print()

# 5. 尝试完整导入 app 模块
print("[5/5] 检查 app 模块导入...")
try:
    import importlib
    import app as app_module
    print("  ✓ app 模块导入成功")
    print(f"    - 包含类: BackupApp, SetupDialog")
    print(f"    - 包含函数: main, run_cli_backup, start_scheduler")
except Exception as e:
    print(f"  ✗ app 模块导入失败: {e}")
    traceback.print_exc()

print()
print("=" * 60)
print("诊断完成")
print("=" * 60)
print()
input("按回车键退出...")
