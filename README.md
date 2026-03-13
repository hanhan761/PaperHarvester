# PaperHarvester

PaperHarvester 是一个面向科研人员的**自动化文献滚雪球检索与下载系统**。从你放入的几篇种子 PDF 出发，自动提取引用网络、调用大语言模型评估相关性、按研究方向分类存储，并持续扩展直到达成目标数量——实现 **"投入种子 → 评估 → 下载 → 滚雪球扩展"** 的全自动闭环。

系统被设计为一个**持续演进的数据库**：你可以随时向 `todo/` 文件夹中放入新的种子文献 PDF，无需重启，系统会自动识别并处理新增文件。

---

## 核心特性

| 特性 | 说明 |
|------|------|
| 🔄 **滚雪球拓展** | 通过 Semantic Scholar + Crossref 双 API 构建引用关系网，从种子裂变出完整知识库 |
| 📄 **PDF 深度解析** | 从 PDF 原文提取参考文献（PyMuPDF 全文解析 + 二进制正则兜底），再通过 Crossref 反查 DOI |
| 🤖 **多主题智能评估** | 集成 DeepSeek，一次 API 调用同时评估论文与所有研究方向的契合度（10 级评分） |
| 📂 **自动分类存储** | 按最佳匹配主题自动归入 `核心文献 / 相关文献` 子目录 |
| 🔁 **增量种子管理** | 基于文件指纹跟踪已处理的种子 PDF，支持运行中热加载新增文献 |
| 💾 **断点续传** | SQLite (WAL 模式) 持久化，随时中断、完美续传 |
| 🌐 **智能下载调度** | 基于 Playwright 的隔离下载器，自动轮询镜像站 + 人机验证自动退回 Headed 模式 |

---

## 目录结构

```text
PaperHarvester/
├── main.py                    # 核心主程序引擎
├── config.json                # 核心配置清单
├── .env                       # 环境变量 (DeepSeek API Key)
├── src/
│   └── download_single.py     # 独立的论文下载调度脚本
├── todo/                      # 📥 种子投入口：放入 PDF，系统自动解析为种子
├── papers.db                  # 进度库：自动创建，记录所有 DOI 状态
└── <storage_path>/            # 📤 成果库 (由 config.json 指定)
    ├── 液体火箭发动机组件与参数化设计/
    │   ├── core_papers/       #   ≥7 分的核心文献
    │   └── relevant_papers/   #   5-6 分的相关文献
    ├── 多物理场耦合仿真与PINN求解/
    │   ├── core_papers/
    │   └── relevant_papers/
    └── 基于隐式张量场的生成式几何建模与编译/
        ├── core_papers/
        └── relevant_papers/
```

---

## 快速开始

### 1. 安装依赖

```bash
pip install requests tenacity python-dotenv rich PyMuPDF playwright
playwright install chromium
```

### 2. 配置 API 密钥

在项目根目录的 `.env` 文件中写入：

```env
DEEPSEEK_API_KEY=sk-your-api-key-here
```

### 3. 放入种子文献

将你已有的相关 PDF 拖入 `todo/` 文件夹。系统启动时会自动提取其中的 DOI 作为滚雪球起点。

### 4. 运行

```bash
python main.py
```

终端会显示实时进度面板，下载的文献自动按主题分类存储。

---

## 配置文件 (`config.json`)

所有字段均有合理默认值，你只需覆盖需要修改的部分。

### 基础配置

| 字段 | 类型 | 说明 |
|------|------|------|
| `storage_path` | string | 文献成果存储根目录。留空则使用项目目录 |
| `target_download_count` | int | 目标下载数量，达成后自动停止 |
| `topics` | array | 研究方向列表（见下方说明） |

### 多主题配置 (`topics`)

系统支持同时定义多个研究方向，每篇文献会被评估与所有方向的契合度，归入最佳匹配的主题文件夹：

