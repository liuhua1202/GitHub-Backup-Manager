"""
备份引擎模块
============

封装 GitHub API 交互、git 镜像备份、配置与状态管理、日志缓冲区等核心逻辑。
与 GUI 解耦，可独立测试和复用。
"""

from __future__ import annotations

import json
import os
import logging
import subprocess
import urllib.request
import urllib.error
import urllib.parse
import datetime
import time
import shutil
import threading
from pathlib import Path
from typing import Any

# ──────────────────────────────────────────────────────────────────────
# 常量与路径
# ──────────────────────────────────────────────────────────────────────

BASE_DIR: Path = Path(__file__).resolve().parent
CONFIG_FILE: Path = BASE_DIR / "config.json"
STATE_FILE: Path = BASE_DIR / "backup_state.json"
LOG_DIR: Path = BASE_DIR / "logs"
LOG_FILE: Path = LOG_DIR / "backup.log"

LOG_DIR.mkdir(parents=True, exist_ok=True)

# ──────────────────────────────────────────────────────────────────────
# 默认配置与状态
# ──────────────────────────────────────────────────────────────────────

DEFAULT_CONFIG: dict[str, Any] = {
    "target": "",
    "token": "",
    "backup_dir": str(Path.home() / "github-backups"),
    "schedule_type": "daily",        # "daily" | "weekly" | "monthly" | "yearly" | "custom"
    "cron_expression": "0 2 * * *",  # 默认每天凌晨2点（仅 custom 模式使用）
    "retry_count": 3,
    "retry_delay_base": 10,           # 基础重试间隔（秒）
    "extra_repos": [],                # 指定额外备份的仓库列表，如 ["octocat/Hello-World"]
    "clone_mode": "mirror",           # "mirror" (bare, git clone --mirror) | "normal" (git clone, 含工作目录)
    "is_configured": False,
    "suppress_git_warning": False,    # 用户在 git 缺失对话框里勾选"下次不再提醒"后置 True
    "daily_hour": 2,            # daily / weekly / monthly / yearly 模式下的执行小时（0-23）
    "daily_minute": 0,          # 执行分钟（0-59）
    "weekly_day_of_week": "sun",  # weekly 模式下的星期几
    "monthly_day_of_month": 1,   # monthly 模式下的几号（1-31）
    "yearly_month": 1,           # yearly 模式下的月份（1-12）
    "yearly_day_of_month": 1,    # yearly 模式下的几号（1-31）
    "scheduler_paused": False,    # True 时调度器不启动（保留配置但暂停运行）
    "sort_option": "名称",         # 仓库列表排序选项（持久化）
}

DEFAULT_STATE: dict[str, Any] = {
    "repos": {},            # {repo_full_name: {name, clone_url, last_backup, status, size, error}}
    "last_full_backup": None,
    "backup_in_progress": False,
}

# ──────────────────────────────────────────────────────────────────────
# 日志配置
# ──────────────────────────────────────────────────────────────────────

LOG_FORMAT = "%(asctime)s [%(levelname)s] %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

logger = logging.getLogger("github-backup")
logger.setLevel(logging.DEBUG)

# 避免重复添加 handler
if not logger.handlers:
    fh = logging.FileHandler(str(LOG_FILE), encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(logging.Formatter(LOG_FORMAT, DATE_FORMAT))
    logger.addHandler(fh)

    ch = logging.StreamHandler()
    ch.setLevel(logging.INFO)
    ch.setFormatter(logging.Formatter(LOG_FORMAT, DATE_FORMAT))
    logger.addHandler(ch)


# ──────────────────────────────────────────────────────────────────────
# 日志缓冲区（供 GUI 实时读取）
# ──────────────────────────────────────────────────────────────────────

# 自定义 log level：LOG_SUCCESS 介于 INFO(20) 和 WARNING(30) 之间，
# 专门给"完成/成功"类消息着色（绿色）。
LOG_SUCCESS = 25

class LogBuffer:
    """线程安全的内存日志缓冲区，供 GUI 轮询读取。每条带 level (供着色用)。"""

    def __init__(self, max_lines: int = 2000):
        self._lines: list[tuple[str, int]] = []  # (text, level)
        self._max_lines = max_lines
        self._lock = threading.Lock()

    def add(self, msg: str, level: int = logging.INFO) -> None:
        ts = datetime.datetime.now().strftime("%H:%M:%S")
        with self._lock:
            self._lines.append((f"[{ts}] {msg}", level))
            if len(self._lines) > self._max_lines:
                self._lines = self._lines[-self._max_lines:]

    def get_all(self) -> list[str]:
        """纯文本版本（向后兼容，丢 level 信息）。"""
        with self._lock:
            return [t for t, _ in self._lines]

    def get_all_with_levels(self) -> list[tuple[str, int]]:
        """返回 (text, level) 对，GUI 用以着色。"""
        with self._lock:
            return list(self._lines)

    def clear(self) -> None:
        with self._lock:
            self._lines.clear()

    def search(self, keyword: str) -> list[str]:
        """纯文本搜索（向后兼容）。"""
        kw = keyword.lower()
        with self._lock:
            return [t for t, _ in self._lines if kw in t.lower()]

    def search_with_levels(self, keyword: str) -> list[tuple[str, int]]:
        """带 level 的搜索结果。"""
        kw = keyword.lower()
        with self._lock:
            return [(t, lvl) for t, lvl in self._lines if kw in t.lower()]


log_buffer = LogBuffer()


def _infer_level(msg: str) -> int:
    """启发式按内容判断 log level。

    给 add_log 默认级别用：现有大量调用点没传 level，需要按消息内容打颜色标。
    命中优先级从高到低：失败/错误 → 成功/完成 → 警告/跳过 → INFO。

    注：日志系统全中文，这些关键字是只对源码中文字符串敏感 —— 不要因为
    不符合『国际化最佳实践』就误以为它是英文 dead code；它是中文感知。
    """
    # 失败 / 报错 / 异常
    if any(kw in msg for kw in ("失败", "错误", "异常")):
        return logging.ERROR
    # 成功 / 完成。注意顺序：先查"失败/错误"再查"成功/完成"，避免误判。
    if any(kw in msg for kw in ("完成", "成功")):
        return LOG_SUCCESS
    # 警告 / 跳过
    if any(kw in msg for kw in ("跳过", "警告")):
        return logging.WARNING
    return logging.INFO


def add_log(msg: str, level: int | None = None) -> None:
    """同时写入 logger 文件/控制台 和 前端日志缓冲区。

    level 不传时按消息内容启发式推断（成功/失败/警告），
    方便向后兼容现有所有 add_log(msg) 调用点。
    显式传 level 的（比如 logging.WARNING / logging.ERROR）优先。
    """
    if level is None:
        level = _infer_level(msg)
    logger.log(level, msg)
    log_buffer.add(msg, level)


# ──────────────────────────────────────────────────────────────────────
# 配置管理
# ──────────────────────────────────────────────────────────────────────

def load_config() -> dict[str, Any]:
    """加载配置文件，不存在则返回默认配置的副本。"""
    if CONFIG_FILE.exists():
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                cfg = json.load(f)
            return {**DEFAULT_CONFIG, **cfg}
        except (json.JSONDecodeError, OSError) as e:
            logger.error("读取配置文件失败: %s，使用默认配置", e)
    return dict(DEFAULT_CONFIG)


def save_config(cfg: dict[str, Any]) -> None:
    """保存配置到 JSON 文件，并设置 600 权限。

    原子写：写到临时文件后 os.replace，避免崩溃时留下半截 JSON。
    """
    tmp = CONFIG_FILE.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)
    os.replace(tmp, CONFIG_FILE)
    try:
        os.chmod(CONFIG_FILE, 0o600)
    except OSError:
        pass


