"""
Claude Admin Bot - Прямое общение с Claude Code через Telegram
Создаёт отдельный процесс для каждого запроса, не мешая основной сессии
"""

import asyncio
import logging
import os
import subprocess
import signal
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import CommandStart
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties
from aiogram.types import FSInputFile
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
logger = logging.getLogger(__name__)

ADMIN_ID = int(os.getenv("ADMIN_TELEGRAM_ID", "372886754"))
TELEGRAM_TOKEN = os.getenv("CLAUDE_BOT_TOKEN")
WORK_DIR = "/root"
FILES_DIR = "/root/claude-admin-bot/files"

if not TELEGRAM_TOKEN:
    raise ValueError("CLAUDE_BOT_TOKEN не задан в .env")

os.makedirs(FILES_DIR, exist_ok=True)

bot = Bot(token=TELEGRAM_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.MARKDOWN))
dp = Dispatcher()

# Блокировка для предотвращения множественных запросов от бота
bot_busy = False


def is_admin(user_id: int) -> bool:
    return user_id == ADMIN_ID


@dp.message(CommandStart())
async def cmd_start(message: types.Message):
    if not is_admin(message.from_user.id):
        await message.answer("⛔️ Access denied")
        return

    await message.answer("""🤖 *Claude Admin Bot*

Просто пиши мне — я передам всё Claude.

✅ Можешь работать с Claude в терминале параллельно — бот не помешает.

*Файлы:*
📤 Отправь файл → сохраню в `{0}`
📥 Отправь путь к файлу → отправлю его тебе

Просто общайся естественно! 💬
""".format(FILES_DIR))


@dp.message(F.document)
async def handle_document(message: types.Message):
    """Загрузка файлов на сервер"""
    if not is_admin(message.from_user.id):
        return

    try:
        document = message.document
        file_name = document.file_name

        status_msg = await message.answer(f"📥 Загружаю...")

        file = await bot.get_file(document.file_id)
        file_path = os.path.join(FILES_DIR, file_name)

        await bot.download_file(file.file_path, file_path)

        file_size = os.path.getsize(file_path)

        await status_msg.edit_text(
            f"✅ Сохранено:\n`{file_path}`\n({file_size / 1024:.1f} КБ)"
        )

    except Exception as e:
        await message.answer(f"❌ {e}")


@dp.message(F.text)
async def handle_message(message: types.Message):
    """Основной обработчик - всё идёт в Claude"""
    global bot_busy

    if not is_admin(message.from_user.id):
        return

    user_text = message.text.strip()

    # Проверка на путь к файлу для скачивания
    if user_text.startswith("/") and os.path.exists(user_text):
        try:
            if os.path.isdir(user_text):
                await message.answer("❌ Это директория. Укажи файл.")
                return

            file_size = os.path.getsize(user_text)

            if file_size > 50 * 1024 * 1024:
                await message.answer(f"❌ Слишком большой: {file_size / 1024 / 1024:.1f} МБ")
                return

            status_msg = await message.answer("📤 Отправляю...")

            file = FSInputFile(user_text)
            await message.answer_document(file, caption=f"`{user_text}`")
            await status_msg.delete()

        except Exception as e:
            await message.answer(f"❌ {e}")

        return

    # Проверка: уже обрабатывается другой запрос от бота?
    if bot_busy:
        await message.answer("⏳ Предыдущий запрос ещё обрабатывается, подожди...")
        return

    # Блокируем новые запросы
    bot_busy = True
    status_msg = None

    claude_process = None

    try:
        # Экранируем специальные символы для bash
        escaped_text = user_text.replace("'", "'\\''")

        # Формируем команду для Claude через stdin (чтобы не зависал)
        # --continue сохраняет контекст между сообщениями
        # --model haiku для быстрых ответов
        # echo передаёт промпт через pipe в claude -p
        claude_command = f"echo '{escaped_text}' | claude -p --continue --model haiku --input-format text"

        # Запускаем процесс с возможностью его убить по таймауту
        claude_process = subprocess.Popen(
            claude_command,
            shell=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=WORK_DIR,
            preexec_fn=os.setsid  # Создаём новую группу процессов
        )

        # Первые 10 секунд ждём без статуса (для быстрых ответов)
        try:
            stdout, stderr = claude_process.communicate(timeout=10)
            response = stdout.strip() or stderr.strip() or "Нет ответа"
        except subprocess.TimeoutExpired:
            # Процесс идёт дольше 10 секунд — показываем статус
            status_msg = await message.answer("⏳ Обрабатываю...")
            elapsed = 10
            poll_interval = 30

            while elapsed < 300:  # Максимум 5 минут
                try:
                    stdout, stderr = claude_process.communicate(timeout=poll_interval)
                    # Процесс завершился
                    response = stdout.strip() or stderr.strip() or "Нет ответа"
                    await status_msg.delete()
                    break
                except subprocess.TimeoutExpired:
                    # Процесс ещё работает, обновляем статус
                    elapsed += poll_interval
                    dots = "." * (elapsed // 10 % 4)
                    await status_msg.edit_text(f"⏳ Обрабатываю{dots} ({elapsed}с)")
                    continue
            else:
                # Таймаут 5 минут истёк
                os.killpg(os.getpgid(claude_process.pid), signal.SIGTERM)
                claude_process.wait(timeout=5)
                await status_msg.edit_text("⏱ Claude не ответил за 5 минут (процесс завершён)")
                return

        # Очищаем от служебных сообщений
        lines = response.split('\n')
        clean_lines = [l for l in lines if not l.startswith('[') and not l.startswith('Using model')]
        response = '\n'.join(clean_lines).strip() or response

        # Разбиваем длинные ответы
        if len(response) > 4000:
            parts = []
            current_part = ""

            for line in response.split('\n'):
                if len(current_part) + len(line) + 1 > 4000:
                    parts.append(current_part)
                    current_part = line + '\n'
                else:
                    current_part += line + '\n'

            if current_part:
                parts.append(current_part)

            await message.answer(f"💡 *Claude (1/{len(parts)}):*\n\n{parts[0]}")

            for i, part in enumerate(parts[1:], 2):
                await message.answer(f"💡 *Claude ({i}/{len(parts)}):*\n\n{part}")
        else:
            await message.answer(response)

    except Exception as e:
        logger.error(f"Ошибка: {e}")
        await message.answer(f"❌ Ошибка: {e}")

        # Убеждаемся что процесс завершён
        if claude_process and claude_process.poll() is None:
            try:
                os.killpg(os.getpgid(claude_process.pid), signal.SIGTERM)
                claude_process.wait(timeout=5)
            except:
                pass
    finally:
        # Освобождаем блокировку
        bot_busy = False

        # Финальная проверка что процесс точно завершён
        if claude_process and claude_process.poll() is None:
            try:
                os.killpg(os.getpgid(claude_process.pid), signal.SIGKILL)
            except:
                pass


async def main():
    logger.info("🚀 Claude Admin Bot запущен")

    try:
        await bot.delete_webhook(drop_pending_updates=True)
        await dp.start_polling(bot)
    finally:
        await bot.session.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("⏹ Бот остановлен")
