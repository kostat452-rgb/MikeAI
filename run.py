import os, sys, subprocess, time, threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

def run_bot():
    subprocess.run([sys.executable, "-m", "bot.main"])

def run_web():
    subprocess.run([sys.executable, "-m", "uvicorn", "web_ui.app:app", "--host", "0.0.0.0", "--port", "8000"])

if __name__ == "__main__":
    print("=" * 50)
    print("🚀 Mike AI - Запуск")
    print("=" * 50)
    threading.Thread(target=run_bot, daemon=True).start()
    time.sleep(1)
    threading.Thread(target=run_web, daemon=True).start()
    print("✅ Бот + Веб запущены!")
    print("🌐 http://localhost:8000")
    print("Ctrl+C для остановки\n")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n👋 Завершение...")