# 多 Agent 协作版酒店预订系统
#
# 架构设计（对应八股 Multi-Agent 考点）：
# - SupervisorAgent（主控/工头）：意图理解、任务分发、结果汇总、用户交互
# - SearchAgent（搜索专员）：负责酒店搜索、筛选、对比推荐
# - BookingAgent（预订专员）：负责创建订单、支付确认
# - ServiceAgent（客服专员）：负责订单查询、取消等售后
#
# 通信方式：共享状态 + 直接消息（对应八股中的多 Agent 通信机制）
# 协调模式：Supervisor-Worker（工头制，生产环境最常用模式）

import re
from typing import Optional, List
from dataclasses import dataclass, field
from models import AgentState
from tools import (
    search_hotels, get_hotel_detail,
    create_order, pay_order, cancel_order, get_order_status,
    format_hotel_list, list_history_orders, format_order_rows,
    sync_real_order_status, real_order_cancel_guide,
)
from llm import LLMClient
from agent_common import (
    CLARIFY_INTENT_KEYS, BOOKING_PARAM_KEYS, ORDER_FILTER_NAMES,
    ORDER_STATUS_TEXT, merge_params, calc_nights,
    generate_clarify_question, detect_order_filter, wants_order_list,
    format_order_detail, extract_guest_name, parse_hotel_selection,
    match_room_choice, booking_exit_action, quit_booking_message,
    render_hotel_selection, chat_reply, reset_booking_state, is_selection_attempt,
)
from rag import get_rag


# ========== 消息传递结构 ==========
@dataclass
class AgentMessage:
    """Agent 之间传递的消息（对应八股中的 Agent 通信机制）"""
    sender: str          # 发送方 Agent 名称
    receiver: str        # 接收方 Agent 名称
    msg_type: str        # 消息类型：task任务/result结果/error错误/query查询
    content: str         # 消息内容
    data: dict = field(default_factory=dict)  # 附加数据（参数、结果等）


class BaseAgent:
    """所有 Agent 的基类——定义通用接口"""

    def __init__(self, name: str, llm: LLMClient):
        self.name = name
        self.llm = llm
        self.inbox: List[AgentMessage] = []   # 收件箱（消息队列）

    def receive(self, msg: AgentMessage):
        """接收消息"""
        self.inbox.append(msg)

    def process(self, shared_state: AgentState) -> Optional[AgentMessage]:
        """
        处理消息并返回回复（子类实现）
        shared_state 是所有 Agent 共享的状态（对应八股中的共享状态通信）
        """
        raise NotImplementedError


# ========== 搜索专员 Agent ==========
class SearchAgent(BaseAgent):
    """
    搜索专员——负责酒店搜索、筛选、对比推荐
    这是一个 Worker Agent，接收 Supervisor 分发的搜索任务
    """

    def __init__(self, llm: LLMClient):
        super().__init__("SearchAgent", llm)

    def process(self, shared_state: AgentState) -> Optional[AgentMessage]:
        """处理搜索任务"""
        if not self.inbox:
            return None

        msg = self.inbox.pop(0)

        if msg.msg_type == "task" and msg.content == "search_hotels":
            # 执行搜索（Workflow 确定性流程）
            params = shared_state.params
            results = search_hotels(params)
            shared_state.search_results = results

            if not results:
                return AgentMessage(
                    sender=self.name, receiver="Supervisor",
                    msg_type="result",
                    content="no_results",
                    data={"count": 0}
                )

            # 格式化搜索结果
            nights = calc_nights(params.check_in, params.check_out)
            # 位置本身已带城市名时不重复拼（“沈阳”+“沈阳化工大学附近”）
            location = (params.location or "").strip()
            if location:
                area_text = (f"{location}，" if location.startswith(params.city)
                             else f"{params.city}{location}，")
            else:
                area_text = ""
            result_text = (
                f"为你找到 {len(results)} 家符合条件的酒店"
                f"（{area_text}{params.check_in} 入住，{params.check_out} 离店，共{nights}晚）：\n\n"
                f"{format_hotel_list(results)}\n\n"
                f"请告诉我你想选择哪一家（回复序号或酒店名）。"
            )

            return AgentMessage(
                sender=self.name, receiver="Supervisor",
                msg_type="result",
                content="search_done",
                data={"count": len(results), "text": result_text}
            )

        return None


