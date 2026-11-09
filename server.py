"""news-exporter MCP Server（FastMCP，协议 2026-07-28）。

设计要点（均经实测验证）：
- 单工具 generate_news，按 out_format 出卡片/全文PDF/两者
- MRTR：页面无原生摘要时，工具体返回 InputRequiredResult（FastMCP 支持的多轮
  输入协议结构，SDK 原生的 Elicit/Resolve resolver 模式在 FastMCP 下不生效），
  客户端填写后自动重试，工具体带 ctx.input_responses 重新执行
- 服务端零 LLM：摘要要么来自页面，要么来自客户端（经 Elicit 问答）
- 零本地日志：进度经 ctx.info / ctx.report_progress 流式推送客户端
- SQLite 阶段缓存（news2card/store.py）：恢复轮/重复调用命中缓存，
  不重复抓取、不重复下载图片、不重复渲染

运行：.venv/bin/python server.py（http://127.0.0.1:8001）
"""

import asyncio
import base64
import hashlib
import mimetypes
import re
import tempfile
from pathlib import Path
from typing import Literal, Optional

from mcp_types import (ElicitRequest, ElicitRequestFormParams, ImageContent,
                       InputRequiredResult, TaskMetadata)
from fastmcp import FastMCP
from fastmcp.server.context import Context
from fastmcp.tools import ToolResult
from pydantic import BaseModel, Field
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse

import common
from news2card import router
from news2card.chrome_render import html_to_pdf, html_to_png
from news2card.extract_zaobao import ExtractError
from news2card.presets import SITE_DEFAULT_STYLE, get_size
from news2card.store import StageCache
from news2card.templates import build_article_html, build_card_html

mcp = FastMCP("news-exporter-mcp")
cache = StageCache()

FILE_ROOT = common.DEFAULT_OUTDIR.resolve()


@mcp.custom_route("/files/{filename}", methods=["GET"])
async def download_file(request: Request):
    """产物二进制下载（真流式，非 SSE）。文件名白名单校验防目录穿越。"""
    name = request.path_params.get("filename", "")
    if not re.fullmatch(r"[\w][\w.-]*", name):
        return JSONResponse({"error": "invalid filename"}, status_code=400)
    path = (FILE_ROOT / name).resolve()
    if not path.is_file() or not str(path).startswith(str(FILE_ROOT) + "/"):
        return JSONResponse({"error": "not found"}, status_code=404)
    media = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
    return FileResponse(path, media_type=media, filename=name)

ASK_MESSAGE = (
    "无法从该 URL 自动提取摘要。建议先调用 get_news_content 工具获取本文正文，"
    "再基于正文总结 1~4 条要点：summary 字段每条一行、用 \\n 分隔，无需编号；"
    "source 字段补充生成方模型信息（如 Kimi、Qwen3 等）。")


class AiSummary(BaseModel):
    """客户端生成的摘要（Elicit 结构化问答的填写模型）"""
    summary: str = Field(description="1~4条要点，每条一行，用\\n分隔")
    source: str = Field(default="", description="生成方模型信息，如 Kimi、Qwen3")


# ---------------------------------------------------------------- 阶段函数（全部幂等）

async def _stage_article(url: str, ctx: Context):
    hit = cache.get_article(url)
    if hit is not None:
        await ctx.info("命中缓存：文章已解析")
        return hit
    await ctx.info(f"正在解析：{url}")
    article = await asyncio.to_thread(router.extract, url)
    cache.save_article(url, article)
    await ctx.report_progress(0.25, 1.0)
    await ctx.info(f"解析完成：{article.title[:40]}（{article.source}｜{article.section or '-'}）")
    return article


async def _stage_media(article, ctx: Context) -> dict:
    hit = cache.get_media(article.url)
    if hit is not None:
        await ctx.info("命中缓存：图片已下载")
        return hit
    await ctx.info("正在下载图片…")
    hero = await asyncio.to_thread(common.load_hero, article, True)
    inline = await asyncio.to_thread(common.load_inline_images, article, True)
    media = {"hero": hero, "inline": inline}
    cache.save_media(article.url, media)
    await ctx.report_progress(0.5, 1.0)
    await ctx.info("图片就绪")
    return media


async def _stage_summary(article, ctx: Context):
    """返回 True=摘要就绪继续渲染；InputRequiredResult=需客户端生成。"""
    if article.summary:
        return True

    answers = ctx.input_responses  # 恢复轮：客户端的填写结果
    if answers:
        result = answers.get("summary")
        action = getattr(result, "action", None)
        if action == "accept":
            content = result.content or {}
            lines = [ln.strip() for ln in str(content.get("summary", "")).splitlines()
                     if ln.strip()]
            if lines:
                article.summary = lines[:4]
                article.summary_origin = "客户端AI"
                cache.save_article(article.url, article)
                await ctx.info(f"已采用客户端摘要（{content.get('source') or '来源未标注'}）")
                return True
            await ctx.info("客户端摘要为空，转为无摘要渲染")
            return True
        return {"ok": False, "error": f"客户端未提供摘要（action={action}）", "files": []}

    await ctx.info("页面无原生摘要，请客户端生成（MRTR 问答）")
    req = ElicitRequest(params=ElicitRequestFormParams(
        mode="form", message=ASK_MESSAGE,
        requested_schema=AiSummary.model_json_schema(),
        task=TaskMetadata(name="news-summary")))
    return InputRequiredResult(
        input_requests={"summary": req},
        request_state=f"news2card:v1:{article.url}")


