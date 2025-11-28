"""
FastAPI WebSocket API для Claude Admin Bot
Веб-интерфейс в стиле киберпанк для общения с Claude
"""

import asyncio
import logging
import os
import base64
import subprocess
import signal
from typing import Dict, Set
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
logger = logging.getLogger(__name__)

app = FastAPI(title="Claude Admin API")

# Директории
WORK_DIR = "/root"
FILES_DIR = "/root/claude-admin-bot/files"
WEBAPP_DIR = "/root/claude-admin-bot/webapp"

os.makedirs(FILES_DIR, exist_ok=True)

# Активные WebSocket соединения
active_connections: Set[WebSocket] = set()

# Блокировка для предотвращения множественных запросов
claude_busy = False


class ConnectionManager:
    """Менеджер WebSocket соединений"""

    def __init__(self):
        self.active_connections: Dict[WebSocket, bool] = {}

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.active_connections[websocket] = True
        logger.info(f"WebSocket connected. Total: {len(self.active_connections)}")

    def disconnect(self, websocket: WebSocket):
        if websocket in self.active_connections:
            del self.active_connections[websocket]
        logger.info(f"WebSocket disconnected. Total: {len(self.active_connections)}")

    async def send_message(self, websocket: WebSocket, message: dict):
        """Отправка сообщения конкретному клиенту"""
        if websocket in self.active_connections:
            try:
                await websocket.send_json(message)
            except Exception as e:
                logger.error(f"Error sending message: {e}")

    async def broadcast(self, message: dict):
        """Отправка сообщения всем подключенным клиентам"""
        for connection in list(self.active_connections.keys()):
            try:
                await connection.send_json(message)
            except Exception as e:
                logger.error(f"Error broadcasting: {e}")


manager = ConnectionManager()


