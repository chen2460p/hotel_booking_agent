#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
酒店预订 Agent —— 多 Agent 协作版入口
运行方式：python main_multi.py
"""

import sys
from multi_agent import SupervisorAgent


def print_banner():
    banner = """
╔══════════════════════════════════════════════════════╗
║     🏨  酒店预订智能助手（多 Agent 协作版） 🏨      ║
║     Supervisor + Search + Booking + Service + Review║
╠══════════════════════════════════════════════════════╣
║  架构：Supervisor-Worker 工头制多 Agent 协作        ║
║  • SupervisorAgent：主控，意图理解+任务分发         ║
║  • SearchAgent：搜索专员，酒店搜索筛选              ║
║  • BookingAgent：预订专员，下单支付                 ║
║  • ServiceAgent：客服专员，订单查询/取消            ║
║  • ReviewAgent：评价专员，RAG 检索酒店评价          ║
╠══════════════════════════════════════════════════════╣
║  输入示例：                                          ║
║  > 帮我订下周末去三亚的海景房，预算1000以内         ║
║  > 这家酒店海景怎么样                                ║
║  > 查订单 ORDXXXXXXXX                               ║
║  > 退出                                              ║
╚══════════════════════════════════════════════════════╝
"""
    print(banner)


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
