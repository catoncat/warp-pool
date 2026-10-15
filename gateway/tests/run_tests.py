#!/usr/bin/env python3
"""warp-pool 测试运行器: 单元 → 集成 → 基准(可选)。

用法:
    python3 tests/run_tests.py                 # 单元 + 集成(生产网关)
    python3 tests/run_tests.py --no-live       # 只跑单元(无网络)
    python3 tests/run_tests.py --bench         # 单元 + 集成 + 基准
    python3 tests/run_tests.py --bench-only    # 只跑基准
"""
import argparse
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))

def run(cmd, label):
    print("\n" + "=" * 60)
    print("▶ %s" % label)
    print("=" * 60)
    r = subprocess.run(cmd, cwd=os.path.join(HERE, "..", ".."))  # 仓库根 = tests/../..
    return r.returncode

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-live", action="store_true", help="跳过集成测试(无网络)")
    ap.add_argument("--bench", action="store_true", help="跑基准测试")
    ap.add_argument("--bench-only", action="store_true", help="只跑基准")
    ap.add_argument("--gateway", default="http://127.0.0.1:10900")
    ap.add_argument("--key", default=None)
    args = ap.parse_args()

    rc = 0
    if not args.bench_only:
        rc |= run([sys.executable, "-m", "unittest", "discover", "-s", "gateway/tests",
                   "-p", "test_nodepool.py"], "单元测试(NodePool/连接池/指纹)")
        if not args.no_live:
            cmd = [sys.executable, "gateway/tests/test_live.py", "--gateway", args.gateway]
            if args.key:
                cmd += ["--key", args.key]
            rc |= run(cmd, "集成测试(真实网关 %s)" % args.gateway)
    if args.bench or args.bench_only:
        cmd = [sys.executable, "gateway/tests/benchmark.py", "--gateway", args.gateway]
        if args.key:
            cmd += ["--key", args.key]
        rc |= run(cmd, "基准测试(真实网关 %s)" % args.gateway)

    print("\n%s 测试结果: %s" % ("所有" if not args.bench_only else "基准",
          "全部通过 ✅" if rc == 0 else "有失败 ❌ (exit=%d)" % rc))
    sys.exit(rc)

if __name__ == "__main__":
    main()
