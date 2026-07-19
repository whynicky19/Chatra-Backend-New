# Push-уведомления (FCM) — настройка

Доставка через **Firebase Cloud Messaging (FCM), HTTP v1 API**. Платформы: Android + iOS.

Уведомления шлются при событиях:
- за ~1 день до дедлайна задания (фоновый цикл на бэкенде);
- выставлена оценка (ручная и авто-ИИ);
- новое сообщение в чате.

---

## 1. Firebase Console — создать проект и приложения

1. https://console.firebase.google.com → **Add project** (напр. `Chatra`). Analytics можно выключить.
2. **Add app → Android**:
   - Android package name: `com.example.chatra_app` (из `android/app/build.gradle.kts`, `applicationId`).
   - Скачать **`google-services.json`** → положить в `Chatra2/android/app/google-services.json`.
3. **Add app → iOS**:
   - iOS bundle ID: `com.example.chatraApp` (из Runner, `PRODUCT_BUNDLE_IDENTIFIER`).
   - Скачать **`GoogleService-Info.plist`** → положить в `Chatra2/ios/Runner/GoogleService-Info.plist`
     (через Xcode: перетащить в проект Runner, «Copy items if needed»).

> ⚠️ `applicationId`/bundle сейчас `com.example.*` — это плейсхолдеры Flutter. Если планируешь
> публикацию в сторах, лучше сразу сменить их на свой домен (напр. `kz.chatra.app`) и
> зарегистрировать в Firebase именно его. Скажи — помогу переименовать в обоих проектах.

## 2. iOS — APNs (обязательно для пушей на iPhone)

Нужен платный Apple Developer аккаунт.

1. https://developer.apple.com → Certificates, Identifiers & Profiles → **Keys** → **+**.
2. Включить **Apple Push Notifications service (APNs)**, создать ключ, скачать **`.p8`**
   (скачивается один раз!). Запомнить **Key ID** и **Team ID**.
3. Firebase Console → Project Settings → **Cloud Messaging** → Apple app configuration →
   **APNs Authentication Key** → загрузить `.p8` + Key ID + Team ID.
4. В Xcode для таргета Runner: **Signing & Capabilities** → добавить
   **Push Notifications** и **Background Modes** (галка *Remote notifications*).

## 3. Сервисный аккаунт для бэкенда (отправка пушей)

1. Firebase Console → Project Settings → **Service accounts** → **Generate new private key** →
   скачается JSON (напр. `chatra-firebase-adminsdk-xxxx.json`).
2. Положить его **на сервер бэкенда**, НЕ коммитить в git (добавлен в `.gitignore`).
3. В `.env` бэкенда указать путь:
   ```
   FCM_SERVICE_ACCOUNT_FILE=/absolute/path/chatra-firebase-adminsdk-xxxx.json
   ```
   либо инлайном всё содержимое JSON:
   ```
   FCM_SERVICE_ACCOUNT_JSON={"type":"service_account", ... }
   ```
   `project_id` берётся из самого JSON — отдельно указывать не нужно.

> Пока эти переменные не заданы — бэкенд работает как раньше, пуши просто не отправляются
> (в лог пишется предупреждение). Ошибок регистрации токенов и запросов это не ломает.

## 4. Установка зависимостей бэкенда

```
cd App-Backend-with-avatar
./venv/bin/pip install -r requirements.txt   # добавлен google-auth
```

## 5. Миграция БД (таблицы device_tokens, push_log)

Запускать с ЯВНЫМ DATABASE_URL продовой базы (см. заметку про ловушку dotenv):

```
DATABASE_URL="postgresql://...test_jwt" ./venv/bin/python migrations/add_push_tables.py
```

## 6. Проверка отправки (до Flutter)

Получив реальный FCM-токен с устройства (он логируется при старте приложения и
шлётся на `POST /push/register`), можно проверить сервис:

```
./venv/bin/python -c "from services.fcm import send_to_tokens; \
print(send_to_tokens(['<TOKEN>'], 'Тест', 'Привет из Chatra', {'type':'test'}))"
```

---

Дальше — интеграция Flutter (firebase_core / firebase_messaging), делается после того,
как `google-services.json` и `GoogleService-Info.plist` на месте.
