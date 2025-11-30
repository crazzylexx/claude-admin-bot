"""
FastAPI WebSocket API для Claude Admin Bot
Веб-интерфейс в стиле киберпанк для общения с Claude
Биометрическая аутентификация через WebAuthn
"""

import asyncio
import logging
import os
import base64
import subprocess
import signal
import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, Set, List, Optional
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, UploadFile, File, HTTPException, Depends, Header, Request
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from dotenv import load_dotenv
import psutil
import aiofiles

# Импортируем модуль аутентификации
import auth

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
logger = logging.getLogger(__name__)

app = FastAPI(title="Claude Admin API")

# Директории
WORK_DIR = os.path.dirname(os.path.abspath(__file__))
FILES_DIR = os.path.join(WORK_DIR, "files")
WEBAPP_DIR = os.path.join(WORK_DIR, "webapp")

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
        # preexec_fn только для Unix систем
        process_kwargs = {
            'shell': True,
            'stdout': subprocess.PIPE,
            'stderr': subprocess.PIPE,
            'text': True,
            'cwd': WORK_DIR
        }
        
        # os.setsid работает только на Unix
        if os.name != 'nt':  # не Windows
            process_kwargs['preexec_fn'] = os.setsid
        
        claude_process = subprocess.Popen(
            claude_command,
            **process_kwargs
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


@app.get("/api/server/stats")
async def get_server_stats():
    """Получение статистики сервера (CPU, RAM, Disk)"""
    try:
        # CPU
        cpu_percent = psutil.cpu_percent(interval=1)
        
        # RAM
        ram = psutil.virtual_memory()
        ram_total_gb = round(ram.total / (1024**3), 1)
        ram_used_gb = round(ram.used / (1024**3), 1)
        ram_percent = round(ram.percent, 1)
        
        # Disk
        disk = psutil.disk_usage(WORK_DIR)
        disk_total_gb = round(disk.total / (1024**3), 1)
        disk_used_gb = round(disk.used / (1024**3), 1)
        disk_percent = round(disk.percent, 1)
        
        return JSONResponse({
            "cpu": {
                "percent": cpu_percent
            },
            "ram": {
                "total_gb": ram_total_gb,
                "used_gb": ram_used_gb,
                "percent": ram_percent
            },
            "disk": {
                "total_gb": disk_total_gb,
                "used_gb": disk_used_gb,
                "percent": disk_percent
            }
        })
    except Exception as e:
        logger.error(f"Error getting server stats: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/files/recent")
async def get_recent_files():
    """Получение списка недавних файлов"""
    try:
        if not os.path.exists(FILES_DIR):
            return JSONResponse({"files": []})
        
        files = []
        for filename in os.listdir(FILES_DIR):
            filepath = os.path.join(FILES_DIR, filename)
            if os.path.isfile(filepath):
                stat = os.stat(filepath)
                ext = filename.split('.')[-1] if '.' in filename else 'file'
                files.append({
                    "name": filename,
                    "type": ext.lower(),
                    "size": stat.st_size,
                    "modified": stat.st_mtime
                })
        
        # Сортируем по времени модификации (новые первыми)
        files.sort(key=lambda x: x['modified'], reverse=True)
        
        # Возвращаем только последние 10
        return JSONResponse({"files": files[:10]})
    except Exception as e:
        logger.error(f"Error getting recent files: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/files/preview/{filename}")
async def preview_file(filename: str):
    """Предпросмотр содержимого файла"""
    try:
        filepath = os.path.join(FILES_DIR, filename)
        
        if not os.path.exists(filepath):
            raise HTTPException(status_code=404, detail="File not found")
        
        if not os.path.isfile(filepath):
            raise HTTPException(status_code=400, detail="Not a file")
        
        # Проверяем размер файла
        file_size = os.path.getsize(filepath)
        if file_size > 10 * 1024 * 1024:  # 10 MB
            raise HTTPException(status_code=400, detail="File too large for preview")
        
        # Пытаемся прочитать как текстовый файл
        try:
            async with aiofiles.open(filepath, 'r', encoding='utf-8') as f:
                content = await f.read(5000)  # Первые 5000 символов
                
                truncated = file_size > 5000
                
                return JSONResponse({
                    "filename": filename,
                    "content": content,
                    "truncated": truncated,
                    "size": file_size
                })
        except UnicodeDecodeError:
            # Бинарный файл
            raise HTTPException(status_code=400, detail="Binary file, cannot preview")
            
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error previewing file: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/files/upload")
async def upload_file(file: UploadFile = File(...)):
    """Загрузка файла на сервер"""
    try:
        if not file.filename:
            raise HTTPException(status_code=400, detail="No filename provided")
        
        # Безопасное имя файла (убираем путь)
        safe_filename = os.path.basename(file.filename)
        filepath = os.path.join(FILES_DIR, safe_filename)
        
        # Сохраняем файл
        async with aiofiles.open(filepath, 'wb') as f:
            content = await file.read()
            await f.write(content)
        
        file_size = os.path.getsize(filepath)
        
        logger.info(f"File uploaded: {safe_filename} ({file_size} bytes)")
        
        return JSONResponse({
            "success": True,
            "filename": safe_filename,
            "size": file_size,
            "path": filepath
        })
        
    except Exception as e:
        logger.error(f"Error uploading file: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ==================== АУТЕНТИФИКАЦИЯ ====================

class RegisterBeginRequest(BaseModel):
    username: str

class RegisterCompleteRequest(BaseModel):
    username: str
    credential: dict

class LoginBeginRequest(BaseModel):
    username: str

class LoginCompleteRequest(BaseModel):
    username: str
    credential: dict


def get_current_user(authorization: Optional[str] = Header(None)):
    """Middleware для проверки JWT токена"""
    if not authorization:
        raise HTTPException(status_code=401, detail="Not authenticated")
    
    # Ожидаем формат: "Bearer <token>"
    try:
        scheme, token = authorization.split()
        if scheme.lower() != "bearer":
            raise HTTPException(status_code=401, detail="Invalid authentication scheme")
    except ValueError:
        raise HTTPException(status_code=401, detail="Invalid authorization header")
    
    payload = auth.verify_token(token)
    if not payload:
        raise HTTPException(status_code=401, detail="Invalid or expired token")
    
    return payload


@app.get("/api/auth/check")
async def check_auth():
    """Проверка, нужна ли регистрация"""
    user_count = auth.count_users()
    return JSONResponse({
        "needs_registration": user_count == 0,
        "user_count": user_count
    })


@app.post("/api/auth/register/begin")
async def register_begin(request: RegisterBeginRequest):
    """Начало регистрации WebAuthn"""
    try:
        # Проверяем, что это первый пользователь
        if auth.count_users() > 0:
            raise HTTPException(status_code=400, detail="Registration is closed")
        
        # Проверяем, что пользователь не существует
        if auth.get_user_by_username(request.username):
            raise HTTPException(status_code=400, detail="User already exists")
        
        # Создаем пользователя с пустыми credentials (заполним при завершении)
        auth.create_user(request.username, "", "")
        
        # Генерируем опции для регистрации
        options = auth.generate_webauthn_registration_options(request.username)
        
        logger.info(f"Registration started for: {request.username}")
        return JSONResponse(options)
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error in register_begin: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/auth/register/complete")
async def register_complete(request: RegisterCompleteRequest):
    """Завершение регистрации WebAuthn"""
    try:
        # Проверяем регистрацию
        success = auth.verify_webauthn_registration(
            request.username,
            request.credential
        )
        
        if not success:
            raise HTTPException(status_code=400, detail="Registration verification failed")
        
        # Создаем JWT токен
        token = auth.create_access_token(
            data={"sub": request.username}
        )
        
        logger.info(f"Registration completed for: {request.username}")
        
        return JSONResponse({
            "success": True,
            "token": token,
            "username": request.username
        })
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error in register_complete: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/auth/login/begin")
async def login_begin(request: LoginBeginRequest):
    """Начало аутентификации WebAuthn"""
    try:
        # Проверяем, что пользователь существует
        user = auth.get_user_by_username(request.username)
        if not user:
            raise HTTPException(status_code=404, detail="User not found")
        
        # Генерируем опции для аутентификации
        options = auth.generate_webauthn_authentication_options(request.username)
        
        logger.info(f"Login started for: {request.username}")
        return JSONResponse(options)
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error in login_begin: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/auth/login/complete")
async def login_complete(request: LoginCompleteRequest):
    """Завершение аутентификации WebAuthn"""
    try:
        # Проверяем аутентификацию
        success = auth.verify_webauthn_authentication(
            request.username,
            request.credential
        )
        
        if not success:
            raise HTTPException(status_code=401, detail="Authentication failed")
        
        # Создаем JWT токен
        token = auth.create_access_token(
            data={"sub": request.username}
        )
        
        logger.info(f"Login successful for: {request.username}")
        
        return JSONResponse({
            "success": True,
            "token": token,
            "username": request.username
        })
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error in login_complete: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/auth/logout")
async def logout(current_user: dict = Depends(get_current_user)):
    """Выход из системы"""
    # В текущей реализации просто подтверждаем выход
    # Клиент должен удалить токен из localStorage
    return JSONResponse({"success": True})


@app.get("/api/auth/me")
async def get_me(current_user: dict = Depends(get_current_user)):
    """Получить информацию о текущем пользователе"""
    return JSONResponse({
        "username": current_user.get("sub"),
        "authenticated": True
    })


# Статические файлы и главная страница
app.mount("/static", StaticFiles(directory=WEBAPP_DIR), name="static")


@app.get("/login")
async def read_login():
    """Страница входа"""
    return FileResponse(os.path.join(WEBAPP_DIR, "login.html"))


@app.get("/")
async def read_index():
    """Главная страница - требует аутентификации"""
    return FileResponse(os.path.join(WEBAPP_DIR, "index.html"))


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8005, log_level="info")
