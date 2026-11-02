"""摘要级联：页面自带 > 站点摘要 > Gemini/Kimi（可选）> 提取式 > 导语。

默认 auto 模式零 LLM、离线可跑；--summary ai 时对缺少像样摘要的文章
调用 LLM（默认 Gemini，失败自动用 Kimi 兜底，也可 --llm kimi 直接用）。
.env 仅由本模块在运行时读取（GEMINI_API_KEY / KIMI_API_KEY / HTTPS_PROXY /
SUMMARY_MODEL / KIMI_MODEL），密钥不进入日志与调用方上下文；
代理只用于 LLM 请求，不影响页面/图片下载。
"""

import json
import os
import re
import time
from pathlib import Path
from typing import Callable, List, Optional, Tuple

from .models import Article

_ENV_PATHS = (
    Path(__file__).resolve().parent.parent / ".env",
    Path.cwd() / ".env",
)

GEMINI_URL = ("https://generativelanguage.googleapis.com/v1beta/openai"
              "/chat/completions")
KIMI_URL = "https://api.moonshot.cn/v1/chat/completions"
DEFAULT_GEMINI_MODEL = "gemini-3.5-flash-lite"
DEFAULT_KIMI_MODEL = "kimi-k2.6"   # 性价比款；3 系列留作高质量需求

_PROMPT = """你是新闻编辑，为微信分享卡片撰写内容要点。

标题：{title}
频道：{section}
导语：{lead}
正文（节选）：
{body}

要求：视内容长短输出 1~4 条中文要点（短文少写、长文多写，不硬凑数），
每条不超过 40 字，只保留核心事实，不评论、不延展。
每行一条，不要序号、不要项目符号。"""

# 提取式打分用热点词表（与新闻重要性相关）
_KEYWORDS = (
    "宣布", "任命", "当选", "辞职", "访华", "制裁", "会晤", "峰会", "会议",
    "首次", "突破", "发现", "发布", "出台", "签署", "协议", "条约", "政策",
    "危机", "冲突", "选举", "公投", "降息", "加息", "关税", "贸易", "增长",
    "暴跌", "暴涨", "破产", "裁员", "收购", "上市", "判决", "立法",
)

# 句中应降权的"非要点"线索
_NOISE = ("记者", "摄影", "摄", "资料图", "图源", "编辑", "来源：", "图片来源",
          "点击", "阅读全文", "下载", "扫码", "关注", "订阅")


def _load_env() -> dict:
    cfg = {}
    for path in _ENV_PATHS:
        if not path.exists():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                cfg.setdefault(k.strip(), v.strip().strip('"').strip("'"))
    return cfg


def _usable_native(article: Article) -> bool:
    """页面/站点自带摘要是否像样：早报要点式 或 总字数 > 80。"""
    if not article.summary:
        return False
    if article.source_id == "zaobao" and len(article.summary) >= 2:
        return True
    return sum(len(s) for s in article.summary) > 80


# ---------------------------------------------------------------- 提取式摘要

def _split_sentences(article: Article) -> List[Tuple[str, int, int]]:
    """返回 (句子, 段落序号, 段内句序号)，滤掉过短/过长/噪声句。"""
    out = []
    for pidx, para in enumerate(article.plain_paragraphs):
        if pidx == 0 and para == article.lead:
            pass  # 导语也参与，位置分自然偏高
        for sidx, sent in enumerate(re.split(r"(?<=[。！？])", para)):
            sent = sent.strip()
            if not (12 <= len(sent) <= 65):
                continue
            if any(n in sent for n in _NOISE):
                continue
            out.append((sent, pidx, sidx))
    return out


def extractive_summarize(article: Article, max_items: int = 4) -> List[str]:
    candidates = _split_sentences(article)
    if not candidates:
        return []

    scored = []
    for sent, pidx, sidx in candidates:
        score = 0
        score += max(0, 6 - pidx) * 2          # 位置：靠前段落加权
        if sidx == 0:
            score += 3                          # 段首句
        score += min(5, sum(1 for k in _KEYWORDS if k in sent))  # 热点词
        if 22 <= len(sent) <= 48:
            score += 2                          # 长度甜区
        scored.append((score, pidx, sidx, sent))

    scored.sort(key=lambda x: (-x[0], x[1], x[2]))
    picked = scored[:max_items]
    picked.sort(key=lambda x: (x[1], x[2]))     # 按文档顺序输出
    return [s for _, _, _, s in picked]


