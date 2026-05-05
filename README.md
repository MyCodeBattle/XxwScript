# aftersale-exporter

抖音小店（fxg.jinritemai.com）售后单导出工具。通过模拟商家后台的"导出"接口，自动将大时间范围的售后单数据拆分为多个小批次下载，并最终合并为单个 Excel 文件。

---

## 项目结构

```
.
├── cli.py                          # 顶层入口包装脚本
├── seed.curl                       # 从浏览器复制的 curl 请求（身份凭证）
├── aftersale_exporter/
│   ├── __init__.py
│   ├── __main__.py                 # python -m aftersale_exporter 入口
│   ├── cli.py                      # 命令行参数解析、模块组装与启动
│   ├── curl_template.py            # 解析 seed.curl，构建 HTTP 请求模板
│   ├── api.py                      # 封装平台 HTTP API（创建任务、轮询、下载）
│   ├── workflow.py                 # 核心调度器：二分拆分、轮询、下载、限流
│   ├── job.py                      # 任务 orchestration：manifest、合并
│   ├── merge.py                    # 多文件合并为单个 xlsx
│   └── progress.py                 # 终端进度条与事件日志
└── tests/                          # 单元测试
```

---

## 整体处理流程

```
┌──────────────┐      ┌─────────────────┐      ┌──────────────────┐
│   用户输入    │─────▶│   CLI 参数解析   │─────▶│  解析 seed.curl   │
│ --start/end  │      │  生成时间戳区间   │      │  提取 SessionSeed │
└──────────────┘      └─────────────────┘      └──────────────────┘
                                                        │
                                                        ▼
┌──────────────┐      ┌─────────────────┐      ┌──────────────────┐
│ 合并为 merged │◀─────│  Job.run()      │◀─────│  AftersaleApi    │
│   .xlsx      │      │  调度+下载+合并  │      │  Service (HTTP)  │
└──────────────┘      └─────────────────┘      └──────────────────┘
                              │
                              ▼
                    ┌──────────────────┐
                    │ ExportCoordinator │
                    │  核心调度器       │
                    └──────────────────┘
```

---

## 各阶段详细流程

### 1. 命令行入口 (`cli.py`)

**输入参数：**
- `--start` / `--end`：本地时区时间，支持 `YYYY-MM-DD` 或 `YYYY-MM-DD HH:MM:SS`
- `--seed-curl`：从浏览器开发者工具复制的 curl 文本文件路径（默认 `./seed.curl`）
- `--out-dir`：输出目录
- `--poll-interval`：轮询间隔（默认 5 秒）
- `--task-timeout`：单任务超时时间（默认 1800 秒）
- `--timezone`：时区（默认 `Asia/Shanghai`）

**处理细节：**
1. 解析 `--start` 和 `--end` 为本地 `datetime` 对象，若仅为日期则 `end` 自动补齐为当天 `23:59:59`
2. 转换为 Unix 时间戳（带时区信息）
3. 校验 `start <= end`
4. 读取并解析 `seed.curl` 文件，生成 `SessionSeed`
5. 初始化 `AftersaleApiService`（API 客户端）、`TimeProgressBar`（进度展示）、`AftersaleExportJob`（任务总控）
6. 调用 `job.run(start_ts, end_ts)`

---

### 2. Seed Curl 解析 (`curl_template.py`)

**目的：** 从浏览器复制的 curl 命令中提取身份凭证和请求模板，无需硬编码 Cookie。

**解析流程：**
1. 使用 `shlex.split()` 分解 curl 命令字符串
2. 提取 `-H` / `--header` 中的 headers，特别处理 `Cookie` 头
3. 提取 `-b` / `--cookie` 中的 cookies
4. 提取目标 URL，校验必须为 `https://fxg.jinritemai.com`
5. 从 URL query string 中筛选保留白名单参数：
   - `appid`, `__token`, `_bid`, `aid`
   - `aftersale_platform_source`, `msToken`, `a_bogus`, `verifyFp`, `fp`
6. 若缺少任一白名单字段则报错

**生成的 `SessionSeed` 可构建三类请求：**
- `build_export_request()`：POST `/shopuser/aftersale/export`（创建导出任务）
- `build_tasks_request()`：GET `/shopuser/aftersale/export/tasks`（查询任务状态）
- `build_download_request()`：GET `/shopuser/aftersale/export/download`（下载结果文件）

**导出请求体默认过滤条件：**
- `after_sale_status`: `audit_refunded`
- `order_by`: `status_deadline asc`
- `conf_version`: `v13`
- 时间范围由程序动态填入 `apply_time_start` / `apply_time_end`

---

### 3. API 客户端 (`api.py`)

`AftersaleApiService` 封装了与抖音小店后台的所有 HTTP 交互。

