#!/bin/bash
# Управление фоновым автозапуском бота через launchd (macOS).
# Бот работает в фоне, сам стартует при входе в систему и сам перезапускается.
#
#   bash service.sh install   — установить и запустить в фоне
#   bash service.sh stop      — остановить
#   bash service.sh restart   — перезапустить
#   bash service.sh logs      — показать логи
#   bash service.sh status    — статус

set -e

DIR="$(cd "$(dirname "$0")" && pwd)"
LABEL="com.reelsbot.agent"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
LOG="$DIR/data/bot.log"
UID_NUM="$(id -u)"

mkdir -p "$DIR/data"

install_service() {
  cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>$LABEL</string>
    <key>ProgramArguments</key>
    <array>
        <string>/bin/bash</string>
        <string>$DIR/run.sh</string>
    </array>
    <key>WorkingDirectory</key>
    <string>$DIR</string>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>$LOG</string>
    <key>StandardErrorPath</key>
    <string>$LOG</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>/opt/homebrew/opt/ffmpeg-full/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin</string>
    </dict>
</dict>
</plist>
EOF
  launchctl bootout "gui/$UID_NUM/$LABEL" 2>/dev/null || true
  launchctl bootstrap "gui/$UID_NUM" "$PLIST"
  launchctl enable "gui/$UID_NUM/$LABEL"
  echo "✅ Бот установлен и запущен в фоне."
  echo "Логи: bash service.sh logs"
}

stop_service() {
  launchctl bootout "gui/$UID_NUM/$LABEL" 2>/dev/null || true
  echo "🛑 Бот остановлен."
}

case "$1" in
  install) install_service ;;
  stop) stop_service ;;
  restart) stop_service; sleep 1; install_service ;;
  logs) touch "$LOG"; tail -n 100 -f "$LOG" ;;
  status)
    if launchctl print "gui/$UID_NUM/$LABEL" >/dev/null 2>&1; then
      echo "🟢 Бот работает в фоне."
    else
      echo "🔴 Бот не запущен. Установить: bash service.sh install"
    fi
    ;;
  *)
    echo "Использование: bash service.sh {install|stop|restart|logs|status}"
    ;;
esac
