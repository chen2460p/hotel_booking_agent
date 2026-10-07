#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
酒店预订 Agent —— 多 Agent 协作版入口
运行方式：python main_multi.py
"""

import sys
from multi_agent import SupervisorAgent


def _display_width(text: str) -> int:
    """
    计算字符串的显示列宽：中文/全角标点占 2 列，其余占 1 列。
    横幅只使用 ASCII 框线与项目符号，避免 Ambiguous 制表符在不同字体下宽度不一。
    """
    import unicodedata
    return sum(
        2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
        for ch in text
    )


def print_banner():
    """打印欢迎横幅（ASCII 框线按显示宽度动态生成，任何等宽字体下边框都对齐）"""
    rows = [
        ("酒店预订智能助手（多 Agent 协作版）", "title"),
        ("Supervisor + Search + Booking + Service + Review", "title"),
        ("sep", "sep"),
        ("架构：Supervisor-Worker 工头制多 Agent 协作", "section"),
        ("SupervisorAgent：主控，意图理解+任务分发", "item"),
        ("SearchAgent：搜索专员，酒店搜索筛选", "item"),
        ("BookingAgent：预订专员，下单支付", "item"),
        ("ServiceAgent：客服专员，当前/历史订单查询与取消", "item"),
        ("ReviewAgent：评价专员，RAG 检索酒店评价", "item"),
        ("sep", "sep"),
        ("输入示例：", "section"),
        ("帮我订下周末去三亚的海景房，预算1000以内", "example"),
        ("这家酒店海景怎么样", "example"),
        ("查我的订单（当前那笔） / 查历史订单（全部记录）", "example"),
        ("查订单 ORDXXXXXXXX", "example"),
        ("退出", "example"),
    ]
    prefix = {"section": "  ", "item": "  * ", "example": "  > "}

    cells = []
    for text, kind in rows:
        if kind in ("title", "sep"):
            cells.append((kind, text))
        else:
            cells.append((kind, prefix[kind] + text))

    content_w = max(_display_width(t) for kind, t in cells if kind != "sep")
    inner_w = content_w + 2  # 左右各留 1 列边距

    lines = ["+" + "-" * inner_w + "+"]
    for kind, text in cells:
        if kind == "sep":
            lines.append("+" + "-" * inner_w + "+")
        elif kind == "title":
            gap = max(inner_w - _display_width(text), 0)
            left = gap // 2
            lines.append("|" + " " * left + text + " " * (gap - left) + "|")
        else:
            lines.append("| " + text + " " * (inner_w - 1 - _display_width(text)) + "|")
    lines.append("+" + "-" * inner_w + "+")
    print("\n" + "\n".join(lines))


def main():
    print_banner()
    agent = SupervisorAgent()
    print("\n你好！我是酒店预订助手，有什么可以帮你的？")

    while True:
        try:
            user_input = input("\n你 > ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n\n感谢使用，再见！")
            break

        if not user_input:
            continue

        if user_input.lower() in ("退出", "exit", "quit", "再见", "拜拜"):
            print("助手 > 感谢使用，再见！")
            break

        response = agent.run(user_input)
        print(f"\n助手 > {response}")


if __name__ == "__main__":
    main()