#### 3.1 创建导出任务 (`create_export`)
1. 使用 `SessionSeed` 构建导出请求
2. 发送 POST 请求（带 3 次重试）
3. 若返回 HTTP 401/403，抛出 `AuthenticationError`
4. 解析业务错误码：若错误码为 `20309001` 且包含"5万条"或"超过限制"，抛出 `OverLimitError`
5. 从响应中提取 `task_id`

#### 3.2 轮询任务状态 (`poll_task`)
1. 发送 GET 请求到 tasks 接口
2. 从响应的 `task_list` 中匹配当前 `task_id`
3. 判断完成状态：
   - `status` / `task_status` / `state` 为 `2`, `success`, `finished`, `done`, `complete`, `completed`
   - 或 `progress == 100`
4. 返回 `TaskPollResult`，包含：请求时间戳、状态文本、是否完成、文件名

#### 3.3 等待任务完成 (`wait_for_task`)
- 同步阻塞式轮询，直到任务完成或超时
- 用于独立场景，主调度器中由 `ExportCoordinator` 自行控制轮询节奏

#### 3.4 下载文件 (`download_export`)
1. 发送 GET 请求到 download 接口
2. 根据响应头 `Content-Disposition` 解析真实文件名（支持 RFC5987 编码和 mojibake 修复）
3. 若未获得文件名，则根据 `Content-Type` 推断后缀（`.xlsx` / `.csv`）
4. 将二进制内容写入 `raw/` 目录

---

### 4. 核心调度器 (`workflow.py`)

`ExportCoordinator` 是项目最核心的组件，负责将一个大的时间区间自动拆分为多个可导出的子区间，并管理它们的提交、轮询和下载。

#### 4.1 状态队列
调度器维护三个队列：
- **`pending_segments`**：待提交导出的时间区间（双端队列）
- **`active_tasks`**：已提交、正在轮询中的任务（字典，key 为 task_id）
- **`ready_downloads`**：平台已生成文件、等待下载的任务

#### 4.2 主循环 (`_run_scheduler`)
每个循环周期按**优先级**处理：

```
while pending_segments or active_tasks or ready_downloads:
    if ready_downloads:
        立即下载文件 → completed_segments
        continue

    if active_tasks 中有到期的轮询任务:
        按 next_poll_at 排序，依次轮询
        若任务完成 → 移入 ready_downloads
        若超时 → 抛出 TimeoutError
        continue

    if pending_segments 非空 且 满足导出间隔限制:
        取出一个区间，提交导出请求
        若成功 → 移入 active_tasks
        若 OverLimitError → 二分拆分，重新放入 pending_segments
        若其他异常 → 报错
        continue

    计算下一个可执行动作的时间，sleep 等待
```

#### 4.3 关键策略细节

**二分拆分策略（处理超限）：**
- 平台限制单次导出不超过 5 万条
- 当 `create_export` 返回 `OverLimitError` 时：
  - 若区间已缩小到 1 秒（`start_ts == end_ts`），则直接报错，无法继续拆分
  - 否则计算中点 `mid = (start + end) // 2`，将原区间拆分为 `[start, mid]` 和 `[mid+1, end]`
  - 新拆分出的两个区间**优先**（`appendleft`）放入 pending 队列，确保深度优先、尽快缩小粒度

**导出请求频率限制：**
- `EXPORT_GAP_SECONDS = 181` 秒
- 两次成功创建导出任务（成功返回 `task_id`）之间必须间隔至少 181 秒
- `OverLimitError`、鉴权失败、网络失败、响应缺少 `task_id` 等未成功创建任务的请求，不占用这 181 秒间隔
- 若 pending 队列有任务但未到间隔时间，调度器会 sleep 等待

**轮询与超时：**
- 每个任务的首次轮询在提交后立即执行
- 之后按 `--poll-interval`（默认 5 秒）周期性轮询
- 单任务总等待时间不超过 `--task-timeout`（默认 1800 秒）
- 超时前最后一次轮询会跳过，直接抛出 `TimeoutError`

**事件通知：**
- 调度器在关键节点通过 `event_callback` 发送事件：
  - `submitted`：任务已提交
  - `split`：区间因超限被拆分
  - `waiting_task`：进入等待文件生成状态；若后续还有待提交区间且 181 秒导出间隔未结束，会附带导出间隔剩余秒数
  - `task_polled`：轮询结果；任务未完成时若导出间隔仍未结束，也会附带导出间隔剩余秒数
  - `waiting_export_gap`：等待导出间隔，用于按秒刷新 181 秒倒计时
  - `downloaded`：文件已下载
  - `failed`：任何环节出错

---

### 5. Job 层 (`job.py`)

`AftersaleExportJob` 将调度器、文件合并和运行时追踪组装在一起。

#### 5.1 Manifest 追踪 (`ManifestTracker`)
- 在输出目录下实时写入 `manifest.json`
- 记录每个时间区间的状态：`submitted` → `downloaded` / `failed`
- 记录所有拆分事件（`splits`）和失败事件（`failures`）
- 每次事件发生后立即 `write()`，确保崩溃后可恢复现场

