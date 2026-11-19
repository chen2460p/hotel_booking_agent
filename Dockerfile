# 酒店预订 Agent —— FastAPI Web 版镜像（入口：hotel_booking_agent/web.py）
#
# 构建：docker build -t hotel-booking-agent .
# 运行：docker run --rm -p 8000:8000 --env-file hotel_booking_agent/.env hotel-booking-agent
# 推荐：docker compose up -d --build（见 docker-compose.yml）
#
# 两段构建：前端（React + Vite）在 node 阶段产出 frontend/dist，再拷进 python 镜像。
# 运行时镜像里没有 node，也不需要任何 VITE_* 变量（接口同源）。

# ---------- 前端构建阶段 ----------
FROM node:22-alpine AS frontend
WORKDIR /fe
# 先只拷依赖清单：源码改动不会击穿 npm ci 的缓存层
COPY hotel_booking_agent/frontend/package.json hotel_booking_agent/frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY hotel_booking_agent/frontend/ ./
# 构建失败就让镜像构建失败，避免产出"没有前端"的镜像
RUN npm run build && test -f dist/index.html

# ---------- 运行时镜像 ----------
FROM python:3.12-slim

# 时区数据：容器默认 UTC，而 llm.py 用 datetime.now() 推算「明天 / 下周六」，
# 不装 tzdata 且不设 TZ，每天有 8 小时会把日期算错一天。
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update \
 && apt-get install -y --no-install-recommends tzdata \
 && rm -rf /var/lib/apt/lists/*

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    TZ=Asia/Shanghai

WORKDIR /app

# 依赖单独一层：只改业务代码时命中缓存，不必重装依赖
COPY hotel_booking_agent/requirements-web.txt /tmp/requirements-web.txt

# dashscope 仅 RAG 真实 Embedding 需要（不装则自动降级 TF-IDF）
# pymysql 仅 DATABASE_URL 指向 MySQL 时需要
ARG WITH_DASHSCOPE=1
ARG WITH_PYMYSQL=0
RUN pip install -r /tmp/requirements-web.txt \
 && if [ "$WITH_DASHSCOPE" = "1" ]; then pip install "dashscope>=1.20"; fi \
 && if [ "$WITH_PYMYSQL" = "1" ]; then pip install "pymysql>=1.1"; fi

# 只拷贝应用目录；.env / __pycache__ / *.db / cache 由 .dockerignore 排除
COPY hotel_booking_agent/ /app/

# 前端构建产物（web.py 从 frontend/dist 托管首页）
COPY --from=frontend /fe/dist /app/frontend/dist

# 非 root 运行；预先建好运行时可写目录，命名卷首次挂载会继承这里的属主
RUN useradd --create-home --uid 10001 app \
 && mkdir -p /app/cache /data /home/app/.hotel-cli \
 && chown -R app:app /app /data /home/app

# 默认把订单库放到 /data（TZ / DATABASE_URL 可在 compose 里覆盖）
ENV HOME=/home/app \
    ROLLINGGO_TOKEN_FILE=/home/app/.hotel-cli/token.json \
    DATABASE_URL=sqlite:////data/orders.db

USER app
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=3).status == 200 else 1)"

# 不要加 --workers > 1：会话池（web.py 的 _agents）和 SQLite 句柄都在单进程内存里，
# 多 worker 会导致多轮对话状态丢失、订单写入互相不可见。
CMD ["uvicorn", "web:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers"]
