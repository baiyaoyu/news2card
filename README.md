# news2card

新闻详情页 → PNG 分享卡片 / PDF 全文。**纯代码流水线，零 LLM 参与**。

支持站点：
| 站点 | 域名 | 卡片默认风格 | PDF 强调色 |
|---|---|---|---|
| 联合早报 | zaobao.com | 早报红（zaobao） | 红 |
| 澎湃新闻 | thepaper.cn | 澎湃蓝（thepaper） | 蓝 |

## 环境

- Python 3.13（本机 `/usr/local/python3.13/bin/python3.13`，已装 requests / beautifulsoup4 / lxml / qrcode / Pillow）
- 系统安装 `google-chrome`（无头渲染 PNG/PDF）
- 中文字体（文泉驿微米黑等）

```bash
pip install -r requirements.txt   # 如缺依赖
```

## 用法

```bash
python3.13 news2card.py <文章URL> [选项]
```

| 选项 | 说明 |
|---|---|
| `--format card\|pdf\|all` | 输出格式（默认 `all`） |
| `--size square\|portrait\|story` | 卡片尺寸：方形1080² / 竖版3:4 / 竖长9:16（默认 `square`） |
| `--style clean\|zaobao\|thepaper\|dark` | 卡片风格（默认按站点自动匹配官网拟态色，可强制指定） |
| `--summary auto\|ai\|off` | 摘要模式（默认 `auto` 纯代码级联；`ai` 缺摘要时调 LLM；`off` 关闭） |
| `--llm gemini\|kimi` | AI 提供方（默认 `gemini`，失败自动兜 `kimi`；`kimi` 只用 Kimi） |
| `--outdir <dir>` | 覆盖默认输出目录 |
| `--no-images` | 不下载图片（纯文字，速度快） |

输出（默认目录 `/home/share/news-pics/`，平铺 + `NNN-` 递增编号）：
- `NNN-card-<尺寸>-<风格>-<slug>.png` — 微信分享卡片（封面 + 标题 + 摘要 + 来源 + 原文二维码）
- `NNN-article-<slug>.pdf` — 全文 PDF（标题/元信息/摘要框/正文/头图/图片/扫码尾卡）
- 卡片与 PDF 均带摘要来源小字标注：「要点由 AI 生成」「摘要来自联合早报 · AI 辅助生成」「要点摘自原文」等

示例：

```bash
python3.13 news2card.py "https://www.thepaper.cn/newsDetail_forward_34329723"   # 自动澎湃蓝
python3.13 news2card.py "<zaobao-url>" --style dark                              # 强制暗夜风
```

## 架构

```
news2card.py            CLI 入口
news2card/
├── router.py           域名 → 抽取器路由（新增站点在此注册）
├── extract_zaobao.py   早报抽取：JSON-LD + DOM 选择器
├── extract_thepaper.py 澎湃抽取：__NEXT_DATA__ 内嵌 JSON
├── models.py           Article 标准数据模型
├── images.py           图片下载（按图床匹配 Referer 防 403）+ base64 内嵌
├── presets.py          卡片尺寸 × 风格预设 + 站点默认风格映射
├── summarizers.py      摘要级联（页面/站点/提取式/Gemini/Kimi），可选 LLM
├── templates.py        HTML 模板（卡片 + 全文页，PDF 强调色按站点参数化）
└── chrome_render.py    无头 Chrome：HTML → PNG / PDF
```

### 各站抽取来源

**联合早报**（服务端直出）：JSON-LD NewsArticle（标题/时间/头图）+ DOM（`a.byline_area_small` 频道、`.byline_area` 署名、`更新 / ...` 时间、`<zb-ai-summary>` AI 摘要、`div.order-8 article` 正文）。

**澎湃新闻**（Next.js SSR）：`__NEXT_DATA__ → contentDetail`（name/pubTime/author/summary/nodeInfo/pic）+ content HTML 按文档序解析 p/img；图注经 `images[]` 数组按 imageid 映射；视频插入占位说明。

### MCP Server（可选）