# ========== 预订专员 Agent ==========
class BookingAgent(BaseAgent):
    """
    预订专员——负责创建订单、支付确认
    处理高危操作，包含二次确认机制（对应八股中的高危操作防呆）
    """

    def __init__(self, llm: LLMClient):
        super().__init__("BookingAgent", llm)

    def process(self, shared_state: AgentState) -> Optional[AgentMessage]:
        if not self.inbox:
            return None

        msg = self.inbox.pop(0)

        # 任务1：创建订单
        if msg.msg_type == "task" and msg.content == "create_order":
            hotel = shared_state.selected_hotel
            params = shared_state.params
            room_type = msg.data.get("room_type", "")
            guest_name = msg.data.get("guest_name", "")
            price = msg.data.get("price_per_night")
            plan_note = msg.data.get("plan_note", "")

            if not hotel:
                return AgentMessage(
                    sender=self.name, receiver="Supervisor",
                    msg_type="error", content="no_hotel_selected"
                )

            valid_rooms = list(dict.fromkeys(
                hotel.room_types + [rp.room_name for rp in hotel.rate_plans]))
            if room_type not in valid_rooms:
                return AgentMessage(
                    sender=self.name, receiver="Supervisor",
                    msg_type="error", content="invalid_room_type",
                    data={"available": valid_rooms}
                )

            # 调用工具创建订单（多档报价时用所选档位的每晚价）
            order = create_order(hotel, params, room_type, guest_name,
                                 price_per_night=price)
            shared_state.current_order = order
            plan_line = f"{plan_note}\n" if plan_note else ""

            result_text = (
                f"订单创建成功！\n"
                f"订单号：{order.order_id}\n"
                f"酒店：{order.hotel_name}\n"
                f"房型：{order.room_type}\n"
                f"{plan_line}"
                f"入住人：{order.guest_name}\n"
                f"入住：{order.check_in} → {order.check_out}\n"
                f"总价：{order.total_price}元\n"
                f"状态：待支付\n\n"
                f"回复【确认支付】即可完成支付，或回复【取消订单】放弃预订。"
            )

            return AgentMessage(
                sender=self.name, receiver="Supervisor",
                msg_type="result", content="order_created",
                data={"order_id": order.order_id, "text": result_text}
            )

        # 任务2：支付
        if msg.msg_type == "task" and msg.content == "pay_order":
            order = shared_state.current_order
            if not order:
                return AgentMessage(
                    sender=self.name, receiver="Supervisor",
                    msg_type="error", content="no_order"
                )

            success = pay_order(order.order_id)
            if success:
                order.status = "paid"
                result_text = (
                    f"支付成功！\n"
                    f"订单号：{order.order_id}\n"
                    f"酒店：{order.hotel_name}\n"
                    f"入住人：{order.guest_name}\n"
                    f"总价：{order.total_price}元\n"
                    f"状态：已支付\n\n"
                    f"祝你入住愉快！有其他需要随时告诉我。"
                )
                return AgentMessage(
                    sender=self.name, receiver="Supervisor",
                    msg_type="result", content="payment_done",
                    data={"text": result_text}
                )
            else:
                return AgentMessage(
                    sender=self.name, receiver="Supervisor",
                    msg_type="error", content="payment_failed"
                )

        # 任务3：取消订单
        if msg.msg_type == "task" and msg.content == "cancel_order":
            order = shared_state.current_order
            if not order:
                return AgentMessage(
                    sender=self.name, receiver="Supervisor",
                    msg_type="error", content="no_order"
                )
            result = cancel_order(order.order_id)
            if result == "remote_unsupported":
                # 道旅真实订单：API 无取消能力，给出手动取消指引
                return AgentMessage(
                    sender=self.name, receiver="Supervisor",
                    msg_type="result", content="remote_unsupported",
                    data={"order_id": order.order_id}
                )
            return AgentMessage(
                sender=self.name, receiver="Supervisor",
                msg_type="result", content="order_cancelled"
            )

        return None