# ──────────────────────────────────────────────────────────────────────
# 状态管理
# ──────────────────────────────────────────────────────────────────────

def load_state() -> dict[str, Any]:
    """加载备份状态文件。"""
    if STATE_FILE.exists():
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                state = json.load(f)
            # 合并默认值确保字段完整
            return {**DEFAULT_STATE, **state}
        except (json.JSONDecodeError, OSError) as e:
            logger.error("读取状态文件失败: %s", e)
    return json.loads(json.dumps(DEFAULT_STATE))  # 深拷贝


def save_state(state: dict[str, Any]) -> None:
    """保存备份状态。原子写：写到临时文件后 os.replace。"""
    tmp = STATE_FILE.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)
    os.replace(tmp, STATE_FILE)


# ──────────────────────────────────────────────────────────────────────
# 工具函数
# ──────────────────────────────────────────────────────────────────────

def mask_token(token: str) -> str:
    """Token 脱敏：保留前4位和后4位。"""
    if not token or len(token) < 8:
        return "****"
    return token[:4] + "*" * (len(token) - 8) + token[-4:]


def sanitize_text(text: str, token: str = "") -> str:
    """
    对文本中的敏感信息进行脱敏。
    - 如果提供了 token，将文本中出现的 token 替换为脱敏形式
    - 自动识别 URL 中的 token（如 https://token@github.com/...）并脱敏
    """
    if not text:
        return text

    result = text

    # 1. 如果已知 token，直接替换所有出现
    if token:
        masked = mask_token(token)
        result = result.replace(token, masked)

    # 2. 兜底：正则匹配 URL 中的 token 部分（https://xxx@host/...）
    import re
    # 匹配 https://<token>@github.com/... 形式的 URL
    result = re.sub(
        r'(https?://)([^@\s]+)(@[^\s/]+)',
        lambda m: m.group(1) + mask_token(m.group(2)) + m.group(3),
        result
    )

    return result


def format_size(size_bytes: float) -> str:
    """字节数转人类可读格式。"""
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size_bytes < 1024:
            return f"{size_bytes:.1f} {unit}"
        size_bytes /= 1024
    return f"{size_bytes:.1f} PB"


def get_dir_size(path: Path) -> str:
    """获取目录大小的人类可读字符串。"""
    return format_size(get_dir_size_bytes(path))


def get_dir_size_bytes(path: Path) -> int:
    """获取目录大小的字节数（字节）。

    - `path` 不存在 / 无权限：返回 0，不崩
    - 单个文件 stat 失败：跳过那个文件（权限被拒、文件被并发删，等）
    - 真正的 bug（如 KeyboardInterrupt、MemoryError）依旧让上层看到
    """
    if not path.exists():
        return 0
    total = 0
    for entry in path.rglob("*"):
        if entry.is_file():
            try:
                total += entry.stat().st_size
            except OSError:
                # 权限被拒 / 文件刚好被删 —— 这种是常态，跳过
                continue
    return total


def get_disk_free(path: Path) -> str:
    """获取指定路径所在磁盘的可用空间。"""
    try:
        target = path if path.exists() else path.parent
        usage = shutil.disk_usage(target)
        return format_size(usage.free)
    except OSError as e:
        # 跨平台：权限被拒 / 路径不存在 / 网络盘断线 —— 都返回 N/A
        logger.warning("get_disk_free 失败: %s", e)
        return "N/A"


def check_git() -> tuple[bool, str]:
    """检测 git 是否安装，返回 (是否安装, 版本字符串)。"""
    try:
        result = subprocess.run(
            ["git", "--version"],
            capture_output=True, text=True, timeout=10
        )
        if result.returncode == 0:
            return True, result.stdout.strip()
        return False, "git command failed"
    except FileNotFoundError:
        return False, "git not found in PATH"
    except Exception as e:
        return False, str(e)


# ──────────────────────────────────────────────────────────────────────
# GitHub API
# ──────────────────────────────────────────────────────────────────────

