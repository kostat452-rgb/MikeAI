#!/bin/bash
# Mike AI — Add new client
# Usage: ./add_client.sh <tenant_id> <company_name> [bot_token] [owner_tg_id] [plan: start|pro|business]
#
# Example: ./add_client.sh salon_beauty "Салон Красота" "123456:ABC" "987654" pro

set -e

TENANT_ID="${1:?Usage: $0 <tenant_id> <company_name> [bot_token] [owner_tg_id] [plan]}"
COMPANY_NAME="${2:?Company name required}"
BOT_TOKEN="${3:-}"
OWNER_TG_ID="${4:-0}"
PLAN="${5:-start}"

BASE_DIR="/opt/mikeai"
CLIENT_DIR="$BASE_DIR/$TENANT_ID"
MAIN_DIR="$BASE_DIR/MikeAI"

# Generate random passwords
WEB_PASS=$(openssl rand -hex 8)
API_KEY="mikeai_${TENANT_ID}_$(openssl rand -hex 6)"

echo "========================================"
echo "Mike AI — Создание клиента"
echo "========================================"
echo "Tenant ID:  $TENANT_ID"
echo "Компания:   $COMPANY_NAME"
echo "Тариф:      $PLAN"
echo ""

# Check if already exists
if [ -d "$CLIENT_DIR" ]; then
    echo "[!] Клиент $TENANT_ID уже существует в $CLIENT_DIR"
    exit 1
fi

# Create directories
echo "[*] Создаю директории..."
mkdir -p "$CLIENT_DIR/data/uploads/$TENANT_ID"
mkdir -p "$CLIENT_DIR/data/chroma_db"

# Set limits based on plan
case "$PLAN" in
    pro)
        DAILY_LIMIT=500
        MONTHLY_TOKENS=500000
        MAX_FILES=20
        ;;
    business)
        DAILY_LIMIT=2000
        MONTHLY_TOKENS=2000000
        MAX_FILES=50
        ;;
    *)  # start
        DAILY_LIMIT=200
        MONTHLY_TOKENS=200000
        MAX_FILES=10
        ;;
esac

# Create .env
echo "[*] Создаю .env..."
cat > "$CLIENT_DIR/.env" << EOF
# Mike AI — $COMPANY_NAME
TENANT_ID=$TENANT_ID
COMPANY_NAME=$COMPANY_NAME
BOT_NAME=Ассистент $COMPANY_NAME

# Telegram
TELEGRAM_BOT_TOKEN=${BOT_TOKEN:-REPLACE_ME}
OWNER_TELEGRAM_ID=$OWNER_TG_ID

# Auth
WEB_PASSWORD=$WEB_PASS
API_KEY=$API_KEY

# Limits
DAILY_REQUEST_LIMIT=$DAILY_LIMIT
MONTHLY_TOKEN_LIMIT=$MONTHLY_TOKENS
MAX_FILES_PER_TENANT=$MAX_FILES

# AI
BOT_ROLE=hybrid
BOT_TONE=friendly
OPENROUTER_API_KEY=\${OPENROUTER_API_KEY}
OPENROUTER_MODEL=deepseek/deepseek-chat

# RAG
CHROMA_PERSIST_DIR=$CLIENT_DIR/data/chroma_db
EMBEDDING_MODEL=intfloat/multilingual-e5-small

# VK (optional)
VK_ENABLED=false
VK_GROUP_TOKEN=
VK_GROUP_ID=0

# Widget
WIDGET_ENABLED=true
EOF

# Create symlinks to shared code
echo "[*] Создаю симлинки на код..."
for item in bot config core hunter rag services web_ui run.py requirements.txt; do
    ln -sf "$MAIN_DIR/$item" "$CLIENT_DIR/$item" 2>/dev/null || true
done

echo ""
echo "========================================"
echo "Клиент создан!"
echo "========================================"
echo ""
echo "Директория:   $CLIENT_DIR"
echo "Пароль админки: $WEB_PASS"
echo "API ключ:     $API_KEY"
echo ""
echo "Следующие шаги:"
echo "1. Замени TELEGRAM_BOT_TOKEN в $CLIENT_DIR/.env"
echo "2. Добавь OPENROUTER_API_KEY в $CLIENT_DIR/.env"
echo "3. Для запуска: cd $CLIENT_DIR && python run.py"
echo "4. Для systemd: скопируй mikeai.service и измени WorkingDirectory"
echo ""
