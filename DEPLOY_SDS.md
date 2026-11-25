# Подключение бота к PostgreSQL на VPS

Этот вариант использует уже работающий контейнер `sds` и базу `sds`.

1. В каталоге проекта создай `.env` с действующими настройками бота (`BOT_TOKEN`, `SUPER_ADMIN_ID`, идентификаторы чатов и остальные используемые параметры). Добавь `SDS_DATABASE_PASSWORD_URLENCODED` с паролем пользователя `postgres` базы на VPS в формате URL. Например, символ `@` в пароле записывается как `%40`. Секреты хранятся в `.env` на сервере.
2. Создай общую сеть Docker и подключи к ней PostgreSQL. Выполни команды один раз на VPS.

   ```bash
   docker network create sds-network
   docker network connect sds-network sds
   ```

3. Собери образ и проверь `DATABASE_URL` тем же драйвером, которым пользуется бот. Команда выполняет только SELECT.

   ```bash
   docker compose -f compose.sds.yaml build bot
   docker compose -f compose.sds.yaml run --rm -T --no-deps bot python - <<'PY'
   import asyncio
   from app.config import load_settings
   from app.db import Database
   from sqlalchemy import text

   async def main():
       db = Database(load_settings().database_url)
       try:
           async with db.engine.connect() as conn:
               result = await conn.execute(text(
                   'SELECT current_database(), (SELECT count(*) FROM public.users)'
               ))
               database, users = result.one()
               print(f'База: {database}, пользователей: {users}')
       finally:
           await db.close()

   asyncio.run(main())
   PY
   ```

4. Для переключения рабочего бота сначала перенеси изменения, появившиеся в Supabase после тестового дампа. На время финальной синхронизации нужно прекратить запись старым экземпляром, включая Vercel cron, если он используется. Если токен подключён к webhook, перед запуском polling удали webhook через Telegram Bot API с `drop_pending_updates=false`.
5. После переключения запусти бота и проверь журналы и `/start`.

   ```bash
   docker compose -f compose.sds.yaml up -d bot
   docker compose -f compose.sds.yaml logs --tail=60 bot
   ```

Для параллельного запуска используй отдельного тестового бота с собственным токеном и отдельными тестовыми чатами. У копии рабочей базы могут быть ожидающие рассылки, поэтому для проверки соединения используй шаг 3.
