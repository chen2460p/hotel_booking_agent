# RAG 检索增强模块——酒店评价检索
#
# 对应八股 RAG 核心考点：
# 1. 数据准备：模拟酒店评价语料
# 2. 文档切块（Chunking）：按评价条目切分
# 3. Embedding 向量化：支持 DashScope 真实 embedding 和模拟模式
# 4. 向量召回：根据用户问题检索相关评价
# 5. Rerank 重排序：对召回结果精排
# 6. 生成回答：基于检索到的真实评价生成回复
#
# 双模式设计（和 llm.py 一致）：
# - 真实模式：调用 DashScope text-embedding-v2
# - 模拟模式：用 TF-IDF 风格的关键词评分模拟向量检索

import os
import re
import math
from typing import List, Optional, Tuple
from collections import Counter

# 从 .env 文件加载环境变量（优先脚本所在目录，兼容从其他工作目录启动）
try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"), override=True)
except ImportError:
    pass


# ========== 模拟酒店评价语料 ==========
# 真实场景中这些数据来自爬虫、用户评论、攻略文章等
REVIEW_CORPUS = [
    # 三亚海景大酒店 H001
    {"review_id": "R001", "hotel_id": "H001", "author": "旅行达人小王",
     "text": "三亚海景大酒店位置非常好，推开窗户就能看到大海，海景名不虚传。泳池很干净，早餐种类丰富，中西餐都有。前台服务态度热情，办理入住很快。唯一缺点是周末价格偏贵，建议提前预订。",
     "date": "2026-08-15", "rating": 5},
    {"review_id": "R002", "hotel_id": "H001", "author": "亲子游妈妈",
     "text": "带孩子住了三天，酒店有儿童泳池但没有儿童乐园，小孩玩的项目不多。房间很大，海景视野开阔，孩子看海很开心。健身房设施齐全，老公每天去跑步。周边吃饭不太方便，建议打车出去吃。",
     "date": "2026-07-22", "rating": 4},
    {"review_id": "R003", "hotel_id": "H001", "author": "商务出差李先生",
     "text": "出差住的，WiFi速度很快，视频会议完全没问题。行政套房空间大，办公区域舒适。早餐6点半就开始，适合早班机。隔音效果一般，晚上能听到走廊声音。整体性价比不错，商务出行推荐。",
     "date": "2026-09-01", "rating": 4},
    {"review_id": "R004", "hotel_id": "H001", "author": "蜜月旅行",
     "text": "蜜月选择了海景大床房，房间布置浪漫，到店还送了水果和香槟。日落时分在阳台看海景特别美，拍照出片率极高。SPA体验很棒，技师手法专业。强烈推荐情侣入住，物超所值。",
     "date": "2026-09-20", "rating": 5},

    # 三亚湾假日度假酒店 H002
    {"review_id": "R005", "hotel_id": "H002", "author": "预算旅行者",
     "text": "价格实惠，4星酒店这个价位在三亚算性价比很高了。泳池不错，早餐中规中矩。房间稍微有点旧，设施有些老化。离海边步行5分钟，虽然不是一线海景但也方便。整体来说值这个价。",
     "date": "2026-08-03", "rating": 4},
    {"review_id": "R006", "hotel_id": "H002", "author": "自驾游",
     "text": "自驾过来的，停车场很大而且免费，这点很赞。酒店位置好找，导航直接到。房间干净整洁，标准间空间够用。泳池开放到晚上10点，游了夜泳很舒服。前台办理速度一般，等了20分钟。",
     "date": "2026-07-15", "rating": 4},

    # 三亚亚龙湾希尔顿 H003
    {"review_id": "R007", "hotel_id": "H003", "author": "度假专家",
     "text": "亚龙湾希尔顿是三亚顶级度假酒店，私人海滩沙质细腻海水清澈，人少安静。儿童乐园很大，亲子活动丰富，带娃首选。早餐自助餐种类极其丰富，海鲜粥和现做煎饼必吃。别墅带私人泳池，体验奢华。虽然贵但物有所值。",
     "date": "2026-09-10", "rating": 5},
    {"review_id": "R008", "hotel_id": "H003", "author": "SPA爱好者",
     "text": "冲着SPA来的，没有失望，水疗中心环境私密，精油按摩手法专业，做完身心放松。海景房阳台直面大海，听着海浪声入睡。花园景观房也很美，热带植物环绕。服务周到，客房一天打扫两次。",
     "date": "2026-08-28", "rating": 5},

    # 三亚如家 H004
    {"review_id": "R009", "hotel_id": "H004", "author": "穷游学生",
     "text": "价格便宜，200多一晚在三亚很划算了。房间不大但干净，WiFi信号好。没有早餐，周边有小吃店。离景点稍远，需要坐公交。适合预算有限、只是找个地方睡觉的旅客。",
     "date": "2026-07-01", "rating": 4},

    # 北京王府井希尔顿 H005
    {"review_id": "R010", "hotel_id": "H005", "author": "购物达人",
     "text": "位置绝佳，步行到王府井大街5分钟，购物吃饭极其方便。酒店服务专业，行政酒廊下午茶不错。房间隔音好，闹中取静。健身房24小时开放。价格确实贵，但北京核心地段这个品质值了。",
     "date": "2026-09-05", "rating": 5},
    {"review_id": "R011", "hotel_id": "H005", "author": "商务出行",
     "text": "商务入住，会议室设备齐全，WiFi稳定。早餐种类多，北京特色小吃都有。前台效率高，退房秒办。房间装修偏商务风格，办公桌宽敞。打车方便，去机场不堵车时40分钟。",
     "date": "2026-08-18", "rating": 5},

    # 北京如家精选 H006
    {"review_id": "R012", "hotel_id": "H006", "author": "出差党",
     "text": "西单位置很好，逛街方便，地铁口步行3分钟。含早餐这个价格在北京很良心，虽然早餐简单但管饱。房间偏小，大床房两个人转不开。整体干净卫生，连锁酒店品质稳定，性价比之选。",
     "date": "2026-09-12", "rating": 4},

    # 上海外滩茂悦 H007
    {"review_id": "R013", "hotel_id": "H007", "author": "夜景控",
     "text": "江景房视野无敌，外滩夜景和陆家嘴天际线尽收眼底，晚上在房间看夜景比去外滩人挤人强太多。酒店设计现代有格调，SPA和健身房都不错。早餐可以边吃边看江景。强烈推荐江景房，贵有贵的道理。",
     "date": "2026-09-15", "rating": 5},
    {"review_id": "R014", "hotel_id": "H007", "author": "美食家",
     "text": "酒店餐厅出品精致，下午茶三层架很有仪式感。周边步行可达外滩源和南京路，吃饭选择多。房间卫浴用的大牌，洗浴用品好闻。服务细节到位，夜床服务还送小点心。",
     "date": "2026-08-25", "rating": 5},

    # 上海汉庭 H008
    {"review_id": "R015", "hotel_id": "H008", "author": "背包客",
     "text": "南京东路地段无敌，300块住市中心太值了。房间很小，设施基础，没有早餐。隔音差，隔壁说话能听到。适合白天在外面玩、晚上回来睡个觉的年轻人。胜在位置和价格。",
     "date": "2026-07-20", "rating": 4},

    # 杭州西湖国宾馆 H009
    {"review_id": "R016", "hotel_id": "H009", "author": "园林爱好者",
     "text": "西湖国宾馆本身就是景点，江南园林精致典雅，湖景房直面西湖，推开窗就是水墨画。茶室喝龙井吃茶点，体验杭州慢生活。别墅私密性好，适合家庭入住。早餐在湖边用，意境满分。来杭州必住。",
     "date": "2026-09-08", "rating": 5},
    {"review_id": "R017", "hotel_id": "H009", "author": "文化游",
     "text": "国宾馆历史底蕴深厚，毛主席曾住过，有展览馆。园林一步一景，随手拍都是大片。服务人员训练有素，讲解园林历史。湖景房清晨看西湖薄雾，如仙境。价格虽高但体验独一无二。",
     "date": "2026-08-12", "rating": 5},

    # 杭州七天 H010
    {"review_id": "R018", "hotel_id": "H010", "author": "学生党",
     "text": "武林广场商圈，购物吃饭方便，离西湖地铁两站。198的价格在杭州很友好。房间设施简单，WiFi可以。没有电梯，住高层搬行李累。适合预算有限的学生和背包客。",
     "date": "2026-07-08", "rating": 4},
]


