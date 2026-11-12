#!/bin/sh
set -eu
cd "$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
if [ ! -x .bootstrap/bin/uv ]; then
  python3 -m pip install --target .bootstrap 'uv==0.13.7'
fi
.bootstrap/bin/uv sync --frozen --python 3.11
echo '安装完成。运行 ./scut login 登录，然后 ./scut courses 查看课程。'
