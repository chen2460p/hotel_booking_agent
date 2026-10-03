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

from typing import Optional, List
from dataclasses import dataclass, field
from models import Hotel, Order, BookingParams, AgentState
from tools import (
    search_hotels, get_hotel_detail, calculate_total_price,
    create_order, pay_order, cancel_order, get_order_status,
    format_hotel_list
)
from llm import LLMClient
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
            nights = self._calc_nights(params.check_in, params.check_out)
            result_text = (
                f"为你找到 {len(results)} 家符合条件的酒店"
                f"（{params.check_in} 入住，{params.check_out} 离店，共{nights}晚）：\n\n"
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

    def _calc_nights(self, check_in: str, check_out: str) -> int:
        from datetime import datetime
        try:
            d1 = datetime.strptime(check_in, "%Y-%m-%d")
            d2 = datetime.strptime(check_out, "%Y-%m-%d")
            return (d2 - d1).days
        except (ValueError, TypeError):
            return 0


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

            if not hotel:
                return AgentMessage(
                    sender=self.name, receiver="Supervisor",
                    msg_type="error", content="no_hotel_selected"
                )

            if room_type not in hotel.room_types:
                return AgentMessage(
                    sender=self.name, receiver="Supervisor",
                    msg_type="error", content="invalid_room_type",
                    data={"available": hotel.room_types}
                )

            # 调用工具创建订单
            order = create_order(hotel, params, room_type, guest_name)
            shared_state.current_order = order

            result_text = (
                f"订单创建成功！\n"
                f"订单号：{order.order_id}\n"
                f"酒店：{order.hotel_name}\n"
                f"房型：{order.room_type}\n"
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
            cancel_order(order.order_id)
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
        if msg.msg_type == "task" and msg.content == "query_order":
            order_id = msg.data.get("order_id", "")
            if not order_id:
                return AgentMessage(
                    sender=self.name, receiver="Supervisor",
                    msg_type="error", content="missing_order_id"
                )

            order = get_order_status(order_id)
            if not order:
                return AgentMessage(
                    sender=self.name, receiver="Supervisor",
                    msg_type="result", content="order_not_found",
                    data={"order_id": order_id}
                )

            status_map = {"pending": "待支付", "paid": "已支付", "cancelled": "已取消"}
            result_text = (
                f"订单信息：\n"
                f"订单号：{order.order_id}\n"
                f"酒店：{order.hotel_name}\n"
                f"入住：{order.check_in} → {order.check_out}\n"
                f"房型：{order.room_type}\n"
                f"入住人：{order.guest_name}\n"
                f"总价：{order.total_price}元\n"
                f"状态：{status_map.get(order.status, order.status)}"
            )
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

            if order.status == "cancelled":
                return AgentMessage(
                    sender=self.name, receiver="Supervisor",
                    msg_type="result", content="already_cancelled",
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
        self._merge_params(extracted)

        # ===== Step 2：根据意图分发任务给对应 Worker =====
        if intent in ("book", "search"):
            return self._handle_booking_intent(user_input)
        elif intent == "review":
            return self._dispatch_review(user_input)
        elif intent == "order_query":
            return self._dispatch_service_query(extracted)
        elif intent == "cancel":
            return self._dispatch_cancel(extracted=extracted)
        else:
            return self._handle_chat(user_input)

    def _handle_booking_intent(self, user_input: str) -> str:
        """处理预订/搜索意图——参数澄清或分发搜索任务"""
        params = self.shared_state.params

        # 参数不齐全 → Supervisor 直接追问（不需要 Worker）
        if not params.is_complete():
            self.shared_state.stage = "clarify"
            missing = params.missing_fields()
            response = self._generate_clarify_question(missing)
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
                self._reset_booking_state()
                return result.data["text"]
            return "支付失败，请稍后重试。"

        # 取消订单
        if "取消订单" in user_input and self.shared_state.current_order:
            msg = AgentMessage(
                sender="Supervisor", receiver="BookingAgent",
                msg_type="task", content="cancel_order"
            )
            self.workers["booking"].receive(msg)
            self.workers["booking"].process(self.shared_state)
            self._reset_booking_state()
            return "订单已取消。"

        # 解析房型和入住人
        import re
        room_type = None
        guest_name = None

        if self.shared_state.selected_hotel:
            for rt in self.shared_state.selected_hotel.room_types:
                if rt in user_input:
                    room_type = rt
                    break

        name_match = re.search(r'入住人?\s*[:：是]?\s*([\u4e00-\u9fa5]{2,4})', user_input)
        if name_match:
            guest_name = name_match.group(1)

        if room_type and guest_name:
            # 分发创建订单任务给 BookingAgent
            msg = AgentMessage(
                sender="Supervisor", receiver="BookingAgent",
                msg_type="task", content="create_order",
                data={"room_type": room_type, "guest_name": guest_name}
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
            return f"好的，{room_type}。请告诉我入住人姓名。"
        elif not room_type and guest_name and self.shared_state.selected_hotel:
            return f"好的，入住人{guest_name}。请选择房型：{'、'.join(self.shared_state.selected_hotel.room_types)}"

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

    def _dispatch_service_query(self, extracted: dict) -> str:
        """分发订单查询任务给 ServiceAgent"""
        order_id = extracted.get("order_id", "")
        msg = AgentMessage(
            sender="Supervisor", receiver="ServiceAgent",
            msg_type="task", content="query_order",
            data={"order_id": order_id}
        )
        self.workers["service"].receive(msg)
        result = self.workers["service"].process(self.shared_state)

        if result:
            if result.content == "missing_order_id":
                return "请提供订单号，我帮你查询订单状态。订单号格式如 ORDXXXXXXXX"
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
            if result.content == "need_confirmation":
                self.shared_state.stage = "booking"  # 复用 booking 阶段处理确认
                return result.data["text"]
            if result.content == "cancelled":
                self.shared_state.stage = "intent"
                return f"订单 {result.data.get('order_id', '')} 已成功取消，退款将在1-3个工作日内原路返回。"

        return "取消操作失败，请联系客服。"

    def _handle_chat(self, user_input: str) -> str:
        """处理闲聊和酒店选择"""
        # 酒店选择（推荐阶段用户回复序号）
        if self.shared_state.stage == "recommend" and self.shared_state.search_results:
            selection = self._parse_hotel_selection(user_input)
            if selection:
                self.shared_state.selected_hotel = selection
                self.shared_state.stage = "booking"
                hotel = selection
                nights = self._calc_nights(
                    self.shared_state.params.check_in,
                    self.shared_state.params.check_out
                )
                total = calculate_total_price(
                    hotel, self.shared_state.params.check_in,
                    self.shared_state.params.check_out
                )
                response = (
                    f"好的，你选择了：{hotel.name}\n"
                    f"地址：{hotel.address}\n"
                    f"入住：{self.shared_state.params.check_in} → {self.shared_state.params.check_out}（{nights}晚）\n"
                    f"房价：{hotel.price_per_night}元/晚 × {nights}晚 = {total}元\n\n"
                    f"可选房型：{'、'.join(hotel.room_types)}\n\n"
                    f"请告诉我：\n1. 选择哪个房型？\n2. 入住人姓名？\n"
                    f"（例如：海景大床房，入住人张三）"
                )
                self.shared_state.conversation_history.append(f"助手：{response}")
                return response

            # 序号超出范围
            import re
            if re.search(r'\d+', user_input):
                total = len(self.shared_state.search_results)
                return f"请输入 1-{total} 之间的序号，或直接告诉我酒店名称。"

        # 预订流程中补充信息
        if self.shared_state.stage in ("clarify", "intent"):
            return self._handle_booking_intent(user_input)

        # 默认回复
        response = (
            "你好！我是酒店预订助手，可以帮你：\n"
            "1. 搜索和预订酒店（告诉我城市、日期、预算等）\n"
            "2. 查询订单状态（提供订单号）\n"
            "3. 取消订单（提供订单号）\n\n"
            "请问有什么可以帮你的？"
        )
        self.shared_state.conversation_history.append(f"助手：{response}")
        return response

    def _parse_hotel_selection(self, user_input: str) -> Optional[Hotel]:
        """解析用户的酒店选择"""
        import re
        num_match = re.search(r'(\d+)', user_input)
        if num_match:
            idx = int(num_match.group(1)) - 1
            if 0 <= idx < len(self.shared_state.search_results):
                return self.shared_state.search_results[idx]
        for hotel in self.shared_state.search_results:
            if hotel.name in user_input or hotel.hotel_id in user_input:
                return hotel
        return None

    def _merge_params(self, extracted: dict):
        """合并提取的参数到共享状态"""
        p = self.shared_state.params
        if "city" in extracted:
            p.city = extracted["city"]
        if "check_in" in extracted:
            p.check_in = extracted["check_in"]
        if "check_out" in extracted:
            p.check_out = extracted["check_out"]
        if "min_star" in extracted:
            p.min_star = extracted["min_star"]
        if "max_price" in extracted:
            p.max_price = extracted["max_price"]
        if "facilities" in extracted:
            existing = set(p.facilities)
            existing.update(extracted["facilities"])
            p.facilities = list(existing)
        if "keyword" in extracted:
            p.keyword = extracted["keyword"]

    def _generate_clarify_question(self, missing: list) -> str:
        """生成参数追问"""
        if len(missing) == 1:
            templates = {
                "城市": "请问你想去哪个城市？",
                "入住日期": "请问你计划哪天入住？（如：下周末、2026-10-15）",
                "离店日期": "请问你计划哪天离店？或住几晚？",
            }
            return templates.get(missing[0], f"请问{missing[0]}是什么？")
        fields_str = "、".join(missing)
        return f"好的，我还需要确认一些信息：{fields_str}。请告诉我这些信息，我帮你搜索。"

    def _calc_nights(self, check_in: str, check_out: str) -> int:
        from datetime import datetime
        try:
            d1 = datetime.strptime(check_in, "%Y-%m-%d")
            d2 = datetime.strptime(check_out, "%Y-%m-%d")
            return (d2 - d1).days
        except (ValueError, TypeError):
            return 0

    def _reset_booking_state(self):
        """重置预订状态"""
        self.shared_state.params = BookingParams()
        self.shared_state.search_results = []
        self.shared_state.selected_hotel = None
        self.shared_state.current_order = None
        self.shared_state.stage = "intent"