async def execute_claude_command(text: str, websocket: WebSocket):
    """
    Выполнение команды Claude с отправкой статусов
    """
    global claude_busy

    if claude_busy:
        await manager.send_message(websocket, {
            "type": "response",
            "content": "⏳ Предыдущий запрос ещё обрабатывается, подожди...",
            "has_code": False
        })
        return

    claude_busy = True
    claude_process = None

    try:
        # Экранируем специальные символы для bash
        escaped_text = text.replace("'", "'\\''")

        # Формируем команду для Claude
        claude_command = f"echo '{escaped_text}' | claude -p --continue --model haiku --input-format text"

        # Запускаем процесс
        claude_process = subprocess.Popen(
            claude_command,
            shell=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=WORK_DIR,
            preexec_fn=os.setsid
        )

        # Первые 10 секунд ждём без статуса
        try:
            stdout, stderr = claude_process.communicate(timeout=10)
            response = stdout.strip() or stderr.strip() or "Нет ответа"
        except subprocess.TimeoutExpired:
            # Процесс идёт дольше 10 секунд — показываем статус
            await manager.send_message(websocket, {
                "type": "status",
                "content": "⏳ Обрабатываю..."
            })

            elapsed = 10
            poll_interval = 30

            while elapsed < 300:  # Максимум 5 минут
                try:
                    stdout, stderr = claude_process.communicate(timeout=poll_interval)
                    response = stdout.strip() or stderr.strip() or "Нет ответа"
                    break
                except subprocess.TimeoutExpired:
                    elapsed += poll_interval
                    await manager.send_message(websocket, {
                        "type": "status",
                        "content": f"⏳ Обрабатываю... ({elapsed}с)"
                    })
                    continue
            else:
                # Таймаут 5 минут истёк
                os.killpg(os.getpgid(claude_process.pid), signal.SIGTERM)
                claude_process.wait(timeout=5)
                await manager.send_message(websocket, {
                    "type": "response",
                    "content": "⏱ Claude не ответил за 5 минут (процесс завершён)",
                    "has_code": False
                })
                return

        # Очищаем от служебных сообщений
        lines = response.split('\n')
        clean_lines = [l for l in lines if not l.startswith('[') and not l.startswith('Using model')]
        response = '\n'.join(clean_lines).strip() or response

        # Проверяем, есть ли в ответе код (простая эвристика)
        has_code = False
        code_snippet = None

        # Если в ответе есть блоки кода между ```
        if '```' in response:
            # Извлекаем первый блок кода
            parts = response.split('```')
            if len(parts) >= 3:
                code_snippet = parts[1]
                # Убираем язык если указан (bash, python и тд)
                if '\n' in code_snippet:
                    lines = code_snippet.split('\n')
                    if lines[0].strip() in ['bash', 'sh', 'python', 'js', 'json']:
                        code_snippet = '\n'.join(lines[1:])
                code_snippet = code_snippet.strip()
                has_code = True
                # Убираем блок кода из основного текста
                response = parts[0] + (parts[2] if len(parts) > 2 else '')
                response = response.strip()

        # Отправляем ответ
        await manager.send_message(websocket, {
            "type": "response",
            "content": response,
            "has_code": has_code,
            "code_snippet": code_snippet
        })

    except Exception as e:
        logger.error(f"Ошибка выполнения Claude: {e}")
        await manager.send_message(websocket, {
            "type": "response",
            "content": f"❌ Ошибка: {str(e)}",
            "has_code": False
        })

        # Убеждаемся что процесс завершён
        if claude_process and claude_process.poll() is None:
            try:
                os.killpg(os.getpgid(claude_process.pid), signal.SIGTERM)
                claude_process.wait(timeout=5)
            except:
                pass
    finally:
        # Освобождаем блокировку
        claude_busy = False

        # Финальная проверка что процесс точно завершён
        if claude_process and claude_process.poll() is None:
            try:
                os.killpg(os.getpgid(claude_process.pid), signal.SIGKILL)
            except:
                pass


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    """WebSocket endpoint для чата"""
    await manager.connect(websocket)

    try:
        while True:
            # Получаем сообщение от клиента
            data = await websocket.receive_json()

            message_type = data.get("type")

            if message_type == "text":
                # Текстовое сообщение
                content = data.get("content", "").strip()
                if content:
                    logger.info(f"Received text: {content[:50]}...")
                    # Выполняем команду Claude в фоне
                    asyncio.create_task(execute_claude_command(content, websocket))

            elif message_type == "file":
                # Загрузка файла
                filename = data.get("filename")
                file_content = data.get("content")  # base64

                if filename and file_content:
                    try:
                        file_data = base64.b64decode(file_content)
                        file_path = os.path.join(FILES_DIR, filename)

                        with open(file_path, 'wb') as f:
                            f.write(file_data)

                        file_size = os.path.getsize(file_path)

                        await manager.send_message(websocket, {
                            "type": "response",
                            "content": f"✅ Файл сохранён:\n{file_path}\n({file_size / 1024:.1f} КБ)",
                            "has_code": True,
                            "code_snippet": file_path
                        })

                        logger.info(f"File saved: {filename} ({file_size} bytes)")
                    except Exception as e:
                        await manager.send_message(websocket, {
                            "type": "response",
                            "content": f"❌ Ошибка сохранения файла: {str(e)}",
                            "has_code": False
                        })

            elif message_type == "image":
                # Загрузка изображения (аналогично файлу)
                filename = data.get("filename")
                file_content = data.get("content")

                if filename and file_content:
                    try:
                        file_data = base64.b64decode(file_content)
                        file_path = os.path.join(FILES_DIR, filename)

                        with open(file_path, 'wb') as f:
                            f.write(file_data)

                        file_size = os.path.getsize(file_path)

                        await manager.send_message(websocket, {
                            "type": "response",
                            "content": f"✅ Изображение сохранено:\n{file_path}\n({file_size / 1024:.1f} КБ)",
                            "has_code": True,
                            "code_snippet": file_path
                        })

                        logger.info(f"Image saved: {filename} ({file_size} bytes)")
                    except Exception as e:
                        await manager.send_message(websocket, {
                            "type": "response",
                            "content": f"❌ Ошибка сохранения изображения: {str(e)}",
                            "has_code": False
                        })

    except WebSocketDisconnect:
        manager.disconnect(websocket)
    except Exception as e:
        logger.error(f"WebSocket error: {e}")
        manager.disconnect(websocket)


# Статические файлы и главная страница
app.mount("/static", StaticFiles(directory=WEBAPP_DIR), name="static")


@app.get("/")
async def read_index():
    """Главная страница"""
    return FileResponse(os.path.join(WEBAPP_DIR, "index.html"))


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8005, log_level="info")
