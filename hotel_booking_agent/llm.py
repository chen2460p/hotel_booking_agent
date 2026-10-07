# LLM 客户端封装
# 支持两种模式：
# 1. 真实模式：调用 DashScope（通义千问）API，需要设置 DASHSCOPE_API_KEY
# 2. 模拟模式：用规则引擎模拟 LLM 的意图理解和参数提取，无需 API Key 即可运行
#
# 这样设计的好处：用户即使没有 API Key 也能完整运行程序看到效果，
# 有 Key 时自动切换为真实 LLM，体验更自然。

import os
import re
import json
from typing import Optional, Tuple

# 从 .env 文件加载环境变量（优先脚本所在目录，兼容从其他工作目录启动）
try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"), override=True)
except ImportError:
    pass


# 姓名候选中出现这些词时判定不是入住人（"入住时间改到…"等时间/业务表达）
_NAME_DENY_WORDS = (
    "时间", "日期", "改到", "改成", "推迟", "提前", "查询", "搜索",
    "重新", "酒店", "宾馆", "房型", "入住", "离店", "周末", "预算",
    "价格", "附近", "一下", "看看", "选择", "序号", "名称", "取消",
    "订单", "支付", "帮忙", "帮我", "可以", "能不", "怎么", "什么",
    "多少", "哪里", "哪个", "今天", "明天", "后天", "上午", "下午",
    "晚上", "中午", "早上", "凌晨", "几点", "小时", "星期", "推荐",
    "便宜",
)


