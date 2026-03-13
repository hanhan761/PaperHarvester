# PaperHarvester

PaperHarvester 是一个针对科研人员的自动化文献检索引擎。它从给定的初始种子文献出发，通过网络获取论文元数据与其引用网络，结合大语言模型分析判断文献与课题的相关性，并自动从公开渠道将相关文献的 PDF 抓取到本地，实现“查阅 -> 评估 -> 下载 -> 扩展”的“滚雪球”式全自动闭环。

---

## 核心特性

- **自动化滚雪球拓展**：利用 Semantic Scholar 和 Crossref API 构建文献引用关系网，从几篇种子文献裂变出完整的课题知识库。
- **本地 PDF 提取**：支持直接从你下载的 PDF 原文中提取 DOI 进一步发掘。
- **大模型智能过滤**：集成 DeepSeek （或其他兼容模型），对检索到的文献摘要进行深度打分过滤（核心相关 / 部分相关 / 不相关）。
- **极具鲁棒性的断点续传**：基于 SQLite (WAL模式) 进行状态持久化。随时可安全中断程序，下次启动完美接续进度。
- **智能防卡死调度**：内置基于 Playwright 的隔离下载工具，支持自动轮询多个镜像站点。当触发人机验证时，会自动退回有界面模式（Headed）等待人工点击。
- **全局与非活动超时监控**：遇死链接或无限验证码循环自动终止防卡死，同时拥有聪明的失败轮转队列，临时错误的 DOI 将推迟到末尾再试。

---

## 目录结构

```text
PaperHarvester/
├── main.py                   # 核心主程序引擎
├── src/
│   └── download_single.py    # 独立的、抗封锁的论文下载调度脚本
├── config.json               # 核心配置清单 (控制抓取深度、目标数等)
├── .env                      # 环境变量配置 (用于安全存放 API 密钥)
├── todo/                     # 启动池：放置已有文献 PDF，系统自动解析为种子
├── output/                   # 成果库：自动划分为 core_papers/ 和 relevant_papers/
└── papers.db                 # 进度库：系统运行自动创建，记录所有 DOI 当前的状态
```

---

## 安装与配置

### 1. 环境依赖

确保已安装 Python 3.8+。在项目根目录下执行：

```bash
pip install requests tenacity python-dotenv rich playwright
playwright install chromium
```
*(注：Playwright 需要安装无头浏览器组件用于稳定下载。)*

### 2. 配置 API 密钥

在项目根目录新建 `.env` 文件，填入你的大模型 API 密钥（如 DeepSeek）：

```env
DEEPSEEK_API_KEY=sk-your-api-key-here
```

### 3. 配置文件详解 (`config.json`)

`config.json` 是系统的核心配置清单，所有字段均有默认值，你只需覆盖需要修改的部分。完整示例：

```json
{
    "topic_description": "火箭发动机推力室一体化设计、再生冷却与增材制造",
    "target_download_count": 500,
    "seed_dois": [
        "10.2514/1.B38364",
        "10.1016/j.actaastro.2021.01.032"
    ],
    "paths": {
        "todo_dir": "todo",
        "output_dir": "output",
        "db_file": "papers.db",
        "download_script": "src/download_single.py",
        "env_file": ".env"
    },
    "api": {
        "semantic_scholar": "https://api.semanticscholar.org/graph/v1",
        "crossref": "https://api.crossref.org/works",
        "deepseek_url": "https://api.deepseek.com/v1/chat/completions",
        "deepseek_model": "deepseek-chat"
    },
    "timeouts": {
        "http_timeout_sec": 30,
        "download_subprocess_timeout_sec": 300,
        "api_call_interval_sec": 1.5,
        "max_runtime_sec": 3600,
        "inactivity_timeout_sec": 600
    },
    "snowball": {
        "enabled": true,
        "extract_from_downloaded_pdf": true,
        "max_depth": 3
    }
}
```

#### 基础配置

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `topic_description` | string | `"火箭发动机推力室一体化设计..."` | **研究课题描述**。用于 DeepSeek 评估每篇文献的相关性，描述越精确筛选越精准。 |
| `target_download_count` | int | `50` | 目标下载数量，达成后程序自动停止。 |
| `seed_dois` | string[] | `[...]` | 种子 DOI 列表，作为滚雪球的起点。当 `todo/` 文件夹中没有 PDF 时使用。 |

