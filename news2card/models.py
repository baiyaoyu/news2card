"""标准化文章数据模型（抽取层与渲染层之间的唯一契约）。"""

from dataclasses import dataclass, field
from typing import List, Optional


@dataclass
class BodyImage:
    """正文内嵌图片"""
    url: str
    caption: str = ""


@dataclass
class BodyBlock:
    """正文块，按原文顺序保留类型。
    type: "paragraph" | "image" | "note"
    note 用于无法静态还原的元素（如交互图表）的占位说明。
    """
    type: str
    text: str = ""
    image: Optional[BodyImage] = None

@dataclass
class AiSummary:
    """AI 摘要条目"""
    summary: str
    source: str = ""  # 摘要来源标签（Gemini/页面摘要/提取式/…，用于卡片标注）

@dataclass
class Article:
    url: str
    source: str                      # 来源名，如 "联合早报"
    source_id: str = ""              # 来源标识，如 "zaobao"（用于站点默认风格/PDF 强调色）
    title: str = ""
    author: str = ""
    section: str = ""                # 频道/版面，如 "中国·政情"
    published_iso: str = ""          # ISO 8601，含时区
    published_display: str = ""      # 已格式化展示时间
    summary: List[str] = field(default_factory=list)   # AI 摘要等条目化要点
    lead: str = ""                   # 导语（首段）
    blocks: List[BodyBlock] = field(default_factory=list)
    hero_url: str = ""               # 头图
    hero_caption: str = ""
    word_count: int = 0
    summary_origin: str = ""         # 摘要来源标签（Gemini/页面摘要/提取式/…，用于卡片标注）

    @property
    def plain_paragraphs(self) -> List[str]:
        return [b.text for b in self.blocks if b.type == "paragraph"]