def github_api_request(
    url: str,
    token: str,
    method: str = "GET",
    retry_count: int = 3,
    retry_delay_base: int = 10,
) -> tuple[int, dict | list | None]:
    """
    发送 GitHub REST API 请求，带指数退避重试。
    返回 (HTTP状态码, JSON数据或None)。
    """
    for attempt in range(1, retry_count + 1):
        try:
            req = urllib.request.Request(url, method=method)
            # 仅当 Token 非空时才添加认证头（公开仓库可匿名访问）
            if token:
                req.add_header("Authorization", f"token {token}")
            req.add_header("Accept", "application/vnd.github.v3+json")
            req.add_header("User-Agent", "GitHub-Backup-Desktop/1.0")

            with urllib.request.urlopen(req, timeout=30) as resp:
                body = resp.read().decode("utf-8")
                return resp.status, json.loads(body) if body else None

        except urllib.error.HTTPError as e:
            status = e.code
            # 读取错误响应体，用于区分限流和权限不足
            try:
                err_body = e.read().decode("utf-8")
                err_data = json.loads(err_body) if err_body else None
            except (json.JSONDecodeError, OSError):
                err_data = None

            # 403 限流时检查剩余配额，给出明确提示
            if status == 403 and err_data and isinstance(err_data, dict):
                msg = err_data.get("message", "")
                if "rate limit" in msg.lower():
                    logger.error("GitHub API 限流: %s", msg)
                    return status, err_data  # 限流不重试，直接返回
                logger.warning("API 请求失败 (HTTP 403): %s", msg)

            logger.warning("API 请求失败 (HTTP %d): %s，尝试 %d/%d",
                           status, url, attempt, retry_count)
            if attempt < retry_count:
                delay = retry_delay_base * (2 ** (attempt - 1))
                time.sleep(delay)
            else:
                return status, err_data

        except (urllib.error.URLError, OSError) as e:
            logger.warning("API 网络错误: %s，尝试 %d/%d", e, attempt, retry_count)
            if attempt < retry_count:
                delay = retry_delay_base * (2 ** (attempt - 1))
                time.sleep(delay)
            else:
                return -1, None

    return -1, None


def get_all_repos(
    target: str,
    token: str,
    retry_count: int = 3,
    retry_delay_base: int = 10,
) -> tuple[bool, list[dict]]:
    """
    获取目标用户/组织的所有仓库（含私有仓库），支持分页。
    自动探测目标是用户还是组织，使用对应端点。
    返回 (成功标志, 仓库列表)。
    """
    all_repos: list[dict] = []
    if not target:
        return False, []

    per_page = 100

    # ── 第一步：探测目标类型（用户 or 组织），确定正确的 API 端点 ──
    api_base = None
    test_url = f"https://api.github.com/users/{target}/repos?per_page=1&page=1"
    status, _ = github_api_request(
        test_url, token, retry_count=retry_count, retry_delay_base=retry_delay_base
    )

    if status in (200, 304):
        api_base = f"https://api.github.com/users/{target}/repos"
    elif status == 404:
        # 可能是组织，尝试组织端点
        test_url_org = f"https://api.github.com/orgs/{target}/repos?per_page=1&page=1"
        status_org, _ = github_api_request(
            test_url_org, token, retry_count=retry_count, retry_delay_base=retry_delay_base
        )
        if status_org in (200, 304):
            api_base = f"https://api.github.com/orgs/{target}/repos"
        elif status_org == 404:
            logger.error("目标 %s 既不是用户也不是组织（404）", target)
            return False, []
        else:
            logger.error("探测组织端点失败: HTTP %d", status_org)
            return False, []
    elif status == 401:
        if token:
            logger.error("Token 无效 (HTTP 401)")
        else:
            logger.error("认证失败 (HTTP 401)")
        return False, []
    elif status == 403:
        logger.error("API 限流或权限不足 (HTTP 403)")
        return False, []
    else:
        logger.error("探测用户端点失败: HTTP %d", status)
        return False, []

    # ── 第二步：使用确定的端点分页获取所有仓库 ──
    page = 1
    while True:
        url = f"{api_base}?per_page={per_page}&page={page}&type=all&sort=updated"
        status, data = github_api_request(
            url, token, retry_count=retry_count, retry_delay_base=retry_delay_base
        )

        if status == 401:
            if token:
                logger.error("Token 无效 (HTTP 401)")
            else:
                logger.error("认证失败 (HTTP 401)")
            return False, []
        if status == 403:
            logger.error("API 限流或权限不足 (HTTP 403)")
            return False, []
        if status not in (200, 304) or data is None:
            logger.error("获取仓库列表失败: HTTP %d", status)
            return False, all_repos if all_repos else []

        repos = data if isinstance(data, list) else []
        if not repos:
            break

        all_repos.extend(repos)
        if len(repos) < per_page:
            break
        page += 1

    return True, all_repos


def verify_token(token: str) -> tuple[bool, str]:
    """
    验证 Token 是否有效。
    返回 (是否有效, 用户名或错误信息)。
    """
    if not token:
        return False, "Token 不能为空"
    status, data = github_api_request(
        "https://api.github.com/user", token, retry_count=1
    )
    if status == 200 and isinstance(data, dict):
        return True, data.get("login", "unknown")
    return False, f"Token 无效 (HTTP {status})"


def get_repo_info(
    repo_full_name: str,
    token: str,
    retry_count: int = 3,
    retry_delay_base: int = 10,
) -> tuple[bool, dict | None]:
    """
    获取单个仓库信息（GET /repos/{owner}/{repo}）。
    返回 (成功标志, 仓库信息字典或None)。
    """
    url = f"https://api.github.com/repos/{repo_full_name.strip()}"
    status, data = github_api_request(
        url, token, retry_count=retry_count, retry_delay_base=retry_delay_base
    )
    if status == 200 and isinstance(data, dict):
        return True, data
    logger.error("获取仓库信息失败 [%s]: HTTP %d", repo_full_name, status)
    return False, None


