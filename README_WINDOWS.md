# aftersale-exporter Windows 使用指南

## 快速开始（推荐）

### 1. 安装 Python
- 访问 https://www.python.org/downloads/
- 下载并安装 **Python 3.11 或更高版本**
- **安装时勾选 "Add Python to PATH"**

### 2. 准备文件
将以下文件放在同一个文件夹内：
```
aftersale-exporter/
  ├── aftersale_exporter/     (Python 源码文件夹)
  ├── tests/                  (测试文件夹，可选)
  ├── cli.py                  (入口脚本)
  ├── pyproject.toml          (项目配置)
  ├── build.bat               (打包脚本)
  ├── seed.curl               (你从浏览器复制的 curl 请求)
  └── README_WINDOWS.md       (本文件)
```

### 3. 打包成 exe（只需执行一次）
双击运行 `build.bat`，等待几分钟后会在 `dist/` 文件夹内生成 `aftersale-exporter.exe`。

### 4. 使用方法
```cmd
dist\aftersale-exporter.exe --start "2026-04-29" --end "2026-04-30" --out-dir out
```

参数说明：
- `--start`：开始时间，格式 `YYYY-MM-DD` 或 `YYYY-MM-DD HH:MM:SS`
- `--end`：结束时间
- `--out-dir`：输出目录
- `--seed-curl`：curl 文件路径（默认读取同级目录下的 `seed.curl`）
- `--poll-interval`：轮询间隔（秒，默认 5）
- `--task-timeout`：任务超时（秒，默认 1800）
- `--timezone`：时区（默认 Asia/Shanghai）

### 5. 获取 seed.curl
1. 登录抖店后台
2. 打开浏览器开发者工具 (F12) → Network
3. 执行一次售后单导出操作
4. 右键点击对应的请求 → Copy → Copy as cURL (bash)
5. 粘贴保存为 `seed.curl` 文件

---

## 方案 B：不打包，直接运行

如果不需要 exe，同事可以直接用 Python 运行：

```cmd
python -m pip install -e .
python cli.py --start "2026-04-29" --end "2026-04-30" --out-dir out
```

---

## 方案 C：pip 安装 wheel

你也可以自己打包成 wheel 分发：

```bash
# 在你的电脑上执行
python -m pip install build
python -m build --wheel
```

生成 `dist/*.whl` 文件发给同事：

```cmd
# 在同事电脑上执行
python -m pip install aftersale_exporter-0.1.0-py3-none-any.whl
aftersale-exporter --start "2026-04-29" --end "2026-04-30" --out-dir out
```

---

## 常见问题

**Q: 提示缺少模块？**  
A: 确保运行 `build.bat` 前已经执行过 `python -m pip install -e .`

**Q: seed.curl 是什么？**  
A: 这是从浏览器开发者工具复制的原始请求，包含登录凭证，请勿泄露给他人。

**Q: 输出文件在哪？**  
A: 在 `--out-dir` 指定的目录下，包含：
- `manifest.json`：导出记录
- `merged.xlsx`：合并后的表格
- `raw/`：原始下载文件
