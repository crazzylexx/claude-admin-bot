/**
 * Модуль биометрической аутентификации через WebAuthn
 * Поддерживает отпечатки пальцев, Face ID, Windows Hello
 */

class BiometricAuth {
    constructor() {
        this.token = localStorage.getItem('auth_token');
        this.username = localStorage.getItem('username');
        this.isAuthenticated = false;
    }

    /**
     * Проверка поддержки WebAuthn в браузере
     */
    isWebAuthnSupported() {
        return window.PublicKeyCredential !== undefined && 
               navigator.credentials !== undefined;
    }

    /**
     * Проверка текущей аутентификации
     */
    async checkAuth() {
        try {
            const response = await fetch('/api/auth/check');
            const data = await response.json();
            
            // Если нужна регистрация - показываем форму регистрации
            if (data.needs_registration) {
                return { needsRegistration: true };
            }

            // Если есть токен - проверяем его валидность
            if (this.token) {
                const meResponse = await fetch('/api/auth/me', {
                    headers: {
                        'Authorization': `Bearer ${this.token}`
                    }
                });

                if (meResponse.ok) {
                    const userData = await meResponse.json();
                    this.isAuthenticated = true;
                    this.username = userData.username;
                    return { authenticated: true, username: userData.username };
                }
            }

            // Нужен вход
            return { needsLogin: true };

        } catch (error) {
            console.error('Auth check error:', error);
            return { needsLogin: true };
        }
    }

    /**
     * Регистрация нового пользователя с биометрией
     */
    async register(username) {
        if (!this.isWebAuthnSupported()) {
            throw new Error('WebAuthn не поддерживается в вашем браузере');
        }

        try {
            // Шаг 1: Начало регистрации
            const beginResponse = await fetch('/api/auth/register/begin', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ username })
            });

            if (!beginResponse.ok) {
                const error = await beginResponse.json();
                throw new Error(error.detail || 'Ошибка начала регистрации');
            }

            const options = await beginResponse.json();

            // Преобразуем challenge и user.id из base64
            options.challenge = this.base64ToArrayBuffer(options.challenge);
            options.user.id = this.base64ToArrayBuffer(options.user.id);

            // Шаг 2: Создание credential через WebAuthn
            const credential = await navigator.credentials.create({
                publicKey: options
            });

            // Шаг 3: Завершение регистрации
            const credentialData = {
                id: credential.id,
                rawId: this.arrayBufferToBase64(credential.rawId),
                type: credential.type,
                response: {
                    clientDataJSON: this.arrayBufferToBase64(credential.response.clientDataJSON),
                    attestationObject: this.arrayBufferToBase64(credential.response.attestationObject)
                }
            };

            const completeResponse = await fetch('/api/auth/register/complete', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    username,
                    credential: credentialData
                })
            });

            if (!completeResponse.ok) {
                const error = await completeResponse.json();
                throw new Error(error.detail || 'Ошибка завершения регистрации');
            }

            const result = await completeResponse.json();

            // Сохраняем токен
            this.token = result.token;
            this.username = result.username;
            this.isAuthenticated = true;
            localStorage.setItem('auth_token', result.token);
            localStorage.setItem('username', result.username);

            return result;

        } catch (error) {
            console.error('Registration error:', error);
            throw error;
        }
    }

    /**
     * Вход через биометрию
     */
    async login(username) {
        if (!this.isWebAuthnSupported()) {
            throw new Error('WebAuthn не поддерживается в вашем браузере');
        }

        try {
            // Шаг 1: Начало аутентификации
            const beginResponse = await fetch('/api/auth/login/begin', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ username })
            });

            if (!beginResponse.ok) {
                const error = await beginResponse.json();
                throw new Error(error.detail || 'Ошибка начала входа');
            }

            const options = await beginResponse.json();

            // Преобразуем challenge и allowCredentials
            options.challenge = this.base64ToArrayBuffer(options.challenge);
            if (options.allowCredentials) {
                options.allowCredentials = options.allowCredentials.map(cred => ({
                    ...cred,
                    id: this.base64ToArrayBuffer(cred.id)
                }));
            }

            // Шаг 2: Получение credential через WebAuthn
            const credential = await navigator.credentials.get({
                publicKey: options
            });

            // Шаг 3: Завершение аутентификации
            const credentialData = {
                id: credential.id,
                rawId: this.arrayBufferToBase64(credential.rawId),
                type: credential.type,
                response: {
                    clientDataJSON: this.arrayBufferToBase64(credential.response.clientDataJSON),
                    authenticatorData: this.arrayBufferToBase64(credential.response.authenticatorData),
                    signature: this.arrayBufferToBase64(credential.response.signature),
                    userHandle: credential.response.userHandle ? 
                        this.arrayBufferToBase64(credential.response.userHandle) : null
                }
            };

            const completeResponse = await fetch('/api/auth/login/complete', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    username,
                    credential: credentialData
                })
            });

            if (!completeResponse.ok) {
                const error = await completeResponse.json();
                throw new Error(error.detail || 'Ошибка завершения входа');
            }

            const result = await completeResponse.json();

            // Сохраняем токен
            this.token = result.token;
            this.username = result.username;
            this.isAuthenticated = true;
            localStorage.setItem('auth_token', result.token);
            localStorage.setItem('username', result.username);

            return result;

        } catch (error) {
            console.error('Login error:', error);
            throw error;
        }
    }

    /**
     * Выход из системы
     */
    async logout() {
        try {
            if (this.token) {
                await fetch('/api/auth/logout', {
                    method: 'POST',
                    headers: {
                        'Authorization': `Bearer ${this.token}`
                    }
                });
            }
        } catch (error) {
            console.error('Logout error:', error);
        } finally {
            this.token = null;
            this.username = null;
            this.isAuthenticated = false;
            localStorage.removeItem('auth_token');
            localStorage.removeItem('username');
        }
    }

    /**
     * Получить токен для API запросов
     */
    getToken() {
        return this.token;
    }

    /**
     * Получить имя пользователя
     */
    getUsername() {
        return this.username;
    }

    // Утилиты для преобразования base64 <-> ArrayBuffer

    base64ToArrayBuffer(base64) {
        // Удаляем padding и заменяем URL-safe символы
        base64 = base64.replace(/-/g, '+').replace(/_/g, '/');
        const padding = '='.repeat((4 - base64.length % 4) % 4);
        base64 += padding;

        const binaryString = window.atob(base64);
        const bytes = new Uint8Array(binaryString.length);
        for (let i = 0; i < binaryString.length; i++) {
            bytes[i] = binaryString.charCodeAt(i);
        }
        return bytes.buffer;
    }

    arrayBufferToBase64(buffer) {
        const bytes = new Uint8Array(buffer);
        let binary = '';
        for (let i = 0; i < bytes.byteLength; i++) {
            binary += String.fromCharCode(bytes[i]);
        }
        let base64 = window.btoa(binary);
        // Делаем URL-safe
        return base64.replace(/\+/g, '-').replace(/\//g, '_').replace(/=/g, '');
    }
}

// Экспортируем для использования
window.BiometricAuth = BiometricAuth;