def parse_extra_repos(text: str) -> tuple[list[str], list[str]]:
    """
    将多行文本解析为指定仓库列表。
    每行一个 owner/repo，忽略空行和 # 开头的注释行。
    返回 (有效列表, 无效行列表)。
    """
    valid: list[str] = []
    invalid: list[str] = []
    for line in text.strip().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        # 去除行内注释（# 后内容）
        if "#" in line:
            line = line.split("#")[0].strip()
            if not line:
                continue
        # 校验格式：owner/repo，owner 和 repo 非空，仅一个斜杠
        parts = line.split("/")
        if len(parts) == 2 and parts[0].strip() and parts[1].strip():
            full_name = f"{parts[0].strip()}/{parts[1].strip()}"
            if full_name not in valid:
                valid.append(full_name)
        else:
            invalid.append(line)
    return valid, invalid


# ──────────────────────────────────────────────────────────────────────
# Git 备份操作
# ──────────────────────────────────────────────────────────────────────

def run_git_command(
    cmd: list[str],
    cwd: Path | None = None,
    timeout: int = 600,
) -> tuple[int, str, str]:
    """运行 git 命令，返回 (返回码, stdout, stderr)。"""
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True,
            cwd=str(cwd) if cwd else None,
            timeout=timeout,
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
        )
        return result.returncode, result.stdout.strip(), result.stderr.strip()
    except subprocess.TimeoutExpired:
        return -1, "", "Command timed out"
    except Exception as e:
        return -1, "", str(e)


def _watch_and_cancel(
    proc: subprocess.Popen,
    stop_event: threading.Event | None,
    cancelled_flag: list[bool],
) -> threading.Event:
    """后台线程：轮询 stop_event，发现则调用 proc.terminate() 终止子进程。

    cancelled_flag: 单元素 list 当作可写 box 让 caller 看到本线程是否发起过取消。
    这样在 Windows 上也能区分"自然退出 rc=1"和"我们硬终止 rc=1"。

    返回一个 Event，让调用方在事后 join/等待时挂上去（已确保会被 set）。
    """
    done = threading.Event()

    def _watch():
        try:
            # 200 ms 间隔轮询：响应时延 ≤ 200ms（合理 UX 范围）
            while proc.poll() is None:
                if stop_event is not None and stop_event.is_set():
                    if not cancelled_flag[0]:
                        cancelled_flag[0] = True
                        try:
                            proc.terminate()  # SIGTERM / TerminateProcess，git 收到后会清理
                        except Exception:
                            pass
                    # 已发起取消，等子进程退出后退出本线程
                    return
                import threading as _t
                _t.Event().wait(0.2)
        finally:
            # 任何 return 路径都让 caller 的 `watcher_done.wait(2.0)` 立刻放行，
            # 否则等待 2 秒 timeout 才走 — 那会让取消响应被感知为卡顿
            done.set()
    t = threading.Thread(target=_watch, daemon=True)
    t.start()
    return done


def _run_and_log(
    cmd: list[str],
    label: str,
    cwd: Path | None = None,
    timeout: int = 600,
    stop_event: threading.Event | None = None,
) -> tuple[int, str]:
    """运行 git 命令并把 stdout/stderr 逐行转发到日志（按仓库 label 前缀）。

    与 run_git_command 的区别：
    - 把进度行（`Receiving objects: 12%`）和 ref 更新行
      （`* [new ref]` / `- [deleted]` / `= [up to date]`）直接写进 log_buffer 和 backup.log,
      用户能从实时日志 / 文件里看到具体下载 / 更新 / 删除的对象。
    - 调用方已通过 `--progress` / `--verbose` 等开关打开相应输出。
    - **可中途取消**：传 stop_event 后，内部起 watchdog 线程 200ms 轮询，发现 set
      就 proc.terminate()，立即打断 git 子进程（不再等几十秒的 fetch 完成）。

    返回 (returncode, ""). 日志已经处理过了。
    取消场景下返回 (-1, "") 并标记 cancelled=True 写到日志。
    """
    cancelled_box: list[bool] = [False]
    try:
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            encoding="utf-8", errors="replace",
            cwd=str(cwd) if cwd else None,
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
        )
    except Exception as e:
        add_log(f"[{label}] 异常: {e}", logging.ERROR)
        return -1, ""

    # 看门狗：发现 stop_event 就 terminate 子进程
    watcher_done = _watch_and_cancel(proc, stop_event, cancelled_box)

    try:
        try:
            stdout, stderr = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            try:
                proc.kill()
                proc.communicate()
            except Exception:
                pass
            add_log(f"[{label}] 超时 ({timeout}s) — 已 kill", logging.ERROR)
            return -1, ""
    finally:
        # 等 watchdog 线程结束（popen 已退出后会立刻退出）
        watcher_done.wait(2.0)

    if cancelled_box[0]:
        add_log(f"[{label}] ⚠ 已取消（用户中断）", logging.WARNING)
        return -1, ""

    # 双流都转发，git 的 --progress 和 --verbose 都走 stderr
    merged = ((stderr or "") + "\n" + (stdout or "")).rstrip()
    for line in merged.splitlines():
        if line.strip():
            add_log(f"[{label}] {line}")

    return proc.returncode, ""


def _run_and_log_raw(
    cmd: list[str],
    label: str,
    cwd: Path | None = None,
    timeout: int = 600,
    stop_event: threading.Event | None = None,
) -> tuple[int, str, str]:
    """运行 git 命令、**也把 stdout/stderr 转发到日志**，但**额外返回原始字符串**。

    适用场景：除了显示给用户，调用方还要**自己解析**命令输出做后续判断
    （比如 fsck 退出码、统计错误数）。既不丢用户可见性，也不丢程序可解析性。
    支持 stop_event 取消：与 _run_and_log 一致。
    """
    cancelled_box: list[bool] = [False]
    try:
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            encoding="utf-8", errors="replace",
            cwd=str(cwd) if cwd else None,
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
        )
    except Exception as e:
        add_log(f"[{label}] 异常: {e}", logging.ERROR)
        return -1, "", str(e)

    watcher_done = _watch_and_cancel(proc, stop_event, cancelled_box)

    try:
        try:
            stdout, stderr = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            try:
                proc.kill()
                proc.communicate()
            except Exception:
                pass
            add_log(f"[{label}] 超时 ({timeout}s) — 已 kill", logging.ERROR)
            return -1, "", "timeout"
    finally:
        watcher_done.wait(2.0)

    if cancelled_box[0]:
        add_log(f"[{label}] ⚠ 已取消（用户中断）", logging.WARNING)
        return -1, "", ""

    merged = ((stderr or "") + "\n" + (stdout or "")).rstrip()
    for line in merged.splitlines():
        if line.strip():
            add_log(f"[{label}] {line}")

    return proc.returncode, stdout or "", stderr or ""


