# Docker 部署说明

项目是标准 Python 3.10+ Web 应用（FastAPI + Jinja2），无 Node、无编译步骤、无本地数据库服务依赖，
**可以直接容器化**。仓库里提供三个文件：

| 文件 | 作用 |
| --- | --- |
| `Dockerfile` | 构建 FastAPI Web 版镜像（入口 `web:app`，非 root，含健康检查） |
| `.dockerignore` | 排除 `.env`、`*.db`、`cache/`、`__pycache__`、无关的本地文件 |
| `docker-compose.yml` | 端口、环境变量、三个数据卷、健康检查、日志轮转 |

## 本机实测状态（2026-11-03）

| 项 | 值 |
| --- | --- |
| 宿主 | Windows 11 家庭中文版 25H2（build 26200.9711） |
| Docker Desktop | 29.9.2（WSL2 后端，overlayfs） |
| WSL | 3.0.3.0，内核 6.18.54.1，发行版 `docker-desktop` |
| Docker Compose | v5.6.1 |
| 容器 | `hotel-booking-agent`，`Up (healthy)`，`0.0.0.0:8000->8000` |
| `/api/health` | `{"status":"ok","storage_backend":"sqlite","rollinggo_logged_in":true}` |
| 运行用户 / 时区 | `uid=10001(app)` / `TZ=Asia/Shanghai`（`date` 输出 CST） |

## 快速开始

```bash
# 1. 准备密钥（不进镜像，运行时注入）
cp hotel_booking_agent/.env.example hotel_booking_agent/.env
#    然后填 DASHSCOPE_API_KEY / ROLLINGGO_API_KEY / AMAP_API_KEY

# 2. 构建并启动
docker compose up -d --build

# 3. 打开 http://127.0.0.1:8000
```

不使用 compose 时：

```bash
docker build -t hotel-booking-agent .
docker run -d --name hotel-booking-agent -p 8000:8000 \
  --env-file hotel_booking_agent/.env \
  -v hotel-data:/data -v hotel-cache:/app/cache -v hotel-token:/home/app/.hotel-cli \
  hotel-booking-agent
```

## 验证

```bash
docker compose ps                      # STATUS 应为 Up (healthy)
curl http://127.0.0.1:8000/api/health  # {"status":"ok","storage_backend":"sqlite",...}
docker compose logs -f web             # 启动日志：storage_backend=sqlite，真实 LLM 模式
```

`/api/health` 里 `storage_backend` 是 `sqlite` 而不是 `memory`，说明订单持久化生效。
`rollinggo_logged_in` 为 `false` 时只能走模拟下单，需要先登录（见下）。

## 道旅账号登录（一次性）

`rollinggo_book.py login` 用的是「浏览器授权 + 服务端回调轮询」，不需要容器内开浏览器，
但**登录态写在文件里**，所以要落在卷上，否则每次重建容器都得重登：

```bash
docker compose exec web python rollinggo_book.py login
#   → 打印一条短链接，复制到宿主机浏览器打开并同意授权
#   → 最长等 5 分钟，成功后 token 写入 /home/app/.hotel-cli/token.json（在 hotel-token 卷里）
```

宿主机**已经登录过**的话可以直接把 token 搬进容器，省得重登：

```bash
docker cp ~/.hotel-cli/token.json hotel-booking-agent:/home/app/.hotel-cli/token.json
docker compose exec -u root web chown app:app /home/app/.hotel-cli/token.json
```

## 数据持久化

| 卷 | 容器路径 | 内容 |
| --- | --- | --- |
| `hotel-booking_hotel-data` | `/data` | `orders.db`（SQLite 订单库） |
| `hotel-booking_hotel-cache` | `/app/cache` | 携程图片 / 酒店评分缓存，删了会重新抓 |
| `hotel-booking_hotel-token` | `/home/app/.hotel-cli` | 道旅 OAuth `token.json` |

```bash
docker compose down          # 停服务，保留全部数据
docker compose down -v       # 连数据一起删（订单、登录态都会没了）
```

