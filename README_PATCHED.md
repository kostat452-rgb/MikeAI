# Mike AI — исправленная версия

## Что добавлено
- `tenant_id` в БД, RAG и веб-загрузках.
- Фильтр мусорных файлов до попадания в базу знаний.
- Фильтр мусорных/опасных запросов.
- Защита от prompt injection внутри документов.
- Confidence threshold для RAG (`MIN_RAG_SCORE`).
- Кеш ответов в SQLite.
- Лиды: `/lead` и автосохранение при коммерческом интенте.
- Feedback после ответа: 👍 / 👎.
- Таблицы `leads`, `feedback`, `missing_questions`, `answer_cache`, `tenants`.
- Безопаснее web session cookie.
- Лимиты на размер файла, количество файлов и количество чанков.

## Важно
Не хранить реальные ключи в `.api_key` и `.env` в репозитории. Используйте `.env` на сервере.

## Запуск
```bash
cp .env.example .env
# заполнить TELEGRAM_BOT_TOKEN, OPENROUTER_API_KEY, WEB_PASSWORD
python run.py
```

## Multi-tenant сейчас
Версия готова к разделению по `TENANT_ID`. Для каждого клиента можно запускать отдельный контейнер с разным `.env`, но на одном VPS и с общей кодовой базой. Следующий шаг — supervisor, который поднимает N bot workers по таблице `tenants`.
