#!/bin/bash
# 设置 launchd 定时任务
# 任务1: 每个工作日 08:30 — 开盘前 ETF 信号系统
# 任务2: 每个工作日 9:25/9:45/14:45 — 尾盘/早盘择时扫描
# 任务3: 每个工作日 09:25→14:50（每5分钟共66次）— 操作指导推送
# 任务4: 每个工作日 09:45/10:45/11:45/12:45/13:45/14:45 — 个股研究池择时
# 任务5: 每个工作日 09:25/15:10 — 宽基指数择时（开盘前+收盘后）
# 使用方法：chmod +x setup_scheduler.sh && ./setup_scheduler.sh

export SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
export PYTHON_BIN="$(which python3)"
export LOG_DIR="${SCRIPT_DIR}/logs"
mkdir -p "$LOG_DIR"

# ── 任务1：开盘前信号（08:30） ────────────────────────────────────────────────
LABEL_AM="com.claudetrade.signal"
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
        <string>${SCRIPT_DIR}/main.py</string>
    </array>
    <key>StartCalendarInterval</key>
    <dict>
        <key>Hour</key><integer>8</integer>
        <key>Minute</key><integer>30</integer>
    </dict>
    <key>WorkingDirectory</key>
    <string>${SCRIPT_DIR}</string>
    <key>StandardOutPath</key>
    <string>${LOG_DIR}/launchd_am_stdout.log</string>
    <key>StandardErrorPath</key>
    <string>${LOG_DIR}/launchd_am_stderr.log</string>
    <key>RunAtLoad</key>
    <false/>
</dict>
</plist>
EOF

launchctl unload "$PLIST_AM" 2>/dev/null
launchctl load "$PLIST_AM"
echo "✅ 开盘信号任务已加载（每日 08:30）"

# ── 任务2：尾盘择时（14:45） ─────────────────────────────────────────────────
LABEL_TAIL="com.claudetrade.tail"
PLIST_TAIL="$HOME/Library/LaunchAgents/${LABEL_TAIL}.plist"

cat > "$PLIST_TAIL" << EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>${LABEL_TAIL}</string>
    <key>ProgramArguments</key>
    <array>
        <string>${PYTHON_BIN}</string>
        <string>${SCRIPT_DIR}/tail_main.py</string>
    </array>
    <key>StartCalendarInterval</key>
    <array>
        <dict><key>Hour</key><integer>9</integer><key>Minute</key><integer>25</integer></dict>
        <dict><key>Hour</key><integer>9</integer><key>Minute</key><integer>45</integer></dict>
        <dict><key>Hour</key><integer>14</integer><key>Minute</key><integer>45</integer></dict>
    </array>
    <key>WorkingDirectory</key>
    <string>${SCRIPT_DIR}</string>
    <key>StandardOutPath</key>
    <string>${LOG_DIR}/launchd_tail_stdout.log</string>
    <key>StandardErrorPath</key>
    <string>${LOG_DIR}/launchd_tail_stderr.log</string>
    <key>RunAtLoad</key>
    <false/>
</dict>
</plist>
EOF

launchctl unload "$PLIST_TAIL" 2>/dev/null
launchctl load "$PLIST_TAIL"
echo "✅ 尾盘/早盘买点任务已加载（每日 9:25/9:45/14:45）"

# ── 任务3：每5分钟操作指导（09:25 → 14:50，共66次） ─────────────────────────
LABEL_GUIDANCE="com.claudetrade.guidance"
PLIST_GUIDANCE="$HOME/Library/LaunchAgents/${LABEL_GUIDANCE}.plist"

python3 - << 'PYEOF'
import os, sys
script_dir = os.environ.get("SCRIPT_DIR", os.path.dirname(os.path.abspath(__file__)))
python_bin = os.environ.get("PYTHON_BIN", sys.executable)
log_dir = os.environ.get("LOG_DIR", os.path.join(script_dir, "logs"))
label = "com.claudetrade.guidance"
plist_path = os.path.expanduser(f"~/Library/LaunchAgents/{label}.plist")

entries = []
for h in range(9, 15):
    for m in range(0, 60, 5):
        if h == 9 and m < 25: continue
        if h == 14 and m > 50: continue
        entries.append(f"        <dict><key>Hour</key><integer>{h}</integer><key>Minute</key><integer>{m}</integer></dict>")

content = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>{label}</string>
    <key>ProgramArguments</key>
    <array>
        <string>{python_bin}</string>
        <string>{script_dir}/daily_guidance.py</string>
    </array>
    <key>StartCalendarInterval</key>
    <array>
{chr(10).join(entries)}
    </array>
    <key>WorkingDirectory</key>
    <string>{script_dir}</string>
    <key>StandardOutPath</key>
    <string>{log_dir}/launchd_guidance_stdout.log</string>
    <key>StandardErrorPath</key>
    <string>{log_dir}/launchd_guidance_stderr.log</string>
    <key>RunAtLoad</key>
    <false/>