**manifest.json 结构示例：**
```json
{
  "summary": {
    "segment_count": 2,
    "failed_count": 0
  },
  "segments": [
    {
      "start_ts": 1774972800,
      "end_ts": 1775296799,
      "task_id": "3817396224571605464",
      "status": "downloaded",
      "file_path": "raw/售后单导出-2026-05-05-12-46-49.xlsx"
    }
  ],
  "splits": [...],
  "failures": []
}
```

#### 5.2 执行流程 (`run`)
1. 创建 `ManifestTracker`
2. 实例化 `ExportCoordinator` 并启动 `coordinator.run(start_ts, end_ts)`
3. 若全部成功，调用 `merge_tabular_exports()` 将所有原始文件合并为 `merged.xlsx`
4. 若合并阶段出错（如格式异常），将错误信息记录到 `manifest.json` 的 `merge_error` 字段
5. 任何异常都会触发 `tracker.finalize()`，保证 manifest 状态完整

---

### 6. 文件合并 (`merge.py`)

`merge_tabular_exports(files, destination)`：
1. 依次读取每个原始文件（支持 `.xlsx` 和 `.csv`）
2. 将所有行数据收集到内存列表
3. 动态收集所有出现过的列名（保持首次出现顺序）
4. 使用 `openpyxl` 创建新 Workbook
5. 第一行写入列名，后续写入各文件数据
6. 保存为 `merged.xlsx`

---

### 7. 终端进度展示 (`progress.py`)

`TimeProgressBar` 实时展示导出进度：

- **进度条**：基于已完成的"时间秒数"占总区间的比例计算
  - 总长度 = `end_ts - start_ts + 1`
  - 每下载一个区间，增加该区间的秒数
- **状态文本**：实时显示当前动作，例如：
  - `splitting 2026-04-29 00:00:00..2026-04-30 23:59:59`
  - 交互式终端（TTY）：`submitted 2026-05-05 12:46:49..2026-05-05 23:59:59 task=3817396224571605464 | 等待文件生成 | 导出间隔 181s`
  - 非交互式终端 / `TERM=dumb`：`submitted 2026-05-05 12:46:49..2026-05-05 23:59:59 task=3817396224571605464`
  - `downloaded 2026-04-29 00:00:00..2026-04-29 11:59:59`
- **事件日志**：
  - 交互式终端（TTY）：`split`、`downloaded`、`failed` 等关键事件单独输出一行到终端；`submitted` 作为主状态行起点持续刷新，不再单独打印历史日志
  - 非交互式终端 / `TERM=dumb`：关闭逐秒刷新，只输出 `submitted`、首次等待文件生成、`split`、`downloaded`、`failed` 等关键状态，避免刷屏
- **完成/失败**：结束时输出最终状态并换行

---

## 完整数据流示例

假设用户请求导出 `2026-04-29` 到 `2026-05-01`（共 3 天 = 259200 秒）：

```
1. CLI 解析时间 → start_ts=1745856000, end_ts=1746115199
2. Job 启动 Coordinator
3. Coordinator 提交 [1745856000, 1746115199]
   → OverLimitError（超过5万条）
   → split 为 [1745856000, 1745985599] 和 [1745985600, 1746115199]
4. 提交 [1745856000, 1745985599]
   → 返回 task_id=AAA
   → 轮询 AAA... 完成！文件名：售后单导出-1.xlsx
   → 下载到 raw/售后单导出-1.xlsx
5. 等待距离上一次成功创建任务满 181 秒
6. 提交 [1745985600, 1746115199]
   → 返回 task_id=BBB
   → 轮询 BBB... 完成！文件名：售后单导出-2.xlsx
   → 下载到 raw/售后单导出-2.xlsx
7. Coordinator 返回所有 segments
8. Job 调用 merge_tabular_exports()
   → 读取两个 xlsx，合并列，写入 merged.xlsx
9. manifest.json 记录全部过程
```

---

## 安装与运行

```bash
# 安装依赖（在 conda 环境 zjh 中）
conda activate zjh
python -m pip install -e .[dev]

# 查看帮助
python cli.py --help

# 执行导出（确保 seed.curl 已准备）
python -m aftersale_exporter \
  --start "2026-04-29" \
  --end "2026-04-30" \
  --out-dir ./out

# 或精确到时分秒
python -m aftersale_exporter \
  --start "2026-04-29 08:00:00" \
  --end "2026-04-29 20:00:00" \
  --out-dir ./out
```

---

## 测试

```bash
# 运行全部测试
python -m pytest

# 仅测试调度器逻辑
python -m pytest tests/test_workflow.py
```

---

## 安全与数据注意事项

- `seed.curl` 包含登录态 Cookie 和 token，**视为敏感信息**，勿提交到 Git
- 导出的 `raw/` 目录文件和 `merged.xlsx` 包含商家售后数据，注意本地保管
- `.gitignore` 已默认忽略 `raw/`、`*.xlsx`、`seed.curl` 等敏感文件
