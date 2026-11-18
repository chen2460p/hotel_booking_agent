# -*- coding: utf-8 -*-
"""离线单测总入口：一条命令跑完 tests/ 下所有 *_unit.py。

用法：
    python tests/run_all.py
    python tests/run_all.py --verbose      # 失败时也打印完整输出

设计约定：
- 只跑离线单测（不联网、不写 orders.db）；test_api_smoke.py 需要先起服务，故意排除。
- 每个文件用独立进程跑，互不污染全局状态；任一失败则整体退出码非 0。
- 新增测试文件命名为 tests/test_<主题>_unit.py，会被自动发现。
"""

import argparse
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

PROJ = Path(__file__).resolve().parent.parent
TESTS = PROJ / "tests"


def cleanup_leftovers() -> None:
    """清掉上次因 SQLite 句柄未释放而残留的测试临时目录（Windows 常见）。"""
    for path in PROJ.glob("hb_order_time_tmp_*"):
        if path.is_dir():
            shutil.rmtree(path, ignore_errors=True)


def discover():
    return sorted(p for p in TESTS.glob("test_*_unit.py") if p.is_file())


def run_one(path: Path, verbose: bool):
    env = dict(os.environ)
    env["PYTHONUTF8"] = "1"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    proc = subprocess.run(
        [sys.executable, "-B", str(path)],
        cwd=str(PROJ), capture_output=True, text=True, encoding="utf-8",
        errors="replace", timeout=300, env=env,
    )
    output = (proc.stdout or "") + (proc.stderr or "")
    lines = [ln for ln in output.splitlines() if ln.strip()]
    summary = "(无输出)"
    for line in reversed(lines):
        if re.search(r"\d+ passed|failed|^OK$|^Ran \d+", line.strip()):
            summary = line.strip()
            break
    ok = proc.returncode == 0
    print(f"{'PASS' if ok else 'FAIL'}  {path.name:<40s} {summary[:80]}")
    if not ok and not verbose:
        print("\n".join(lines[-12:]))
    if verbose:
        print(output)
    return ok


def main(argv=None):
    parser = argparse.ArgumentParser(description="离线单测总入口")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)

    cleanup_leftovers()
    files = discover()
    if not files:
        print("没有发现 tests/test_*_unit.py")
        return 1

    print(f"共 {len(files)} 个离线测试文件（不含需要服务的 test_api_smoke.py）\n")
    failed = [p.name for p in files if not run_one(p, args.verbose)]
    print(f"\n{len(files) - len(failed)} passed, {len(failed)} failed")
    if failed:
        print("失败文件：" + "、".join(failed))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