def backup_repo(
    repo: dict,
    backup_dir: Path,
    state: dict[str, Any],
    token: str,
    source: str = "all",
    clone_mode: str = "mirror",
    stop_event: threading.Event | None = None,
) -> bool:
    """
    备份单个仓库（clone --mirror 或 git clone + fetch）。
    返回是否成功。状态写入 state["repos"]。
    source: "all"（全量）或 "extra"（指定仓库）。
    clone_mode: "mirror"（bare 镜像）或 "normal"（含工作目录，可直接浏览文件）。

    安全：token 绝不进 subprocess argv（避免 /proc/<pid>/cmdline / ps aux 暴露）。
    改为 `git -c http.extraheader=Authorization: Basic <base64>` 注入 header。
    """
    import base64

    repo_name: str = repo.get("name", "unknown")
    repo_full: str = repo.get("full_name", repo_name)
    clone_url: str = repo.get("clone_url", "")

    if not clone_url:
        add_log(f"[{repo_name}] 跳过：无 clone_url", logging.WARNING)
        return False

    # 构造 auth 注入参数：把 token 通过 http.extraheader 传，URL 保持裸的
    # 这样 ps / /proc 都看不到 token（argv 只有 -c http.extraheader=<encoded>）
    auth_args: list[str] = []
    if token and clone_url.startswith("https://"):
        basic = base64.b64encode(f"x-access-token:{token}".encode()).decode()
        auth_args = ["-c", f"http.extraHeader=Authorization: Basic {basic}"]

    # 根据克隆模式确定仓库路径（兼容模式切换：已有仓库保持原格式）
    if clone_mode == "normal":
        primary_path = backup_dir / repo_name
        secondary_path = backup_dir / f"{repo_name}.git"
    else:
        primary_path = backup_dir / f"{repo_name}.git"
        secondary_path = backup_dir / repo_name

    if primary_path.exists():
        repo_path = primary_path
    elif secondary_path.exists():
        repo_path = secondary_path
    else:
        repo_path = primary_path

    is_mirror = repo_path.name.endswith(".git")
    old_info = state["repos"].get(repo_full, {})

    try:
        # 入口先检查一次用户取消信号：worker 可能在上次 run 时挂起，
        # 入 backup_repo 那一刻就要让出资源，避免把"已经 cancel 的"仓也跑了
        if stop_event is not None and stop_event.is_set():
            add_log(f"[{repo_name}] 备份在开跑前已取消", logging.WARNING)
            return False

        if repo_path.exists():
            # 增量更新：用 `git fetch --all --prune --tags` 而非 `git remote update`。
            #
            # 原因：`git remote update` 在某些 git 版本（部分 Windows build、老版本）会
            # fork 子进程执行 `git fetch`，子进程不继承父进程的 `-c http.extraheader`
            # 配置 → token 注入失效 → 私有 ref 拉不下来，UI 看上去"没更新"。
            # `git fetch` 是 builtin，同进程内执行，-c 100% 生效。
            #
            # 同时 `--tags` 显式覆盖默认 refspec：mirror 模式 `+refs/*:refs/*` 已包含，
            # 普通模式默认 refspec 不含 tag（会漏 tag 更新），一并补全。
            #
            # `--progress --verbose`：
            # - `--progress` 让 subprocess 跑时也能看到 Receiving objects 进度
            # - `--verbose` 显示出 ref 变更细节：`From url` / `* [new ref]` /
            #   `= [up to date]` / `- [deleted]`，用户能清楚看到这次具体
            #   下载 / 删除 / 跳过了哪些 ref。
            add_log(f"[{repo_name}] 已有{'镜像' if is_mirror else '仓库'}，执行增量更新...")
            update_cmd = ["git", *auth_args, "fetch", "--all", "--prune", "--tags",
                          "--progress", "--verbose"]

            ret, _ = _run_and_log(update_cmd, repo_name, cwd=repo_path, stop_event=stop_event)
            if ret != 0:
                if stop_event is not None and stop_event.is_set():
                    # 用户取消：不要再 retry（继续跑只会浪费时间和触发 set-url 副作用）
                    return False
                # 可能 origin URL 失效，把 remote URL 改成裸 URL（不带 token），再试一次
                add_log(f"[{repo_name}] 重试：清理 remote URL")
                run_git_command(
                    ["git", "remote", "set-url", "origin", clone_url],
                    cwd=repo_path,
                )
                ret, _ = _run_and_log(update_cmd, repo_name, cwd=repo_path, stop_event=stop_event)
            if ret != 0:
                if stop_event is not None and stop_event.is_set():
                    return False
                add_log(f"[{repo_name}] 更新失败（已记录上方 git 报错）", logging.ERROR)
                _set_fail(state, repo_full, repo_name, clone_url, old_info,
                          f"git fetch 退出码 {ret}", source)
                return False

            # 普通模式：尝试更新工作目录
            if not is_mirror:
                ret2, _ = _run_and_log(
                    ["git", *auth_args, "pull", "--ff-only", "--progress"],
                    f"{repo_name}/worktree", cwd=repo_path, stop_event=stop_event,
                )
                if ret2 != 0:
                    if not (stop_event is not None and stop_event.is_set()):
                        add_log(f"[{repo_name}/worktree] 工作区更新退出码 {ret2}（已记录）",
                                logging.WARNING)

            add_log(f"[{repo_name}] 增量更新完成")
        else:
            # 首次克隆
            repo_path.parent.mkdir(parents=True, exist_ok=True)
            if is_mirror:
                add_log(f"[{repo_name}] 首次备份，git clone --mirror...")
                ret, _ = _run_and_log(
                    ["git", *auth_args, "clone", "--mirror", "--progress",
                     clone_url, str(repo_path)],
                    repo_name, timeout=1800, stop_event=stop_event,
                )
            else:
                add_log(f"[{repo_name}] 首次备份，git clone...")
                ret, _ = _run_and_log(
                    ["git", *auth_args, "clone", "--progress",
                     clone_url, str(repo_path)],
                    repo_name, timeout=1800, stop_event=stop_event,
                )
            if ret != 0:
                if stop_event is not None and stop_event.is_set():
                    return False
                add_log(f"[{repo_name}] 克隆失败（已记录上方 git 报错）", logging.ERROR)
                _set_fail(state, repo_full, repo_name, clone_url, old_info,
                          f"git clone 退出码 {ret}", source)
                return False
            add_log(f"[{repo_name}] 首次克隆完成")

        # ── git fsck 自检：检测 mirror / 普通仓库的对象完整性 ──
        # 仅镜像仓库跑 fsck（普通仓库只有 working tree 没有意义）
        # 失败不阻塞——标记 self._fsck_failed 透传即可
        fsck_failed = False
        if is_mirror:
            add_log(f"[{repo_name}] 完整性自检: git fsck ...")
            ret_fsck, out_fsck, _ = _run_and_log_raw(
                ["git", "fsck", "--no-progress", "--no-dangling"],
                repo_name, cwd=repo_path, timeout=120, stop_event=stop_event,
            )
            if ret_fsck != 0:
                # 被取消（rc < 0）和真的 fsck 失败（rc != 0）需区分：
                if stop_event is not None and stop_event.is_set():
                    add_log(f"[{repo_name}] fsck 取消", logging.WARNING)
                    return False
                # _run_and_log_raw 返 (rc, stderr-string)；上面调用已经把 stderr 写进日志
                # 失败时仍让仓库算 success（数据已落到本地），标记 fsck_failed 提示用户
                fsck_failed = True
                add_log(f"[{repo_name}] ⚠ fsck 校验失败（备份数据可能不完整），请手动检查",
                        logging.WARNING)

        # 保留原有 source 标记（若已有），否则使用传入的 source
        existing_source = old_info.get("source", source)
        state["repos"][repo_full] = {
            "name": repo_name,
            "full_name": repo_full,
            "clone_url": clone_url,
            "last_backup": datetime.datetime.now().isoformat(),
            "status": "success",
            "size": get_dir_size(repo_path),
            "size_bytes": get_dir_size_bytes(repo_path),
            "error": None,
            "fsck_failed": fsck_failed,
            "source": existing_source,
        }
        return True

    except Exception as e:
        safe_error = sanitize_text(str(e), token)
        add_log(f"[{repo_name}] 异常: {safe_error}", logging.ERROR)
        _set_fail(state, repo_full, repo_name, clone_url, old_info, safe_error, source)
        return False


