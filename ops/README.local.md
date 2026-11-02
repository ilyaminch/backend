# Локальный API для frontend

Внешний HTTPS-стенд для кураторов описан отдельно в `README.public.md`.
Публичный профиль использует другой Origin и Secure-cookie; не смешивайте его
с командами локального профиля ниже во время демонстрации.

Стенд содержит backend с установленным MLChat и PostGIS. `init` — однократная
команда миграций, прав и локальных аккаунтов; постоянного третьего приложения нет.
В базовом профиле реальная LLM и демонабор не устанавливаются. Дополнительные
compose.ollama.yml и compose.demo.yml подключают установленную модель и пакет.
Это локальный профиль интеграции,
не сетевой выпуск полного MVP. CI и обычный `backend/Dockerfile` не меняются.

## Подготовка и запуск

Репозитории должны находиться рядом: `MDP/backend` и `MDP/mlchat`.
MLChat зафиксирован ревизией и SHA-256 исходников в `mlchat-reference.json`.
Нужны Python, Docker Desktop с Linux-контейнерами, Docker Compose с поддержкой
named build contexts; профиль проверен на Docker 29.1.3 / Compose 2.40.3.
Первой сборке нужен доступ к PyPI, Docker Hub и ghcr.io.

Из корня репозитория backend:

```powershell
python ops/prepare_local.py
docker compose -f ops/compose.local.yml up --build --detach --wait --wait-timeout 180 backend
docker compose -f ops/compose.local.yml ps -a
```

Если Python уже есть в рабочем backend-окружении, первую команду можно выполнить
через `backend/.venv/Scripts/python.exe`. Скрипт подготовки использует только
стандартную библиотеку и не выполняет Git-команд. Для другого расположения MLChat
есть `--mlchat-dir ПУТЬ`. Изменившиеся исходники MLChat не подхватываются молча:
их версия и эталон обновляются владельцем backend после проверки совместимости.

В Docker отправляются только проверенные файлы MLChat, а не его `.git`, окружение,
эксперименты или секреты. Python/uv/PostGIS образы закреплены digest; runtime-
зависимости backend берутся из uv.lock, MLChat собирается в wheel и устанавливается
без замены зависимостей backend. В образе проверяется совместимость пакетов.

Готовый статус: db и backend `healthy`, init `Exited (0)`. Инициализация ждёт
готовности TCP-сервера PostGIS, применяет Alembic и runtime GRANT, создаёт аккаунты.
Повторный запуск не сбрасывает существующие пароли, роли, сессии и геоданные.
При несовпадении локального профиля аккаунта init завершится ошибкой, не перезапишет его.

## Адреса и вход

- API: `http://127.0.0.1:8000/api/v1`.
- OpenAPI: `http://127.0.0.1:8000/openapi.json`.
- Swagger: `http://127.0.0.1:8000/docs` (просмотр схемы).
- Frontend: `http://localhost:5173`.
- PUBLIC_ORIGIN: `http://localhost:5173`; COOKIE_SECURE=false только локально.

Frontend вызывает относительный `/api/v1/...`; его dev-server проксирует `/api`
на `http://127.0.0.1:8000`, сохраняя путь и Origin браузера. Без такого proxy прямой
cross-origin вызов не настроен. Открывайте frontend именно через localhost:5173;
127.0.0.1:5173 — другой Origin. Вход через Swagger на 127.0.0.1:8000 тоже имеет
другой Origin и закономерно отклоняется. Для ручного HTTP-клиента задавайте
Origin явно, равным PUBLIC_ORIGIN.

Логины: `frontend_user` (user), `frontend_admin` (admin). Случайные пароли находятся
в `.local/frontend_accounts.json` на машине оператора. Получите их локально;
не переносите файл/пароли в Git, frontend-сборку, скриншоты или отчёты.
Это прикладные аккаунты ГИС, они не связаны с GitLab-аккаунтами разработчиков.

Вход: POST `/auth/login` с JSON login/password и корректным Origin; cookie
хранится браузером. После входа POST/DELETE требуют X-CSRF-Token из login/me.
Параметры cookie и остальные контракты описаны в OpenAPI.

## Что доступно без модели и данных

Вход обеих ролей, реестр шести слоёв, GeoJSON слоёв, прямой `/search`, API карточек,
создание/удаление chat-сессий и административные методы доступны на реальном API.
При отсутствии данных слой/поиск возвращает пустой успешный результат, неизвестная
карточка — 404; районный подсчёт требует четыре загруженных района.

