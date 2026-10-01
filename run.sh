#!/usr/bin/env bash
# 一条本地启动命令：重置数据库并生成续时冲突、少件与换桌样例，然后启动服务。
set -euo pipefail
cd "$(dirname "$0")"
exec python3 -m app.main --reset "$@"