def _set_fail(state, repo_full, repo_name, clone_url, old_info, error, source="all"):
    """记录仓库备份失败状态。"""
    state["repos"][repo_full] = {
        "name": repo_name,
        "full_name": repo_full,
        "clone_url": clone_url,
        "last_backup": old_info.get("last_backup"),
        "status": "failed",
        "size": old_info.get("size", "N/A"),
        "size_bytes": old_info.get("size_bytes", 0),
        "error": error,
        "source": old_info.get("source", source),
    }


# ──────────────────────────────────────────────────────────────────────
# 完整备份流程
# ──────────────────────────────────────────────────────────────────────

def run_full_backup(
    config: dict[str, Any],
    state: dict[str, Any],
    progress_callback=None,
    stop_event: threading.Event | None = None,
) -> bool:
    """
    执行完整备份流程。在后台线程中调用。
    包含两个阶段：① 用户/组织全量仓库 ② 指定额外仓库（去重）。
    progress_callback(current, total, message) 可选，用于通知 GUI。
    stop_event：可选取消信号，set() 后本函数安全终止。
    - **仓内取消**：set() 后会经 backup_repo → _run_and_log 的看门狗线程（≤200ms）
      调用 proc.terminate()，立即中断正在跑的 git clone / fetch / pull / fsck。
    - **仓间取消**：set() 后下个仓库开跑前直接跳过。
    返回 True 表示正常完成，False 表示被用户取消。
    """
    if stop_event is None:
        stop_event = threading.Event()

    target = config.get("target", "")
    token = config.get("token", "")
    backup_dir = Path(config.get("backup_dir", str(Path.home() / "github-backups")))
    retry_count = config.get("retry_count", 3)
    retry_delay_base = config.get("retry_delay_base", 10)
    extra_repos: list[str] = config.get("extra_repos", [])
    clone_mode = config.get("clone_mode", "mirror")

    state["backup_in_progress"] = True
    log_buffer.clear()

    add_log("========== 开始备份任务 ==========")
    add_log(f"目标: {target}")
    add_log(f"备份目录: {backup_dir}")
    if extra_repos:
        add_log(f"指定额外仓库: {len(extra_repos)} 个")
    if progress_callback:
        progress_callback(0, 0, "正在获取仓库列表...")

    backup_dir.mkdir(parents=True, exist_ok=True)

    success_count = 0
    fail_count = 0
    backed_up_names: set[str] = set()  # 已备份的仓库 full_name，用于去重

    # ── 先获取所有仓库列表，统计总任务数 ──
    all_repos_list: list[dict] = []
    extra_pending: list[str] = []

    # 阶段一：获取全量仓库
    if target:
        success, repos = get_all_repos(target, token, retry_count, retry_delay_base)
        if not success:
            add_log("获取仓库列表失败！请检查 Token 和目标是否正确。", logging.ERROR)
        else:
            all_repos_list = repos
            for r in repos:
                backed_up_names.add(r.get("full_name", r.get("name")))

    # 阶段二：统计额外仓库（去重）
    if extra_repos:
        extra_pending = [r for r in extra_repos if r not in backed_up_names]
        skipped = len(extra_repos) - len(extra_pending)
        if skipped > 0:
            add_log(f"跳过 {skipped} 个已在全量阶段备份的重复仓库")

    # 总任务数
    total_tasks = len(all_repos_list) + len(extra_pending)
    current_task = 0

    if progress_callback and total_tasks > 0:
        progress_callback(0, total_tasks, f"共 {total_tasks} 个仓库待备份")

    # ── 阶段一：全量备份用户/组织仓库 ──
    cancelled = False
    if all_repos_list:
        total = len(all_repos_list)
        add_log(f"获取到 {total} 个仓库（全量）")

        for i, repo in enumerate(all_repos_list, 1):
            # 每个仓间检查取消信号
            if stop_event.is_set():
                add_log("⚠ 用户取消，停止全量备份阶段", logging.WARNING)
                cancelled = True
                break

            full = repo.get("full_name", repo.get("name"))
            current_task += 1
            add_log(f"--- [{i}/{total}] 处理: {full} ---")
            if progress_callback:
                progress_callback(current_task, total_tasks, f"正在备份: {full}")

            if backup_repo(repo, backup_dir, state, token, source="all",
                           clone_mode=clone_mode, stop_event=stop_event):
                success_count += 1
            else:
                fail_count += 1
    elif target:
        add_log("未获取到仓库列表")
    else:
        add_log("未设置目标用户/组织，跳过全量备份阶段")

    # ── 阶段二：备份指定额外仓库（取消时不跑） ──
    if extra_pending and not cancelled:
        add_log(f"--- 开始备份指定仓库（{len(extra_pending)} 个）---")

        for i, repo_full_name in enumerate(extra_pending, 1):
            if stop_event.is_set():
                add_log("⚠ 用户取消，停止指定仓库备份阶段", logging.WARNING)
                cancelled = True
                break

            current_task += 1
            add_log(f"--- [指定 {i}/{len(extra_pending)}] 处理: {repo_full_name} ---")
            if progress_callback:
                progress_callback(current_task, total_tasks, f"指定仓库备份: {repo_full_name}")

            ok, repo_info = get_repo_info(
                repo_full_name, token, retry_count, retry_delay_base
            )
            if not ok or repo_info is None:
                add_log(f"[{repo_full_name}] 获取信息失败，跳过", logging.ERROR)
                # 记录失败状态
                state["repos"][repo_full_name] = {
                    "name": repo_full_name.split("/")[-1] if "/" in repo_full_name else repo_full_name,
                    "full_name": repo_full_name,
                    "clone_url": "",
                    "last_backup": None,
                    "status": "failed",
                    "size": "N/A",
                    "size_bytes": 0,
                    "error": "获取仓库信息失败",
                    "source": "extra",
                }
                fail_count += 1
                continue

            if backup_repo(repo_info, backup_dir, state, token, source="extra",
                           clone_mode=clone_mode, stop_event=stop_event):
                success_count += 1
            else:
                fail_count += 1
            backed_up_names.add(repo_full_name)

    state["backup_in_progress"] = False
    if not cancelled:
        # 取消时不更新 last_full_backup（不算一次完整备份）
        state["last_full_backup"] = datetime.datetime.now().isoformat()

    # 失败数 0 → 绿色（成功），失败数 > 0 → 红色（错误）。
    # 否则走 _infer_level 启发式会被消息里的"失败"二字误判为 ERROR，
    # 出现"失败=0 仍标红"的问题。
    final_level = logging.ERROR if fail_count > 0 else LOG_SUCCESS
    if cancelled:
        add_log(f"========== 备份已取消：完成 {success_count}，失败 {fail_count} ==========",
                final_level)
    else:
        add_log(f"========== 备份完成：成功 {success_count}，失败 {fail_count} ==========",
                final_level)
    save_state(state)

    if progress_callback:
        total_all = success_count + fail_count
        if cancelled:
            progress_callback(total_all, total_tasks, f"已取消：完成 {success_count}，失败 {fail_count}")
        else:
            progress_callback(total_all, total_all, f"完成：成功 {success_count}，失败 {fail_count}")

    return not cancelled


