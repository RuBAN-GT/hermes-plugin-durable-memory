# Durable Memory: сквозной запуск

Краткая инструкция для одного профиля Hermes.

## Установка и профиль

```bash
python3 -m pip install .
export DURABLE_MEMORY_PROFILE=main
export DURABLE_MEMORY_STORE=postgres
export DURABLE_MEMORY_MIGRATION_DATABASE_URL='postgresql://<owner>:<password>@<host>:5432/<database>'
```

Значения передаются менеджером процесса. Плагин сам не читает `.env`. Создайте
базу и роли PostgreSQL по основной документации, затем выполните:

```bash
hermes durable-memory migrate
hermes durable-memory bootstrap-profile --slug main --runtime-role hermes_memory_main
```

В конфигурации Hermes включите provider:

```yaml
memory:
  provider: durable-memory
```

Hermes активирует только один внешний memory provider одновременно. Без этого
пункта durable recall не будет подключён к агенту.

Не запускайте Hermes от имени владельца схемы. Для локальных тестов можно
оставить `DURABLE_MEMORY_STORE=memory`; это временное хранилище процесса.
Выдайте runtime-роли минимальные права из раздела PostgreSQL Setup в README;
миграция намеренно не предоставляет доступ роли `PUBLIC`.

## Инвентарь через диалог или tool

Попросите Hermes: «создай инвентарь `person` с обязательным searchable-полем».
Hermes может подготовить заявку через tool, но не может сам её подтвердить.

```text
hermes durable-memory create-inventory --type person --fields '{"name":{"kind":"string","required":true,"searchable":true},"age":{"kind":"integer","filterable":true}}'
```

После подтверждения предложите запись через `propose` с `--payload` JSON. Для
общего пространства сначала создайте namespace и выдайте профильную capability
`read` или `propose`.

## Подтверждения

При политике `require` результат изменения имеет статус `pending`. Выполните
`hermes durable-memory pending`, проверьте операцию, затем:

```text
hermes durable-memory approve --request-id <request-id>
```

Отклонение выполняется через `reject`. Не называйте pending-запись сохранённой.
В Telegram плагин запрашивает одноразовое решение кнопками через публичный
`ctx.human_decisions`. Для этого выдайте capability
`gateway.human_decisions` стандартным consent flow Hermes. Решение привязано к
исходной gateway-сессии и принять его может только её actor.

Если capability не выдана, платформа не поддерживается, сессия устарела,
доставка не удалась или вышел timeout, заявка остаётся `pending`. Её можно
подтвердить явно через `/durable-memory approve --request-id <request-id>`.
Этот flow не меняет `ApprovalPolicy` и не является auto-approve или tool
approval; AI tool по-прежнему не может вызывать `approve`, `reject`, `grant`,
миграции или bootstrap.

## Поиск и фильтры

```text
hermes durable-memory search --query Ada --type person --filters '{"age":{"gte":18}}'
```

В PostgreSQL используется текущий полнотекстовый поиск PostgreSQL FTS и
фильтрация JSON-полей по объявленным `filterable` полям. Векторного столбца и
hybrid/vector retrieval пока нет.

## Ollama

Это необязательный адаптер без новых зависимостей:

```bash
export DURABLE_MEMORY_EMBEDDING_PROVIDER=ollama
export DURABLE_MEMORY_OLLAMA_BASE_URL=http://127.0.0.1:11434
export DURABLE_MEMORY_OLLAMA_MODEL=nomic-embed-text
```

Он вызывает только `POST /api/embed`. При неполной конфигурации, сетевой или
форматной ошибке адаптер возвращает отсутствие embedding и не ломает обычный
поиск. Embedding готов для будущей проекции, но сейчас не сохраняется и не
участвует в retrieval.

## OpenCode и Codex

Скопируйте `integrations/opencode/durable-memory.ts` в
`.opencode/plugins/durable-memory.ts`; пользовательский OpenCode config менять не
нужно. Шаблон вызывает установленный CLI безопасно, без shell.

Для Codex используйте `integrations/codex/AGENTS.md` как проектный `AGENTS.md`.
Он требует Hermes CLI/tool, соблюдение approval policy и изоляцию профилей.