# ---------------------------------------------------------------- LLM 摘要（OpenAI 兼容接口通用实现）

def _parse_lines(text: str) -> List[str]:
    lines = [re.sub(r"^\s*(?:\d{1,2}[.、)]\s*)?[\s•\-—·.、]*", "", ln).strip()
             for ln in text.splitlines()]
    return [ln for ln in lines if 8 <= len(ln) <= 80][:4] or None


def _chat_summarize(url: str, key: Optional[str], model: str,
                    proxy: Optional[str], article: Article,
                    retries: int = 2, timeout: int = 90,
                    temperature: Optional[float] = None) -> Optional[List[str]]:
    if not key:
        return None
    body_text = "\n".join(article.plain_paragraphs[:10])[:3000]
    prompt = _PROMPT.format(title=article.title, section=article.section or "新闻",
                            lead=article.lead[:200], body=body_text)
    proxies = {"http": proxy, "https": proxy} if proxy else None
    payload = {"model": model,
               "messages": [{"role": "user", "content": prompt}],
               "max_tokens": 2048}   # 思考型模型（kimi-k2.x）的推理 token 计入上限，需留余量
    if temperature is not None:
        payload["temperature"] = temperature

    import requests
    for attempt in range(retries + 1):
        try:
            r = requests.post(
                url,
                headers={"Authorization": f"Bearer {key}",
                         "Content-Type": "application/json"},
                json=payload,
                proxies=proxies, timeout=timeout)
            if r.status_code == 200:
                text = r.json()["choices"][0]["message"]["content"]
                return _parse_lines(text)
            if r.status_code in (429, 500, 502, 503) and attempt < retries:
                time.sleep(2 * (attempt + 1))
                continue
            return None
        except Exception:
            if attempt < retries:
                time.sleep(2 * (attempt + 1))
                continue
    return None


def _cfg(key: str) -> str:
    return os.environ.get(key) or _load_env().get(key, "")


def gemini_summarize(article: Article) -> Optional[List[str]]:
    return _chat_summarize(
        GEMINI_URL, _cfg("GEMINI_API_KEY"),
        _cfg("SUMMARY_MODEL") or DEFAULT_GEMINI_MODEL,
        _cfg("SUMMARY_PROXY") or _cfg("HTTPS_PROXY"), article)


def kimi_summarize(article: Article) -> Optional[List[str]]:
    return _chat_summarize(
        KIMI_URL, _cfg("KIMI_API_KEY"),
        _cfg("KIMI_MODEL") or DEFAULT_KIMI_MODEL,
        _cfg("SUMMARY_PROXY") or _cfg("HTTPS_PROXY"), article)


_PROVIDERS = {
    "gemini": gemini_summarize,
    "kimi": kimi_summarize,
}


# ---------------------------------------------------------------- 级联入口

def resolve(article: Article, mode: str = "auto",
            llm: str = "gemini") -> Tuple[List[str], str]:
    """返回 (要点列表, 来源标签)。
    mode: auto(纯代码) / ai(可调用 LLM)；llm: gemini(失败自动兜 Kimi) / kimi(只用 Kimi)。
    """
    if _usable_native(article):
        return article.summary[:4], "页面摘要"

    if mode == "ai":
        order = ["gemini", "kimi"] if llm == "gemini" else ["kimi"]
        for name in order:
            lines = _PROVIDERS[name](article)
            if lines:
                label = "Gemini" if name == "gemini" else "Kimi"
                return lines, label

    lines = extractive_summarize(article)
    if lines:
        return lines, "提取式"

    if article.summary:
        return article.summary[:3], "站点摘要"
    if article.lead:
        return [article.lead], "导语"
    return [], "无"
