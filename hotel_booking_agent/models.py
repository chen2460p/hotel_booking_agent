# 数据模型定义
# 用 dataclass 定义酒店、订单等核心数据结构，让代码更清晰

from dataclasses import dataclass, field
from typing import List, Optional
from datetime import date


@dataclass
class RoomRatePlan:
    """
    房型报价计划（来自道旅 getHotelDetail 的实时报价）
    同一房型可能有多个报价（含早/不含早、可取消/不可取消），
    适配器按房型聚合并保留最便宜的一档作为代表。
    """
    room_name: str          # 房型名称，如"行政单房公寓（双床）"
    price_per_night: float  # 每晚均价（元）
    meal: str               # 餐食说明，如"含早餐"/"不含早餐"
    cancelable: bool        # 是否可免费取消
    cancel_policy: str      # 退改政策原文，如"免费取消截止至...18:00:00"
    bed_type: str = ""      # 床型，如"1 特大床"/"双床"
    max_occupancy: int = 0  # 最大入住人数
    room_size: str = ""     # 面积（平方米，接口可能给区间）
    on_request: bool = False  # 是否需申请确认（非即时确认）
    rate_plan_id: str = ""  # 报价计划ID（真实下单时需要）


@dataclass
class Hotel:
    """酒店信息模型"""
    hotel_id: str           # 酒店唯一ID
    name: str               # 酒店名称
    city: str               # 所在城市
    address: str            # 详细地址
    star: int               # 星级（1-5）
    price_per_night: float  # 每晚价格（元，搜索阶段的最低价）
    facilities: List[str]   # 设施列表，如["泳池","WiFi","早餐"]
    room_types: List[str]   # 房型列表
    rating: float           # 用户评分（0-5）
    review_count: int       # 评论数
    # 实时详情（仅道旅详情接口返回；模拟/高德数据为空）
    rate_plans: List[RoomRatePlan] = field(default_factory=list)
    booking_url: str = ""   # 真实预订页链接


@dataclass
class BookingParams:
    """预订参数模型——Agent 需要从用户对话中提取这些参数"""
    city: Optional[str] = None          # 城市（必填）
    check_in: Optional[str] = None      # 入住日期（必填），格式 YYYY-MM-DD
    check_out: Optional[str] = None     # 离店日期（必填）
    min_star: Optional[int] = None      # 最低星级（可选）
    max_price: Optional[float] = None   # 每晚预算上限（可选）
    facilities: List[str] = field(default_factory=list)  # 必须设施（可选）
    keyword: Optional[str] = None       # 关键词，如"海景""亲子"（可选）

    def is_complete(self) -> bool:
        """判断必填参数是否齐全"""
        return all([self.city, self.check_in, self.check_out])

    def missing_fields(self) -> List[str]:
        """返回缺失的必填字段列表，用于 Agent 追问用户"""
        missing = []
        if not self.city:
            missing.append("城市")
        if not self.check_in:
            missing.append("入住日期")
        if not self.check_out:
            missing.append("离店日期")
        return missing


@dataclass
class Order:
    """订单模型"""
    order_id: str           # 订单号
    hotel_id: str           # 酒店ID
    hotel_name: str         # 酒店名称（冗余存储，方便展示）
    check_in: str           # 入住日期
    check_out: str          # 离店日期
    room_type: str          # 房型
    guest_name: str         # 入住人姓名
    total_price: float      # 总价
    status: str = "pending" # 订单状态：pending待支付 / paid已支付 / cancelled已取消
    # 真实订单（道旅）相关字段；模拟订单留空
    source: str = "mock"            # mock=本地模拟 / rollinggo=道旅真实订单
    payment_url: str = ""           # 道旅通用收银台链接（创建订单后返回，需用户自行支付）
    contact_email: str = ""         # 联系人邮箱


@dataclass
class AgentState:
    """Agent 运行时状态——对应八股中的工作记忆"""
    params: BookingParams = field(default_factory=BookingParams)  # 当前提取的预订参数
    search_results: List[Hotel] = field(default_factory=list)     # 最近一次搜索结果
    selected_hotel: Optional[Hotel] = None                        # 用户选中的酒店
    current_order: Optional[Order] = None                         # 当前订单
    conversation_history: List[str] = field(default_factory=list) # 对话历史（短期记忆）
    # 真实预订多轮上下文：草稿（等邮箱）与待确认验价单（等【确认下单】）
    booking_context: dict = field(default_factory=dict)
    stage: str = "intent"  # 当前阶段：intent意图理解 / clarify参数澄清 / search搜索 / recommend推荐 / booking预订 / booking_confirm验价待确认 /售后