def backup_single_repo(
    repo_full_name: str,
    config: dict[str, Any],
    state: dict[str, Any],
    progress_callback=None,
    stop_event: threading.Event | None = None,
) -> None:
    """备份单个仓库（后台线程调用）。

    stop_event：可选取消信号，set() 后经 backup_repo → _run_and_log 看门狗线程
    （≤200ms）调用 proc.terminate()，立即中断正在跑的 git 子进程。
    """
    token = config.get("token", "")
    backup_dir = Path(config.get("backup_dir", str(Path.home() / "github-backups")))
    retry_count = config.get("retry_count", 3)
    retry_delay_base = config.get("retry_delay_base", 10)
    clone_mode = config.get("clone_mode", "mirror")

    state["backup_in_progress"] = True
    log_buffer.clear()

    add_log(f"========== 单仓库备份: {repo_full_name} ==========")
    if progress_callback:
        progress_callback(0, 1, f"获取仓库信息: {repo_full_name}")

    url = f"https://api.github.com/repos/{repo_full_name}"
    status, data = github_api_request(
        url, token, retry_count=retry_count, retry_delay_base=retry_delay_base
    )

    if status != 200 or data is None or not isinstance(data, dict):
        add_log(f"获取仓库信息失败: HTTP {status}", logging.ERROR)
        state["backup_in_progress"] = False
        save_state(state)
        if progress_callback:
            progress_callback(0, 1, "获取仓库信息失败")
        return

    if progress_callback:
        progress_callback(1, 1, f"正在备份: {repo_full_name}")
    # 判断来源：如果在 extra_repos 中则标记为 extra
    extra_repos = config.get("extra_repos", [])
    src = "extra" if repo_full_name in extra_repos else "all"
    backup_repo(data, backup_dir, state, token, source=src,
                clone_mode=clone_mode, stop_event=stop_event)

    state["backup_in_progress"] = False
    add_log(f"========== 单仓库备份完成: {repo_full_name} ==========")
    save_state(state)

    if progress_callback:
        progress_callback(1, 1, "完成")