def _render(article, media: dict, out_format: str, size_name: str,
            style_name: str) -> list:
    """同步渲染管线（在线程中执行）。返回产物文件路径列表。"""
    outdir = common.DEFAULT_OUTDIR
    outdir.mkdir(parents=True, exist_ok=True)
    seq = common.next_seq(outdir)
    slug = common.slug_from_url(article.url)
    files = []

    if out_format in ("card", "all"):
        size = get_size(size_name)
        qr = common.make_qr_datauri(article.url, style_name)
        html_text = build_card_html(article, media["hero"], qr,
                                    size["name"], style_name)
        out = outdir / f"{seq:03d}-card-{size['name']}-{style_name}-{slug}.png"
        with tempfile.TemporaryDirectory() as td:
            hp = Path(td) / "card.html"
            hp.write_text(html_text, encoding="utf-8")
            html_to_png(hp, out, size["width"], size["height"])
        files.append(str(out))
        seq += 1

    if out_format in ("pdf", "all"):
        qr = common.make_qr_datauri(article.url, "clean")
        html_text = build_article_html(article, media["hero"],
                                       media["inline"], qr)
        out = outdir / f"{seq:03d}-article-{slug}.pdf"
        with tempfile.TemporaryDirectory() as td:
            hp = Path(td) / "article.html"
            hp.write_text(html_text, encoding="utf-8")
            html_to_pdf(hp, out)
        files.append(str(out))

    return files


# ---------------------------------------------------------------- MCP 工具

@mcp.tool
async def generate_news(
    url: str = Field(description="新闻文章链接（支持联合早报/澎湃新闻）"),
    out_format: Literal["card", "pdf", "all"] = Field(
        default="all", description="card=分享卡片PNG / pdf=全文PDF / all=两者"),
    size: Literal["square", "portrait", "story"] = Field(
        default="square", description="卡片尺寸：方形1080² / 竖版3:4 / 竖长9:16"),
    style: Optional[Literal["clean", "zaobao", "thepaper", "wechat", "dark"]] = Field(
        default=None, description="卡片风格（缺省按站点自动匹配）"),
    ctx: Context = None,
) -> dict:
    try:
        article = await _stage_article(url, ctx)
    except ExtractError as e:
        return {"ok": False, "error": str(e), "files": []}

    summary_state = await _stage_summary(article, ctx)
    if isinstance(summary_state, InputRequiredResult):
        return summary_state          # MRTR：暂停，等客户端生成摘要
    if isinstance(summary_state, dict):
        return summary_state          # 客户端拒绝等失败
    # summary_state is True → 继续

    media = await _stage_media(article, ctx)
    style_name = style or SITE_DEFAULT_STYLE.get(article.source_id, "clean")

    # 渲染缓存键含摘要指纹：换摘要（不同客户端/模型重生成）必须重渲染
    summary_fp = hashlib.md5(
        ("\n".join(article.summary) + "|" + article.summary_origin).encode("utf-8")
    ).hexdigest()[:8]
    render_key = f"{article.url}|{out_format}|{size}|{style_name}|{summary_fp}"
    files = cache.get_render(render_key)
    if files:
        await ctx.info("命中缓存：产物已存在")
    else:
        await ctx.info(f"正在渲染（{out_format} / {size} / {style_name}）…")
        files = await asyncio.to_thread(_render, article, media,
                                        out_format, size, style_name)
        cache.save_render(render_key, files)
        await ctx.info("渲染完成")

    await ctx.report_progress(1.0, 1.0)
    structured = {"ok": True,
                  "title": article.title,
                  "summary_origin": article.summary_origin or "页面摘要" if article.summary else "",
                  "files": files,
                  "downloads": [{"name": Path(f).name,
                                 "url": f"/files/{Path(f).name}"} for f in files]}
    # 卡片 PNG 内嵌为 ImageContent（客户端 LLM 可直接"看到"卡片）；PDF 走下载流
    images = []
    for f in files:
        if f.endswith(".png"):
            b64 = base64.b64encode(Path(f).read_bytes()).decode("ascii")
            images.append(ImageContent(type="image", data=b64, mime_type="image/png"))
    if images:
        return ToolResult(content=images, structured_content=structured)
    return structured


@mcp.tool
async def get_news_content(
    url: str = Field(description="新闻文章链接（联合早报 / 澎湃新闻 / 微信公众号）"),
    ctx: Context = None,
) -> dict:
    """获取新闻详情：元信息 + 全文内容（按原文块序），不生成摘要。"""
    try:
        article = await _stage_article(url, ctx)
    except ExtractError as e:
        return {"ok": False, "error": str(e)}

    blocks = []
    for b in article.blocks:
        if b.type == "paragraph":
            blocks.append({"type": "paragraph", "text": b.text})
        elif b.type == "image" and b.image:
            blocks.append({"type": "image", "url": b.image.url,
                           "caption": b.image.caption})
        else:
            blocks.append({"type": "note", "text": b.text})

    await ctx.report_progress(1.0, 1.0)
    return {"ok": True,
            "url": article.url,
            "title": article.title,
            "source": article.source,
            "source_id": article.source_id,
            "author": article.author,
            "section": article.section,
            "published": article.published_display,
            "word_count": article.word_count,
            "hero_url": article.hero_url,
            "hero_caption": article.hero_caption,
            "has_native_summary": bool(article.summary),
            "summary": article.summary,
            "lead": article.lead,
            "content": blocks}


if __name__ == "__main__":
    mcp.run(
        transport="http",
        host="0.0.0.0",
        port=8001,
        stateless_http=False,
    )
