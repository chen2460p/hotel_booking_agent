# -*- coding: utf-8 -*-
"""姓名/订单状态等文本规则常量（llm 与 agent_common 共用，避免规则漂移）。"""

import re
from typing import Optional

# 等待补姓名时，裸回复这些词不应被当成姓名
NAME_STOPWORDS = {
    "谢谢", "感谢", "你好", "您好", "再见", "退出", "取消", "不要",
    "算了", "不用", "好的", "知道", "等等", "稍后", "随便",
}
# 含这些词的片段绝不是姓名（防止"入住时间改到…"被正则误抓成"时间改到"）
NAME_DENY_WORDS = (
    "时间", "日期", "改到", "改成", "推迟", "提前", "查询", "搜索",
    "重新", "酒店", "宾馆", "房型", "入住", "离店", "周末", "预算",
    "价格", "附近", "一下", "看看", "选择", "序号", "名称", "取消",
    "订单", "支付", "帮忙", "帮我", "可以", "能不", "怎么", "什么",
    "多少", "哪里", "哪个", "今天", "明天", "后天", "上午", "下午",
    "晚上", "中午", "早上", "凌晨", "几点", "小时", "星期", "推荐",
    "便宜",
)
NAME_PATTERNS = (
    r'入住人\s*[:：是叫]?\s*([一-龥]{2,4})',
    r'(?:我叫|名字是|姓名是|名字叫|叫)\s*([一-龥]{2,4})',
    r'([一-龥]{2,4})\s*(?:入住|住店|来住)',
)

# 订单状态筛选：用户说法 → 统一状态桶
ORDER_FILTER_WORDS = {
    "PENDING": ("待支付", "未支付", "未付款", "没付款", "没支付", "待付款"),
    "CANCELLED": ("已取消", "取消过", "退订的", "退款的", "退款"),
    "FINISHED": ("已完成", "完成的", "已支付", "已付款", "付过款", "已入住", "住过的"),
}
ORDER_FILTER_NAMES = {
    "PENDING": "待支付", "FINISHED": "已完成/已支付", "CANCELLED": "已取消",
}
# 明确要"翻历史/看清单"的说法——与"查我当前那笔订单"区分开
ORDER_LIST_WORDS = (
    "历史", "所有", "全部", "记录", "列表", "有哪些", "都有", "一共",
    "以前", "之前的", "以往", "过往", "老订单", "订单簿", "清单",
)
# 本地订单状态 → 统一中文文案
ORDER_STATUS_TEXT = {
    "pending": "待支付",
    "paid": "已支付",
    "cancelled": "已取消",
}

_ORDER_ID_RE = re.compile(r'(?<![A-Za-z0-9])([A-Z0-9]{8,})(?![A-Za-z0-9])')


def match_order_status(text: str) -> Optional[str]:
    """从文本识别订单状态桶；识别不出返回 None。"""
    text = text or ""
    for bucket, words in ORDER_FILTER_WORDS.items():
        if any(w in text for w in words):
            return bucket
    if any(w in text for w in ORDER_LIST_WORDS):
        return "ALL"
    return None


def extract_order_id(text: str) -> str:
    """从文本兜底提取道旅长订单号（字母数字 ≥8 位）；没有返回空串。"""
    m = _ORDER_ID_RE.search((text or "").upper())
    return m.group(1) if m else ""
