# Windows 任务栏图标修复 —— Tk 应用完整指南

> 适用场景：用 **Python + Tkinter / ttkbootstrap** 写的桌面应用，PyInstaller `--onefile` 打包成 exe 后，**任务栏 / 标题栏 / 对话框左上角图标不对**。本文给出一套生产可用的修复方案，附完整代码、验证方法、踩坑清单。

---

## 目录

1. [问题现象](#1-问题现象)
2. [Windows 图标系统基础](#2-windows-图标系统基础)
3. [Tk 的三个大坑](#3-tk-的三个大坑)
4. [完整方案：三层覆盖](#4-完整方案三层覆盖)
5. [完整代码](#5-完整代码)
6. [PyInstaller 配置](#6-pyinstaller-配置)
7. [验证方法](#7-验证方法)
8. [Windows Icon Cache 处理](#8-windows-icon-cache-处理)
9. [踩坑速查清单](#9-踩坑速查清单)
10. [参考资料](#10-参考资料)

---

## 1. 问题现象

打包后的 `app.exe` 在 Windows 10/11 上跑：

| 位置 | 表现 |
|---|---|
| exe 资源图标（资源管理器、开始菜单） | ✅ 正确（PyInstaller `--icon` 设了） |
| 主窗口**标题栏左上角** | ❌ Tk 默认调色板图标（彩色圆点）|
| 任务栏（运行中应用） | ❌ Tk 默认调色板图标 |
| 所有 `Toplevel` 对话框**左上角** | ❌ Tk 默认调色板图标 |
| 系统菜单（窗口左上角点击弹出的菜单）| ❌ Tk 默认调色板图标 |

源码模式（`python app.py`）下也可能有同样问题，但用户通常在打包后才注意到。

---

## 2. Windows 图标系统基础

Windows 的窗口图标有 **4 个独立的来源**，分别服务不同场景：

```
┌─────────────────────────────────────────────────────────┐
│ 1. exe 资源图标                                          │
│    Windows 启动时从 .exe 的 RT_ICON 资源里读             │
│    用途：资源管理器、开始菜单、任务栏【固定】的图标       │
│    设置：PyInstaller --icon / VS 资源管理器              │
├─────────────────────────────────────────────────────────┤
│ 2. WNDCLASSEX.HICON  （class icon / 大图标）             │
│    注册窗口类时设置的"默认"图标                          │
│    用途：未组合任务栏、窗口系统菜单的图标                │
│    设置：RegisterClassExW / SetClassLongPtrW GCLP_HICON  │
├─────────────────────────────────────────────────────────┤
│ 3. WNDCLASSEX.HICONSM  （class icon / 小图标）           │
│    同上，16x16 版本                                     │
│    用途：标题栏 16x16 图标、系统菜单小图标              │
│    设置：SetClassLongPtrW GCLP_HICONSM                  │
├─────────────────────────────────────────────────────────┤
│ 4. WM_SETICON  （per-window icon，big / small）          │
│    每个窗口实例自己的图标                               │
│    用途：标题栏（small）、【组合】任务栏的图标           │
│    设置：SendMessage(hwnd, WM_SETICON, ...)             │
└─────────────────────────────────────────────────────────┘
```

**Windows 10/11 任务栏的实际行为**（关键）：

| 任务栏模式 | 图标来源 |
|---|---|
| 组合模式（"始终合并"）| `WM_GETICON(hwnd, ICON_BIG)`（per-window）|
| 始终显示标签 | `WNDCLASSEX.HICON`（class）|
| 不合并 | `WNDCLASSEX.HICON`（class）|

Win11 默认是组合模式 + 始终隐藏标签，所以走 `WM_GETICON`。但**用户改设置后**或**特定场景下**会走 class icon。**生产应用必须 4 个都设对**，否则有用户看到对的、有用户看到错的。

---

## 3. Tk 的三个大坑

### 坑 1：`winfo_id()` 返回的不是 top-level 窗口

Tk 内部把窗口拆成两层：

```
[Windows top-level HWND]  ←── wm frame (class=TkTopLevel)
   └─ [Tk inner window]   ←── winfo_id()  (class=TkChild)
       └─ [实际 widget]   ←── 真正的 UI
```

- `window.winfo_id()` 返回 **Tk 内部子窗口**的 HWND
- 这个子窗口的 Windows class 是 **`TkChild`**，不是 `TkTopLevel`
- **对 `TkChild` 调 `SetClassLongPtr` 会直接 `err=6 INVALID_HANDLE`，静默失败**
- 真正能改 WNDCLASSEX 的 HWND 必须用 `window.tk.call("wm", "frame", window._w)` 拿

**验证代码**：

```python
import ctypes, ctypes.wintypes as w
from ctypes import create_unicode_buffer

user32 = ctypes.WinDLL("user32", use_last_error=True)
user32.GetClassNameW.argtypes = [w.HWND, w.LPCWSTR, ctypes.c_int]
user32.GetClassNameW.restype = ctypes.c_int

def show(window, label):
    winfo = int(window.winfo_id())
    try:
        # Tk 9.x 加了 0x 前缀，老版本是纯十进制
        frame_str = window.tk.call("wm", "frame", window._w)
        frame_str = str(frame_str).strip()
        frame = int(frame_str, 16) if frame_str.startswith(("0x", "0X")) else int(frame_str)
    except Exception:
        frame = winfo
    cls_w = create_unicode_buffer(256); user32.GetClassNameW(winfo, cls_w, 256)
    cls_f = create_unicode_buffer(256); user32.GetClassNameW(frame, cls_f, 256)
    print(f"{label}: winfo_id=0x{winfo:x} [{cls_w.value}]  wm frame=0x{frame:x} [{cls_f.value}]")

# 输出：
# root: winfo_id=0x5315ba [TkChild]  wm frame=0x9b1562 [TkTopLevel]
# tpl : winfo_id=0xc416e2 [TkChild]  wm frame=0x6c109e [TkTopLevel]
```

### 坑 2：`iconbitmap` 在 Win10/11 上的局限

Tk 的 `window.iconbitmap(path)` 内部会调 `LoadImageW` 加载 ICO、然后用 `WM_SETICON` 设置 **per-window icon**。问题：

- `WM_SETICON` 只设 per-window，**不动 WNDCLASSEX**
- Win10/11 组合任务栏走 `WM_GETICON`（OK），但**非组合模式**走 class icon（不 OK）
- Tk 在某些 Tk 版本上 `iconbitmap` 对 `ICON_BIG`（32x32）的支持有 bug，**只设 `ICON_SMALL`**

**结论**：光靠 `iconbitmap` 不够，必须自己补 Win32 调用。

### 坑 3：Tk 会**反过来改回 class icon**

这是最恶心的一坑。Tk 在以下时机会用 `SetClassLongPtr` 把 WNDCLASSEX.HICON 改回 Tk 默认调色板：

- 第一次 `Toplevel.__init__` 之后
- 每次新 Toplevel（combobox popup、菜单等）创建
- 部分 paint 事件

**实测序列**（在 `_apply_icon_win32` 里加日志）：

```
T=0.0s: SetClassLongPtr(我的 octocat) → GCLP=octocat ✓
T=0.3s: VERIFY → GCLP=octocat ✓
T=0.5s: Tk 弹出 combobox
T=0.6s: SetClassLongPtr(我的 octocat) → GCLP=octocat ✓  ← 我的 retry 救回来了
T=2.0s: 再无 Toplevel 事件
T=2.5s: 之后某次 paint，Tk 用 SetClassLongPtr 把 GCLP 改回调色板
T=2.6s: GCLP=Tk 默认 ✗  ← 救不回来
```

**对策**：用 `window.after()` 链持续重设 60 秒以上，覆盖所有 Toplevel 创建时机。5 秒不够，至少 60 秒。

---

## 4. 完整方案：三层覆盖

按强度递进，必须全做：

| 层 | API | 设置什么 | 适用场景 |
|---|---|---|---|
| 1 | `window.iconphoto(False, img)` | per-window 32x32 PhotoImage | macOS / Linux；Windows 标题栏兜底 |
| 2 | `window.iconbitmap(path)` | per-window 16+32 .ico | Windows 标题栏 / 组合任务栏 |
| 3 | Win32 直接：`WM_SETICON` + `SetClassLongPtrW` | per-window + WNDCLASSEX 16+32 | Windows 任务栏 / 系统菜单 |

**关键点**：

- 第 3 层用 **`wm frame`** 拿的 HWND（不是 `winfo_id`）
- 验证 Windows class 是 `TkTopLevel` 才调 `SetClassLongPtrW`
- 60 秒 `after()` 链持续重设

---

## 5. 完整代码

### 5.1 `apply_icon` —— 对所有窗口调一次

```python
# app.py
from pathlib import Path
import sys

# 业务代码里的 BASE_DIR（已经存在）
from backup_engine import BASE_DIR


def apply_icon(window) -> None:
    """设置应用图标（窗口装饰 + Windows 任务栏）。

    对所有顶层窗口（Tk root + Toplevel）都要调一次。
    Pillow 缺失 / Win32 调用失败都静默，不阻塞启动。
    """
    logo_ico = BASE_DIR / "logo.ico"
    if not logo_ico.exists():
        return

    # ── 1. iconphoto（跨平台） ──────────────────────────────
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

    # ── 2 & 3. iconbitmap + Win32 强制（Windows） ─────────
    if sys.platform == "win32":
        try:
            window.iconbitmap(str(logo_ico))
        except Exception as e:
            print(f"[apply_icon] iconbitmap 失败: {e}", file=sys.stderr)

        try:
            _apply_icon_win32(window, str(logo_ico))
        except Exception as e:
            print(f"[apply_icon] win32 强制失败: {e}", file=sys.stderr)

        # 60 秒内 12 次重试，覆盖 Tk 在 Toplevel 创建 / paint
        # 时机用 SetClassLongPtr 把 class icon 改回默认的副作用
        def _retry():
            try:
                _apply_icon_win32(window, str(logo_ico))
            except Exception:
                pass
        try:
            for ms in (50, 150, 300, 600, 1200, 2500, 5000,
                       10000, 20000, 35000, 50000, 60000):
                window.after(ms, _retry)
        except Exception:
            pass


def _apply_icon_win32(window, ico_path: str) -> None:
    """Windows 任务栏图标强制覆盖：Win32 API 直接 LoadImage + WM_SETICON +
    SetClassLongPtrW，绕开 Tk 内置 iconbitmap 的局限。

    关键点：
    1. 一定要用 wm frame 拿 top-level HWND；winfo_id() 拿到的是 TkChild
       （class 错），对 TkChild 调 SetClassLongPtr 会 err=6
    2. 验证 class 是 TkTopLevel 才继续
    3. 失败静默（外面有 try/except 兜底）
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

    # ── 拿真正的 top-level HWND ──────────────────────────
    try:
        frame_id = window.tk.call("wm", "frame", window._w)
    except Exception:
        frame_id = window.winfo_id()
    s = str(frame_id).strip()
    # Tk 9.x 加了 0x 前缀，老版本是纯十进制
    if s.startswith(("0x", "0X")):
        hwnd_int = int(s, 16)
    else:
        hwnd_int = int(s)
    hwnd = wintypes.HWND(hwnd_int)

    # ── 验证 class ──────────────────────────────────────
    user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPCWSTR, ctypes.c_int]
    user32.GetClassNameW.restype = ctypes.c_int
    class_name = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(hwnd, class_name, 256)
    if class_name.value != "TkTopLevel":
        return  # 拿到的不是 top-level，不要乱改别人的 class

    # ── ctypes 签名（64-bit 上不配会 crash 在 stdcall 栈错位） ──
    LoadImageW = user32.LoadImageW
    LoadImageW.argtypes = [
        wintypes.HINSTANCE, wintypes.LPCWSTR, wintypes.UINT,
        ctypes.c_int, ctypes.c_int, wintypes.UINT,
    ]
    LoadImageW.restype = wintypes.HANDLE

    SendMessageW = user32.SendMessageW
    SendMessageW.argtypes = [wintypes.HWND, wintypes.UINT,
                             wintypes.WPARAM, wintypes.LPARAM]
    SendMessageW.restype = ctypes.c_ssize_t  # LRESULT, wintypes 没提供

    SetClassLongPtrW = user32.SetClassLongPtrW
    SetClassLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_ssize_t]
    SetClassLongPtrW.restype = ctypes.c_ssize_t  # LONG_PTR = pointer-sized signed

    # ── 加载 16/32 两个 HICON ────────────────────────────
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

    # ── 改写 WNDCLASSEX（关键！任务栏非组合模式用这个） ──
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
```

### 5.2 在每个 Toplevel `__init__` 里调一次

```python
class SetupDialog(ttkb.Toplevel):
    def __init__(self, parent, ...):
        super().__init__(parent)
        self.title("初始配置向导")
        # ...
        self.grab_set()
        # ── 关键：Toplevel 必须自己调一次 ──
        apply_icon(self)
        set_light_titlebar(self)


class GitMissingDialog(ttkb.Toplevel):
    def __init__(self, parent, ...):
        super().__init__(parent)
        # ...
        apply_icon(self)
        set_light_titlebar(self)


# 所有 Toplevel（包括菜单驱动的对话框）都要调
def _show_settings_dialog(self):
    dlg = ttkb.Toplevel(self)
    # ...
    apply_icon(dlg)  # ← 别漏
    set_light_titlebar(dlg)
```

### 5.3 主窗口

```python
class BackupApp(ttkb.Window):
    def __init__(self):
        super().__init__(themename="flatly")
        # ...
        apply_icon(self)  # ← 主窗口调一次
        set_light_titlebar(self)
```

---

## 6. PyInstaller 配置

### 6.1 `logo.ico` 要含多分辨率

任务栏是 16x16 / 32x32，资源管理器是 48x48+，高分屏会要 256x256。**至少要 16/32 两份**，最好全尺寸都备齐。

**生成命令**（ImageMagick）：

```bash
magick convert logo.svg \
  -define icon:auto-resize=16,32,48,64,128,256 \
  logo.ico
```

**验证**：

```python
# 解析 ICO header
import struct
with open("logo.ico", "rb") as f:
    data = f.read()
_, _, count = struct.unpack("<HHH", data[:6])
print(f"ICO 包含 {count} 帧：")
for i in range(count):
    off = 6 + i * 16
    w, h, _, _, _, bpp, size, _ = struct.unpack("<BBBBHHII", data[off:off+16])
    w = 256 if w == 0 else w
    h = 256 if h == 0 else h
    print(f"  {w}x{h} {bpp}bpp {size} bytes")
```

输出（示例）：

```
ICO 包含 6 帧：
  16x16 32bpp 900 bytes
  32x32 32bpp 2612 bytes
  48x48 32bpp 4915 bytes
  64x64 32bpp 7583 bytes
  128x128 32bpp 21926 bytes
  256x256 32bpp 73946 bytes
```

### 6.2 PyInstaller 命令行

```bash
pyinstaller --onefile --windowed \
            --name GitHubBackupManager \
            --icon "logo.ico" \                # ← 必须：把图标塞进 exe 资源
            --add-data "logo.ico;." \          # ← 必须：把图标打包到 _MEIPASS，
                                                #    让运行时 BASE_DIR/logo.ico 能找到
            app.py
```

或者 `app.spec`：

```python
# -*- mode: python ; coding: utf-8 -*-
a = Analysis(
    ['app.py'],
    datas=[('logo.ico', '.')],  # ← 必须
    ...
)
exe = EXE(
    ...,
    icon=['logo.ico'],  # ← 必须
)
```

**两个都加，缺一不可**：
- `--icon` → exe 资源图标（资源管理器、开始菜单、固定的快捷方式）
- `--add-data` → 运行时文件（`BASE_DIR/logo.ico` 存在 → `iconphoto` / `iconbitmap` / `LoadImageW` 才能读）

### 6.3 CI workflow 同步

`.github/workflows/build-windows.yml`：

```yaml
- name: PyInstaller
  run: |
    pyinstaller --onefile --windowed --name GitHubBackupManager `
                --icon "logo.ico" `
                --add-data "logo.ico;." `
                app.py
```

---

## 7. 验证方法

### 7.1 进程内验证（最可靠）

加个临时调试函数：

```python
def _debug_icon(window):
    """打印窗口的 4 个图标来源，验证 apply_icon 是否生效"""
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)

    # 拿 wm frame 的 HWND
    frame_str = window.tk.call("wm", "frame", window._w)
    s = str(frame_str).strip()
    hwnd = int(s, 16) if s.startswith(("0x", "0X")) else int(s)
    hwnd = wintypes.HWND(hwnd)

    # class name
    user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPCWSTR, ctypes.c_int]
    user32.GetClassNameW.restype = ctypes.c_int
    cls = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(hwnd, cls, 256)

    # 4 个图标来源
    user32.GetClassLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.GetClassLongPtrW.restype = ctypes.c_ssize_t
    gcl_big = int(user32.GetClassLongPtrW(hwnd, -14))   # GCLP_HICON
    gcl_sm  = int(user32.GetClassLongPtrW(hwnd, -34))   # GCLP_HICONSM

    user32.SendMessageW.argtypes = [wintypes.HWND, wintypes.UINT,
                                    wintypes.WPARAM, wintypes.LPARAM]
    user32.SendMessageW.restype = ctypes.c_ssize_t
    wmi_big = int(user32.SendMessageW(hwnd, 0x7F, 1, 0))  # WM_GETICON ICON_BIG
    wmi_sm  = int(user32.SendMessageW(hwnd, 0x7F, 0, 0))  # WM_GETICON ICON_SMALL

    print(f"hwnd=0x{hwnd:x} class={cls.value}")
    print(f"  GCLP_HICON     = 0x{gcl_big:x}")
    print(f"  GCLP_HICONSM   = 0x{gcl_sm:x}")
    print(f"  WM_GETICON BIG = 0x{wmi_big:x}")
    print(f"  WM_GETICON SM  = 0x{wmi_sm:x}")

# 调用
_debug_icon(root)
_debug_icon(setup_dialog)
```

**期望输出**（`apply_icon` 跑完之后）：

```
hwnd=0x17182c class=TkTopLevel
  GCLP_HICON     = 0x5f4513d1   ← 应该是你的 HICON
  GCLP_HICONSM   = 0x14360c81
  WM_GETICON BIG = 0x5f4513d1
  WM_GETICON SM  = 0x14360c81
```

**抽 HICON 看实际像素**（.NET 跨进程可读）：

```powershell
# 在 PowerShell 里，对运行中的 exe 读
$pid_target = (Get-Process GitHubBackupManager).Id
$setup = Get-Process GitHubBackupManager | Where-Object { $_.MainWindowTitle -ne '' } | Select-Object -First 1
$hwnd = $setup.MainWindowHandle

Add-Type @"
using System;
using System.Runtime.InteropServices;
public class W {
    [DllImport("user32.dll")] public static extern IntPtr GetClassLongPtr(IntPtr hWnd, int nIndex);
    [DllImport("user32.dll")] public static extern IntPtr SendMessage(IntPtr hWnd, uint Msg, IntPtr wParam, IntPtr lParam);
}
"@

$h = [W]::GetClassLongPtr($hwnd, -14)
$icon = [System.Drawing.Icon]::FromHandle($h)
$icon.ToBitmap().Save("$env:TEMP\test_class_hicon.png")
# 看 test_class_hicon.png 是不是你的图标
```

### 7.2 验证 HICON 加载正确

`LoadImageW` 失败会返回 0，签名错会 crash。单独测一下：

```python
import ctypes, ctypes.wintypes as w

ico = r"C:\path\to\logo.ico"
user32 = ctypes.WinDLL("user32", use_last_error=True)
user32.LoadImageW.argtypes = [w.HINSTANCE, w.LPCWSTR, w.UINT,
                             ctypes.c_int, ctypes.c_int, w.UINT]
user32.LoadImageW.restype = w.HANDLE

h = user32.LoadImageW(None, ico, 1, 32, 32, 0x10)  # IMAGE_ICON=1, LR_LOADFROMFILE=0x10
print(f"hicon: 0x{h:x}")  # 应该是非 0
if not h:
    print(f"err: {ctypes.get_last_error()}: {ctypes.FormatError(ctypes.get_last_error())}")
```

### 7.3 验证 PyInstaller 资源图标

```powershell
# 抽 exe 资源的 32x32 图标
$exe = "dist\GitHubBackupManager.exe"
Add-Type -AssemblyName System.Drawing
$icon = [System.Drawing.Icon]::ExtractAssociatedIcon($exe)
$icon.ToBitmap().Save("$env:TEMP\exe_resource_32.png")
# 看 exe_resource_32.png 是不是你的图标
```

### 7.4 完整验证清单

跑过一遍下面所有项，才算真的修好：

- [ ] exe 资源图标正确（资源管理器、开始菜单）
- [ ] 主窗口标题栏左上角图标正确
- [ ] 主窗口**系统菜单**图标正确（点左上角图标弹的菜单）
- [ ] 主窗口任务栏图标正确（运行中应用）
- [ ] 主窗口任务栏**悬停预览**图标正确
- [ ] 所有 Toplevel 标题栏左上角图标正确
- [ ] 所有 Toplevel 系统菜单图标正确
- [ ] 跨进程读 GCLP_HICON 是 octocat HICON（不是 NULL/Tk 默认）
- [ ] 跨进程读 WM_GETICON 是 octocat HICON

---

## 8. Windows Icon Cache 处理

**症状**：你修了代码、重新打包、跑了新 exe，**任务栏还是显示旧图标**。

**原因**：Windows 按 exe 路径缓存图标，路径相同就不重新读资源。即使 exe 的图标资源已经变了，Explorer 也可能继续用旧缓存。

**最快的解决办法**（任选其一）：

### 方法 A：取消固定 + 重新固定

任务栏上右键 GitHub Backup Manager → **取消固定** → 重新固定。

### 方法 B：重启 Explorer

PowerShell 管理员：

```powershell
Stop-Process -Name explorer -Force
Start-Process explorer
```

### 方法 C：删 icon cache 文件

PowerShell 管理员：

```powershell
Stop-Process -Name explorer -Force
Remove-Item "$env:LOCALAPPDATA\IconCache.db" -ErrorAction SilentlyContinue
Remove-Item "$env:LOCALAPPDATA\Microsoft\Windows\Explorer\iconcache_*" -ErrorAction SilentlyContinue
Start-Process explorer
```

### 方法 D：移动 exe 到新路径

最暴力的方式 —— Windows 按路径缓存，换个文件名/文件夹就不缓存了。

```bash
mv dist\GitHubBackupManager.exe dist\GitHubBackupManager_v2.exe
```

**注意**：方法 A/B/C 都不需要重新打包代码，**只影响显示，不影响功能**。如果只是你本机显示问题，clear cache 就行；如果要发布给用户，**让 release notes 提醒一下**。

---

## 9. 踩坑速查清单

按"看到症状 → 根因 → 修法"组织：

| # | 症状 | 根因 | 修法 |
|---|---|---|---|
| 1 | 标题栏左上角是 Tk 调色板 | `Toplevel.__init__` 没调 `apply_icon` | 每个 Toplevel `__init__` 末尾 `apply_icon(self)` |
| 2 | 任务栏是 Tk 调色板 | `SetClassLongPtr` 改的是 TkChild class | 用 `wm frame` 拿 TkTopLevel HWND |
| 3 | `SetClassLongPtr` 静默失败 | 64-bit Python 缺 ctypes 签名 | 配 `argtypes` / `restype`（参考 §5.1）|
| 4 | `SetClassLongPtr` 返回 `err=6` | 拿的是 TkChild 不是 TkTopLevel | 同 #2 |
| 5 | 改完后立刻被 Tk 改回去 | Tk 在 Toplevel paint 时会 SetClassLongPtr 改回默认 | `after()` 链重试 60 秒（§5.1）|
| 6 | `int("0x123")` ValueError | Tk 9.x 的 `wm frame` 返回 `"0x..."` 字符串 | 兼容 `0x` 前缀再 int（§5.1）|
| 7 | `LoadImageW` 报 `err=1813` | ico 文件找不到 / 在 _MEIPASS 外 | `--add-data "logo.ico;."` + `BASE_DIR / "logo.ico"` |
| 8 | `LoadImageW` 报 `err=0` 但 HICON 是 0 | ctypes 签名错 | 同 #3 |
| 9 | 跨进程读 `GCLP_HICON` 失败 | `SetClassLongPtr` 跨进程会 `err=5 ACCESS_DENIED` | 跨进程只能**读**，改要在同进程 |
| 10 | 代码改了任务栏没变 | Windows icon cache | §8 的清缓存方法 |
| 11 | `iconbitmap` 后任务栏部分对部分错 | Tk 只设 `ICON_SMALL` 不设 `ICON_BIG` | 跳过 `iconbitmap`，用 Win32 直接 `WM_SETICON` |
| 12 | 任务栏 ok 但系统菜单图错 | class icon 没设 / 被 Tk 改回 | `SetClassLongPtrW GCLP_HICON` + retry |
| 13 | 多个 Toplevel 各显示各的 | class icon 共享没问题，per-window 没设 | 每个 Toplevel 各自 `apply_icon` |
| 14 | 拖窗口到另一个屏幕图标糊了 | DPI 切换时 Windows 会重新读 | 多分辨率 ICO（§6.1）|
| 15 | `c_void_p.__format__` 报错 | Py 3.14 ctypes 行为变化 | 用 `int(v.value)` 或 `struct.unpack('<Q', bytes(v))` |

---

## 10. 参考资料

- [Microsoft Docs: SetClassLongPtrW](https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-setclasslongptrw)
- [Microsoft Docs: WM_SETICON message](https://learn.microsoft.com/en-us/windows/win32/winmsg/wm-seticon)
- [Microsoft Docs: WNDCLASSEXW structure](https://learn.microsoft.com/en-us/windows/win32/api/winuser/ns-winuser-wndclassexw)
- [Microsoft Docs: LoadImageW](https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-loadimagew)
- [PyInstaller: icon argument](https://pyinstaller.org/en/stable/usage.html#windows)
- [Tk 源码：`generic/tkWinWindow.c`](https://core.tcl-lang.org/tk/file/generic/tkWinWindow.c) —— 看 `TkWinSetClassIcon` / `SetClassLongPtr` 调用的地方
- [Tk `wm frame` 命令](https://www.tcl-lang.org/man/tcl8.6/TkCmd/wm.htm)
- [StackOverflow: Setting Windows taskbar icon for Python Tk app](https://stackoverflow.com/questions/23306799/)

---

## 附：完整 apply_icon 模块（生产可用版）

见项目 [`app.py`](../app.py) 第 263-410 行。

---

*文档版本：v1.0 — 2026-07-13*
*适用：Tkinter / ttkbootstrap + PyInstaller `--onefile` on Windows 10/11*
