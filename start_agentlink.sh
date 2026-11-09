#!/bin/sh
# AgentLink 管理面板的 Linux / macOS / 类 Unix 启动入口。
# 用法：./start_agentlink.sh [参数…]     例如 ./start_agentlink.sh --no-browser
# 从文件管理器“作为程序运行”也可以：脚本始终从自身所在目录启动，不依赖当前工作目录。
set -u

script_dir() {
    # 跟随符号链接：把脚本软链到 ~/bin 之类的目录后仍然从项目根目录启动。
    target=$1
    while [ -L "$target" ]; do
        dir=$(CDPATH= cd -- "$(dirname -- "$target")" 2>/dev/null && pwd -P) || return 1
        link=$(readlink "$target" 2>/dev/null) || return 1
        case $link in
            /*) target=$link ;;
            *) target=$dir/$link ;;
        esac
    done
    CDPATH= cd -- "$(dirname -- "$target")" 2>/dev/null && pwd -P
}

PROJECT_DIR=$(script_dir "$0") || PROJECT_DIR=""
ENTRY="$PROJECT_DIR/agentlink.py"

pause_on_failure() {
    # 无终端启动时不要让报错一闪而过：交互式终端里等一次回车；有桌面对话框工具就地弹窗；
    # 脚本、CI、管道运行时什么都不做（AGENTLINK_NO_PAUSE=1 也可以强制跳过等待）。
    if [ -t 0 ] && [ "${AGENTLINK_NO_PAUSE:-0}" != "1" ]; then
        printf '\n按回车键关闭此窗口……'
        read -r _ || true
    elif command -v zenity >/dev/null 2>&1; then
        zenity --error --title=AgentLink --text="$1" >/dev/null 2>&1 || true
    elif command -v xmessage >/dev/null 2>&1; then
        xmessage -center "AgentLink: $1" >/dev/null 2>&1 || true
    fi
}

fail() {
    printf '[AgentLink] %s\n' "$1" >&2
    pause_on_failure "$1"
    exit 1
}

if [ -z "$PROJECT_DIR" ]; then
    fail "无法定位脚本所在目录。
请从项目根目录运行 start_agentlink.sh，或把它放在项目根目录（也可以软链到 PATH 目录）。"
fi

if [ ! -f "$ENTRY" ]; then
    fail "未找到入口文件：$ENTRY
请确认 start_agentlink.sh 位于 AgentLink 项目根目录。"
fi

# 优先 python3，其次 python；需要 Python 3.8 或更高（项目只用标准库）。
PYTHON=""
for candidate in python3 python; do
    if command -v "$candidate" >/dev/null 2>&1 \
            && "$candidate" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 8) else 1)' >/dev/null 2>&1; then
        PYTHON="$candidate"
        break
    fi
done

if [ -z "$PYTHON" ]; then
    fail "未找到可用的 Python 3.8 或更高版本。
请先安装 python3（例如 sudo apt install python3 或 sudo dnf install python3），再运行本脚本。"
fi

cd "$PROJECT_DIR" || fail "无法进入项目目录：$PROJECT_DIR"

# 面板是常驻进程：前台运行，Ctrl+C 退出；参数原样传给 agentlink.py。
"$PYTHON" -I "$ENTRY" panel "$@"
STATUS=$?

if [ "$STATUS" -ne 0 ]; then
    printf '\n[AgentLink] 管理面板启动失败，退出码 %s。\n' "$STATUS" >&2
    pause_on_failure "管理面板启动失败，退出码 $STATUS。"
fi
exit "$STATUS"