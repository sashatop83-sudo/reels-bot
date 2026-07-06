#!/bin/bash
# Запуск бота с авто-перезапуском при изменении кода и при падении.
cd "$(dirname "$0")" || exit 1

# venv
if [ -d ".venv" ]; then
  # shellcheck disable=SC1091
  source .venv/bin/activate
fi

# ffmpeg-full в PATH (на всякий случай)
export PATH="/opt/homebrew/opt/ffmpeg-full/bin:/usr/local/opt/ffmpeg-full/bin:$PATH"

# watchfiles сам перезапускает бота при изменении файлов в bot/
# и держит его живым. Если watchfiles нет — обычный цикл перезапуска.
if python -c "import watchfiles" 2>/dev/null; then
  exec watchfiles "python -m bot.main" bot
else
  while true; do
    python -m bot.main
    echo "Бот остановился. Перезапуск через 3 сек…"
    sleep 3
  done
fi
