#!/bin/bash
set -e
cd "$(dirname "$0")"

# Local Bot API — снимает лимит 20 MB (нужны TELEGRAM_API_ID + TELEGRAM_API_HASH)
if [ -n "$TELEGRAM_API_ID" ] && [ -n "$TELEGRAM_API_HASH" ] && [ -x /usr/local/bin/telegram-bot-api ]; then
  echo "Starting Local Telegram Bot API..."
  mkdir -p /tmp/tg-bot-api
  /usr/local/bin/telegram-bot-api \
    --api-id="$TELEGRAM_API_ID" \
    --api-hash="$TELEGRAM_API_HASH" \
    --local \
    --http-port=8081 \
    --dir=/tmp/tg-bot-api \
    --verbosity=1 &
  export TELEGRAM_API_URL="${TELEGRAM_API_URL:-http://127.0.0.1:8081}"
  sleep 4
  echo "Local Bot API ready at $TELEGRAM_API_URL"
else
  echo "Local Bot API off — лимит файлов 20 MB. Добавь TELEGRAM_API_ID/HASH для больших видео."
fi

exec python -m bot.main