# ========== RAG 检索引擎 ==========
class HotelReviewRAG:
    """
    酒店评价 RAG 检索引擎
    实现完整的 RAG 流程：切块 → Embedding → 召回 → Rerank → 生成
    """

    def __init__(self):
        self.api_key = os.environ.get("DASHSCOPE_API_KEY", "")
        self.use_real_embedding = False

        # 语料切块后的文档列表（一个评价就是一个 chunk）
        self.chunks = []
        # 缓存的 embedding 向量
        self.embeddings = []
        # 模拟模式的 IDF 权重
        self.idf = {}

        self._prepare_corpus()

        if self.api_key:
            try:
                import dashscope
                self.dashscope = dashscope
                dashscope.api_key = self.api_key
                self.use_real_embedding = True
                print("[RAG] 使用 DashScope 真实 Embedding")
                self._build_real_embeddings()
            except ImportError:
                print("[RAG] dashscope 未安装，使用模拟检索")
                self._build_mock_index()
        else:
            print("[RAG] 使用模拟检索（TF-IDF 关键词评分）")
            self._build_mock_index()

    def _prepare_corpus(self):
        """
        Step 1：文档切块（Chunking）
        这里按评价条目切分，每个评价是一个 chunk
        真实场景中长文攻略会按语义段落切分并设置重叠量
        """
        for item in REVIEW_CORPUS:
            self.chunks.append({
                "chunk_id": item["review_id"],
                "hotel_id": item["hotel_id"],
                "text": item["text"],
                "metadata": {
                    "author": item["author"],
                    "date": item["date"],
                    "rating": item["rating"],
                }
            })

    def _build_mock_index(self):
        """
        模拟模式：构建 TF-IDF 风格的检索索引
        用关键词权重模拟向量相似度，无需外部依赖
        """
        # 对所有文档分词（简单的中文分词：按标点和单字/双字词）
        def tokenize(text):
            # 提取中文词组（2-4字）和单字
            words = re.findall(r'[\u4e00-\u9fa5]{2,4}', text)
            chars = re.findall(r'[\u4e00-\u9fa5]', text)
            return words + chars

        # 计算文档频率 DF
        doc_count = len(self.chunks)
        df = Counter()
        self.chunk_tokens = []
        for chunk in self.chunks:
            tokens = tokenize(chunk["text"])
            self.chunk_tokens.append(tokens)
            for token in set(tokens):
                df[token] += 1

        # 计算 IDF
        self.idf = {
            token: math.log((doc_count + 1) / (freq + 1)) + 1
            for token, freq in df.items()
        }

        # 预计算每个文档的 TF 向量（归一化）
        self.chunk_vectors = []
        for tokens in self.chunk_tokens:
            tf = Counter(tokens)
            total = len(tokens)
            vec = {
                token: (count / total) * self.idf.get(token, 1)
                for token, count in tf.items()
            }
            # L2 归一化
            norm = math.sqrt(sum(v ** 2 for v in vec.values()))
            if norm > 0:
                vec = {k: v / norm for k, v in vec.items()}
            self.chunk_vectors.append(vec)

    def _build_real_embeddings(self):
        """真实模式：调用 DashScope Embedding API 向量化所有文档"""
        texts = [c["text"] for c in self.chunks]
        try:
            # 批量调用 embedding（DashScope 支持批量）
            batch_size = 10
            all_embeddings = []
            for i in range(0, len(texts), batch_size):
                batch = texts[i:i + batch_size]
                resp = self.dashscope.TextEmbedding.call(
                    model="text-embedding-v2",
                    input=batch
                )
                for item in resp.output["embeddings"]:
                    all_embeddings.append(item["embedding"])
            self.embeddings = all_embeddings
        except Exception as e:
            print(f"[RAG] Embedding 构建失败，回退模拟模式：{e}")
            self.use_real_embedding = False
            self._build_mock_index()

    def retrieve(self, query: str, hotel_id: Optional[str] = None,
                 top_k: int = 5) -> List[Tuple[dict, float]]:
        """
        Step 2：向量召回
        根据用户问题检索最相关的评价

        参数：
        - query：用户问题，如"这家酒店海景怎么样"
        - hotel_id：限定酒店（可选）
        - top_k：召回数量
        """
        if self.use_real_embedding:
            scores = self._real_retrieve(query)
        else:
            scores = self._mock_retrieve(query)

        # 按酒店过滤
        if hotel_id:
            scores = [
                (idx, score) for idx, score in scores
                if self.chunks[idx]["hotel_id"] == hotel_id
            ]

        # 取 top_k
        top_results = scores[:top_k]
        return [(self.chunks[idx], score) for idx, score in top_results]

    def _real_retrieve(self, query: str) -> List[Tuple[int, float]]:
        """真实 embedding 余弦相似度检索"""
        try:
            resp = self.dashscope.TextEmbedding.call(
                model="text-embedding-v2",
                input=[query]
            )
            query_vec = resp.output["embeddings"][0]["embedding"]

            scores = []
            for i, doc_vec in enumerate(self.embeddings):
                # 余弦相似度
                dot = sum(a * b for a, b in zip(query_vec, doc_vec))
                norm_q = math.sqrt(sum(a ** 2 for a in query_vec))
                norm_d = math.sqrt(sum(b ** 2 for b in doc_vec))
                sim = dot / (norm_q * norm_d) if norm_q and norm_d else 0
                scores.append((i, sim))

            scores.sort(key=lambda x: x[1], reverse=True)
            return scores
        except Exception as e:
            print(f"[RAG] 真实检索失败，回退模拟：{e}")
            return self._mock_retrieve(query)

    def _mock_retrieve(self, query: str) -> List[Tuple[int, float]]:
        """模拟 TF-IDF 余弦相似度检索"""
        def tokenize(text):
            words = re.findall(r'[\u4e00-\u9fa5]{2,4}', text)
            chars = re.findall(r'[\u4e00-\u9fa5]', text)
            return words + chars

        tokens = tokenize(query)
        tf = Counter(tokens)
        total = max(len(tokens), 1)
        query_vec = {
            token: (count / total) * self.idf.get(token, 1)
            for token, count in tf.items()
        }
        norm = math.sqrt(sum(v ** 2 for v in query_vec.values()))
        if norm > 0:
            query_vec = {k: v / norm for k, v in query_vec.items()}

        # 计算与每个文档的余弦相似度
        scores = []
        for i, doc_vec in enumerate(self.chunk_vectors):
            # 稀疏向量点积
            common = set(query_vec.keys()) & set(doc_vec.keys())
            sim = sum(query_vec[t] * doc_vec[t] for t in common)
            scores.append((i, sim))

        scores.sort(key=lambda x: x[1], reverse=True)
        return scores

    def rerank(self, query: str, results: List[Tuple[dict, float]]) -> List[Tuple[dict, float]]:
        """
        Step 3：Rerank 重排序（精排）
        真实场景用 cross-encoder 重排模型，这里用规则增强：
        - 评价评分高的加权
        - 包含查询关键词密度高的加权
        - 日期较新的轻微加权
        """
        def extract_year(date_str):
            try:
                return int(date_str[:4])
            except (ValueError, TypeError):
                return 2025

        query_chars = set(re.findall(r'[\u4e00-\u9fa5]{2,}', query))

        reranked = []
        for chunk, base_score in results:
            score = base_score
            # 评分加权（高评分评价更可信）
            rating = chunk["metadata"].get("rating", 4)
            score *= 0.8 + rating * 0.05  # 5星→1.05倍，3星→0.95倍

            # 关键词覆盖加权
            text = chunk["text"]
            if query_chars:
                coverage = sum(1 for kw in query_chars if kw in text) / len(query_chars)
                score *= 0.9 + coverage * 0.2

            # 时效性轻微加权
            year = extract_year(chunk["metadata"].get("date", ""))
            if year >= 2026:
                score *= 1.05

            reranked.append((chunk, score))

        reranked.sort(key=lambda x: x[1], reverse=True)
        return reranked

    def answer_question(self, query: str, hotel_id: Optional[str] = None) -> str:
        """
        Step 4：完整 RAG 问答流程
        召回 → 重排 → 基于检索结果生成回答

        这是 RAG 的核心入口：让 Agent 基于真实评价回答，而非凭空编造
        """
        # 召回（粗排，多取一些）
        candidates = self.retrieve(query, hotel_id=hotel_id, top_k=6)

        if not candidates or candidates[0][1] < 0.01:
            return "抱歉，暂时没有检索到相关的酒店评价信息。"

        # 重排（精排）
        reranked = self.rerank(query, candidates)

        # 取 top 3 作为生成上下文
        top_chunks = reranked[:3]

        # 基于检索结果组织回答（真实场景中这里会把上下文交给 LLM 生成）
        lines = ["根据住客真实评价，为你整理了相关信息：\n"]
        for i, (chunk, score) in enumerate(top_chunks, 1):
            meta = chunk["metadata"]
            lines.append(
                f"{i}. {meta['author']}（{meta['date']}，{meta['rating']}星）：\n"
                f"   {chunk['text']}"
            )

        lines.append("\n以上评价来自真实住客，供你参考。需要了解其他方面可以继续问我。")
        return "\n".join(lines)


# 全局单例（避免重复构建索引）
_rag_instance: Optional[HotelReviewRAG] = None

def get_rag() -> HotelReviewRAG:
    """获取 RAG 单例"""
    global _rag_instance
    if _rag_instance is None:
        _rag_instance = HotelReviewRAG()
    return _rag_instance