class LLMClient:
    """LLM 客户端——封装意图理解和参数提取能力"""

    def __init__(self):
        """初始化时检测是否有 API Key，自动选择模式"""
        self.api_key = os.environ.get("DASHSCOPE_API_KEY", "")
        self.use_real_llm = bool(self.api_key)
        if self.use_real_llm:
            print("[系统] 检测到 DASHSCOPE_API_KEY，使用真实 LLM 模式")
            try:
                import dashscope
                dashscope.api_key = self.api_key
                self.dashscope = dashscope
            except ImportError:
                print("[系统] 未安装 dashscope 库，回退到模拟模式")
                print("[系统] 安装命令：pip install dashscope")
                self.use_real_llm = False
        else:
            print("[系统] 未检测到 DASHSCOPE_API_KEY，使用模拟模式（规则引擎）")
            print("[系统] 设置环境变量 DASHSCOPE_API_KEY 可启用真实 LLM")

    def extract_intent_and_params(self, user_input: str) -> Tuple[str, dict]:
        """
        核心方法：从用户输入中提取意图和参数
        返回 (意图, 参数字典)

        意图类型：
        - book: 预订酒店
        - search: 搜索/查询酒店
        - order_query: 查询订单
        - cancel: 取消订单
        - chat: 闲聊/其他
        """
        if self.use_real_llm:
            return self._extract_with_llm(user_input)
        else:
            return self._extract_with_rules(user_input)

    def _extract_with_llm(self, user_input: str) -> Tuple[str, dict]:
        """用真实 LLM 提取意图和参数"""
        prompt = f"""你是一个酒店预订助手的意图识别模块。今天是2026-10-03（周六）。
请从用户输入中提取意图和参数，严格以 JSON 格式返回，只返回 JSON 不要其他文字。

用户输入：{user_input}

意图可选值：book（预订）、search（搜索查询）、order_query（查订单/历史订单）、cancel（取消订单）、chat（其他）
参数可选字段：city（城市）、check_in（入住日期）、check_out（离店日期）、
min_star（最低星级整数）、max_price（价格上限数字）、facilities（设施列表）、keyword（关键词）、
location（城市内的具体位置/商圈/地标，如"市中心""春熙路步行街附近""三亚湾海边""高铁站旁边"）、
order_id（订单号）、order_status（订单状态筛选，仅查订单时使用：
ALL全部 / PENDING待支付 / FINISHED已完成已支付 / CANCELLED已取消）、guest_name（入住人姓名）

重要规则：
- location 只放城市以下的位置修饰（行政区、商圈、地标、路名及"附近/周边/市中心"等），
  城市本身放 city；例如"成都市中心春熙路步行街附近"→city="成都"、location="市中心春熙路步行街附近"。
- check_in 和 check_out 必须转换成 YYYY-MM-DD 格式的具体日期。
  口语日期换算示例（今天是2026-10-03周六）："这周末"→check_in=2026-10-10、check_out=2026-10-11；"下周末"→2026-10-17/2026-10-18；"明天"→2026-10-04；"后天"→2026-10-05。
  如果提供了"住N晚"，用入住日期推算离店日期。
- city 是城市中文名，例如"沈阳"→"沈阳"。
- max_price 是数字（元/晚）。
- 无法确定的字段不要填，不要编造。

示例返回：{{"intent":"book","city":"沈阳","check_in":"2026-10-17","check_out":"2026-10-18","max_price":1000}}"""

        try:
            response = self.dashscope.Generation.call(
                model="qwen-turbo",
                prompt=prompt,
                result_format="message",
                response_format={"type": "json_object"}
            )
            text = response.output.choices[0].message.content
            # 尝试解析 JSON
            json_match = re.search(r'\{.*\}', text, re.DOTALL)
            if json_match:
                data = json.loads(json_match.group())
                intent = data.get("intent", "chat")
                params = {k: v for k, v in data.items() if k != "intent" and v}
                return intent, params
        except Exception as e:
            print(f"[LLM 调用失败，回退规则引擎] {e}")

        return self._extract_with_rules(user_input)

    def _extract_with_rules(self, user_input: str) -> Tuple[str, dict]:
        """
        用规则引擎模拟 LLM 提取（模拟模式）
        真实场景中这部分由 LLM 完成，这里用关键词匹配演示效果
        """
        text = user_input
        params = {}
        intent = "chat"

        # ---- 意图判断（注意顺序：查询/售后/评价优先于预订，避免误匹配）----
        # 保护：含"取消"但实际是查询句式（如"查已取消的订单"）必须归为查订单
        is_order_lookup = "订单" in text and any(
            kw in text for kw in
            ["查", "历史", "记录", "列表", "有哪些", "看看", "我的"]
        )
        if is_order_lookup:
            intent = "order_query"
        elif any(kw in text for kw in ["取消", "退订", "退款", "退了"]):
            intent = "cancel"
        elif any(kw in text for kw in [
                "查订单", "订单状态", "我的订单", "订单号",
                "历史订单", "订单记录", "订单列表"]):
            intent = "order_query"
        elif any(kw in text for kw in [
            "评价", "口碑", "点评", "评论", "怎么样", "好不好",
            "住客", "客人怎么说", "体验如何", "真实评价",
            "适合带孩子", "适合亲子", "适合情侣", "海景怎么样",
            "怎么评价", "有什么评价", "差评", "好评"
        ]):
            intent = "review"
        elif any(kw in text for kw in ["预订", "预定", "开房间", "开房", "住店", "住宿", "帮我订", "订个", "订一家", "订酒店"]):
            intent = "book"
        elif "订单" in text:
            intent = "order_query"
        elif any(kw in text for kw in ["查", "搜索", "找", "看看", "推荐", "有什么", "哪家"]):
            intent = "search"
        elif any(kw in text for kw in ["酒店", "宾馆", "民宿", "客栈"]):
            intent = "search"

        # ---- 城市提取 ----
        cities = ["三亚", "北京", "上海", "杭州", "广州", "深圳", "成都", "西安", "南京", "武汉", "厦门", "青岛", "大理", "丽江", "沈阳", "大连", "哈尔滨", "长春", "天津", "重庆", "苏州", "长沙"]
        for city in cities:
            if city in text:
                params["city"] = city
                break

        # ---- 位置/商圈提取（城市以下的位置修饰：市中心、地标+附近等）----
        location_phrases = []
        location_patterns = (
            r'[一-龥A-Za-z0-9]{2,12}?(?:步行街|商业街|商圈|广场|火车站|高铁站|机场|'
            r'大学城|会展中心|体育中心|地铁站|景区|景点|主题公园|海洋公园|古镇|古城|夜市|老街|'
            r'海边|海滩|沙滩|湖边|江边|湾)'
            r'(?:附近|周边|旁边|一带)?',
            r'[一-龥]{2,10}?(?:附近|周边|旁边|一带)',
            r'市中心|市区中心|老城区|古城区',
        )
        # 命中片段开头可能连带的动词/意愿词
        _LEAD_VERBS = (
            "帮我订", "帮忙订", "我要订", "我想订", "想要订", "想订", "要订",
            "预订", "预定", "帮订", "找一下", "找一找", "查查", "查一下",
            "搜一下", "我想去", "想要去", "我要去", "想去", "找", "查", "搜", "去",
        )
        # 城市名后若紧跟这些字，说明城市名是地名实体的一部分（如"三亚湾"），不可剥离
        _GEO_ENTITY_CHARS = "湾江湖海山岛桥港口溪潭"
        for pat in location_patterns:
            for phrase in re.findall(pat, text):
                p = phrase.strip()
                changed = True
                while changed:                      # 反复剥离前缀动词
                    changed = False
                    for v in _LEAD_VERBS:
                        if p.startswith(v) and len(p) - len(v) >= 2:
                            p = p[len(v):]
                            changed = True
                            break
                # 剥离城市前缀（"成都春熙路附近"→"春熙路附近"），
                # 但"三亚湾"是地名实体，不能剥成"湾"
                if params.get("city") and p.startswith(params["city"]):
                    rest = p[len(params["city"]):]
                    if rest and rest[0] not in _GEO_ENTITY_CHARS:
                        p = rest.lstrip("的")
                if len(p) >= 2:
                    location_phrases.append(p)
        if location_phrases:
            params["location"] = max(location_phrases, key=len)

        # ---- 日期提取（支持多种口语表达）----
        # 1. 明确日期格式 2026-10-05 或 2026/10/05
        date_pattern = r'(\d{4})[-/年](\d{1,2})[-/月](\d{1,2})'
        dates = re.findall(date_pattern, text)
        if len(dates) >= 2:
            params["check_in"] = f"{dates[0][0]}-{int(dates[0][1]):02d}-{int(dates[0][2]):02d}"
            params["check_out"] = f"{dates[1][0]}-{int(dates[1][1]):02d}-{int(dates[1][2]):02d}"
        elif len(dates) == 1:
            params["check_in"] = f"{dates[0][0]}-{int(dates[0][1]):02d}-{int(dates[0][2]):02d}"

        # 2. 口语化日期：下周三、这周末、明天等
        from datetime import datetime, timedelta
        today = datetime.now()

        if "明天" in text and "check_in" not in params:
            params["check_in"] = (today + timedelta(days=1)).strftime("%Y-%m-%d")
        if "后天" in text and "check_in" not in params:
            params["check_in"] = (today + timedelta(days=2)).strftime("%Y-%m-%d")
        if "这周末" in text or "本周末" in text:
            # 找到本周六
            days_until_sat = (5 - today.weekday()) % 7
            if days_until_sat == 0:
                days_until_sat = 7
            params["check_in"] = (today + timedelta(days=days_until_sat)).strftime("%Y-%m-%d")
            params["check_out"] = (today + timedelta(days=days_until_sat + 1)).strftime("%Y-%m-%d")
        if "下周末" in text:
            days_until_sat = (5 - today.weekday()) % 7 + 7
            params["check_in"] = (today + timedelta(days=days_until_sat)).strftime("%Y-%m-%d")
            params["check_out"] = (today + timedelta(days=days_until_sat + 1)).strftime("%Y-%m-%d")

        # 住几晚 → 推算离店日期
        nights_match = re.search(r'住(\d+)晚|(\d+)晚|住(\d+)天', text)
        if nights_match and "check_in" in params and "check_out" not in params:
            nights = int([g for g in nights_match.groups() if g][0])
            ci = datetime.strptime(params["check_in"], "%Y-%m-%d")
            params["check_out"] = (ci + timedelta(days=nights)).strftime("%Y-%m-%d")

        # ---- 星级提取 ----
        star_match = re.search(r'(\d)\s*星|星级.*?(\d)|要?(\d)星', text)
        if star_match:
            star = int([g for g in star_match.groups() if g][0])
            if 1 <= star <= 5:
                params["min_star"] = star

        # ---- 价格提取 ----
        price_match = re.search(r'(\d+)\s*元?\s*(以)?内|预算\s*(\d+)|不超过\s*(\d+)|低于\s*(\d+)', text)
        if price_match:
            price = int([g for g in price_match.groups() if g][0])
            params["max_price"] = float(price)

        # ---- 设施提取 ----
        facility_keywords = {
            "泳池": ["泳池", "游泳", "游泳池"],
            "早餐": ["早餐", "含早", "双早"],
            "海景": ["海景", "看海", "海边"],
            "健身房": ["健身", "健身房"],
            "SPA": ["SPA", "spa", "水疗", "按摩"],
            "停车场": ["停车", "停车场", "车位"],
            "儿童乐园": ["亲子", "儿童", "小孩", "带娃"],
            "WiFi": ["WiFi", "wifi", "上网", "网络"],
        }
        facilities = []
        for fac, keywords in facility_keywords.items():
            if any(kw in text for kw in keywords):
                facilities.append(fac)
        if facilities:
            params["facilities"] = facilities

        # ---- 关键词提取（海景、亲子等已在设施中，这里补充其他）----
        if "海景" in text and "keyword" not in params:
            params["keyword"] = "海景"

        # ---- 订单号提取 ----
        order_match = re.search(r'(ORD[A-F0-9]{6,})|订单号?\s*[:：]?\s*(\w+)', text)
        if order_match:
            order_id = [g for g in order_match.groups() if g][0]
            if order_id.startswith("ORD"):
                params["order_id"] = order_id

        # ---- 订单状态筛选（仅查订单场景）----
        if intent == "order_query":
            if any(w in text for w in ["待支付", "未支付", "未付款", "待付款"]):
                params["order_status"] = "PENDING"
            elif any(w in text for w in ["已取消", "取消过", "退款"]):
                params["order_status"] = "CANCELLED"
            elif any(w in text for w in
                     ["已完成", "完成的", "已支付", "已付款", "已入住", "住过的"]):
                params["order_status"] = "FINISHED"
            elif any(w in text for w in ["全部", "所有", "历史"]):
                params["order_status"] = "ALL"

        # ---- 入住人姓名提取（简单规则："入住人XXX"）----
        # 注意必须带"人"字，否则"入住时间改到…"会被误抓成"时间改到"；
        # 再用业务/时间类词语做一次排除。agent/multi_agent 内有更严格的校验。
        name_match = re.search(r'入住人\s*[:：是叫]?\s*([一-龥]{2,4})', text)
        if name_match:
            cand = name_match.group(1)
            if not any(w in cand for w in _NAME_DENY_WORDS):
                params["guest_name"] = cand

        return intent, params

    def generate_response(self, context: str, user_input: str) -> str:
        """
        生成自然语言回复（用于推荐解释、闲聊等）
        模拟模式下用模板回复，真实模式下调用 LLM
        """
        if self.use_real_llm:
            try:
                prompt = f"{context}\n\n用户：{user_input}\n助手："
                response = self.dashscope.Generation.call(
                    model="qwen-turbo",
                    prompt=prompt,
                    result_format="message"
                )
                return response.output.choices[0].message.content
            except Exception as e:
                print(f"[LLM 回复生成失败] {e}")

        # 模拟模式：返回简单确认
        return "好的，我明白了。"
