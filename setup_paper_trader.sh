#!/bin/bash
# 设置模拟盘定时任务：每个工作日 15:15（收盘后15分钟）运行

export SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
export PYTHON_BIN="$(which python3)"
export LOG_DIR="${SCRIPT_DIR}/logs"
mkdir -p "$LOG_DIR"

LABEL="com.claudetrade.paper_trader"
PLIST="$HOME/Library/LaunchAgents/${LABEL}.plist"

cat > "$PLIST" << EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>${LABEL}</string>
    <key>ProgramArguments</key>
    <array>
        <string>${PYTHON_BIN}</string>
        <string>${SCRIPT_DIR}/paper_trader.py</string>
    </array>
    <key>StartCalendarInterval</key>
    <dict>
        <key>Hour</key><integer>15</integer>
        <key>Minute</key><integer>15</integer>
    </dict>
    <key>WorkingDirectory</key>
    <string>${SCRIPT_DIR}</string>
    <key>StandardOutPath</key>
    <string>${LOG_DIR}/launchd_paper_trader_stdout.log</string>
    <key>StandardErrorPath</key>
    <string>${LOG_DIR}/launchd_paper_trader_stderr.log</string>
    <key>RunAtLoad</key>
    <false/>
</dict>
</plist>
EOF

launchctl unload "$PLIST" 2>/dev/null
launchctl load "$PLIST"
echo "✅ 模拟盘定时任务已加载（每个工作日 15:15 收盘后运行，推送飞书日报）"
