#!/bin/bash
# 模拟盘定时任务：早盘 09:35 + 尾盘 15:15 各运行一次
# 早盘：执行 signal_detail + stock_timing 的买卖信号
# 尾盘：执行 tail_detail 尾盘买点 + 推送三账户日报

export SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
export PYTHON_BIN="$(which python3)"
export LOG_DIR="${SCRIPT_DIR}/logs"
mkdir -p "$LOG_DIR"

# ── 早盘任务（09:35） ──
LABEL_AM="com.claudetrade.paper_trader.morning"
PLIST_AM="$HOME/Library/LaunchAgents/${LABEL_AM}.plist"

cat > "$PLIST_AM" << EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>${LABEL_AM}</string>
    <key>ProgramArguments</key>
    <array>
        <string>${PYTHON_BIN}</string>
        <string>${SCRIPT_DIR}/paper_trader.py</string>
        <string>--session</string>
        <string>morning</string>
    </array>
    <key>StartCalendarInterval</key>
    <dict>
        <key>Hour</key><integer>9</integer>
        <key>Minute</key><integer>35</integer>
    </dict>
    <key>WorkingDirectory</key>
    <string>${SCRIPT_DIR}</string>
    <key>StandardOutPath</key>
    <string>${LOG_DIR}/launchd_paper_morning_stdout.log</string>
    <key>StandardErrorPath</key>
    <string>${LOG_DIR}/launchd_paper_morning_stderr.log</string>
    <key>RunAtLoad</key>
    <false/>
</dict>
</plist>
EOF

launchctl unload "$PLIST_AM" 2>/dev/null
launchctl load "$PLIST_AM"
echo "✅ 模拟盘早盘任务已加载（09:35 执行早盘择时信号）"

# ── 尾盘任务（15:15） ──
LABEL_PM="com.claudetrade.paper_trader.afternoon"
PLIST_PM="$HOME/Library/LaunchAgents/${LABEL_PM}.plist"

cat > "$PLIST_PM" << EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>${LABEL_PM}</string>
    <key>ProgramArguments</key>
    <array>
        <string>${PYTHON_BIN}</string>
        <string>${SCRIPT_DIR}/paper_trader.py</string>
        <string>--session</string>
        <string>afternoon</string>
    </array>
    <key>StartCalendarInterval</key>
    <dict>
        <key>Hour</key><integer>15</integer>
        <key>Minute</key><integer>15</integer>
    </dict>
    <key>WorkingDirectory</key>
    <string>${SCRIPT_DIR}</string>
    <key>StandardOutPath</key>
    <string>${LOG_DIR}/launchd_paper_afternoon_stdout.log</string>
    <key>StandardErrorPath</key>
    <string>${LOG_DIR}/launchd_paper_afternoon_stderr.log</string>
    <key>RunAtLoad</key>
    <false/>
</dict>
</plist>
EOF

launchctl unload "$PLIST_PM" 2>/dev/null
launchctl load "$PLIST_PM"
echo "✅ 模拟盘尾盘任务已加载（15:15 执行尾盘信号 + 推送日报）"

# 清理旧的单一任务
OLD_LABEL="com.claudetrade.paper_trader"
OLD_PLIST="$HOME/Library/LaunchAgents/${OLD_LABEL}.plist"
if [ -f "$OLD_PLIST" ]; then
    launchctl unload "$OLD_PLIST" 2>/dev/null
    rm -f "$OLD_PLIST"
    echo "🗑️  已清理旧单次任务"
fi