> **💡 提示**：你可以直接把已有的相关 PDF 拖入 `todo/` 文件夹，程序启动时会自动读取它们并提取 DOI 作为种子，此时 `seed_dois` 不会被使用。

#### `paths` — 路径配置

| 字段 | 默认值 | 说明 |
|------|--------|------|
| `todo_dir` | `"todo"` | 种子 PDF 存放目录（相对于项目根目录） |
| `output_dir` | `"output"` | 下载成果目录，内含 `core_papers/` 和 `relevant_papers/` 两个子目录 |
| `db_file` | `"papers.db"` | SQLite 数据库文件路径，用于断点续传 |
| `download_script` | `"src/download_single.py"` | 下载脚本路径 |
| `env_file` | `".env"` | API 密钥文件路径 |

#### `api` — API 端点

| 字段 | 默认值 | 说明 |
|------|--------|------|
| `semantic_scholar` | `"https://api.semanticscholar.org/graph/v1"` | Semantic Scholar API 地址 |
| `crossref` | `"https://api.crossref.org/works"` | Crossref API 地址，用于获取引用文献和解析参考文献 |
| `deepseek_url` | `"https://api.deepseek.com/v1/chat/completions"` | DeepSeek（或兼容 API）地址 |
| `deepseek_model` | `"deepseek-chat"` | 使用的模型名称。可替换为其他兼容 OpenAI 格式的模型 |

#### `timeouts` — 超时与速率控制

| 字段 | 默认值 | 说明 |
|------|--------|------|
| `http_timeout_sec` | `30` | 单次 HTTP 请求超时（秒） |
| `download_subprocess_timeout_sec` | `300` | 单篇 PDF 下载的最大等待时间（秒） |
| `api_call_interval_sec` | `1.5` | 每次 API 调用后的冷却间隔（秒），防止触发频率限制 |
| `max_runtime_sec` | `3600` | 程序全局最大运行时间（秒），到达后自动退出 |
| `inactivity_timeout_sec` | `600` | 连续无成功下载的超时（秒），防止卡死 |

#### `snowball` — 滚雪球行为

| 字段 | 默认值 | 说明 |
|------|--------|------|
| `enabled` | `true` | 是否开启滚雪球拓展。设为 `false` 则只处理种子文献 |
| `extract_from_downloaded_pdf` | `true` | 是否从已下载的 PDF 中提取引用文献的 DOI |
| `max_depth` | `3` | 最大滚雪球深度。深度 0 = 种子，深度 1 = 种子的引用，以此类推 |

#### 相关性评分与筛选策略

系统采用 **10 级评分**（1-10 分），由 DeepSeek 根据 `topic_description` 自动打分：

| 分数 | 级别 | 下载行为 | 滚雪球行为 |
|------|------|----------|------------|
| 9-10 | 🔴 核心相关 | ✅ 下载至 `core_papers/` | 全量展开（API + PDF 引用） |
| 7-8 | 🟠 高度相关 | ✅ 下载至 `core_papers/` | 全量展开（API + PDF 引用） |
| 5-6 | 🟡 中等相关 | ✅ 下载至 `relevant_papers/` | 仅展开 API 引用（精简模式） |
| 3-4 | 🔵 边缘相关 | ❌ 不下载 | ❌ 不展开（剪枝） |
| 1-2 | ⚪ 不相关 | ❌ 不下载 | ❌ 不展开（剪枝） |

> **📌 调优建议**：`topic_description` 越具体，评分越精准。例如 `"火箭发动机设计"` 范围太广，改为 `"液体火箭发动机推力室再生冷却通道的增材制造工艺与热结构一体化设计"` 效果更好。

---

## 运行程序

在终端中执行：

```bash
python main.py
```

终端会显示生动的进度追踪面板。下载的文献将自动保存在 `output/` 目录下。

---

## 常见问题与注意事项

1. **网络代理**：如果你在特定网络环境下，访问学术 API 可能需要系统代理支持。
2. **频率限制**：Semantic Scholar 和 Crossref 有调用频率限制。配置文件中的 `api_call_interval_sec` 防止了请求过频，非必要请勿调小。
3. **遇到人机验证**：当日志提示 `[WARN] 需要验证，请手动完成...` 时，程序会弹出一个浏览器界面。请在窗口中手动完成验证码，程序会在您通过后自动接管。