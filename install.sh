#!/bin/sh
# Install sub2cli, sub2cli-redeem, sub2cli-inject to ~/.local/bin
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
DEST_DIR="${HOME}/.local/bin"

mkdir -p "$DEST_DIR"

for bin in sub2cli sub2cli-redeem sub2cli-inject; do
  src="$SCRIPT_DIR/$bin"
  if [ ! -f "$src" ]; then
    echo "错误: 缺少 $src" >&2
    exit 1
  fi
  cp "$src" "$DEST_DIR/$bin"
  chmod 755 "$DEST_DIR/$bin"
  echo "已安装: $DEST_DIR/$bin"
done

echo ""
echo "Python 依赖 (sub2cli 主体 / sub2cli-redeem 需要):"
echo "  pip3 install --user requests websocket-client"
echo "  sub2cli-inject 无第三方依赖 (纯 stdlib)"

case ":$PATH:" in
  *":$DEST_DIR:"*) ;;
  *)
    echo ""
    echo "$DEST_DIR 不在 PATH; 添加到 shell profile:"
    echo "  export PATH=\"\$HOME/.local/bin:\$PATH\""
    ;;
esac

echo ""
echo "运行: sub2cli"