</dict>
</plist>"""

with open(plist_path, "w") as f:
    f.write(content)
print(f"Written {len(entries)} time entries to {plist_path}")
PYEOF

launchctl unload "$PLIST_GUIDANCE" 2>/dev/null
launchctl load "$PLIST_GUIDANCE"
echo "✅ 每5分钟操作指导已加载（09:25 → 14:50，共66次/天）"

# ── 任务4：个股研究池择时信号（15:10） ──────────────────────────────────────
LABEL_STOCK="com.claudetrade.stock"
PLIST_STOCK="$HOME/Library/LaunchAgents/${LABEL_STOCK}.plist"

cat > "$PLIST_STOCK" << EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>${LABEL_STOCK}</string>
    <key>ProgramArguments</key>
    <array>
        <string>${PYTHON_BIN}</string>
        <string>${SCRIPT_DIR}/stock_timing.py</string>
    </array>
    <key>StartCalendarInterval</key>
    <array>
        <dict><key>Hour</key><integer>9</integer><key>Minute</key><integer>45</integer></dict>
        <dict><key>Hour</key><integer>10</integer><key>Minute</key><integer>45</integer></dict>
        <dict><key>Hour</key><integer>11</integer><key>Minute</key><integer>45</integer></dict>
        <dict><key>Hour</key><integer>12</integer><key>Minute</key><integer>45</integer></dict>
        <dict><key>Hour</key><integer>13</integer><key>Minute</key><integer>45</integer></dict>
        <dict><key>Hour</key><integer>14</integer><key>Minute</key><integer>45</integer></dict>
    </array>
    <key>WorkingDirectory</key>
    <string>${SCRIPT_DIR}</string>
    <key>StandardOutPath</key>
    <string>${LOG_DIR}/launchd_stock_stdout.log</string>
    <key>StandardErrorPath</key>
    <string>${LOG_DIR}/launchd_stock_stderr.log</string>
    <key>RunAtLoad</key>
    <false/>
</dict>
</plist>
EOF

launchctl unload "$PLIST_STOCK" 2>/dev/null
launchctl load "$PLIST_STOCK"
echo "✅ 个股择时任务已加载（每日 09:45/10:45/11:45/12:45/13:45/14:45）"

# ── 任务5：指数ETF择时（09:25 → 14:50，每10分钟）─────────────────────────────
LABEL_INDEX="com.claudetrade.index"
PLIST_INDEX="$HOME/Library/LaunchAgents/${LABEL_INDEX}.plist"

python3 - << 'PYEOF'
import os, sys
script_dir = os.environ.get("SCRIPT_DIR", os.path.dirname(os.path.abspath(__file__)))
python_bin = os.environ.get("PYTHON_BIN", sys.executable)
log_dir = os.environ.get("LOG_DIR", os.path.join(script_dir, "logs"))
label = "com.claudetrade.index"
plist_path = os.path.expanduser(f"~/Library/LaunchAgents/{label}.plist")

entries = []
for h in range(9, 15):
    for m in range(0, 60, 10):
        if h == 9 and m < 25: continue
        if h == 14 and m > 50: continue
        entries.append(f"        <dict><key>Hour</key><integer>{h}</integer><key>Minute</key><integer>{m}</integer></dict>")
# 14:50 需要单独加入（14:50不在10分钟间隔上，但14:50是收盘前最后一次）
if not any("50" in e and "14" in e for e in entries):
    entries.append(f"        <dict><key>Hour</key><integer>14</integer><key>Minute</key><integer>50</integer></dict>")

content = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>{label}</string>
    <key>ProgramArguments</key>
    <array>
        <string>{python_bin}</string>
        <string>{script_dir}/index_timing.py</string>
    </array>
    <key>StartCalendarInterval</key>
    <array>
{chr(10).join(entries)}
    </array>
    <key>WorkingDirectory</key>
    <string>{script_dir}</string>
    <key>StandardOutPath</key>
    <string>{log_dir}/launchd_index_stdout.log</string>
    <key>StandardErrorPath</key>
    <string>{log_dir}/launchd_index_stderr.log</string>
    <key>RunAtLoad</key>
    <false/>
</dict>
</plist>"""

with open(plist_path, "w") as f:
    f.write(content)
print(f"Written {len(entries)} time entries to {plist_path}")
PYEOF

launchctl unload "$PLIST_INDEX" 2>/dev/null
launchctl load "$PLIST_INDEX"
echo "✅ 指数ETF择时任务已加载（09:25→14:50，每10分钟，约33次/天）"

echo ""
echo "定时任务状态："
launchctl list | grep claudetrade
echo ""
echo "常用命令："
echo "  手动运行开盘信号：  python3 ${SCRIPT_DIR}/main.py"
echo "  手动运行每小时指导：python3 ${SCRIPT_DIR}/daily_guidance.py"
echo "  手动运行尾盘扫描：  python3 ${SCRIPT_DIR}/tail_main.py"
echo "  手动运行个股择时：  python3 ${SCRIPT_DIR}/stock_timing.py"
echo "  手动运行宽基择时：  python3 ${SCRIPT_DIR}/index_timing.py"
echo "  个股择时（仅打印）：python3 ${SCRIPT_DIR}/stock_timing.py --dry"
echo "  查看开盘日志：      tail -f ${LOG_DIR}/signal_$(date +%Y%m).log"
echo "  查看尾盘日志：      tail -f ${LOG_DIR}/tail_$(date +%Y%m).log"
echo "  查看指导日志：      tail -f ${LOG_DIR}/launchd_guidance_stdout.log"
echo "  查看个股日志：      tail -f ${LOG_DIR}/stock_timing_$(date +%Y%m).log"
echo "  查看指数日志：      tail -f ${LOG_DIR}/launchd_index_stdout.log"
echo "  卸载开盘任务：      launchctl unload $PLIST_AM"
echo "  卸载尾盘任务：      launchctl unload $PLIST_TAIL"
echo "  卸载指导任务：      launchctl unload $PLIST_GUIDANCE"
echo "  卸载个股任务：      launchctl unload $PLIST_STOCK"
echo "  卸载指数任务：      launchctl unload $PLIST_INDEX"
