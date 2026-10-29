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
from datetime import datetime, timedelta
from typing import Optional, Tuple

import requests

# 从 .env 文件加载环境变量（优先脚本所在目录，兼容从其他工作目录启动）
try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"), override=True)
except ImportError:
    pass

from text_rules import NAME_DENY_WORDS, NAME_PATTERNS, match_order_status
from geo_data import find_city, infer_city_from_location

# DashScope OpenAI 兼容接口：qwen3.8-flash 等新模型走原生 Generation.call 会报
# "url error"，统一用 compatible-mode 更稳，也不再依赖 dashscope SDK 做对话。
_COMPAT_URL = os.environ.get(
    "DASHSCOPE_COMPAT_URL",
    "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions",
)


def shift_date(check_in: str, days: int) -> Optional[str]:
    """在 YYYY-MM-DD 日期上加减天数；解析失败返回 None。"""
    try:
        return (datetime.strptime(check_in, "%Y-%m-%d") + timedelta(days=days)).strftime("%Y-%m-%d")
    except (ValueError, TypeError):
        return None


class LLMClient:
    """LLM 客户端——封装意图理解和参数提取能力"""

    def __init__(self):
        """初始化时检测是否有 API Key，自动选择模式"""
        self.api_key = os.environ.get("DASHSCOPE_API_KEY", "")
        # qwen3.8-flash：DashScope 新版 Flash 模型；可用 .env 的 DASHSCOPE_MODEL 覆盖
        self.model = os.environ.get("DASHSCOPE_MODEL", "qwen3.8-flash")
        self.use_real_llm = bool(self.api_key)
        if self.use_real_llm:
            print(f"[系统] 检测到 DASHSCOPE_API_KEY，使用真实 LLM 模式（模型：{self.model}）")
        else:
            print("[系统] 未检测到 DASHSCOPE_API_KEY，使用模拟模式（规则引擎）")
            print("[系统] 设置环境变量 DASHSCOPE_API_KEY 可启用真实 LLM")

    def _chat(self, prompt: str, json_mode: bool = False) -> Optional[str]:
        """调用 DashScope OpenAI 兼容接口；失败返回 None，由调用方回退规则引擎。"""
        payload = {
            "model": self.model,
            "messages": [{"role": "user", "content": prompt}],
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        try:
            resp = requests.post(
                _COMPAT_URL,
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
                timeout=(8, 90),
            )
            if resp.status_code != 200:
                print(f"[LLM 调用失败] status={resp.status_code} "
                      f"body={resp.text[:300]}，回退规则引擎")
                return None
            data = resp.json()
            choices = data.get("choices") or []
            if not choices:
                print(f"[LLM 调用失败] 无 choices：{str(data)[:300]}，回退规则引擎")
                return None
            content = (choices[0].get("message") or {}).get("content") or ""
            return content.strip() or None
        except Exception as e:
            print(f"[LLM 调用失败] {type(e).__name__}: {e}，回退规则引擎")
            return None

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
        today = datetime.now()
        weekday_cn = "一二三四五六日"[today.weekday()]
        tomorrow = today + timedelta(days=1)
        day_after = today + timedelta(days=2)
        prompt = f"""你是一个酒店预订助手的意图识别模块。今天是{today:%Y-%m-%d}（周{weekday_cn}）。
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
  口语日期换算示例（今天是{today:%Y-%m-%d}周{weekday_cn}）："明天"→check_in={tomorrow:%Y-%m-%d}；"后天"→check_in={day_after:%Y-%m-%d}；"这周末"→本周六/周日；"下周末"→下周六/周日。
  用户只说"24号"这类只有日、没有月份的表达时，换算成不早于今天的最近一个24号（本月该日已过则算下月）。
  如果提供了"住N晚""住N天"（N可能是中文数字，如"三天"），用入住日期推算离店日期。
- city 是城市中文名，例如"沈阳"→"沈阳"。
- max_price 是数字（元/晚）。
- 无法确定的字段不要填，不要编造。

示例返回：{{"intent":"book","city":"沈阳","check_in":"{tomorrow:%Y-%m-%d}","check_out":"{day_after:%Y-%m-%d}","max_price":1000}}"""

        text = self._chat(prompt, json_mode=True)
        if text:
            try:
                # 尝试解析 JSON
                json_match = re.search(r'\{.*\}', text, re.DOTALL)
                if json_match:
                    data = json.loads(json_match.group())
                    intent = data.get("intent", "chat")
                    params = {k: v for k, v in data.items() if k != "intent" and v}
                    # LLM 对"24号""住三天"这类口语日期容易漏提，用规则引擎补齐缺失字段
                    _, rule_params = self._extract_with_rules(user_input)
                    for k, v in rule_params.items():
                        if k == "location" and isinstance(v, str):
                            cur = params.get("location") or ""
                            # LLM 常把“来宾高级中学附近”剥成“高级中学附近”，
                            # 规则结果更完整时保留完整地标，距离展示才不会退化
                            if cur and len(v) > len(cur) and cur in v:
                                params[k] = v
                                continue
                        params.setdefault(k, v)
                    return intent, params
            except Exception as e:
                print(f"[LLM 返回解析失败，回退规则引擎] {e}")

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
        city = find_city(text)
        if city:
            params["city"] = city

        # ---- 位置/商圈提取（城市以下的位置修饰：市中心、地标+附近等）----
        location_phrases = []
        location_patterns = (
            r'[一-龥A-Za-z0-9]{2,12}?(?:步行街|商业街|商圈|广场|火车站|高铁站|机场|'
            r'大学城|大学|学院|学校|中学|小学|高中|初中|幼儿园|医院|会展中心|体育中心|地铁站|景区|景点|主题公园|海洋公园|古镇|古城|夜市|老街|'
            r'海边|海滩|沙滩|湖边|江边|湾)'
            r'(?:附近|周边|旁边|一带)?',
            r'[一-龥]{2,10}?(?:附近|周边|旁边|一带)',
            r'市中心|市区中心|老城区|古城区',
        )
        # 命中片段开头可能连带的动词/意愿词（长词在前，避免只剥掉“帮我”）
        _LEAD_VERBS = (
            "帮我查询", "帮我查", "帮忙查询", "帮忙查", "请帮我查询", "请帮我查",
            "帮我订", "帮忙订", "我要订", "我想订", "想要订", "想订", "要订",
            "预订", "预定", "帮订", "找一下", "找一找", "查查", "查一下",
            "搜一下", "我想去", "想要去", "我要去", "想去", "查询",
            "找", "查", "搜", "去",
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
                    # 带地标类型的完整名称保留城市前缀（"来宾高级中学"），
                    # 否则距离展示会退化成泛泛的"高级中学"；
                    # "成都春熙路"这类纯街道仍剥成"春熙路"
                    if (rest and rest[0] not in _GEO_ENTITY_CHARS
                            and not infer_city_from_location(p)):
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

        today = datetime.now()

        def _future_month_day(month: int, day: int) -> Optional[datetime]:
            """（月, 日）落到不早于今天的最近日期，跨年时取明年。非法日期返回 None。"""
            for year in (today.year, today.year + 1):
                try:
                    d = datetime(year, month, day)
                except ValueError:
                    return None
                if d.date() >= today.date():
                    return d
            return None

        def _future_day_only(day: int) -> Optional[datetime]:
            """只有"24号"这种日：本月该日已过则算下月。"""
            year, month = today.year, today.month
            if day < today.day:
                month += 1
                if month > 12:
                    year, month = year + 1, 1
            try:
                return datetime(year, month, day)
            except ValueError:
                return None

        # 2. 只有月日：10月24号 / 10月24日
        if "check_in" not in params:
            md_match = re.search(r'(\d{1,2})\s*月\s*(\d{1,2})\s*[号日]?', text)
            if md_match:
                d = _future_month_day(int(md_match.group(1)), int(md_match.group(2)))
                if d:
                    params["check_in"] = d.strftime("%Y-%m-%d")

        # 3. 只有日：24号 / 24日（"帮我订24号沈阳的酒店"）
        #    先支持“3号到6号 / 3号-6号”这类日期区间，避免只抓到入住日
        if "check_in" not in params and "check_out" not in params:
            range_match = re.search(
                r'(?<!\d)(\d{1,2})\s*[号日]?\s*(?:到|至|[-—~])\s*'
                r'(\d{1,2})\s*[号日]', text)
            if range_match:
                d1 = _future_day_only(int(range_match.group(1)))
                d2 = _future_day_only(int(range_match.group(2)))
                if d1 and d2:
                    if d2 <= d1:
                        try:
                            d2 = d2.replace(
                                month=d2.month % 12 + 1,
                                year=d2.year + (1 if d2.month == 12 else 0))
                        except ValueError:
                            pass
                    if d2 > d1:
                        params["check_in"] = d1.strftime("%Y-%m-%d")
                        params["check_out"] = d2.strftime("%Y-%m-%d")

        if "check_in" not in params:
            day_match = re.search(r'(?<!\d)(\d{1,2})\s*[号日](?!\d)', text)
            if day_match:
                d = _future_day_only(int(day_match.group(1)))
                if d:
                    params["check_in"] = d.strftime("%Y-%m-%d")

        # 4. 口语化日期：今天/明天/后天等
        #    关键：先判断语境——"后天离店""明天退房""25号走"里的日期词是【离店日期】，
        #    不能一律塞进 check_in，否则用户被问"哪天离店"时答"后天"会把入住日期改掉。
        _ORAL_DATE_WORDS = [("大后天", 3), ("明天", 1), ("后天", 2), ("今晚", 0), ("今天", 0)]
        for _word, _delta in _ORAL_DATE_WORDS:
            if _word not in text:
                continue
            _idx = text.find(_word)
            # 防止"大后天"里的"后天"被二次命中
            if _word == "后天" and text[max(0, _idx - 1):_idx] == "大":
                continue
            _d = (today + timedelta(days=_delta)).strftime("%Y-%m-%d")
            # 日期词后 0~3 字内出现离店提示词，或前面 0~3 字内出现"离店/退房"
            _after = text[_idx + len(_word): _idx + len(_word) + 3]
            _before = text[max(0, _idx - 3): _idx]
            _is_checkout_ctx = (
                any(h in _after for h in ("离店", "退房", "走", "退"))
                or any(h in _before for h in ("离店", "退房"))
            )
            if _is_checkout_ctx:
                if "check_out" not in params:
                    params["check_out"] = _d
            else:
                if "check_in" not in params:
                    params["check_in"] = _d
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

        # 住几晚/几天 → 推算离店日期（数字和中文数字都认："住三天""住3晚""两晚"）
        _CN_NUMS = {"一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5,
                    "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}
        nights_match = re.search(
            r'住\s*(\d+|[一二两三四五六七八九十])\s*(?:晚|天)(?!前|后)'
            r'|(\d+|[一二两三四五六七八九十])\s*(?:晚|天)(?!前|后)',
            text)
        if nights_match:
            raw = nights_match.group(1) or nights_match.group(2)
            nights = int(raw) if raw.isdigit() else _CN_NUMS.get(raw, 0)
            if nights > 0:
                if "check_in" in params and "check_out" not in params:
                    params["check_out"] = shift_date(params["check_in"], nights)
                elif "check_in" not in params:
                    # 本条只说"住三天"：把晚数交给 agent，结合上一轮的入住日期推算
                    params["_nights"] = nights

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
            status = match_order_status(text)
            if status:
                params["order_status"] = status

        # ---- 入住人姓名提取（简单规则："入住人XXX"）----
        # 注意必须带"人"字，否则"入住时间改到…"会被误抓成"时间改到"；
        # 再用业务/时间类词语做一次排除。agent/multi_agent 内有更严格的校验。
        name_match = re.search(NAME_PATTERNS[0], text)
        if name_match:
            cand = name_match.group(1)
            if not any(w in cand for w in NAME_DENY_WORDS):
                params["guest_name"] = cand

        return intent, params

    def generate_response(self, context: str, user_input: str) -> str:
        """
        生成自然语言回复（用于推荐解释、闲聊等）
        模拟模式下用模板回复，真实模式下调用 LLM
        """
        if self.use_real_llm:
            prompt = f"{context}\n\n用户：{user_input}\n助手："
            text = self._chat(prompt)
            if text:
                return text

        # 模拟模式：返回简单确认
        return "好的，我明白了。"
