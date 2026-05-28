#!/bin/bash
# 市场资金动向周报：每周五 15:30 运行

export SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
export PYTHON_BIN="$(which python3)"
export LOG_DIR="${SCRIPT_DIR}/logs"
mkdir -p "$LOG_DIR"

LABEL="com.claudetrade.market_capital_report"
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
        <string>${SCRIPT_DIR}/market_capital_report.py</string>
    </array>
    <key>StartCalendarInterval</key>
    <dict>
        <key>Weekday</key><integer>5</integer>
        <key>Hour</key><integer>15</integer>
        <key>Minute</key><integer>30</integer>
    </dict>
    <key>WorkingDirectory</key>
    <string>${SCRIPT_DIR}</string>
    <key>StandardOutPath</key>
    <string>${LOG_DIR}/launchd_market_report_stdout.log</string>
    <key>StandardErrorPath</key>
    <string>${LOG_DIR}/launchd_market_report_stderr.log</string>
    <key>RunAtLoad</key>
    <false/>
</dict>
</plist>
EOF

launchctl unload "$PLIST" 2>/dev/null
launchctl load "$PLIST"
echo "✅ 市场资金动向周报已加载（每周五 15:30 自动推送）"
