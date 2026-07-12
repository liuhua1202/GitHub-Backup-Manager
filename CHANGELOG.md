# Changelog

All notable changes to **GitHub Backup Manager** are documented here.
Format: [SemVer](https://semver.org/). 项目作者：liuhua。

## [v1.2.0] - 2026-07-13

### 🎨 UI 升级
- **仪表盘指标卡片重做**（Metro 磁贴风，参考 subnet-calculator `.tile` 风格）
  - **顶部 10px accent 描边**：粗条带，凸显卡片语义（蓝/绿/红/青）
  - **卡片底色 = accent 浅色版**（Tailwind 100 调色板）：dbeafe / dcfce7 / fee2e2 / cffafe
  - **4 边 6px 圆角**：Canvas 像素级自绘（4 个 PIESLICE + 中心矩形组合）
  - **数值字色 = accent 色**：与顶部描边联动，强语义化
  - **卡片间距 padx 10**：更通透
- **Palette 调色板扩展**：新增 `PRIMARY_LIGHT` / `SUCCESS_LIGHT` / `DANGER_LIGHT` / `INFO_LIGHT` 4 个浅色常量
- **BORDER 加深**：从 `#e2e8f0` (slate-200) 改为 `#cbd5e1` (slate-300)，高 DPI 缩放下也清晰

### 🐛 修复
- **备份完成日志「失败 = 0」不再误标红**：之前用 `_infer_level` 启发式看到"失败"字眼就 ERROR，现在显式按 `fail_count > 0` 决定 level
- **修复窗口 resize 后仪表盘文字消失**：Canvas 自绘的圆角 bg/stripe 在 resize handler 重画后会盖住 text 元素 —— 改用 `tag_raise("text", "stripe")` 显式重建 z 顺序
- **修复 Windows 高 DPI 缩放下 1px 边框不可见**：边框用 `ttk.Frame` 的 `relief="solid", borderwidth=2, bordercolor=BORDER`，比 bg-trick 稳

### 🛠 重构
- **`_metric_card` 重写为 Canvas 像素级自绘**：原来用 `tk.Frame` + pack/grid 各种踩坑（ttk padding 吃 pack 间隙 / grid propagate 缩掉 stripe / ttkbootstrap Canvas bg 被覆盖），最终方案：Canvas 内部画圆角矩形 + 顶条 + 文字
- **轻量 `_Handle` 类 mock Label 接口**：`val.configure(text=...)` 自动同步 Canvas 文字，调用方无感
- **删除死代码**：`MetricCard*.TFrame` 样式已无人使用（v1.2.0 改 Canvas 自绘）

### 📝 文档
- **README.md** 更新版本号到 v1.2.0 + 替换仪表盘截图
- **CHANGELOG.md** 加 v1.2.0 段

---

## [v1.1.0] - 2026-07-06

### 🎨 新增 / 优化
- **顶部菜单栏**：文件 / 设置 / 帮助 三大类入口，所有功能统一管理
  - **文件**：立即备份所有 / 备份选中 / 刷新 / 取消 / 打开目录 / 日志 / 失败详情 / 退出
  - **设置**：系统配置… / 暂停调度器 / 重新初始化 / 重置备份状态
  - **帮助**：关于… / 系统定时任务参考（Windows / macOS / Linux）/ 打开备份目录
- **系统配置对话框**：原来的设置 Notebook Tab 改为菜单驱动的 Toplevel 弹窗
- **取消粒度升级**：从「下个仓库检查点」升级到「子进程级」，`proc.terminate()` ≤200ms 打断正在跑的 `git clone / fetch / pull / fsck`
- **频率 UI 同行布局**：HH:MM spinner + 右侧周几 / 几号下拉（之前独立一行）

### ❌ 移除
- **「每年」和「自定义 Cron」UI 选项**：用户反馈 UI 太繁复；模型层保留 `get_schedule_trigger("yearly"|"custom", ...)` 分支向后兼容

### 🐛 修复
- **`diagnose.py:24` `ttkbootstrap.__version__` AttributeError**：换 `importlib.metadata.version()`
- **Tk `command=` 回调异常静默吞掉**：加 `_tk_excepthook` → 写 stderr + add_log
- **`_on_close` 中 `backup_in_progress=True` 直接被 daemon 强 kill**：现在问是否确认 + 触发 stop_event + 等 1.5s
- **`get_dir_size_bytes` / `get_disk_free` 宽 `except Exception`**：窄化到 `OSError`，不再吞 KeyboardInterrupt 等
- **`_poll_queue` 中 `in_progress` 一次读取**：每次 tick 都重新读
- **`_save_settings` 旧 monthly_dom IntVar 写 None**：走 `_clamp_int` 兜底
- **`_save_settings` 中 stale `monthly_day_of_month=None` 传到 `CronTrigger` 抛 `ValueError`**：见上述 `_clamp_int` 修复
- **`app.py:73` 用未导入的 `logging.X`**：改为 `_logging.X`
- **用户 config 里 `monthly_*/yearly_*=None` 旧版本占位符**：UI / start_scheduler / save 全路径 `_clamp_int` 兜底

### 🛠 重构
- **DRY**：`_get_text_widget` 提到模块级；`BackupApp._get_text_widget` 和 `RepoFileManager._get_text_widget` 不再各自复制
- **`_infer_level` 移除永不命中英文关键字**：基于消息全中文事实清理

### 📝 文档
- **README.md** 全量重写：覆盖 1.1.0 新增（菜单栏 / 取消粒度 / 系统配置对话框 / 跨平台定时任务参考）
- **config.json.example** 全字段模板：每个字段含 `_doc_xxx` 注释键
- **CHANGELOG.md** 建立
- **LICENSE** 加 MIT 全文

---

## [v1.0.0] - 之前

首个公开发布。功能集：
- 镜像 / 普通 双克隆模式
- 首次 `clone --mirror`，后续 `git fetch --all --prune --tags` 增量
- 每天 / 每周 / 自定义 Cron 调度
- APScheduler 后台驱动
- Token 通过 `-c http.extraheader` 注入，不进 argv
- 嵌入式文件浏览器（兼容 bare / 普通两种格式）
- 失败详情弹窗 + 批量重试
- 会话日志 + 完整日志文件 + 级别着色
- 系统通知（Windows Toast）
- 快捷键：Ctrl+S / F5 / Ctrl+R / Ctrl+L / Ctrl+,
- 命令行模式 `--cron-job`
- 跨平台：Windows / macOS / Linux
