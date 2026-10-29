# -*- coding: utf-8 -*-
"""文本归一与名称相似度工具（携程/高德/评分匹配共用）。"""

import difflib
import re


def normalize_name(name: str) -> str:
    """名称归一：去括号/标点/空格，只保留中英文数字。"""
    return re.sub(r"[^\u4e00-\u9fa5a-zA-Z0-9]", "", name or "")


def name_similarity(a: str, b: str) -> float:
    """名称相似度：归一后取 SequenceMatcher 比率。"""
    na, nb = normalize_name(a), normalize_name(b)
    if not na or not nb:
        return 0.0
    return difflib.SequenceMatcher(None, na, nb).ratio()
