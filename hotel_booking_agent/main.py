#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
酒店预订 Agent —— 主程序入口
运行方式：python3 main.py
"""

import sys
from agent import HotelBookingAgent


def print_banner():
    """打印欢迎横幅"""
    banner = """
╔══════════════════════════════════════════════╗
║          🏨  酒店预订智能助手  🏨           ║
║      Agent + Workflow 混合架构演示           ║
╠══════════════════════════════════════════════╣
║  支持功能：                                   ║
║  • 自然语言搜索酒店（城市/日期/预算/设施）   ║
║  • 多轮对话澄清参数                           ║
║  • 酒店对比推荐                               ║
║  • 下单 + 支付流程                            ║
║  • 订单查询 / 取消                            ║
╠══════════════════════════════════════════════╣
║  输入示例：                                   ║
║  > 帮我订下周末去三亚的海景房，预算1000以内  ║
║  > 查订单 ORDXXXXXXXX                        ║
║  > 退出                                       ║
╚══════════════════════════════════════════════╝
"""
    print(banner)


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