def refresh_repos_list(
    config: dict[str, Any],
    state: dict[str, Any],
) -> tuple[bool, int, str]:
    """
    仅刷新仓库列表（不执行备份）。
    同时拉取全量仓库和指定额外仓库的信息。
    返回 (成功, 仓库数, 消息)。
    """
    target = config.get("target", "")
    token = config.get("token", "")
    retry_count = config.get("retry_count", 3)
    retry_delay_base = config.get("retry_delay_base", 10)
    extra_repos: list[str] = config.get("extra_repos", [])

    total_count = 0

    # ── 全量仓库 ──
    if target:
        success, repos = get_all_repos(target, token, retry_count, retry_delay_base)
        if not success:
            return False, 0, "获取仓库列表失败，请检查 Token 和目标"

        for repo in repos:
            full_name = repo.get("full_name", repo.get("name"))
            if full_name not in state["repos"]:
                # size / size_bytes 不在这里写：refresh 不知道大小，
                # 只有真正备份（backup_repo）后才算
                state["repos"][full_name] = {
                    "name": repo.get("name", ""),
                    "full_name": full_name,
                    "clone_url": repo.get("clone_url", ""),
                    "last_backup": None,
                    "status": "pending",
                    "error": None,
                    "source": "all",
                }
            else:
                # 已存在则确保 source 标记正确，同时刷新 clone_url（防止 GitHub 改路径）
                state["repos"][full_name]["source"] = "all"
                state["repos"][full_name]["clone_url"] = repo.get("clone_url", "")
                # 刷新清掉 stale error（除非失败的）
                if state["repos"][full_name].get("status") != "failed":
                    state["repos"][full_name]["error"] = None
        total_count += len(repos)

    # ── 指定额外仓库 ──
    extra_added = 0
    for repo_full_name in extra_repos:
        repo_full_name = repo_full_name.strip()
        if not repo_full_name:
            continue
        if repo_full_name not in state["repos"]:
            state["repos"][repo_full_name] = {
                "name": repo_full_name.split("/")[-1] if "/" in repo_full_name else repo_full_name,
                "full_name": repo_full_name,
                "clone_url": "",
                "last_backup": None,
                "status": "pending",
                "error": None,
                "source": "extra",
            }
            extra_added += 1
        else:
            # 已存在但来自全量，标记为 "all"（优先保留全量来源）
            pass
    total_count += extra_added

    save_state(state)
    parts = [f"全量 {total_count - extra_added} 个" if target else "无全量目标"]
    parts.append(f"指定 {extra_added} 个")
    return True, total_count, f"已获取仓库：{', '.join(parts)}"


# ──────────────────────────────────────────────────────────────────────
# 调度器 trigger 生成
# ──────────────────────────────────────────────────────────────────────

def validate_cron(expr: str) -> tuple[bool, str]:
    """校验 5-field cron 表达式（分 时 日 月 星期）是否合法。

    用 APScheduler 的 CronTrigger.from_crontab 真解析一次，捕获其 ValueError 给出友好错误。
    返回 (是否合法, 错误信息)；合法时错误信息为空串。
    """
    expr = (expr or "").strip()
    if not expr:
        return False, "Cron 表达式不能为空"
    if len(expr.split()) != 5:
        return False, f"应为 5 字段（分 时 日 月 星期），当前 {len(expr.split())} 个"
    try:
        from apscheduler.triggers.cron import CronTrigger
        CronTrigger.from_crontab(expr)
        return True, ""
    except Exception as e:
        return False, f"格式错误: {e}"


def get_schedule_trigger(schedule_type: str, cron_expression: str,
                         daily_hour: int = 2, daily_minute: int = 0,
                         weekly_day_of_week: str = "sun",
                         monthly_day_of_month: int = 1,
                         yearly_month: int = 1, yearly_day_of_month: int = 1,
                         scheduler_paused: bool = False):
    """根据配置返回 APScheduler CronTrigger 或 None（暂停）。

    参数：
    - schedule_type: "daily" | "weekly" | "monthly" | "yearly" | "custom"
    - cron_expression: 仅 custom 模式使用
    - daily_hour / daily_minute: daily / weekly / monthly / yearly 模式通用，0-23 / 0-59
    - weekly_day_of_week: "mon"-"sun"；weekly 模式用
    - monthly_day_of_month: 1-31；monthly 模式用（每个月的这一天）
    - yearly_month: 1-12；yearly 模式用（月份）
    - yearly_day_of_month: 1-31；yearly 模式用（几号）
    - scheduler_paused: True 时返回 None，调用方据此不启动调度器

    返回 CronTrigger 或 None（暂停）。
    调用前 custom 类型应先 validate_cron()；这里不再 fallback，直接抛 ValueError。
    """
    from apscheduler.triggers.cron import CronTrigger

    if scheduler_paused:
        return None

    if schedule_type == "daily":
        return CronTrigger(hour=int(daily_hour), minute=int(daily_minute))
    elif schedule_type == "weekly":
        return CronTrigger(day_of_week=str(weekly_day_of_week),
                           hour=int(daily_hour), minute=int(daily_minute))
    elif schedule_type == "monthly":
        # 每月几号：day=1-31。APScheduler 会跳过当月无效的（如 2/30）顺延到下月
        return CronTrigger(day=str(int(monthly_day_of_month)),
                           hour=int(daily_hour), minute=int(daily_minute))
    elif schedule_type == "yearly":
        # 自定义几月几号：month=1-12, day=1-31
        return CronTrigger(month=str(int(yearly_month)),
                           day=str(int(yearly_day_of_month)),
                           hour=int(daily_hour), minute=int(daily_minute))
    elif schedule_type == "custom":
        ok, err = validate_cron(cron_expression)
        if not ok:
            raise ValueError(f"无效 cron 表达式 '{cron_expression}': {err}")
        return CronTrigger.from_crontab(cron_expression.strip())
    else:
        return CronTrigger(hour=2, minute=0)
