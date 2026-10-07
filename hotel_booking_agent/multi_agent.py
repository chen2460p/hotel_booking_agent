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
from models import Hotel, Order, BookingParams, AgentState
from tools import (
    search_hotels, get_hotel_detail, calculate_total_price,
    create_order, pay_order, cancel_order, get_order_status,
    format_hotel_list, list_history_orders, format_order_rows,
    sync_real_order_status, real_order_cancel_guide,
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
            area_text = f"{params.city}{params.location}，" if params.location else ""
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
                status_map = {"pending": "待支付", "paid": "已支付", "cancelled": "已取消"}
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
                    f"状态：{status_map.get(order.status, order.status)}"
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
                flt_names = {"PENDING": "待支付", "FINISHED": "已完成/已支付",
                             "CANCELLED": "已取消"}
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

    # 模拟房型床型关键词兜底（"大床"→"大床房"），与 agent.py 保持一致
    _BED_KEYWORDS = (
        ("特大床", "特大床"), ("大床", "大床"),
        ("双床", "双床"), ("单人床", "单人床"),
        ("标间", "标准"), ("标房", "标准"),
        ("家庭", "家庭"), ("套房", "套房"),
    )
    # 等待补姓名时，裸回复这些词不应被当作姓名
    _NAME_STOPWORDS = {
        "谢谢", "感谢", "你好", "您好", "再见", "退出", "取消", "不要",
        "算了", "不用", "好的", "知道", "等等", "稍后", "随便",
    }
    # 含这些词的片段绝不是姓名（防止"入住时间改到…"被正则误抓成"时间改到"）
    _NAME_DENY_WORDS = (
        "时间", "日期", "改到", "改成", "推迟", "提前", "查询", "搜索",
        "重新", "酒店", "宾馆", "房型", "入住", "离店", "周末", "预算",
        "价格", "附近", "一下", "看看", "选择", "序号", "名称", "取消",
        "订单", "支付", "帮忙", "帮我", "可以", "能不", "怎么", "什么",
        "多少", "哪里", "哪个", "今天", "明天", "后天", "上午", "下午",
        "晚上", "中午", "早上", "凌晨", "几点", "小时", "星期", "推荐",
        "便宜",
    )
    # 选房阶段的逃生口：明确的重查指令 / 彻底放弃
    _RESEARCH_WORDS = (
        "重新查询", "重新搜索", "重新搜", "重新查", "再搜", "再查", "重搜",
        "重查", "换一家", "换个酒店", "换酒店", "重新选", "重选", "重新开始",
    )
    _QUIT_BOOKING_WORDS = ("不订了", "取消预订", "算了不订", "不买了")

    # 订单状态筛选：用户说法 → 统一状态桶（与 agent.py 保持一致）
    _FILTER_WORDS = {
        "PENDING": ("待支付", "未支付", "未付款", "没付款", "没支付", "待付款"),
        "CANCELLED": ("已取消", "取消过", "退订的", "退款的"),
        "FINISHED": ("已完成", "完成的", "已支付", "已付款", "付过款", "已入住", "住过的"),
    }
    # 明确要"翻历史/看清单"的说法——与"查我当前那笔订单"区分开
    _ORDER_LIST_WORDS = (
        "历史", "所有", "全部", "记录", "列表", "有哪些", "都有", "一共",
        "以前", "之前的", "以往", "过往", "老订单", "订单簿", "清单",
    )

    def _detect_order_filter(self, extracted: dict, raw_text: str) -> str:
        """从提取结果或原文识别订单状态筛选，识别不出默认 ALL。"""
        flt = str(extracted.get("order_status") or "").upper()
        if flt in ("ALL", "PENDING", "FINISHED", "CANCELLED"):
            return flt
        for bucket, words in self._FILTER_WORDS.items():
            if any(w in (raw_text or "") for w in words):
                return bucket
        return "ALL"

    def _wants_order_list(self, extracted: dict, raw_text: str) -> bool:
        """历史清单词或具体状态筛选 → 清单；否则视为查当前那笔订单。"""
        text = raw_text or ""
        if any(w in text for w in self._ORDER_LIST_WORDS):
            return True
        return self._detect_order_filter(extracted, raw_text) != "ALL"

    def _valid_person_name(self, name: str) -> bool:
        """姓名候选校验：2-4 个汉字且不含业务/时间类词语。"""
        if not name or not re.fullmatch(r'[\u4e00-\u9fa5]{2,4}', name):
            return False
        if name in self._NAME_STOPWORDS:
            return False
        return not any(w in name for w in self._NAME_DENY_WORDS)

    def _looks_like_new_search(self, text: str) -> bool:
        """识别"一句话发起新搜索"：提到酒店且带位置/价格/日期/晚数等信号。"""
        if not re.search(r"酒店|宾馆|住宿|旅店", text):
            return False
        # 必须再带一个位置/时间信号，避免把"这家酒店有300以内的房吗"误判为新搜索
        return bool(re.search(
            r"附近|住.{0,4}晚|周末|周[一二三四五六日天]|"
            r"\d{4}\s*[-/.]\s*\d{1,2}|\d{1,2}月\d{1,2}日",
            text))

    def _exit_booking_stage(self, user_input: str) -> Optional[str]:
        """
        选房/入住人收集阶段的逃生口（必须在房型、姓名解析之前调用）：
        - 新搜索条件句 → 清空预订状态并返回 None，由 run() 继续走意图识别
        - "重新查询"等无新条件指令 → 沿用原条件直接重搜
        - "不订了" → 彻底放弃
        """
        text = user_input.strip()
        st = self.shared_state
        if self._looks_like_new_search(text):
            st.selected_hotel = None
            st.pending_booking = {}
            st.params = BookingParams()
            st.stage = "intent"
            return None
        if any(w in text for w in self._RESEARCH_WORDS):
            st.selected_hotel = None
            st.pending_booking = {}
            return self._handle_booking_intent(user_input)
        if any(w in text for w in self._QUIT_BOOKING_WORDS):
            self._reset_booking_state()
            return ("好的，本次预订已取消。下次想订酒店时，直接告诉我城市和入住"
                    "日期就可以～")
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
        self._merge_params(extracted)

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
            result = self.workers["booking"].process(self.shared_state)
            if result and result.content == "remote_unsupported":
                order_id = result.data.get("order_id", "")
                self._reset_booking_state()
                return real_order_cancel_guide(order_id)
            self._reset_booking_state()
            return "订单已取消。"

        # 逃生口：重新查询/换酒店/一句话发起新搜索/彻底放弃
        # （必须在房型、姓名解析之前，否则会被跨轮暂存信息困在选房状态）
        exited = self._exit_booking_stage(user_input)
        if exited is not None or self.shared_state.stage != "booking":
            return exited

        # 解析房型和入住人（支持跨轮补全：先给名字后补房型，反之亦然）
        hotel = self.shared_state.selected_hotel
        pending = self.shared_state.pending_booking

        room_type = None
        guest_name = None

        if hotel:
            # 优先按序号选房（实时房型列表提示"回复序号"，如"1，入住人张三"）
            m_num = re.search(r'\d+', user_input)
            if m_num:
                idx = int(m_num.group())
                if 1 <= idx <= len(hotel.room_types):
                    room_type = hotel.room_types[idx - 1]
            # 再按房型名完整包含，最后按床型关键词兜底（"大床"→"大床房"）
            if not room_type:
                for rt in hotel.room_types:
                    if rt in user_input:
                        room_type = rt
                        break
            if not room_type:
                for keyword, room_key in self._BED_KEYWORDS:
                    if keyword in user_input:
                        room_type = next(
                            (rt for rt in hotel.room_types if room_key in rt), None)
                        if room_type:
                            break

        # 显式说法：入住人张三 / 我叫张三 / 张三入住
        # （"入住"前缀必须带"人"，否则"入住时间改到…"会被误抓成姓名）
        for pat in (r'入住人\s*[:：是叫]?\s*([\u4e00-\u9fa5]{2,4})',
                    r'(?:我叫|名字是|姓名是|名字叫|叫)\s*([\u4e00-\u9fa5]{2,4})',
                    r'([\u4e00-\u9fa5]{2,4})\s*(?:入住|住店|来住)'):
            m = re.search(pat, user_input)
            if m and self._valid_person_name(m.group(1)):
                guest_name = m.group(1)
                break
        # 上一轮已给房型、本轮只回复裸姓名（如"陈老二"）
        if not guest_name and pending.get("room_type"):
            t = user_input.strip().strip("。.!！?？,， ")
            if self._valid_person_name(t):
                guest_name = t

        # 与上一轮暂存的另一半信息合并
        if not guest_name:
            guest_name = pending.get("guest_name")
        if not room_type:
            room_type = pending.get("room_type")

        if room_type and guest_name:
            self.shared_state.pending_booking = {}
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
            self.shared_state.pending_booking = {"room_type": room_type}
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
            "list" if self._wants_order_list(extracted, raw_text) else "current")
        msg = AgentMessage(
            sender="Supervisor", receiver="ServiceAgent",
            msg_type="task", content="query_order",
            data={
                "order_id": order_id,
                "order_status": self._detect_order_filter(extracted, raw_text),
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

    # 这些字段出现，说明用户本轮确实在补充预订信息（而非闲聊）
    _BOOKING_PARAM_KEYS = (
        "city", "check_in", "check_out", "min_star",
        "max_price", "facilities", "keyword", "guest_name",
    )

    def _handle_chat(self, user_input: str, extracted: Optional[dict] = None) -> str:
        """处理闲聊和酒店选择"""
        extracted = extracted or {}
        # 酒店选择（推荐阶段用户回复序号）
        if self.shared_state.stage == "recommend" and self.shared_state.search_results:
            selection = self._parse_hotel_selection(user_input)
            if selection:
                self.shared_state.selected_hotel = selection
                self.shared_state.stage = "booking"
                self.shared_state.pending_booking = {}
                hotel = selection

                p = self.shared_state.params
                nights = self._calc_nights(p.check_in, p.check_out)

                # 道旅酒店实时拉房型报价（含每档房间实拍图）；失败回退搜索缓存
                live_detail = get_hotel_detail(
                    hotel.hotel_id, p.check_in, p.check_out)
                if live_detail is not None:
                    hotel = live_detail
                    self.shared_state.selected_hotel = hotel

                image_line = f"实拍图：{hotel.image_url}\n" if hotel.image_url else ""
                header = (
                    f"好的，你选择了：{hotel.name}\n"
                    f"地址：{hotel.address}\n"
                    f"入住：{p.check_in} → {p.check_out}（{nights}晚）\n"
                    f"{image_line}"
                )

                if hotel.rate_plans:
                    # 实时报价模式：逐房型展示价格/餐食/退改/房间实拍（前8档）
                    max_rooms = 8
                    shown = hotel.rate_plans[:max_rooms]
                    room_lines = [
                        f"可选房型（实时报价，按价格排序，共{len(hotel.rate_plans)}种）："]
                    for i, rp in enumerate(shown, 1):
                        cancel_text = "可免费取消" if rp.cancelable else "不可取消"
                        meal_text = rp.meal or "餐食未知"
                        bed_text = f"，{rp.bed_type}" if rp.bed_type else ""
                        line = (
                            f"{i}. {rp.room_name} — {rp.price_per_night:g}元/晚"
                            f"（{meal_text}，{cancel_text}{bed_text}）"
                        )
                        if rp.image_url:
                            line += f"\n   房间实拍：{rp.image_url}"
                        room_lines.append(line)
                    if len(hotel.rate_plans) > max_rooms:
                        room_lines.append(
                            f"... 另有 {len(hotel.rate_plans) - max_rooms} 种更高价位房型，"
                            f"需要的话告诉我预算或床型帮你筛")
                    if any(rp.image_url for rp in shown):
                        room_lines.append(
                            "（房间实拍链接可在终端中 Ctrl+点击 打开查看）")
                    room_lines.append(
                        "\n请告诉我：选择哪个房型（回复序号或房型名）+ 入住人姓名？"
                        "\n（例如：1，入住人张三）")
                    response = header + "\n" + "\n".join(room_lines)
                else:
                    # 模拟库/高德回退：沿用酒店每晚最低价与房型名
                    total = calculate_total_price(hotel, p.check_in, p.check_out)
                    response = (
                        header
                        + f"房价：{hotel.price_per_night}元/晚 × {nights}晚 = {total}元\n\n"
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

        # 参数澄清阶段：仅当本轮消息确实携带预订信息时才继续预订流程
        if self.shared_state.stage in ("clarify", "intent") and any(
                k in extracted for k in self._BOOKING_PARAM_KEYS):
            return self._handle_booking_intent(user_input)

        # 真正的闲聊
        response = self._chat_reply(user_input)
        self.shared_state.conversation_history.append(f"助手：{response}")
        return response

    def _chat_reply(self, user_input: str) -> str:
        """闲聊回复：谢谢/问候/告别给友好模板，其余交给真实 LLM 或能力介绍。"""
        text = user_input.strip().lower()
        if any(w in user_input for w in ("谢谢", "感谢", "多谢", "辛苦")):
            return "不客气～还有酒店查询、预订或订单相关的需要，随时告诉我。"
        if any(w in user_input for w in ("再见", "拜拜", "拜")) or text in ("bye", "goodbye"):
            return "再见！祝你旅途愉快，需要订酒店时再来找我。"
        if user_input.strip() in ("你好", "您好", "嗨", "你好啊") \
                or text in ("hi", "hello"):
            return (
                "你好！我是酒店预订智能助手，可以帮你搜索预订酒店、查当前订单、"
                "查历史订单、取消订单。\n"
                "想订哪里的酒店？直接告诉我就行。"
            )
        if self.llm.use_real_llm:
            reply = self.llm.generate_response(
                "你是一个酒店预订智能助手，语气亲切简洁。请简短回应用户的寒暄或无关问题"
                "（不超过两句话），并自然引导用户说出订房需求。不要编造酒店信息。",
                user_input)
            if reply and reply != "好的，我明白了。":
                return reply
        return (
            "我主要能帮你：搜索预订酒店（如：帮我订下周末成都 300 元以内的酒店）、"
            "查当前订单（我的订单/订单状态）、查历史订单清单、取消订单。"
            "告诉我你的需求吧。"
        )

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
        if "location" in extracted:
            p.location = extracted["location"]

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
        self.shared_state.pending_booking = {}
        self.shared_state.stage = "intent"
