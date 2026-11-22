ATLANTIS v0.14 — changes from v0.13

1. Removed the participant-facing 'Карточки без сжатия' button from Hero Path. Existing media group of two cards remains unchanged.
2. Added 'Статистика бота' to /admin for administrators only. Shows users recorded in DB, users with FIO, unique users with active lecture registrations, trip whitelist size and Telegram-linked trip attendees.
3. No database schema changes. Preserve .env and existing PostgreSQL volume during deployment. Do not run docker compose down -v.
4. After deployment check /admin -> Статистика бота, Hero Path keyboard, and server logs.