想直接看宿主机上的 `orders.db`，把 `/data` 换成绑定挂载：

```yaml
    volumes:
      - ./data:/data
```

## 配置

`hotel_booking_agent/.env` 里的任何变量都能在 `docker-compose.yml` 的 `environment:` 里覆盖，
**`environment` 优先级最高**。一个容易踩的点：`.env` 里若写了 `DATABASE_URL=sqlite:///orders.db`，
该路径是相对 `hotel_booking_agent/` 解析的，会写进镜像层、重建即丢；
所以 compose 里显式改成绝对路径 `sqlite:////data/orders.db`（4 个斜杠 = 绝对路径）。

换 MySQL 时：

```yaml
    environment:
      DATABASE_URL: mysql+pymysql://user:pass@mysql:3306/hotel
```

并把构建参数 `WITH_PYMYSQL` 设为 `"1"`，同网络下起一个 mysql 服务即可。

其他可选构建参数：

| 参数 | 默认 | 说明 |
| --- | --- | --- |
| `WITH_DASHSCOPE` | `1` | `0` = 不装 dashscope SDK，RAG 降级 TF-IDF，镜像更小 |
| `WITH_PYMYSQL` | `0` | `1` = 支持 `mysql+pymysql://` 订单库 |

## 中文目录名 / 镜像拉取 这两个坑

**1. 项目目录是中文（`酒店预订`）**：Compose 会拿目录名推导项目名，归一化后得到空串并报
`project name must not be empty`。`docker-compose.yml` 里已显式声明：

```yaml
name: hotel-booking
```

**2. 首次 `docker compose build` 可能卡在镜像元数据**：报
`failed to fetch oauth token ... dial tcp [2a03:2880:...]:443`，这是 `auth.docker.io` 被 DNS 污染到
不可达的 IPv6 地址（不是网络不通）。绕过办法是先让守护进程把基础镜像拉到本地，再离线构建：

```bash
docker pull python:3.12-slim
docker build --pull=false -t hotel-booking-agent .   # BuildKit 直接用本地镜像，不再查远端元数据
docker compose up -d --no-build
```

基础镜像在本地之后，平时的 `docker compose build` / `up -d --build` 都能正常走缓存。

## 注意事项

1. **不要 `--workers 2`**：会话池（`web.py` 的 `_agents`）和 SQLite 句柄都是进程内状态，
   多 worker 会让多轮对话串不起来、订单各写各的。要扩容应改为外部会话存储 + MySQL 后再加 worker。
2. **时区**：镜像已装 `tzdata` 并设 `TZ=Asia/Shanghai`。`llm.py` 用 `datetime.now()` 推算
   「明天 / 下周六」，容器默认 UTC 会每天有 8 小时把日期算错。
3. **密钥不进镜像**：`.env` 被 `.dockerignore` 排除，只在运行时注入。
   注意 `rollinggo_book.py` 用 `load_dotenv(override=True)`，若你把源码目录 bind mount 进容器，
   目录里的 `.env` 会反过来覆盖 compose 的环境变量。
4. **出口网络**：容器需要能访问 `dashscope.aliyuncs.com`、`mcp.rollinggo.cn`、
   `restapi.amap.com`、携程图片 CDN。本机实测三者均可达（CLI 直连，未给 Docker 单独配代理）。
5. **反代部署**：容器已带 `--proxy-headers`；放在 Nginx / Caddy 后面时记得转发
   `X-Forwarded-For`，SSE 接口 `/api/chat/stream` 还需关闭响应缓冲（Nginx `proxy_buffering off`）。
6. 镜像只暴露 Web 入口。两个 CLI 入口仍可在容器里跑：
   `docker compose run --rm web python main.py`（需 `-it` 才有交互）。
7. Docker Desktop 当前未设开机自启（`AutoStart=false`）。需要的话在
   Settings → General 勾选 **Start Docker Desktop when you sign in to your computer**。
