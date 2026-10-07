#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
酒店预订 Agent —— 主程序入口
运行方式：python3 main.py
"""

import sys
from agent import HotelBookingAgent


def _display_width(text: str) -> int:
    """
    计算字符串的显示列宽：中文/全角标点占 2 列，其余占 1 列。
    刻意不把 Ambiguous 字符（制表符/• 等）算成 2 列——它们在不同字体下
    宽度不一，因此横幅只使用 ASCII 框线与项目符号，保证任何等宽字体对齐。
    """
    import unicodedata
    return sum(
        2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
        for ch in text
    )


def print_banner():
    """打印欢迎横幅（ASCII 框线按显示宽度动态生成，任何等宽字体下边框都对齐）"""
    # (文本, 样式)：title 居中；section/item/example 左对齐带前缀；sep 分隔线
    rows = [
        ("酒店预订智能助手", "title"),
        ("Agent + Workflow 混合架构演示", "title"),
        ("sep", "sep"),
        ("支持功能：", "section"),
        ("自然语言搜索酒店（城市/日期/预算/设施）", "item"),
        ("多轮对话澄清参数", "item"),
        ("酒店对比推荐", "item"),
        ("下单 + 支付流程", "item"),
        ("查当前订单、历史订单查询/取消（支持状态筛选）", "item"),
        ("sep", "sep"),
        ("输入示例：", "section"),
        ("帮我订下周末去三亚的海景房，预算1000以内", "example"),
        ("查我的订单（当前那笔的状态）", "example"),
        ("查我的历史订单（全部订单记录）", "example"),
        ("查待支付的订单", "example"),
        ("查订单 ORDXXXXXXXX", "example"),
        ("退出", "example"),
    ]
    prefix = {"section": "  ", "item": "  * ", "example": "  > "}

    cells = []  # (kind, 内部文本)
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
    """主交互循环"""
    print_banner()
    agent = HotelBookingAgent()
    # 道旅真实预订登录状态（登录命令：python rollinggo_book.py login）
    try:
        import rollinggo_book as rlg
        if rlg.is_logged_in():
            print("🔓 道旅真实预订：已登录（下单将走 验价→确认→支付链接 的真实链路）")
        else:
            print("🔒 道旅真实预订：未登录（当前下单为模拟；需真实预订请运行 "
                  "python rollinggo_book.py login）")
    except Exception:
        pass
    print("\n你好！我是酒店预订助手，有什么可以帮你的？")

    while True:
        try:
            user_input = input("\n你 > ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n\n感谢使用，再见！")
            break

        if not user_input:
            continue

        # 退出命令
        if user_input.lower() in ("退出", "exit", "quit", "再见", "拜拜"):
            print("助手 > 感谢使用，再见！")
            break

        # 特殊处理：预订阶段（选房/入住人/邮箱）与验价后的二次确认
        if agent.state.stage in ("booking", "booking_confirm"):
            booking_response = agent.process_booking_confirmation(user_input)
            if booking_response:
                print(f"\n助手 > {booking_response}")
                continue

        # 正常 Agent 处理
        response = agent.run(user_input)
        print(f"\n助手 > {response}")


if __name__ == "__main__":
    main()
