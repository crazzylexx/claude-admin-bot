"""
Модуль биометрической аутентификации через WebAuthn
Поддерживает отпечатки пальцев, Face ID, Windows Hello
"""

import sqlite3
import secrets
import json
from datetime import datetime, timedelta
from typing import Optional, Dict, Any
from pathlib import Path

from webauthn import (
    generate_registration_options,
    verify_registration_response,
    generate_authentication_options,
    verify_authentication_response,
    options_to_json,
)
from webauthn.helpers.structs import (
    PublicKeyCredentialDescriptor,
    UserVerificationRequirement,
    AuthenticatorSelectionCriteria,
    ResidentKeyRequirement,
)
from webauthn.helpers.cose import COSEAlgorithmIdentifier
from jose import JWTError, jwt
from passlib.context import CryptContext
from dotenv import load_dotenv
import os

load_dotenv()

# Настройки
DB_PATH = Path(__file__).parent / "users.db"
SECRET_KEY = os.getenv("SECRET_KEY", secrets.token_urlsafe(32))
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_HOURS = 24

# RP (Relying Party) настройки
RP_ID = os.getenv("RP_ID", "localhost")
RP_NAME = os.getenv("RP_NAME", "Claude Admin Bot")
ORIGIN = os.getenv("ORIGIN", "http://localhost:8005")

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


def init_db():
    """Инициализация базы данных"""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            credential_id TEXT NOT NULL,
            public_key TEXT NOT NULL,
            sign_count INTEGER DEFAULT 0,
            current_challenge TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    
    conn.commit()
    conn.close()


def get_user_by_username(username: str) -> Optional[Dict[str, Any]]:
    """Получить пользователя по имени"""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    
    cursor.execute("SELECT * FROM users WHERE username = ?", (username,))
    row = cursor.fetchone()
    conn.close()
    
    if row:
        return dict(row)
    return None


def get_user_by_credential_id(credential_id: str) -> Optional[Dict[str, Any]]:
    """Получить пользователя по credential_id"""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    
    cursor.execute("SELECT * FROM users WHERE credential_id = ?", (credential_id,))
    row = cursor.fetchone()
    conn.close()
    
    if row:
        return dict(row)
    return None


def create_user(username: str, credential_id: str, public_key: str) -> int:
    """Создать нового пользователя"""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    cursor.execute(
        "INSERT INTO users (username, credential_id, public_key) VALUES (?, ?, ?)",
        (username, credential_id, public_key)
    )
    
    user_id = cursor.lastrowid
    conn.commit()
    conn.close()
    
    return user_id


def update_challenge(username: str, challenge: str):
    """Обновить challenge для пользователя"""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    cursor.execute(
        "UPDATE users SET current_challenge = ? WHERE username = ?",
        (challenge, username)
    )
    
    conn.commit()
    conn.close()


def update_sign_count(credential_id: str, sign_count: int):
    """Обновить счетчик подписей"""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    cursor.execute(
        "UPDATE users SET sign_count = ? WHERE credential_id = ?",
        (sign_count, credential_id)
    )
    
    conn.commit()
    conn.close()


def count_users() -> int:
    """Подсчитать количество пользователей"""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    cursor.execute("SELECT COUNT(*) FROM users")
    count = cursor.fetchone()[0]
    conn.close()
    
    return count


def create_access_token(data: dict, expires_delta: Optional[timedelta] = None):
    """Создать JWT токен"""
    to_encode = data.copy()
    
    if expires_delta:
        expire = datetime.utcnow() + expires_delta
    else:
        expire = datetime.utcnow() + timedelta(hours=ACCESS_TOKEN_EXPIRE_HOURS)
    
    to_encode.update({"exp": expire})
    encoded_jwt = jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)
    
    return encoded_jwt


def verify_token(token: str) -> Optional[Dict[str, Any]]:
    """Проверить JWT токен"""
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        return payload
    except JWTError:
        return None