# ========== 客服专员 Agent ==========
class ServiceAgent(BaseAgent):
    """
    客服专员——负责订单查询、取消等售后场景
    """

    def __init__(self, llm: LLMClient):
        super().__init__("ServiceAgent", llm)

    def process(self, shared_state: AgentState) -> Optional[AgentMessage]:
        if not self.inbox:
            return None

        msg = self.inbox.pop(0)

        # 任务1：查询订单
        #   scope=detail 有订单号查单笔
        #   scope=list   查历史订单清单（可带状态筛选）
        #   scope=current 查当前会话那笔订单，没有进行中订单时回退历史清单
        if msg.msg_type == "task" and msg.content == "query_order":
            order_id = msg.data.get("order_id", "")
            scope_mode = msg.data.get("scope", "list" if not order_id else "detail")

            # —— 当前订单优先：直接取共享状态里正在跟进的那笔 ——
            if scope_mode == "current" and shared_state.current_order:
                order = shared_state.current_order
                pay_hint = ""
                if order.payment_url and order.status == "pending":
                    pay_hint = f"\n\n支付链接：{order.payment_url}"
                text = (
                    "你当前有一笔进行中的订单：\n"
                    f"订单号：{order.order_id}\n"
                    f"酒店：{order.hotel_name}\n"
                    f"入住：{order.check_in} → {order.check_out}\n"
                    f"房型：{order.room_type}\n"
                    f"入住人：{order.guest_name}\n"
                    f"总价：{order.total_price:g}元\n"
                    f"状态：{ORDER_STATUS_TEXT.get(order.status, order.status)}"
                    f"{pay_hint}\n\n想看全部历史订单，回复【查历史订单】即可。"
                )
                return AgentMessage(
                    sender=self.name, receiver="Supervisor",
                    msg_type="result", content="order_found",
                    data={"text": text, "order": order}
                )

            if not order_id:
                flt = str(msg.data.get("order_status", "ALL")).upper()
                result = list_history_orders(flt)
                rows = result["rows"]
                flt_names = ORDER_FILTER_NAMES
                scope_name = "道旅账号" if result["logged_in"] else "本次会话"
                title = f"你的{scope_name}订单"
                if flt in flt_names:
                    title += f"（{flt_names[flt]}）"
                if result.get("remote_error"):
                    title += f"\n⚠️ 道旅查询失败：{result['remote_error']}"
                # scope=current 但无进行中订单：回退清单时先说明一句
                prefix = ""
                if scope_mode == "current":
                    prefix = "你当前没有进行中的订单，下面是你账号里的订单记录：\n\n"
                if rows:
                    text = prefix + title + "：\n" + format_order_rows(rows) \
                        + "\n\n回复订单号可查询单笔详情。"
                elif not result["logged_in"]:
                    text = prefix + (
                        "目前没有订单。\n"
                        "💡 登录道旅（python rollinggo_book.py login）后可查询账号下"
                        "的全部真实历史订单。"
                    )
                else:
                    text = prefix + f"{title}：账号下暂无相关订单。"
                return AgentMessage(
                    sender=self.name, receiver="Supervisor",
                    msg_type="result", content="order_list",
                    data={"text": text}
                )

            order = get_order_status(order_id)
            if not order:
                return AgentMessage(
                    sender=self.name, receiver="Supervisor",
                    msg_type="result", content="order_not_found",
                    data={"order_id": order_id}
                )

            result_text = format_order_detail(order)
            return AgentMessage(
                sender=self.name, receiver="Supervisor",
                msg_type="result", content="order_found",
                data={"text": result_text, "order": order}
            )

        # 任务2：取消订单（售后，需要二次确认）
        if msg.msg_type == "task" and msg.content == "cancel_aftersale":
            order_id = msg.data.get("order_id", "")
            order = get_order_status(order_id)

            if not order:
                return AgentMessage(
                    sender=self.name, receiver="Supervisor",
                    msg_type="result", content="order_not_found",
                    data={"order_id": order_id}
                )

            # 道旅真实订单以远端状态为准（本地快照可能滞后）
            sync_real_order_status(order)

            if order.status == "cancelled":
                return AgentMessage(
                    sender=self.name, receiver="Supervisor",
                    msg_type="result", content="already_cancelled",
                    data={"order_id": order_id}
                )

            # 道旅未开放取消接口：不能只在本地标记取消，直接给指引
            if order.source == "rollinggo":
                return AgentMessage(
                    sender=self.name, receiver="Supervisor",
                    msg_type="result", content="remote_unsupported",
                    data={"order_id": order_id}
                )

            # 第一次请求：要求确认（高危操作防呆——确认层）
            if not msg.data.get("confirmed", False):
                shared_state.current_order = order
                confirm_text = (
                    f"确认要取消以下订单吗？\n"
                    f"订单号：{order.order_id}\n"
                    f"酒店：{order.hotel_name}\n"
                    f"入住：{order.check_in} → {order.check_out}\n"
                    f"总价：{order.total_price}元\n\n"
                    f"回复【确认取消】即可取消，或回复【取消操作】放弃。"
                )
                return AgentMessage(
                    sender=self.name, receiver="Supervisor",
                    msg_type="result", content="need_confirmation",
                    data={"text": confirm_text, "order_id": order_id}
                )

            # 用户已确认，执行取消
            cancel_order(order_id)
            shared_state.current_order = None
            return AgentMessage(
                sender=self.name, receiver="Supervisor",
                msg_type="result", content="cancelled",
                data={"order_id": order_id}
            )

        return None