```json
{
    "topics": [
        {
            "name": "液体火箭发动机组件与参数化设计",
            "description": "推力室热力学循环分析，Rao Bell 喷管参数化设计，再生冷却通道构型..."
        },
        {
            "name": "多物理场耦合仿真与PINN求解",
            "description": "共轭传热(CHT)求解，流固耦合(FSI)，低周疲劳预测，PINN求解PDE..."
        }
    ]
}
```

> **💡 调优建议**：`description` 越具体，评分越精准。避免 `"火箭发动机设计"` 这样宽泛的描述，改为精确的技术关键词堆叠效果更好。

### 超时与速率控制 (`timeouts`)

| 字段 | 默认值 | 说明 |
|------|--------|------|
| `http_timeout_sec` | `30` | 单次 HTTP 请求超时（秒） |
| `download_subprocess_timeout_sec` | `60` | 单篇 PDF 下载最大等待（秒） |
| `api_call_interval_sec` | `1.5` | API 调用冷却间隔（秒），防止频率限制 |
| `max_runtime_sec` | `0` | 全局最大运行时间（秒），`0` = 不限 |
| `inactivity_timeout_sec` | `0` | 连续无成功下载超时（秒），`0` = 不限 |

### 滚雪球行为 (`snowball`)

| 字段 | 默认值 | 说明 |
|------|--------|------|
| `enabled` | `true` | 是否开启滚雪球拓展 |
| `extract_from_downloaded_pdf` | `true` | 是否从已下载 PDF 提取引用 DOI |
| `max_depth` | `5` | 最大滚雪球深度 (0=种子, 1=种子的引用, ...) |

---

## 相关性评分与筛选策略

系统采用 **10 级评分**，由 DeepSeek 根据论文与各主题的契合度自动打分：

| 分数 | 级别 | 下载行为 | 滚雪球行为 |
|------|------|----------|------------|
| 9-10 | 🔴 核心相关 | ✅ → `core_papers/` | 全量展开 (API + PDF 引用) |
| 7-8 | 🟠 高度相关 | ✅ → `core_papers/` | 全量展开 (API + PDF 引用) |
| 5-6 | 🟡 中等相关 | ✅ → `relevant_papers/` | 仅展开 API 引用 (精简模式) |
| 3-4 | 🔵 边缘相关 | ❌ 不下载 | ❌ 剪枝 |
| 1-2 | ⚪ 不相关 | ❌ 不下载 | ❌ 剪枝 |

---

## 持续演进：增量种子管理

PaperHarvester 被设计为一个**持续演进的数据库**，支持随时追加种子文献：

1. **随时投入**：将新的 PDF 拖入 `todo/` 文件夹
2. **自动识别**：系统通过文件指纹 (`sha256(文件名|大小|修改时间)`) 跟踪已处理的种子，只处理新增/变更的文件
3. **热加载**：运行中每处理 50 个 DOI 会自动重扫 `todo/`，无需重启

已处理记录存储在 `papers.db` 的 `processed_seeds` 表中，可通过以下命令查看：

```bash
python -c "import sqlite3; conn=sqlite3.connect('papers.db'); [print(r) for r in conn.execute('SELECT filename, score, doi_count, processed_at FROM processed_seeds').fetchall()]; conn.close()"
```

---

## 数据库说明

`papers.db` 包含两张核心表：

| 表 | 用途 |
|----|------|
| `papers` | DOI 全生命周期状态 (`pending → evaluated / downloaded / failed`) |
| `processed_seeds` | 已处理的种子 PDF 指纹记录，支持增量处理 |

状态流转：`pending` → (获取元数据 + 评估) → `evaluated`(不相关) / `downloaded`(下载成功) / `failed`(下载失败)

---

## 常见问题

1. **网络代理**：访问学术 API 可能需要系统代理支持。
2. **频率限制**：`api_call_interval_sec` 防止请求过频，非必要请勿调小。
3. **人机验证**：日志提示需要验证时，会弹出浏览器窗口，手动完成后程序自动接管。
4. **断点续传**：随时 `Ctrl+C` 中断，再次运行自动接续进度。
5. **重置数据库**：运行 `python reset.py` 可重置进度数据库。