def generate_webauthn_registration_options(username: str) -> Dict[str, Any]:
    """
    Генерация опций для регистрации WebAuthn
    """
    # Генерируем случайный user_id
    user_id = secrets.token_bytes(32)
    
    options = generate_registration_options(
        rp_id=RP_ID,
        rp_name=RP_NAME,
        user_id=user_id,
        user_name=username,
        user_display_name=username,
        authenticator_selection=AuthenticatorSelectionCriteria(
            resident_key=ResidentKeyRequirement.PREFERRED,
            user_verification=UserVerificationRequirement.PREFERRED,
        ),
        supported_pub_key_algs=[
            COSEAlgorithmIdentifier.ECDSA_SHA_256,
            COSEAlgorithmIdentifier.RSASSA_PKCS1_v1_5_SHA_256,
        ],
    )
    
    # Сохраняем challenge для последующей проверки
    challenge_str = options.challenge.decode('utf-8') if isinstance(options.challenge, bytes) else options.challenge
    update_challenge(username, challenge_str)
    
    return json.loads(options_to_json(options))


def verify_webauthn_registration(username: str, credential: Dict[str, Any]) -> bool:
    """
    Проверка регистрации WebAuthn
    """
    user = get_user_by_username(username)
    if not user or not user.get('current_challenge'):
        return False
    
    try:
        verification = verify_registration_response(
            credential=credential,
            expected_challenge=user['current_challenge'].encode('utf-8'),
            expected_origin=ORIGIN,
            expected_rp_id=RP_ID,
        )
        
        # Сохраняем credential
        credential_id = verification.credential_id.decode('utf-8') if isinstance(verification.credential_id, bytes) else verification.credential_id
        public_key = verification.credential_public_key.decode('utf-8') if isinstance(verification.credential_public_key, bytes) else verification.credential_public_key
        
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE users SET credential_id = ?, public_key = ?, current_challenge = NULL WHERE username = ?",
            (credential_id, public_key, username)
        )
        conn.commit()
        conn.close()
        
        return True
        
    except Exception as e:
        print(f"Registration verification error: {e}")
        return False


def generate_webauthn_authentication_options(username: str) -> Dict[str, Any]:
    """
    Генерация опций для аутентификации WebAuthn
    """
    user = get_user_by_username(username)
    if not user or not user.get('credential_id'):
        raise ValueError("User not found or not registered")
    
    # Создаем список разрешенных credentials
    allow_credentials = [
        PublicKeyCredentialDescriptor(
            id=user['credential_id'].encode('utf-8')
        )
    ]
    
    options = generate_authentication_options(
        rp_id=RP_ID,
        allow_credentials=allow_credentials,
        user_verification=UserVerificationRequirement.PREFERRED,
    )
    
    # Сохраняем challenge
    challenge_str = options.challenge.decode('utf-8') if isinstance(options.challenge, bytes) else options.challenge
    update_challenge(username, challenge_str)
    
    return json.loads(options_to_json(options))


def verify_webauthn_authentication(username: str, credential: Dict[str, Any]) -> bool:
    """
    Проверка аутентификации WebAuthn
    """
    user = get_user_by_username(username)
    if not user or not user.get('current_challenge'):
        return False
    
    try:
        verification = verify_authentication_response(
            credential=credential,
            expected_challenge=user['current_challenge'].encode('utf-8'),
            expected_origin=ORIGIN,
            expected_rp_id=RP_ID,
            credential_public_key=user['public_key'].encode('utf-8'),
            credential_current_sign_count=user['sign_count'],
        )
        
        # Обновляем счетчик подписей
        update_sign_count(user['credential_id'], verification.new_sign_count)
        
        # Очищаем challenge
        update_challenge(username, None)
        
        return True
        
    except Exception as e:
        print(f"Authentication verification error: {e}")
        return False


# Инициализация БД при импорте модуля
init_db()
