#!/usr/bin/env bash
#
# dev_watch.sh — Geliştirme sırasında botu otomatik yeniden başlatır.
# İzlenen .py / .txt / .env dosyaları değişince main.py süreci durdurulup yeniden başlar.
# Bağımlılık yok (sadece bash + stat). Durdurmak için Ctrl+C.
#
# Kullanım:
#   ./dev_watch.sh
#
set -u

cd "$(dirname "$0")" || exit 1

PY="${PY:-.venv/bin/python}"
[ -x "$PY" ] || PY="python3"

LOG="/tmp/ai_influencer_bot.log"
BOT_PID=""

# İzlenecek dosyalar: kök .py, alt klasör .py, promptlar ve .env
watched_files() {
  find . \
    -type d -name .venv -prune -o \
    -type d -name __pycache__ -prune -o \
    \( -name '*.py' -o -name '*.txt' -o -name '.env' \) -type f -print
}

# Tüm izlenen dosyaların mtime imzası (değişiklik tespiti için)
signature() {
  watched_files | sort | while read -r f; do
    stat -f '%m %N' "$f" 2>/dev/null
  done | md5
}

start_bot() {
  : > "$LOG"
  "$PY" main.py >> "$LOG" 2>&1 &
  BOT_PID=$!
  echo "▶️  Bot başlatıldı (PID $BOT_PID) — log: $LOG"
}

stop_bot() {
  if [ -n "$BOT_PID" ] && kill -0 "$BOT_PID" 2>/dev/null; then
    kill "$BOT_PID" 2>/dev/null
    # Telegram polling oturumunun temiz kapanması için kısa bekle
    for _ in 1 2 3 4 5; do
      kill -0 "$BOT_PID" 2>/dev/null || break
      sleep 1
    done
    kill -9 "$BOT_PID" 2>/dev/null
    echo "⏹️  Bot durduruldu (PID $BOT_PID)"
  fi
  BOT_PID=""
}

cleanup() {
  echo ""
  echo "🛑 İzleyici kapanıyor..."
  stop_bot
  exit 0
}
trap cleanup INT TERM

# Aynı anda başka main.py çalışıyorsa (Telegram tek oturum ister) durdur
pkill -f "main.py" 2>/dev/null
sleep 2

echo "👀 İzleniyor: $(watched_files | wc -l | tr -d ' ') dosya. Değişiklikte otomatik restart. (Ctrl+C ile çık)"
start_bot
LAST_SIG="$(signature)"

while true; do
  sleep 2
  # Bot beklenmedik şekilde çöktüyse yeniden başlat
  if [ -n "$BOT_PID" ] && ! kill -0 "$BOT_PID" 2>/dev/null; then
    echo "⚠️  Bot süreci düştü, yeniden başlatılıyor..."
    start_bot
    LAST_SIG="$(signature)"
    continue
  fi
  NEW_SIG="$(signature)"
  if [ "$NEW_SIG" != "$LAST_SIG" ]; then
    echo "🔄 Değişiklik algılandı — yeniden başlatılıyor ($(date '+%H:%M:%S'))"
    stop_bot
    start_bot
    LAST_SIG="$NEW_SIG"
  fi
done
