#!/usr/bin/env bash
# 开机/启动前拉取最新代码。失败不阻塞启动。
set -u
cd /home/dreamldx/Projects/conic || exit 1
git pull --ff-only origin master >/dev/null 2>&1
# 依赖若有变化则同步 venv（失败不阻塞）
.venv/bin/pip install -e . -q >/dev/null 2>&1 || true
exit 0
