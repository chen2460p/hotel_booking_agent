# -*- coding: utf-8 -*-
"""部署前自检：30 秒内确认环境是否可用。

检查项：
  1. docker 命令 / 守护进程是否可用（Docker Desktop 没启动是最常见翻车原因）
  2. 容器是否存在、状态是否 running、healthcheck 是否 healthy
  3. HTTP 服务是否真的能访问（端口映射、防火墙）
  4. `.env` 关键 Key 是否已配置（只显示「已配置/缺失」，不打印值）
  5. 容器能否访问外部依赖（DashScope / 道旅 / 高德）
  6. 端口是否被别的进程占用

用法：
    python scripts/preflight.py
    python scripts/preflight.py --base http://127.0.0.1:8000 --skip-egress
"""

import argparse
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request

OK, WARN, BAD = "[ OK ]", "[WARN]", "[FAIL]"
rows = []


def add(status, item, detail=""):
    rows.append((status, item, detail))
    print(f"{status} {item}" + (f"  — {detail}" if detail else ""))


def run(cmd, timeout=15):
    """执行命令，返回 (returncode, stdout+stderr)。不抛异常。"""
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                           encoding="utf-8", errors="replace")
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except FileNotFoundError:
        return 127, "命令不存在"
    except subprocess.TimeoutExpired:
        return 124, f"超时（{timeout}s）"


def check_docker():
    code, out = run(["docker", "version", "--format", "{{.Server.Version}}"])
    if code == 0 and out.strip():
        add(OK, "docker 守护进程", f"server {out.strip()}")
        return True
    if "命令不存在" in out:
        add(BAD, "docker 命令", "未安装或不在 PATH；新开一个终端再试")
    else:
        add(BAD, "docker 守护进程", "连不上，先打开 Docker Desktop 等鲸鱼图标变绿")
    return False


def check_container(compose_dir):
    code, out = run(["docker", "compose", "ps", "--format", "json"], timeout=20)
    names = []
    if code == 0 and out.strip():
        for line in out.splitlines():
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            names.append((d.get("Name"), d.get("State"), d.get("Health") or ""))
    if not names:
        add(BAD, "容器", "没有找到本项目容器，先 `docker compose up -d`")
        return False
    ok = True
    for name, state, health in names:
        if state == "running" and health in ("healthy", ""):
            add(OK, f"容器 {name}", f"{state}/{health or '-'}")
        else:
            add(WARN if state == "running" else BAD, f"容器 {name}", f"{state}/{health or '-'}")
            ok = ok and state == "running"
    return ok


def check_http(base):
    try:
        with urllib.request.urlopen(f"{base}/api/health", timeout=8) as r:
            d = json.loads(r.read().decode())
        add(OK, f"HTTP {base}", f"backend={d.get('storage_backend')} "
                                f"logged_in={d.get('rollinggo_logged_in')}")
        return True
    except Exception as e:
        add(BAD, f"HTTP {base}", f"{type(e).__name__}: {e}")
        return False


def check_env_keys(env_path):
    keys = ["DASHSCOPE_API_KEY", "ROLLINGGO_API_KEY", "AMAP_API_KEY"]
    if not os.path.exists(env_path):
        add(BAD, ".env", f"找不到 {env_path}（容器会因为缺 Key 走降级模式）")
        return False
    vals = {}
    with open(env_path, encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            vals[k.strip()] = v.strip()
    for k in keys:
        v = vals.get(k, "")
        if v:
            add(OK, f"Key {k}", f"已配置（{len(v)} 字符）")
        else:
            add(WARN, f"Key {k}", "为空 → 该能力会降级")
    return bool(vals.get("DASHSCOPE_API_KEY"))


def check_egress():
    script = (
        "import urllib.request as u, urllib.error as ue\n"
        "for x in ['https://dashscope.aliyuncs.com','https://mcp.rollinggo.cn','https://restapi.amap.com']:\n"
        "    try:\n"
        "        u.urlopen(x, timeout=8); print('OK', x)\n"
        "    except ue.HTTPError:\n"
        "        print('OK', x)          # 4xx/5xx 说明服务可达，只是根路径不给内容\n"
        "    except Exception as e:\n"
        "        print('ERR', x, type(e).__name__)\n"
    )
    code, out = run(["docker", "compose", "exec", "-T", "web", "python", "-c", script], timeout=40)
    if code != 0:
        add(WARN, "容器出网", "跳过（exec 失败或容器未运行）")
        return
    for line in out.splitlines():
        if line.startswith("OK "):
            add(OK, "出网 " + line[3:], "")
        elif line.startswith("ERR "):
            add(WARN, "出网 " + line[4:], "不可达 → 该源会走降级")


def check_port(port):
    import socket
    s = socket.socket()
    s.settimeout(1.5)
    try:
        s.connect(("127.0.0.1", port))
        add(OK, f"端口 {port}", "已在监听（若是你自己启动的服务则正常）")
    except OSError:
        add(WARN, f"端口 {port}", "未监听")
    finally:
        s.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8000")
    ap.add_argument("--skip-egress", action="store_true")
    ap.add_argument("--env", default=os.path.join("hotel_booking_agent", ".env"))
    args = ap.parse_args()
    base = args.base.rstrip("/")

    print("=" * 64)
    print("部署前自检")
    print("=" * 64)

    docker_ok = check_docker()
    if docker_ok:
        check_container(".")
        check_http(base)
        check_env_keys(args.env)
        if not args.skip_egress:
            check_egress()
        port = base.rsplit(":", 1)[-1].split("/")[0]
        if port.isdigit():
            check_port(int(port))

    bad = [r for r in rows if r[0] == BAD]
    warn = [r for r in rows if r[0] == WARN]
    print("-" * 64)
    print(f"{len(rows) - len(bad) - len(warn)} ok, {len(warn)} warn, {len(bad)} fail")
    if bad:
        print("\n有 FAIL 项，先修好再上场：")
        for _, item, detail in bad:
            print(f"  - {item}: {detail}")
        return 1
    if warn:
        print("\n有 WARN 项：能用，但要知道当前处于降级模式。")
    print("\n可以启动：http://127.0.0.1:8000")
    return 0


if __name__ == "__main__":
    sys.exit(main())