MLChat подключён как Python-фабрика (базовая `mlchat.integration:create_dialogue`), а не отдельным HTTP-
сервисом. Ранние отказы MLChat могут работать без модели. Сообщения, требующие
генерации, возвращают 503 LLM_UNAVAILABLE с сохранением карты; это ожидаемый
режим базового профиля, а не имитация ответа LLM. В базовом профиле LLM_BASE_URL/
LLM_MODEL не назначаются автоматически; профиль Ollama ниже подключает модель.

Геоданные автоматически не загружаются. Готовый пакет500+4 находится в
`data/demo/package`, подключается через `ops/compose.demo.yml` read-only.
Механизм первоначальной загрузки/restore описан в `data/demo/README.md`.
Без подключения пакета restore возвращает DEMO_PACKAGE_UNAVAILABLE.
Самостоятельный небольшой UI fixture не становится реальным демонабором 500+4.

В `data/verification` поставлен отдельный проверочный набор 28 объектов и 4 районов.
Он не загружается автоматически и не является пакетом восстановления 500+4.
Подготовка импортных файлов: `python ops/prepare_verification.py`; затем admin API
импорта каждого слоя. Назначение и происхождение — `data/verification/README.md`.
Для проверки малого набора используйте `data/verification/imports/school.geojson`
(6школ); для полного демостенда — `data/demo/package/school.geojson` (123школы),
слой `school`. Импорт заменяет целый слой.

## Подключение Ollama на Windows

Дополнительный профиль использует Ollama на хосте. Сначала установите модель
по поставляемому MLChat `config/ollama/Modelfile.qwen35-9b`:

```powershell
ollama pull qwen3.5:9b
ollama create mlchat-qwen35-9b -f ../mlchat/config/ollama/Modelfile.qwen35-9b
docker compose -f ops/compose.local.yml -f ops/compose.ollama.yml up --build --detach --wait --wait-timeout 180 backend
```

MLChat 0.1.3: фабрика `mlchat.integration:create_qwen35_9b_dialogue`.
Профиль модели: num_ctx=8192, num_predict=512, temperature=0, top_p=0.8,
top_k=20, min_p=0, repeat_penalty=1, presence_penalty=frequency_penalty=0.
Фабрика передаёт temperature=0, top_p=0.8 и reasoning_effort=none в HTTP-запросе,
отключая рассуждения. Backend использует json_schema и прежний единичный вызов.
Промпты и параметры профиля поставляются разработчиком MLChat, не дублируются в API.
Общий deadline остаётся 300 секунд. Без LLM_TEMPERATURE transport сохраняет
параметры runtime по умолчанию; значение конфигурации допускает диапазон 0–2.
Адрес внутри контейнера — host.docker.internal:11434; доступность Ollama проверяйте
из контейнера. Открывать Ollama в локальную сеть для этого не требуется.
При управлении стендом с моделью передавайте оба файла Compose, иначе backend
вернётся к базовому профилю без модели. Веса сохраняются в хранилище Ollama
Windows и не включаются в образ backend или Git. Начальные геоданные отдельно.

Для полного локального стенда добавьте третий файл:

```powershell
docker compose -f ops/compose.local.yml -f ops/compose.ollama.yml -f ops/compose.demo.yml up --detach --wait --wait-timeout 180 backend
```

Тот же набор из трёх файлов используйте для ps/logs/stop/up. Пакет установлен
read-only, но автоматического restore при up нет: это отдельное действие admin.

## Секреты и данные

Подготовка создаёт игнорируемый каталог `.local` вне Docker build context backend:
owner/runtime env, файлы паролей БД, frontend_accounts.json, минимальные исходники
MLChat и несекретную запись его версии. Повторная подготовка сохраняет секреты.
Неполный набор файлов требует восстановления существующей конфигурации;
автоматической ротации или удаления данных скрипт не делает.

Backend работает UID 10001 и подключается как gis_runtime без superuser/DDL.
Подключение владельца передаётся только init. Порт БД наружу не публикуется.
Данные хранятся в постоянном Compose-томе; сохраняйте также `.local` с его
учётными данными. Runtime env содержит закрытый DSN и не предназначен frontend.

```powershell
docker compose -f ops/compose.local.yml logs --tail 50 backend init
docker compose -f ops/compose.local.yml stop
# Повторный запуск без пересборки, миграции/idempotent init выполнятся при необходимости:
docker compose -f ops/compose.local.yml up --detach --wait --wait-timeout 180 backend
# Удаление контейнеров/сети с сохранением тома БД:
docker compose -f ops/compose.local.yml down
```

Не используйте `down --volumes` для обычного перезапуска: это удаление БД.
Не удаляйте `.local`, пока нужен соответствующий постоянный том.
Обновление приложения: снова prepare_local.py и `up --build ... backend` после
согласованного обновления исходников. Для сетевого доступа нужен отдельный
HTTPS-профиль, новые Origin и secure-cookie; этот стенд публикует только loopback.
