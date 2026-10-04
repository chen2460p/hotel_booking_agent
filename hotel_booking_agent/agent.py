# Agent 核心逻辑
# 实现 ReAct 循环 + Workflow 混合架构
#
# 架构设计（对应八股中的核心考点）：
# - Agent 负责：意图理解、参数提取、多轮澄清、推荐解释、售后路由
# - Workflow 负责：搜索筛选、价格计算、订单创建、支付（确定性流程）
# - 记忆：AgentState 保存工作记忆（当前参数、搜索结果、选中酒店等）
# - 工具：通过 tools.py 调用，每个工具做一件事

import re
from typing import Optional
from models import AgentState, BookingParams, Hotel, Order
from tools import (
    search_hotels, get_hotel_detail, calculate_total_price,
    create_order, create_real_order, pay_order, cancel_order,
    get_order_status, format_hotel_list,
    list_history_orders, format_order_rows,
)
from llm import LLMClient
import rollinggo_book as rlg

LOGIN_HINT = (
    "💡 提示：当前这是一笔【模拟订单】。如需在道旅创建真实订单，"
    "请先在项目目录运行：python rollinggo_book.py login\n"
    "完成浏览器授权后，重新选择房型即可走真实下单链路（验价 → 确认 → 支付链接）。"
)


class HotelBookingAgent:
    """酒店预订 Agent——核心类"""

    def __init__(self):
        """初始化 Agent：加载 LLM 客户端和初始状态"""
        self.llm = LLMClient()
        self.state = AgentState()
        # 工具注册表——Agent 可调用的所有工具（对应八股中的 Tool 设计）
        self.tools = {
            "search_hotels": search_hotels,
            "get_hotel_detail": get_hotel_detail,
            "create_order": create_order,
            "pay_order": pay_order,
            "cancel_order": cancel_order,
            "get_order_status": get_order_status,
        }

    def run(self, user_input: str) -> str:
        """
        Agent 主入口——处理用户输入并返回回复
        这是 ReAct 循环的外层：感知用户输入 → 推理决策 → 执行行动 → 返回观察
        """
        # 记录对话历史（短期记忆）
        self.state.conversation_history.append(f"用户：{user_input}")

        # 真实订单的支付只能由用户打开支付链接完成，任何时候都不能本地标记已支付
        if ("支付" in user_input and self.state.current_order
                and self.state.current_order.source == "rollinggo"):
            order = self.state.current_order
            tail = f"\n支付链接：{order.payment_url}" if order.payment_url else \
                "\n（未保存支付链接，请回复【查询订单】获取最新状态）"
            response = (
                f"订单 {order.order_id} 是道旅真实订单，我无法代为支付，"
                f"请你亲自打开支付页面完成付款：{tail}"
            )
            self.state.conversation_history.append(f"助手：{response}")
            return response

        # ===== Step 1: 意图理解 + 参数提取（Agent 的"思考"）=====
        intent, extracted = self.llm.extract_intent_and_params(user_input)

        # 道旅真实订单号通常是一长串字母数字，LLM/规则没提取到时兜底抓一次
        if intent in ("order_query", "cancel") and not extracted.get("order_id"):
            m = re.search(r'(?<![A-Za-z0-9])([A-Z0-9]{8,})(?![A-Za-z0-9])', user_input.upper())
            if m:
                extracted["order_id"] = m.group(1)

        # 将提取到的参数合并到当前状态（支持多轮累积补充）
        self._merge_params(extracted)

        # ===== Step 2: 根据意图路由到不同处理流程 =====
        if intent == "book" or intent == "search":
            return self._handle_booking_flow(user_input)
        elif intent == "order_query":
            return self._handle_order_query(extracted, user_input)
        elif intent == "cancel":
            return self._handle_cancel(extracted)
        else:
            return self._handle_chat(user_input, extracted)

    def _merge_params(self, extracted: dict):
        """
        将新提取的参数合并到现有参数中
        支持多轮对话：用户第一轮说"去三亚"，第二轮说"下周末"，参数逐步补全
        """
        p = self.state.params
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
            # 设施合并去重
            existing = set(p.facilities)
            existing.update(extracted["facilities"])
            p.facilities = list(existing)
        if "keyword" in extracted:
            p.keyword = extracted["keyword"]
        if "location" in extracted:
            p.location = extracted["location"]

    def _handle_booking_flow(self, user_input: str) -> str:
        """
        处理预订/搜索流程
        这是 Agent + Workflow 混合的核心：
        - 参数不齐全 → Agent 追问（Agent 能力）
        - 参数齐全 → 走 Workflow 搜索（确定性流程）
        - 搜索后 → Agent 推荐解释（Agent 能力）
        - 用户选定 → 走 Workflow 下单（确定性流程）
        """
        params = self.state.params

        # ===== 阶段1：参数澄清（Agent 多轮对话能力）=====
        if not params.is_complete():
            self.state.stage = "clarify"
            missing = params.missing_fields()
            response = self._generate_clarify_question(missing)
            self.state.conversation_history.append(f"助手：{response}")
            return response

        # ===== 阶段2：搜索（Workflow 确定性流程）=====
        # 参数齐全后，不需要 LLM 参与，直接调用搜索工具
        self.state.stage = "search"
        results = search_hotels(params)
        self.state.search_results = results

        # ===== 阶段3：推荐（Agent 解释能力）=====
        self.state.stage = "recommend"
        if not results:
            response = (
                f"抱歉，在{params.city}没有找到符合条件的酒店。\n"
                f"你可以尝试：放宽价格上限、降低星级要求、或减少必选设施。\n"
                f"需要我帮你调整条件重新搜索吗？"
            )
        else:
            # 计算入住晚数用于展示
            nights = self._calc_nights(params.check_in, params.check_out)
            area_text = f"{params.city}{params.location}，" if params.location else ""
            response = (
                f"为你找到 {len(results)} 家符合条件的酒店"
                f"（{area_text}{params.check_in} 入住，{params.check_out} 离店，共{nights}晚）：\n\n"
                f"{format_hotel_list(results)}\n\n"
                f"请告诉我你想选择哪一家（回复序号或酒店名），"
                f"或告诉我更多偏好我帮你进一步筛选。"
            )

        self.state.conversation_history.append(f"助手：{response}")
        return response

    def _generate_clarify_question(self, missing: list) -> str:
        """
        生成参数追问问题
        真实场景中由 LLM 生成自然语言追问，这里用模板（模拟模式）
        """
        if len(missing) == 1:
            field = missing[0]
            templates = {
                "城市": "请问你想去哪个城市？",
                "入住日期": "请问你计划哪天入住？（如：下周末、2026-10-15）",
                "离店日期": "请问你计划哪天离店？或住几晚？",
            }
            return templates.get(field, f"请问{field}是什么？")
        else:
            fields_str = "、".join(missing)
            return f"好的，我还需要确认一些信息：{fields_str}。请告诉我这些信息，我帮你搜索。"

    def _calc_nights(self, check_in: str, check_out: str) -> int:
        """计算入住晚数（Workflow 中的确定性计算）"""
        from datetime import datetime
        try:
            d1 = datetime.strptime(check_in, "%Y-%m-%d")
            d2 = datetime.strptime(check_out, "%Y-%m-%d")
            return (d2 - d1).days
        except ValueError:
            return 0

    # 订单状态筛选：用户说法 → 统一状态桶
    _FILTER_WORDS = {
        "PENDING": ("待支付", "未支付", "未付款", "没付款", "没支付", "待付款"),
        "CANCELLED": ("已取消", "取消过", "退订的", "退款的"),
        "FINISHED": ("已完成", "完成的", "已支付", "已付款", "付过款", "已入住", "住过的"),
    }
    _FILTER_NAMES = {
        "PENDING": "待支付", "FINISHED": "已完成/已支付", "CANCELLED": "已取消",
    }

    def _detect_order_filter(self, extracted: dict, raw_text: str) -> str:
        """从 LLM 提取结果或原文识别订单状态筛选，识别不出默认 ALL。"""
        flt = str(extracted.get("order_status") or "").upper()
        if flt in ("ALL", "PENDING", "FINISHED", "CANCELLED"):
            return flt
        text = raw_text or ""
        for bucket, words in self._FILTER_WORDS.items():
            if any(w in text for w in words):
                return bucket
        return "ALL"

    def _handle_order_query(self, extracted: dict, raw_text: str = "") -> str:
        """处理订单查询（售后场景）：优先本地库，道旅账号已登录时联动远程真实订单"""
        order_id = extracted.get("order_id")

        # 无订单号：展示历史订单列表（道旅远程 + 本地补充，可按状态筛选）
        if not order_id:
            response = self._handle_order_list(extracted, raw_text)
            self.state.conversation_history.append(f"助手：{response}")
            return response

        order = get_order_status(order_id)

        # 本地没有该订单：尝试道旅远程详情
        if not order and rlg.is_logged_in():
            try:
                detail = rlg.get_order_detail(order_id)
                rows = rlg.parse_orders(detail)
                if rows:
                    response = "道旅订单信息：\n" + format_order_rows(rows)
                    self.state.conversation_history.append(f"助手：{response}")
                    return response
                return f"未获取到订单 {order_id} 的详情。"
            except (rlg.RollingGoAuthError, rlg.RollingGoApiError) as e:
                return f"未找到订单号 {order_id}（道旅查询：{e}）"

        if not order:
            return f"未找到订单号 {order_id}，请确认订单号是否正确。"

        status_map = {
            "pending": "待支付",
            "paid": "已支付",
            "cancelled": "已取消",
        }

        # 道旅真实订单：尽量用远程最新状态刷新
        status_text = status_map.get(order.status, order.status)
        if order.source == "rollinggo" and rlg.is_logged_in():
            try:
                rows = rlg.parse_orders(rlg.get_order_detail(order_id))
                if rows and rows[0].get("status_text"):
                    status_text = rows[0]["status_text"]
            except (rlg.RollingGoAuthError, rlg.RollingGoApiError):
                pass

        response = (
            f"{'【道旅真实】' if order.source == 'rollinggo' else ''}订单信息：\n"
            f"订单号：{order.order_id}\n"
            f"酒店：{order.hotel_name}\n"
            f"入住：{order.check_in} → {order.check_out}\n"
            f"房型：{order.room_type}\n"
            f"入住人：{order.guest_name}\n"
            f"总价：{order.total_price:g}元\n"
            f"状态：{status_text}"
        )
        if order.payment_url and order.status == "pending":
            response += (
                f"\n\n该订单尚未支付，请尽快通过下面的链接完成支付（未支付不会扣款，"
                f"超时未支付订单将自动取消）：\n{order.payment_url}"
            )
        self.state.conversation_history.append(f"助手：{response}")
        return response

    def _handle_order_list(self, extracted: dict, raw_text: str) -> str:
        """无订单号时的历史订单列表：道旅账号订单为主，本地订单补充，支持状态筛选。"""
        flt = self._detect_order_filter(extracted, raw_text)
        result = list_history_orders(flt)
        rows = result["rows"]
        flt_name = self._FILTER_NAMES.get(flt, "")
        scope = "道旅账号" if result["logged_in"] else "本次会话"
        title = f"你的{scope}订单" + (f"（{flt_name}）" if flt_name else "") + "："

        blocks = [title]
        if result.get("remote_error"):
            blocks.append(f"⚠️ 道旅订单查询失败：{result['remote_error']}")

        if rows:
            blocks.append(format_order_rows(rows))
            tail = "\n回复订单号可查询单笔详情。"
            if flt != "ALL":
                tail = "\n回复【查全部订单】可查看所有状态的订单。" + tail
            blocks.append(tail)
            return "\n".join(blocks)

        # 空态
        if not result["logged_in"]:
            blocks.append("目前没有订单。")
            blocks.append(
                "💡 当前为未登录状态，只能看到本次运行期间创建的模拟订单；\n"
                "登录道旅后可查询账号下的全部历史订单（真实订单，跨会话保留）：\n"
                "python rollinggo_book.py login"
            )
        elif result.get("remote_error"):
            blocks.append("本次会话本地也没有可展示的订单。")
        else:
            blocks.append(
                f"没有{flt_name}订单。" if flt_name else "账号下暂无订单。"
            )
            if flt != "ALL":
                blocks.append("回复【查全部订单】可查看所有状态的订单。")
        return "\n".join(blocks)

    def _handle_cancel(self, extracted: dict) -> str:
        """
        处理取消订单（售后场景 + 高危操作）
        对应八股中的"高危操作四层防呆"——这里实现确认层
        """
        order_id = extracted.get("order_id")
        if not order_id:
            return "请提供要取消的订单号。"

        order = get_order_status(order_id)
        if not order:
            return f"未找到订单号 {order_id}。"

        if order.status == "cancelled":
            return f"订单 {order_id} 已经是取消状态了。"

        # 高危操作：需要用户二次确认（确认层防呆）
        if self.state.current_order and self.state.current_order.order_id == order_id:
            # 用户已经确认过，执行取消
            success = cancel_order(order_id)
            if success:
                self.state.current_order = None
                return f"订单 {order_id} 已成功取消，退款将在1-3个工作日内原路返回。"
            else:
                return f"取消订单 {order_id} 失败，请联系客服。"
        else:
            # 第一次请求，要求确认
            self.state.current_order = order
            return (
                f"确认要取消以下订单吗？\n"
                f"订单号：{order.order_id}\n"
                f"酒店：{order.hotel_name}\n"
                f"入住：{order.check_in} → {order.check_out}\n"
                f"总价：{order.total_price}元\n\n"
                f"回复【确认取消】即可取消，或回复【取消操作】放弃。"
            )

    # 这些字段出现在提取结果中，说明用户本轮确实在补充预订信息（而非闲聊）
    _BOOKING_PARAM_KEYS = (
        "city", "check_in", "check_out", "min_star",
        "max_price", "facilities", "keyword", "guest_name",
    )

    def _handle_chat(self, user_input: str, extracted: Optional[dict] = None) -> str:
        """处理闲聊和其他问题"""
        extracted = extracted or {}
        # 检查是否是在回复酒店选择（用户回复序号选酒店）
        if self.state.stage == "recommend" and self.state.search_results:
            selection = self._parse_hotel_selection(user_input)
            if selection:
                return self._handle_hotel_selection(selection)
            # 用户输入了数字但超出范围，给出提示
            import re
            if re.search(r'\d+', user_input):
                total = len(self.state.search_results)
                return f"请输入 1-{total} 之间的序号，或直接告诉我酒店名称。当前共有 {total} 家酒店可选。"

        # 检查是否是确认取消
        if "确认取消" in user_input and self.state.current_order:
            return self._handle_cancel({"order_id": self.state.current_order.order_id})
        if "取消操作" in user_input:
            self.state.current_order = None
            return "好的，已取消操作。"

        # 参数澄清阶段：仅当本轮消息确实携带预订信息时才继续预订流程，
        # "你好/谢谢"这类纯闲聊不应被当成对追问的回答
        if self.state.stage in ("clarify", "intent") and any(
                k in extracted for k in self._BOOKING_PARAM_KEYS):
            return self._handle_booking_flow(user_input)

        # 真正的闲聊
        response = self._chat_reply(user_input)
        self.state.conversation_history.append(f"助手：{response}")
        return response

    def _chat_reply(self, user_input: str) -> str:
        """生成闲聊回复：真实 LLM 模式自然作答，模拟模式按场景给模板。"""
        text = user_input.strip().lower()
        if any(w in user_input for w in ("谢谢", "感谢", "多谢", "辛苦")):
            return "不客气～还有酒店查询、预订或订单相关的需要，随时告诉我。"
        if any(w in user_input for w in ("再见", "拜拜", "拜")) or text in ("bye", "goodbye"):
            return "再见！祝你旅途愉快，需要订酒店时再来找我。"
        if user_input.strip() in ("你好", "您好", "嗨", "hi", "hello", "你好啊") \
                or text in ("hi", "hello"):
            return (
                "你好！我是酒店预订智能助手，可以帮你：\n"
                "1. 搜索和预订酒店（告诉我城市、日期、预算等）\n"
                "2. 查询历史订单（如「查我的历史订单」「查待支付的订单」）\n"
                "3. 取消订单\n\n"
                "想订哪里的酒店？直接告诉我就行。"
            )

        # 其他闲聊：有真实 LLM 时自然回答，没有时回到能力介绍
        if self.llm.use_real_llm:
            context = (
                "你是一个酒店预订智能助手，具备酒店搜索预订、历史订单查询、取消订单能力，"
                "语气亲切简洁。请简短回应用户的寒暄或无关问题（不超过两句话），"
                "并自然引导用户说出订房需求（城市、日期、预算）。不要编造酒店信息。"
            )
            reply = self.llm.generate_response(context, user_input)
            if reply and reply != "好的，我明白了。":
                return reply

        return (
            "我主要能帮你处理这些事：\n"
            "1. 搜索和预订酒店（例如：帮我订下周末成都 300 元以内的酒店）\n"
            "2. 查询历史订单（例如：查我的历史订单 / 查待支付的订单）\n"
            "3. 取消订单\n\n"
            "告诉我你的需求吧。"
        )

    def _parse_hotel_selection(self, user_input: str) -> Optional[Hotel]:
        """
        解析用户的酒店选择（回复序号或酒店名）
        这是 Agent 的"理解"能力——从自然语言中解析用户选择
        """
        import re
        # 尝试解析序号
        num_match = re.search(r'(\d+)', user_input)
        if num_match:
            idx = int(num_match.group(1)) - 1
            if 0 <= idx < len(self.state.search_results):
                return self.state.search_results[idx]

        # 尝试匹配酒店名
        for hotel in self.state.search_results:
            if hotel.name in user_input or hotel.hotel_id in user_input:
                return hotel

        return None

    def _handle_hotel_selection(self, hotel: Hotel) -> str:
        """
        处理用户选中酒店后的流程——进入预订 Workflow
        这是从 Agent 推荐阶段切换到 Workflow 下单阶段的关键节点。
        道旅酒店会实时拉取房型报价与退改政策（失败则回退搜索缓存）。
        """
        self.state.selected_hotel = hotel
        self.state.stage = "booking"
        # 新选了酒店，上一家酒店的选房暂存不再适用
        self.state.pending_booking = {}

        p = self.state.params
        # 实时详情：各房型的含早/退改报价
        live_detail = get_hotel_detail(
            hotel.hotel_id, p.check_in, p.check_out)
        if live_detail is not None:
            hotel = live_detail
            self.state.selected_hotel = hotel

        nights = self._calc_nights(p.check_in, p.check_out)

        header = (
            f"好的，你选择了：{hotel.name}\n"
            f"地址：{hotel.address}\n"
            f"入住：{p.check_in} → {p.check_out}（{nights}晚）\n"
        )

        if hotel.rate_plans:
            # 实时报价模式：逐房型展示价格、餐食、退改（过多时只展示前8档）
            max_rooms = 8
            room_lines = [f"可选房型（实时报价，按价格排序，共{len(hotel.rate_plans)}种）："]
            for i, rp in enumerate(hotel.rate_plans[:max_rooms], 1):
                cancel_text = "可免费取消" if rp.cancelable else "不可取消"
                meal_text = rp.meal or "餐食未知"
                bed_text = f"，{rp.bed_type}" if rp.bed_type else ""
                room_lines.append(
                    f"{i}. {rp.room_name} — {rp.price_per_night:g}元/晚"
                    f"（{meal_text}，{cancel_text}{bed_text}）"
                )
            if len(hotel.rate_plans) > max_rooms:
                room_lines.append(
                    f"... 另有 {len(hotel.rate_plans) - max_rooms} 种更高价位房型，"
                    f"需要的话告诉我预算或床型帮你筛")
            room_lines.append(
                "\n请告诉我：选择哪个房型（回复序号或房型名）+ 入住人姓名？"
                "\n（例如：1，入住人张三）")
            if rlg.is_logged_in():
                room_lines.append(
                    "🔓 已登录道旅：回复房型+入住人后我会先【验价锁价】，"
                    "向你展示订单信息并经你【确认下单】后，才会创建真实订单。")
            response = header + "\n" + "\n".join(room_lines)
        else:
            # 模拟库/高德回退：沿用酒店的每晚最低价
            total = calculate_total_price(hotel, p.check_in, p.check_out)
            response = (
                header
                + f"房价：{hotel.price_per_night}元/晚 × {nights}晚 = {total}元\n\n"
                f"可选房型：{'、'.join(hotel.room_types)}\n\n"
                f"请告诉我：\n1. 选择哪个房型？\n2. 入住人姓名？\n"
                f"（例如：海景大床房，入住人张三）"
            )

        self.state.conversation_history.append(f"助手：{response}")
        return response

    def _match_rate_plan(self, user_input: str):
        """
        从用户输入匹配房型报价：优先序号，其次房型名包含，
        最后按床型关键词（大床/双床/特大床等）兜底。
        返回匹配到的 RoomRatePlan，未匹配返回 None。
        """
        hotel = self.state.selected_hotel
        plans = hotel.rate_plans
        if not plans:
            return None

        import re
        # 1) 序号（仅取句首独立数字，避免误匹配姓名中的数字）
        m = re.search(r'(?:^|[第#号\s])(\d+)\s*[号#\.、，,]?', user_input)
        if m:
            idx = int(m.group(1)) - 1
            if 0 <= idx < len(plans):
                return plans[idx]

        # 2) 房型名直接包含
        for rp in plans:
            if rp.room_name in user_input:
                return rp

        # 3) 床型关键词兜底（用户常只说"大床房/双床房"）
        bed_aliases = {
            "特大床": ("特大床",), "大床": ("大床",),
            "双床": ("双床", "单人床"), "单人床": ("单人床",),
            "家庭": ("家庭",), "套房": ("套房",),
        }
        for alias, keys in bed_aliases.items():
            if alias in user_input:
                for rp in plans:
                    target = rp.room_name + " " + rp.bed_type
                    if any(k in target for k in keys):
                        return rp
        return None

    # 模拟库房型的床型关键词兜底（与 _match_rate_plan 的床型兜底保持一致）
    _MOCK_BED_KEYWORDS = (
        ("特大床", "特大床"), ("大床", "大床"),
        ("双床", "双床"), ("单人床", "单人床"),
        ("标间", "标准"), ("标房", "标准"),
        ("家庭", "家庭"), ("套房", "套房"),
    )

    def _match_mock_room_type(self, user_input: str) -> Optional[str]:
        """匹配模拟库房型：先房型名完整包含，再按床型关键词兜底（"大床"→"大床房"）。"""
        room_types = self.state.selected_hotel.room_types or []
        for rt in room_types:                       # 完整房型名优先
            if rt in user_input:
                return rt
        for keyword, room_key in self._MOCK_BED_KEYWORDS:
            if keyword in user_input:
                for rt in room_types:
                    if room_key in rt:
                        return rt
        return None

    # 等待用户补姓名时，裸回复这些词不应被当成姓名
    _NAME_STOPWORDS = {
        "谢谢", "感谢", "你好", "您好", "再见", "退出", "取消", "不要",
        "算了", "不用", "好的", "知道", "等等", "稍后", "随便",
    }

    def _extract_guest_name(self, user_input: str,
                            waiting_for_name: bool = False) -> Optional[str]:
        """
        从消息提取入住人姓名：
        1) "入住人张三 / 姓名：张三 / 我叫张三 / 张三入住" 等显式说法
        2) waiting_for_name=True 时，接受裸姓名（如只回复"陈老二"）
        """
        text = user_input.strip()
        patterns = (
            r'入住人?\s*[:：是叫]?\s*([\u4e00-\u9fa5]{2,4})',
            r'(?:我叫|名字是|姓名是|名字叫|叫)\s*([\u4e00-\u9fa5]{2,4})',
            r'([\u4e00-\u9fa5]{2,4})\s*(?:入住|住店|来住)',
        )
        for pat in patterns:
            m = re.search(pat, text)
            if m:
                return m.group(1)
        if waiting_for_name:
            t = text.strip("。.!！?？,， ")
            if re.fullmatch(r'[\u4e00-\u9fa5]{2,4}', t) and t not in self._NAME_STOPWORDS:
                return t
            if re.fullmatch(r"[A-Za-z][A-Za-z .'\-]{1,30}", t):
                return t
        return None

    def confirm_booking(self, room_type: str, guest_name: str,
                        rate_plan=None) -> str:
        """
        确认预订——调用 Workflow 创建订单
        对应八股中的"高危操作"：创建订单需要确认，支付需要二次验证
        rate_plan：道旅实时房型报价（含每晚安客价、餐食、退改政策）
        """
        if not self.state.selected_hotel:
            return "请先选择酒店。"

        hotel = self.state.selected_hotel
        # 校验房型是否存在
        if room_type not in hotel.room_types:
            return f"{hotel.name}没有【{room_type}】这个房型，可选：{'、'.join(hotel.room_types)}"

        # 调用工具创建订单（实时报价用该房型每晚价，否则用酒店最低价）
        price = rate_plan.price_per_night if rate_plan else None
        order = create_order(hotel, self.state.params, room_type,
                             guest_name, price_per_night=price)
        self.state.current_order = order

        policy_lines = ""
        if rate_plan:
            cancel_text = (rate_plan.cancel_policy if rate_plan.cancelable
                           else "该报价不可免费取消")
            policy_lines = f"餐食：{rate_plan.meal or '未知'}\n退改：{cancel_text}\n"

        response = (
            f"订单创建成功！\n"
            f"订单号：{order.order_id}\n"
            f"酒店：{order.hotel_name}\n"
            f"房型：{order.room_type}\n"
            f"{policy_lines}"
            f"入住人：{order.guest_name}\n"
            f"入住：{order.check_in} → {order.check_out}\n"
            f"总价：{order.total_price:g}元\n"
            f"状态：待支付\n\n"
            f"回复【确认支付】即可完成支付，或回复【取消订单】放弃预订。"
        )
        if hotel.booking_url:
            response += f"\n（真实预订链接：{hotel.booking_url}）"
        self.state.conversation_history.append(f"助手：{response}")
        return response

    # ---------- 道旅真实下单链路 ----------

    def _do_real_price_confirm(self, draft: dict) -> str:
        """
        真实下单第 1 步：调用道旅验价接口锁定实时价格，拿到 referenceNo。
        成功后进入 booking_confirm 阶段，等待用户【确认下单】（高危操作二次确认）。
        联系邮箱优先用用户同条消息提供的，其次用道旅账号 recentGuests 中的邮箱
        （会在确认卡片中明示，由用户确认），都没有则追问。
        """
        hotel = self.state.selected_hotel
        rate_plan = draft["rate_plan_obj"]
        guest_name = draft["guest_name"]
        p = self.state.params

        if not hotel or not rate_plan or not rate_plan.rate_plan_id:
            self.state.booking_context = {}
            return "预订信息不完整，请重新选择房型。"

        try:
            hotel_id_num = int(str(hotel.hotel_id).replace("RLG_", ""))
        except ValueError:
            self.state.booking_context = {}
            return f"酒店ID格式异常（{hotel.hotel_id}），无法发起真实验价，请重新搜索选择。"

        try:
            price_info = rlg.price_confirm(
                hotel_id_num, rate_plan.rate_plan_id,
                p.check_in, p.check_out,
                num_of_rooms=1, adult_count=2,
            )
        except rlg.RollingGoAuthError as e:
            return f"验价失败：{e}"
        except rlg.RollingGoApiError as e:
            self.state.booking_context = {}
            return (
                f"验价失败：{e}\n"
                f"该房型报价可能已变动或售罄，请重新回复房型序号 + 入住人姓名再试。"
            )

        if not price_info.get("reference_no"):
            print(f"[验价响应缺少 referenceNo] {price_info.get('raw')}")
            self.state.booking_context = {}
            return (
                "验价接口未返回预订参考号（referenceNo），暂时无法下单。"
                "请稍后重试，或换一个房型再试。"
            )

        names = rlg.split_cn_name(guest_name)
        email_override = draft.get("email_override")
        recent = price_info.get("recent_guests") or []
        ctx = {
            "phase": "price_confirmed",
            "rate_plan_obj": rate_plan,
            "room_type": draft["room_type"],
            "guest_name": guest_name,
            "names": names,
            "email_override": email_override,
            "chosen_recent": None,   # 用户选择使用道旅账号常用入住人时的下标对应 guest
            "price_info": price_info,
        }
        self.state.booking_context = ctx
        self.state.stage = "booking_confirm"

        # 既没有用户提供的邮箱，账号里也没有常用邮箱：追问邮箱后再展示确认卡
        if not self._resolve_contact_email(ctx) and not recent:
            ctx["phase"] = "await_email"
            response = (
                "🔒 已完成验价锁价。创建道旅真实订单还需要一个联系邮箱（用于接收预订确认），\n"
                "请回复邮箱地址，例如：zhangsan@example.com；或回复【取消】放弃。"
            )
            self.state.conversation_history.append(f"助手：{response}")
            return response

        return self._render_confirm_card(ctx)

    def _resolve_contact_email(self, ctx: dict) -> Optional[str]:
        """解析本次下单使用的邮箱：用户指定 > 选中的常用入住人 > 最近常用入住人。"""
        if ctx.get("email_override"):
            return ctx["email_override"]
        chosen = ctx.get("chosen_recent")
        if chosen and chosen.get("email"):
            return chosen["email"]
        recent = ctx["price_info"].get("recent_guests") or []
        if recent and recent[0].get("email"):
            return recent[0]["email"]
        return None

    def _resolve_submit_names(self, ctx: dict) -> dict:
        """解析提交给道旅的入住人姓/名：优先用户选中的常用入住人，否则用户输入的姓名。"""
        chosen = ctx.get("chosen_recent")
        if chosen and chosen.get("name"):
            return rlg.split_cn_name(chosen["name"])
        return ctx["names"]

    def _render_confirm_card(self, ctx: dict) -> str:
        """渲染验价后的二次确认卡片（展示将要提交的全部信息）。"""
        hotel = self.state.selected_hotel
        rate_plan = ctx["rate_plan_obj"]
        p = self.state.params
        price_info = ctx["price_info"]

        nights = self._calc_nights(p.check_in, p.check_out)
        total = price_info.get("total_price")
        if total is None:
            total = (price_info.get("per_night") or rate_plan.price_per_night) * max(nights, 1)
        meal = price_info.get("meal") or rate_plan.meal or "未注明"
        if price_info.get("cancel_policy"):
            cancel_text = price_info["cancel_policy"]
        elif price_info.get("cancelable") is True:
            cancel_text = "可免费取消"
        elif price_info.get("cancelable") is False:
            cancel_text = "不可免费取消"
        else:
            cancel_text = rate_plan.cancel_policy or "以道旅订单页为准"

        names = self._resolve_submit_names(ctx)
        guest_display = ctx.get("chosen_recent")["name"] if ctx.get("chosen_recent") else ctx["guest_name"]
        email = self._resolve_contact_email(ctx)
        recent = price_info.get("recent_guests") or []

        on_request_text = ""
        if price_info.get("on_request"):
            on_request_text = "\n⚠️ 该房型为【申请确认】房型，下单后需等待酒店确认是否有房。"

        lines = [
            "🔒 验价成功，已锁定实时价格。请核对以下订单信息：",
            "----------------------------------------",
            f"酒店：{hotel.name}",
            f"房型：{price_info.get('room_name') or rate_plan.room_name}",
            f"入住：{p.check_in} → {p.check_out}（{nights}晚）· 1间房 · 2位成人",
            f"入住人：{guest_display}（提交为 姓：{names['last_name']} / 名：{names['first_name']}）",
            f"联系邮箱：{email or '（待补充）'}",
            f"餐食：{meal}",
            f"退改：{cancel_text}",
            f"订单总价：{total:g} {price_info.get('currency') or 'CNY'}（验价实时价，最终以支付页为准）",
        ]
        if recent and not ctx.get("chosen_recent"):
            lines.append("----------------------------------------")
            lines.append("检测到你道旅账号的常用入住人（官方要求需你确认后才能使用）：")
            for i, g in enumerate(recent[:3], 1):
                lines.append(f"  {i}. {g.get('name')} {g.get('email') or ''}")
            lines.append("可回复【使用常用入住人 1】切换，或直接回复另一个邮箱修改联系邮箱。")
        lines += [
            "----------------------------------------",
            "⚠️ 接下来将在道旅创建【真实订单】：创建成功后须在规定时间内通过支付链接"
            "完成付款；不支付不会扣款，超时未支付订单会被自动取消。" + on_request_text,
            "",
            "回复【确认下单】立即创建真实订单；回复【取消】放弃本次预订。",
        ]
        card = "\n".join(lines)
        self.state.conversation_history.append(f"助手：{card}")
        return card

    def _handle_real_booking_decision(self, user_input: str) -> str:
        """真实下单第 2 步：处理 booking_confirm / await_email 阶段的输入。"""
        text = user_input.strip()
        ctx = self.state.booking_context
        if ctx.get("phase") not in ("price_confirmed", "await_email"):
            self.state.stage = "booking"
            return "验价信息已失效，请重新选择房型。"

        # 放弃（最高优先级，避免"取消"二字被其他规则吞掉）
        if any(w in text for w in ("取消", "放弃", "算了", "不要了")):
            self.state.booking_context = {}
            self.state.stage = "booking"
            response = "已放弃本次真实下单。你可以重新选择房型，或回复其他需求。"
            self.state.conversation_history.append(f"助手：{response}")
            return response

        # 临时查询订单（不清空验价上下文，查完仍可确认下单）
        query_text = self._try_order_query_during_confirm(text)
        if query_text is not None:
            return query_text

        email = rlg.extract_email(text)

        # 验价后缺邮箱的追问阶段
        if ctx["phase"] == "await_email":
            if email:
                ctx["email_override"] = email
                ctx["phase"] = "price_confirmed"
                return self._render_confirm_card(ctx)
            return (
                "请回复一个有效的邮箱地址（例如 zhangsan@example.com），"
                "或回复【取消】放弃本次预订。"
            )

        # 选择道旅账号常用入住人："使用常用入住人1"/"用1"
        recent = ctx["price_info"].get("recent_guests") or []
        m = re.search(r'(?:使用|用|选择)?(?:常用)?入住人?\s*(\d+)', text)
        if m and recent:
            idx = int(m.group(1)) - 1
            if 0 <= idx < len(recent[:3]):
                ctx["chosen_recent"] = recent[idx]
                if recent[idx].get("email"):
                    ctx["email_override"] = None
                return self._render_confirm_card(ctx)
            return f"常用入住人只有 {min(len(recent), 3)} 位，请回复 1-{min(len(recent), 3)} 之间的序号。"

        # 直接回复新邮箱：更新联系邮箱
        if email:
            ctx["email_override"] = email
            return self._render_confirm_card(ctx)

        # 二次确认创建真实订单
        yes_words = ("确认下单", "确认预订", "确认创建", "确认订", "下单", "确认", "确定", "是")
        if any(w in text for w in yes_words):
            if not self._resolve_contact_email(ctx):
                ctx["phase"] = "await_email"
                return (
                    "创建订单还需要一个联系邮箱，请回复邮箱地址"
                    "（例如 zhangsan@example.com）。"
                )
            return self._execute_real_booking(ctx)

        return (
            "请回复【确认下单】创建真实订单，或回复【取消】放弃；\n"
            "也可以回复常用入住人序号（如【使用常用入住人 1】）或一个新邮箱。\n"
            "（验价锁定的价格有时效，超时后需要重新验价）"
        )

    def _try_order_query_during_confirm(self, text: str) -> Optional[str]:
        """
        验价待确认阶段用户临时查订单：返回查询文本并保留验价上下文；
        不是订单查询时返回 None，交回确认流程继续处理。
        """
        if "订单" not in text:
            return None
        m = re.search(r'(?<![A-Za-z0-9])([A-Z0-9]{8,})(?![A-Za-z0-9])', text.upper())
        if m:
            body = self._handle_order_query({"order_id": m.group(1)}, text)
        elif any(w in text for w in
                 ("查", "历史", "记录", "列表", "有哪些", "我的", "看看")):
            body = self._handle_order_list({}, text)
        else:
            return None
        return body + (
            "\n----------------------------------------\n"
            "⏳ 你还有一笔已验价的订单尚未决定：回复【确认下单】继续创建，"
            "回复【取消】放弃（验价价格有时效）。"
        )

    def _execute_real_booking(self, ctx: dict) -> str:
        """真实下单第 3 步：用 referenceNo 调道旅 hotelbook 创建订单。"""
        names = self._resolve_submit_names(ctx)
        email = self._resolve_contact_email(ctx)
        price_info = ctx["price_info"]

        try:
            result = rlg.create_booking(
                reference_no=price_info["reference_no"],
                first_name=names["first_name"],
                last_name=names["last_name"],
                email=email,
            )
        except rlg.RollingGoAuthError as e:
            return f"下单失败：{e}\n登录恢复后请重新选择房型并验价。"
        except rlg.RollingGoApiError as e:
            # 常见原因：验价超时 referenceNo 失效、房型售罄
            self.state.booking_context = {}
            self.state.stage = "booking"
            return (
                f"下单失败：{e}\n"
                f"多为验价价格已超时或房型被抢完，请重新选择房型（我会重新验价）。"
            )

        print(f"[道旅下单响应] {result.get('raw')}")
        if not result.get("order_no"):
            self.state.booking_context = {}
            self.state.stage = "booking"
            return (
                "下单请求已发出，但道旅未返回订单号，请到道旅订单中心核实后再操作，"
                "避免重复下单。"
            )

        chosen = ctx.get("chosen_recent")
        guest_display = chosen["name"] if chosen else ctx["guest_name"]
        hotel = self.state.selected_hotel
        order = create_real_order(
            hotel, self.state.params, ctx["room_type"],
            guest_display, email, price_info, result,
        )
        self.state.current_order = order
        # 重置检索上下文，但保留 current_order 便于继续展示支付链接
        self.state.params = BookingParams()
        self.state.search_results = []
        self.state.selected_hotel = None
        self.state.booking_context = {}
        self.state.stage = "intent"

        payment = result.get("payment_url")
        response = (
            "✅ 道旅真实订单已创建（待支付）！\n"
            "----------------------------------------\n"
            f"订单号：{order.order_id}\n"
            f"酒店：{order.hotel_name}\n"
            f"房型：{order.room_type}\n"
            f"入住：{order.check_in} → {order.check_out}\n"
            f"入住人：{guest_display}（{names['last_name']} {names['first_name']}）\n"
            f"联系邮箱：{email}\n"
            f"总价：{order.total_price:g}元\n"
            f"订单状态：{result.get('status') or '待支付'}\n"
        )
        if payment:
            response += (
                "----------------------------------------\n"
                "请尽快打开下面的链接完成支付（我不会代替你支付）：\n"
                f"{payment}\n"
            )
        else:
            response += (
                "----------------------------------------\n"
                "道旅本次未返回支付链接，请登录道旅订单中心查看并支付。\n"
            )
        response += (
            "\n未支付不会扣款，超时未支付订单会自动取消。\n"
            "回复【查询订单】可查看该订单的最新状态。"
        )
        self.state.conversation_history.append(f"助手：{response}")
        return response

    # ---------- 预订阶段输入分发 ----------

    def process_booking_confirmation(self, user_input: str) -> Optional[str]:
        """
        处理预订阶段的用户输入，在 main.py 中被调用：
        - booking_confirm：真实验价后的二次确认（确认下单 / 取消）
        - booking：① 模拟订单的支付/取消 ② 真实链路的邮箱补全、验价
                  ③ 房型 + 入住人解析（真实 / 模拟两条链路）
        """
        # 1) 验价完成，等待是否创建真实订单的二次确认
        if self.state.stage == "booking_confirm":
            return self._handle_real_booking_decision(user_input)

        # 2) 模拟订单：支付确认（真实订单绝不允许本地标记已支付）
        if "确认支付" in user_input and self.state.current_order:
            if self.state.current_order.source == "rollinggo":
                order = self.state.current_order
                tail = f"\n支付链接：{order.payment_url}" if order.payment_url else ""
                return (
                    f"订单 {order.order_id} 是道旅真实订单，需要你亲自在支付页面完成付款，"
                    f"我无法代为支付。{tail}"
                )
            success = pay_order(self.state.current_order.order_id)
            if success:
                self.state.current_order.status = "paid"
                order = self.state.current_order
                # 重置状态，准备下一次预订
                self._reset_booking_state()
                return (
                    f"支付成功！\n"
                    f"订单号：{order.order_id}\n"
                    f"酒店：{order.hotel_name}\n"
                    f"入住人：{order.guest_name}\n"
                    f"总价：{order.total_price:g}元\n"
                    f"状态：已支付\n\n"
                    f"祝你入住愉快！有其他需要随时告诉我。"
                )
            return "支付失败，请稍后重试或联系客服。"

        # 3) 模拟订单：取消
        if "取消订单" in user_input and self.state.current_order:
            cancel_order(self.state.current_order.order_id)
            self._reset_booking_state()
            return "订单已取消。"

        if not self.state.selected_hotel:
            return None  # 未选酒店，让主流程处理

        hotel = self.state.selected_hotel
        ctx = self.state.booking_context
        pending = self.state.pending_booking
        text = user_input.strip()

        # 4) 选房/验价阶段：放弃重选（验价草稿或已暂存一半的房型/姓名都清空）
        if (ctx or pending) and text in ("取消", "放弃", "不要了", "重新选", "重新选择"):
            self.state.booking_context = {}
            self.state.pending_booking = {}
            return "好的，已清空本次选择，请重新回复房型序号 + 入住人姓名。"

        email = rlg.extract_email(user_input) or pending.get("email_override")

        # 5) 解析本轮消息中的房型和入住人（上一轮已给的另一半信息稍后合并）
        # 优先匹配道旅实时房型报价（序号/房型名/床型关键词）
        rate_plan = self._match_rate_plan(user_input) if hotel.rate_plans else None
        room_type = rate_plan.room_name if rate_plan \
            else self._match_mock_room_type(user_input)
        guest_name = self._extract_guest_name(
            user_input, waiting_for_name=bool(pending.get("room_type")))

        # 6) 与上一轮暂存信息合并：先名字后房型 / 先房型后名字都能接上
        if not guest_name:
            guest_name = pending.get("guest_name")
        if not room_type:
            room_type = pending.get("room_type")
            if room_type and not rate_plan:
                # 沿用上一轮房型名时重新解析报价对象，避免使用过期价格
                rate_plan = next(
                    (rp for rp in hotel.rate_plans if rp.room_name == room_type), None)

        # 7) 链路选择：道旅酒店 + 有效报价 + 已登录 OAuth → 直接验价
        #    邮箱在验价后的确认环节处理（可用道旅账号常用邮箱，展示后由用户确认）
        real_capable = bool(
            rate_plan and rate_plan.rate_plan_id
            and str(hotel.hotel_id).startswith("RLG_")
        )
        if room_type and guest_name and real_capable and rlg.is_logged_in():
            self.state.pending_booking = {}
            draft = {
                "rate_plan_obj": rate_plan,
                "room_type": room_type,
                "guest_name": guest_name,
                "email_override": email,   # 用户同条消息附了邮箱就直接带上
            }
            return self._do_real_price_confirm(draft)

        if room_type and guest_name:
            self.state.pending_booking = {}
            # 模拟链路（未登录的真实酒店也先落模拟单，并给出登录提示）
            response = self.confirm_booking(room_type, guest_name, rate_plan)
            if real_capable and not rlg.is_logged_in():
                response += "\n\n" + LOGIN_HINT
            return response
        elif room_type and not guest_name:
            # 只拿到房型：暂存，等下一轮入住人姓名
            self.state.pending_booking = {"room_type": room_type, "email_override": email}
            return f"好的，{room_type}。请告诉我入住人姓名。"
        elif guest_name and not room_type:
            # 只拿到姓名：暂存，等下一轮房型（避免用户分两条消息时重复询问）
            self.state.pending_booking = {"guest_name": guest_name, "email_override": email}
            return f"好的，入住人{guest_name}。请回复房型序号或名称选择房型。"
        return None  # 无法解析，返回 None 让主流程处理

    def _reset_booking_state(self):
        """重置预订状态，准备下一次预订"""
        self.state.params = BookingParams()
        self.state.search_results = []
        self.state.selected_hotel = None
        self.state.current_order = None
        self.state.booking_context = {}
        self.state.pending_booking = {}
        self.state.stage = "intent"