# ========== 评价专员 Agent（RAG） ==========
class ReviewAgent(BaseAgent):
    """
    评价专员——通过 RAG 检索酒店评价，回答用户关于酒店口碑的问题
    集成 RAG 模块，展示检索增强生成能力（对应八股 RAG 考点）
    """

    def __init__(self, llm: LLMClient):
        super().__init__("ReviewAgent", llm)
        self.rag = get_rag()

    def process(self, shared_state: AgentState) -> Optional[AgentMessage]:
        if not self.inbox:
            return None

        msg = self.inbox.pop(0)

        if msg.msg_type == "task" and msg.content == "query_reviews":
            query = msg.data.get("query", "")
            # 如果用户已选中酒店，限定该酒店范围
            hotel_id = None
            if shared_state.selected_hotel:
                hotel_id = shared_state.selected_hotel.hotel_id
            elif msg.data.get("hotel_id"):
                hotel_id = msg.data["hotel_id"]

            # 调用 RAG 完整问答流程
            answer = self.rag.answer_question(query, hotel_id=hotel_id)

            return AgentMessage(
                sender=self.name, receiver="Supervisor",
                msg_type="result", content="reviews_done",
                data={"text": answer}
            )

        return None


# ========== 主控 Agent（Supervisor） ==========
class SupervisorAgent:
    """
    主控 Agent（工头）——多 Agent 系统的核心
    职责：
    1. 意图理解（决定把任务分给哪个 Worker）
    2. 任务分发（向对应 Worker 发送消息）
    3. 结果汇总（收集 Worker 的结果，整理后回复用户）
    4. 状态管理（维护共享状态 AgentState）

    对应八股中的 Supervisor-Worker 协调模式
    """

    def _exit_booking_stage(self, user_input: str) -> Optional[str]:
        """选房/入住人收集阶段的逃生口；命中时 shared_state 已更新。"""
        action = booking_exit_action(self.shared_state, user_input.strip())
        if action == "research":
            return self._handle_booking_intent(user_input)
        if action == "quit":
            return quit_booking_message()
        # "new_search" 与未命中都返回 None，由 run() 继续识别意图
        return None

    def __init__(self):
        self.llm = LLMClient()
        self.shared_state = AgentState()

        # 创建 Worker Agent 团队
        self.workers = {
            "search": SearchAgent(self.llm),
            "booking": BookingAgent(self.llm),
            "service": ServiceAgent(self.llm),
            "review": ReviewAgent(self.llm),
        }

        # 待处理的用户输入（用于 booking 阶段的特殊处理）
        self.pending_booking_input: Optional[str] = None

    def run(self, user_input: str) -> str:
        """主入口——处理用户输入，协调各 Worker Agent"""
        self.shared_state.conversation_history.append(f"用户：{user_input}")

        # ===== 特殊阶段处理：预订确认阶段 =====
        if self.shared_state.stage == "booking":
            response = self._handle_booking_stage(user_input)
            if response:
                self.shared_state.conversation_history.append(f"助手：{response}")
                return response

        # ===== 特殊阶段处理：取消确认阶段 =====
        if "确认取消" in user_input and self.shared_state.current_order:
            return self._dispatch_cancel(confirmed=True)
        if "取消操作" in user_input:
            self.shared_state.current_order = None
            self.shared_state.stage = "intent"
            return "好的，已取消操作。"

        # ===== Step 1：意图理解（Supervisor 的核心能力）=====
        intent, extracted = self.llm.extract_intent_and_params(user_input)
        merge_params(self.shared_state, extracted, user_input)

        # 澄清阶段用户只补一句"24号""住三天"时，常被判成闲聊；
        # 只要带了预订参数就继续走预订流程，避免追问后又被踢回帮助菜单
        if intent == "chat" and self.shared_state.stage == "clarify" and any(
                k in extracted for k in CLARIFY_INTENT_KEYS):
            intent = "book"

        # ===== Step 2：根据意图分发任务给对应 Worker =====
        if intent in ("book", "search"):
            return self._handle_booking_intent(user_input)
        elif intent == "review":
            return self._dispatch_review(user_input)
        elif intent == "order_query":
            return self._dispatch_service_query(extracted, user_input)
        elif intent == "cancel":
            return self._dispatch_cancel(extracted=extracted)
        else:
            return self._handle_chat(user_input, extracted)

    def _handle_booking_intent(self, user_input: str) -> str:
        """处理预订/搜索意图——参数澄清或分发搜索任务"""
        params = self.shared_state.params

        # 参数不齐全 → Supervisor 直接追问（不需要 Worker）
        if not params.is_complete():
            self.shared_state.stage = "clarify"
            missing = params.missing_fields()
            response = generate_clarify_question(missing)
            self.shared_state.conversation_history.append(f"助手：{response}")
            return response

        # 参数齐全 → 分发任务给 SearchAgent
        self.shared_state.stage = "search"
        msg = AgentMessage(
            sender="Supervisor", receiver="SearchAgent",
            msg_type="task", content="search_hotels"
        )
        self.workers["search"].receive(msg)

        # 驱动 SearchAgent 执行并获取结果
        result = self.workers["search"].process(self.shared_state)

        if result and result.data.get("count", 0) == 0:
            self.shared_state.stage = "recommend"
            response = (
                f"抱歉，在{params.city}没有找到符合条件的酒店。\n"
                f"你可以尝试：放宽价格上限、降低星级要求、或减少必选设施。"
            )
        elif result:
            self.shared_state.stage = "recommend"
            response = result.data["text"]
        else:
            response = "搜索过程中出现问题，请重试。"

        self.shared_state.conversation_history.append(f"助手：{response}")
        return response

    def _handle_booking_stage(self, user_input: str) -> Optional[str]:
        """
        处理预订阶段的用户输入（房型/入住人/支付确认）
        这是 Supervisor 直接处理的交互逻辑，不需要分发 Worker
        """
        # 支付确认
        if "确认支付" in user_input and self.shared_state.current_order:
            msg = AgentMessage(
                sender="Supervisor", receiver="BookingAgent",
                msg_type="task", content="pay_order"
            )
            self.workers["booking"].receive(msg)
            result = self.workers["booking"].process(self.shared_state)
            if result and result.msg_type == "result":
                reset_booking_state(self.shared_state)
                return result.data["text"]
            return "支付失败，请稍后重试。"

        # 取消订单
        if "取消订单" in user_input and self.shared_state.current_order:
            msg = AgentMessage(
                sender="Supervisor", receiver="BookingAgent",
                msg_type="task", content="cancel_order"
            )
            self.workers["booking"].receive(msg)
            result = self.workers["booking"].process(self.shared_state)
            if result and result.content == "remote_unsupported":
                order_id = result.data.get("order_id", "")
                reset_booking_state(self.shared_state)
                return real_order_cancel_guide(order_id)
            reset_booking_state(self.shared_state)
            return "订单已取消。"

        # 逃生口：重新查询/换酒店/一句话发起新搜索/彻底放弃
        # （必须在房型、姓名解析之前，否则会被跨轮暂存信息困在选房状态）
        exited = self._exit_booking_stage(user_input)
        if exited is not None or self.shared_state.stage != "booking":
            return exited

        # 解析房型和入住人（支持跨轮补全：先给名字后补房型，反之亦然）
        hotel = self.shared_state.selected_hotel
        pending = self.shared_state.pending_booking

        # 多 Agent 链路只下模拟单，但多档报价时要沿用用户选中的那一档价格
        room_type, rate_plan = (
            match_room_choice(hotel, user_input) if hotel else (None, None))

        guest_name = extract_guest_name(
            user_input, waiting_for_name=bool(pending.get("room_type")))

        # 与上一轮暂存的另一半信息合并
        if not guest_name:
            guest_name = pending.get("guest_name")
        if not room_type:
            room_type = pending.get("room_type")
            if room_type and not rate_plan and hotel:
                # 先选“2-2”再补姓名时，按 ratePlanId 恢复所选档位
                plan_id = pending.get("rate_plan_id")
                rate_plan = next(
                    (rp for rp in hotel.rate_plans
                     if plan_id and rp.rate_plan_id == plan_id), None)
                if rate_plan is None:
                    rate_plan = next(
                        (rp for rp in hotel.rate_plans
                         if rp.room_name == room_type), None)

        if room_type and guest_name:
            self.shared_state.pending_booking = {}
            # 分发创建订单任务给 BookingAgent
            msg = AgentMessage(
                sender="Supervisor", receiver="BookingAgent",
                msg_type="task", content="create_order",
                data={
                    "room_type": room_type,
                    "guest_name": guest_name,
                    "price_per_night": (rate_plan.price_per_night
                                        if rate_plan else None),
                    "plan_note": (f"报价：第 {rate_plan.plan_no} 档"
                                  f"（{rate_plan.meal or '餐食未知'}）"
                                  if rate_plan and getattr(rate_plan, "plan_no", 0)
                                  else ""),
                }
            )
            self.workers["booking"].receive(msg)
            result = self.workers["booking"].process(self.shared_state)

            if result and result.msg_type == "result":
                return result.data["text"]
            elif result and result.msg_type == "error":
                if result.content == "invalid_room_type":
                    return f"房型不存在，可选：{'、'.join(result.data['available'])}"
                return "创建订单失败，请重试。"
            return None

        elif room_type and not guest_name:
            self.shared_state.pending_booking = {
                "room_type": room_type,
                "rate_plan_id": rate_plan.rate_plan_id if rate_plan else "",
            }
            return f"{room_type}已记下～入住人写谁？（告诉我姓名就可以下单了）"
        elif not room_type and guest_name and hotel:
            self.shared_state.pending_booking = {"guest_name": guest_name}
            return (f"收到，入住人写{guest_name}。再选个房型吧——"
                    f"直接回复房型序号，或说房型名/床型（如大床）都行："
                    f"{'、'.join(hotel.room_types[:6])}")

        return None

    def _dispatch_review(self, user_input: str) -> str:
        """分发评价查询任务给 ReviewAgent（RAG 检索）"""
        msg = AgentMessage(
            sender="Supervisor", receiver="ReviewAgent",
            msg_type="task", content="query_reviews",
            data={"query": user_input}
        )
        self.workers["review"].receive(msg)
        result = self.workers["review"].process(self.shared_state)

        if result and result.msg_type == "result":
            response = result.data["text"]
            self.shared_state.conversation_history.append(f"助手：{response}")
            return response
        return "暂时没有检索到相关评价信息。"

    def _dispatch_service_query(self, extracted: dict, raw_text: str = "") -> str:
        """
        分发订单查询任务给 ServiceAgent：
        - 带订单号 → 单笔详情
        - 历史清单词/状态筛选 → 历史订单列表
        - 其余 → 当前会话订单（没有则由 ServiceAgent 回退历史列表）
        """
        order_id = extracted.get("order_id", "")
        scope = "detail" if order_id else (
            "list" if wants_order_list(extracted, raw_text) else "current")
        msg = AgentMessage(
            sender="Supervisor", receiver="ServiceAgent",
            msg_type="task", content="query_order",
            data={
                "order_id": order_id,
                "order_status": detect_order_filter(extracted, raw_text),
                "scope": scope,
            }
        )
        self.workers["service"].receive(msg)
        result = self.workers["service"].process(self.shared_state)

        if result:
            if result.content == "order_list":
                response = result.data["text"]
                self.shared_state.conversation_history.append(f"助手：{response}")
                return response
            if result.content == "order_not_found":
                return f"未找到订单号 {result.data.get('order_id', '')}，请确认订单号是否正确。"
            if result.content == "order_found":
                response = result.data["text"]
                self.shared_state.conversation_history.append(f"助手：{response}")
                return response

        return "查询失败，请重试。"

    def _dispatch_cancel(self, extracted: dict = None, confirmed: bool = False) -> str:
        """分发取消订单任务给 ServiceAgent"""
        if confirmed and self.shared_state.current_order:
            order_id = self.shared_state.current_order.order_id
        elif extracted:
            order_id = extracted.get("order_id", "")
        else:
            order_id = ""

        if not order_id and not self.shared_state.current_order:
            return "请提供要取消的订单号。"

        if not order_id:
            order_id = self.shared_state.current_order.order_id

        msg = AgentMessage(
            sender="Supervisor", receiver="ServiceAgent",
            msg_type="task", content="cancel_aftersale",
            data={"order_id": order_id, "confirmed": confirmed}
        )
        self.workers["service"].receive(msg)
        result = self.workers["service"].process(self.shared_state)

        if result:
            if result.content == "order_not_found":
                return f"未找到订单号 {result.data.get('order_id', '')}。"
            if result.content == "already_cancelled":
                return f"订单 {result.data.get('order_id', '')} 已经是取消状态了。"
            if result.content == "remote_unsupported":
                self.shared_state.stage = "intent"
                return real_order_cancel_guide(result.data.get("order_id", ""))
            if result.content == "need_confirmation":
                self.shared_state.stage = "booking"  # 复用 booking 阶段处理确认
                return result.data["text"]
            if result.content == "cancelled":
                self.shared_state.stage = "intent"
                return f"订单 {result.data.get('order_id', '')} 已成功取消，退款将在1-3个工作日内原路返回。"

        return "取消操作失败，请联系客服。"

    def _handle_chat(self, user_input: str, extracted: Optional[dict] = None) -> str:
        """处理闲聊和酒店选择"""
        extracted = extracted or {}
        # "我想住3天"这类改晚数说法：merge_params 已重算离店日期，
        # 直接回预订流程重新搜索，不能落到下面的序号提示或闲聊
        if "_nights" in extracted:
            return self._handle_booking_intent(user_input)
        # 酒店选择（推荐阶段用户回复序号）
        if self.shared_state.stage == "recommend" and self.shared_state.search_results:
            selection = parse_hotel_selection(
                user_input, self.shared_state.search_results, allow_bare_number=True)
            if selection:
                self.shared_state.selected_hotel = selection
                self.shared_state.stage = "booking"
                self.shared_state.pending_booking = {}
                hotel = selection

                p = self.shared_state.params

                # 道旅酒店实时拉房型报价（含每档房间实拍图）；失败回退搜索缓存
                live_detail = get_hotel_detail(
                    hotel.hotel_id, p.check_in, p.check_out)
                if live_detail is not None:
                    hotel = live_detail
                    self.shared_state.selected_hotel = hotel

                response = render_hotel_selection(hotel, p)
                self.shared_state.conversation_history.append(f"助手：{response}")
                return response

            # 序号超出范围：仅"像在选序号"时才提示，带数字的其他说法不算
            if is_selection_attempt(user_input):
                total = len(self.shared_state.search_results)
                return f"请输入 1-{total} 之间的序号，或直接告诉我酒店名称。"

        # 参数澄清阶段：仅当本轮消息确实携带预订信息时才继续预订流程
        if self.shared_state.stage in ("clarify", "intent") and any(
                k in extracted for k in BOOKING_PARAM_KEYS):
            return self._handle_booking_intent(user_input)

        # 真正的闲聊
        response = chat_reply(self.llm, user_input)
        self.shared_state.conversation_history.append(f"助手：{response}")
        return response
