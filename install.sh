#!/bin/sh
# GuardUp 安装脚本 — 一条命令接入
#   curl -fsSL https://guard.7zi.com/install.sh | sh
# 零依赖：只需要 Python 3.8+。DNS 凭据留在本机，不上传。
set -eu

BASE="${GUARDUP_BASE:-https://guard.7zi.com}"
PREFIX="${GUARDUP_PREFIX:-$HOME/.local/bin}"

info() { printf '\033[36m[GuardUp]\033[0m %s\n' "$*"; }
err()  { printf '\033[31m[GuardUp]\033[0m %s\n' "$*" >&2; }

# 1. Python 检查
if command -v python3 >/dev/null 2>&1; then PY=python3
elif command -v python >/dev/null 2>&1; then PY=python
else err "需要 Python 3.8+，请先安装。"; exit 1; fi
VER=$("$PY" -c 'import sys;print("%d%d"%sys.version_info[:2])')
if [ "$VER" -lt 38 ]; then err "Python 版本过低，需要 3.8+。"; exit 1; fi
info "Python 检查通过"

# 2. 下载单文件 Agent（唯一需要的文件）
info "下载 GuardUp 到 $PREFIX/guardup"
mkdir -p "$PREFIX"
curl -fsSL "$BASE/guardup.py" -o "$PREFIX/guardup"
chmod +x "$PREFIX/guardup"
info "已安装: $PREFIX/guardup"

# 3. PATH 提示
case ":$PATH:" in
  *":$PREFIX:"*) ;;
  *) info "提示：把 $PREFIX 加入 PATH（或直接运行 $PREFIX/guardup）" ;;
esac

info ""
info "下一步（3 步接入，约 2 分钟）:"
info "  1) guardup init     自动识别 DNS 服务商，3 个问题接入"
info "  2) guardup drill    演练一次（只读，不改 DNS）"
info "  3) guardup daemon   启动常驻巡检（或用 systemd）"