`server.py` 基于 FastMCP 4.1.4 / mcp 2.3.0（协议 2026-07-28），HTTP 传输：

```bash
.venv/bin/python server.py        # http://127.0.0.1:8001/mcp/
```

- **单工具** `generate_news(url, out_format, size, style)` → `{ok, title, summary_origin, files}`
- **内容工具** `get_news_content(url)` → 元信息 + 按原文块序的全文（paragraph/image/note）+ `has_native_summary` 标记；同样走缓存与流式，不做摘要生成（澎湃的"原生摘要"为 80 字截断导语，长度由调用方自行判断）
- **服务端零 LLM**：页面有原生摘要直接用；没有则通过 MRTR（多轮输入）协议暂停——工具体返回 `InputRequiredResult`，客户端经 elicitation 填写结构化摘要（`AiSummary: summary/source`）后自动重试恢复，工具从断点继续
- **断点零重复劳动**：SQLite（`news2card_cache.db`，WAL）缓存三类阶段产物——文章解析、图片下载、渲染文件；恢复轮/重复调用全部命中缓存
- **流式进度**：处理信息经 `ctx.info` / `ctx.report_progress` 实时推送客户端，服务端不本地打印
- **文件返回三通道**：工具结果内嵌卡片 PNG 为 `ImageContent`（客户端 LLM 直接可视）；结构化输出含 `downloads` 列表；二进制真流式走自定义路由 `GET /files/{filename}`（FileResponse 分块传输，带目录穿越防护）。PDF 等大文件请走下载 URL，不经 JSON/base64
- **客户端要求**：必须支持 2026-07-28 协商 + elicitation（FastMCP Client 侧注册 `elicitation_handler` 即可，框架自动驱动多轮往返）

## 摘要级联（`--summary`）

```
auto(默认):  页面自带摘要(早报AI要点) → 站点摘要(>80字) → 提取式(纯代码) → 站点截断摘要 → 导语
ai:         同上，但"提取式"之前插 LLM（默认 Gemini，失败自动 Kimi 兜底）→ 最终仍兜底提取式
off:        不生成摘要
```

- 提取式：位置加权 + 热点词打分从正文选句（至多 4 条，短文自然更少；新闻倒金字塔结构，效果可靠）
- LLM：OpenAI 兼容接口，仅用于缺像样摘要的文章；提示词要求按内容长短输出 1~4 条要点（短文不硬凑），一次调用约 500~2000 tokens
- 配置（`news2card/.env`，仅代码运行时读取，不进入任何日志）：

```bash
HTTPS_PROXY=http://127.0.0.1:7890     # 仅 LLM 请求走代理；页面/图片下载保持直连
GEMINI_API_KEY=xxx                    # 免费级别可用（gemini-3.5-flash-lite）
KIMI_API_KEY=xxx                      # 开放平台 key，兜底或 --llm kimi 时用
# SUMMARY_MODEL=gemini-3.5-flash-lite # Gemini 模型（可选）
# KIMI_MODEL=kimi-k2.6                # Kimi 模型（默认 k2.6 性价比款；可按需换 3 系列）
```

注意：
- Gemini API 不支持香港出口，代理需选择日本/美国等支持地区
- kimi-k2.x 为思考型模型：推理 token 计入 max_tokens（代码已加大余量），temperature 仅接受 1（代码已省略）
- 并非所有早报文章都有 AI 摘要（如部分快讯），无原生摘要时按级联自动兜底

### 已知实现要点

- 标题/摘要的行数钳制用模板内 JS 在渲染前完成（本机 Chrome 的 `overflow:hidden` 组合在无头截图下有绘制 bug，已规避）
- 图片 Referer 按图床主机匹配（zbstatic→早报、imgpai→澎湃），不匹配会被 WAF 403
- 二维码由 `qrcode` 本地生成，指向原文 URL；渲染字体用系统文泉驿微米黑
- 新增站点：写一个 `extract_<site>.py`（返回标准 Article）+ 在 `router.py` 注册 + 在 `presets.py`/`templates.py` 加风格映射
