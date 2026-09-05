"""Plugin messages aligned with Hermes' active language selection."""

from __future__ import annotations

import os
from typing import Any

_MESSAGES = {
    "en": {
        "unsafe_runtime_invalid": "DURABLE_MEMORY_DANGER_ALLOW_UNSAFE_RUNTIME must be true or false.",
        "unsafe_runtime_warning": "DANGER: unsafe runtime role allowed. Database privileges can bypass profile isolation and approval. Plugin writes still follow approval policy.",
        "invalid_args": "Those arguments could not be read: {error}",
        "unknown_action": "Unknown action «{action}».\n{usage}",
        "unknown_profile": "Profile «{slug}» was not found.",
        "unknown_namespace": "Namespace «{slug}» was not found.",
        "unknown_record": "That memory record was not found.",
        "unknown_request": "That approval request was not found.",
        "unknown_operation": "Operation must be create, update, or delete.",
        "namespace_exists": "Namespace «{slug}» already exists.",
        "private_namespace_owned": "Private namespaces are created automatically for each profile.",
        "private_namespace_reserved": "Shared namespaces cannot use the reserved profile: prefix.",
        "private_namespace_taken": "The private namespace slug is owned by another namespace.",
        "namespace_kind_invalid": "Namespace kind must be private or shared.",
        "capability_invalid": "Access must be read, propose, approve, or admin.",
        "grant_admin_only": "Only a namespace admin can grant access.",
        "missing_capability": "You do not have {capability} access to «{namespace}».",
        "create_has_record_id": "Create cannot target an existing record.",
        "mutation_needs_record_id": "Update and delete need --record-id.",
        "record_wrong_namespace": "That record does not belong to this namespace.",
        "record_not_active": "Only active records can be changed.",
        "identity_taken": "An active {type} named «{identity}» already exists.",
        "candidate_assessed": "Candidate assessed as {assessment}; no create request was made.",
        "decision_invalid": "Choose approve or reject.",
        "postgres_unimplemented": "PostgreSQL storage is not ready yet. Use DURABLE_MEMORY_STORE=memory for local tests.",
        "database_url_missing": "Set DURABLE_MEMORY_DATABASE_URL to use the PostgreSQL store.",
        "migration_url_missing": "Set DURABLE_MEMORY_MIGRATION_DATABASE_URL to run migrations.",
        "migrations_applied": "Database is ready. Applied {count} migration(s).",
        "migration_status": "Migrations: {applied} of {total} applied.",
        "usage_bootstrap_profile": "Use /durable-memory bootstrap-profile --slug <profile> --runtime-role <postgres-role>",
        "profile_bootstrapped": "Profile «{slug}» is bound to PostgreSQL role «{role}».",
        "store_invalid": "DURABLE_MEMORY_STORE must be memory or postgres.",
        "profile_empty": "Memory profile name must not be empty.",
        "ttl_invalid": "Approval lifetime must be a positive number of seconds.",
        "policy_invalid": "Approval policy for {name} must be require, auto, or deny.",
        "usage_search": "Search like this: /durable-memory search --query <text>",
        "usage_create_namespace": "Create a namespace like this: /durable-memory create-namespace --slug <name> --kind shared",
        "usage_grant": "Grant access like this: /durable-memory grant --namespace <name> --profile <name> --capability read",
        "usage_propose": "Propose a change like this: /durable-memory propose --operation create --type fact --identity <key> --text <text> [--replace true|false]",
        "usage_create_identity": "Creating a record needs --identity.",
        "usage_decide": "Use /durable-memory {command} --request-id <id>",
        "namespace_required": "Specify --namespace <name>.",
        "expected_revision_int": "--expected-revision must be a number.",
        "replace_bool": "--replace must be true or false.",
        "ranked_cursor_unsupported": "Ranked text search does not support cursors without an explicit schema sort.",
        "schema_search_namespace_required": "Schema filters and sorting require an explicit namespace.",
        "identity_immutable": "A record identity cannot be changed or removed.",
        "unexpected_argument": "Unexpected argument: {token}",
        "option_duplicate": "Option specified more than once: {token}",
        "option_missing_value": "Missing value for {token}.",
        "operation_denied": "The profile policy does not allow {operation}.",
        "capability_read": "read",
        "capability_propose": "propose",
        "capability_approve": "approve",
        "capability_admin": "admin",
        "operation_create": "create",
        "operation_update": "update",
        "operation_delete": "delete",
        "kind_private": "private",
        "kind_shared": "shared",
        "status_pending": "pending",
        "status_approved": "approved",
        "status_rejected": "rejected",
        "status_expired": "expired",
        "status_superseded": "out of date",
        "store_memory": "in-memory (temporary)",
        "store_postgres": "PostgreSQL",
        "decision_approve": "approve",
        "decision_reject": "reject",
        "doctor": "Durable memory is using {store} for profile «{profile}».\nApproval: create {create}, update {update}, delete {delete}.\nPostgreSQL backend: {postgres}.",
        "postgres_connected": "connected",
        "postgres_not_connected": "not connected",
        "doctor_ephemeral": "This store is for tests only and will not survive a restart.",
        "namespaces_empty": "No namespaces are visible to profile «{profile}».",
        "namespaces_header": "Namespaces for «{profile}»:",
        "namespaces_item": "{index}. {slug} ({kind}{owner})",
        "namespaces_owner": ", owner",
        "namespace_created": "Created {kind} namespace «{slug}».",
        "usage_create_inventory": "Create an inventory with --type <name> and --fields <JSON>.",
        "inventory_exists": "Inventory «{type}» already exists.",
        "inventory_definition_immutable": "Inventory definitions can only be created with create-inventory.",
        "inventories_found": "Found {count} inventory definition(s).",
        "fields_json_invalid": "Inventory fields must be valid JSON.",
        "fields_json_object": "Inventory fields must be a non-empty JSON object.",
        "inventory_field_invalid": "Each inventory field needs an object definition.",
        "inventory_field_kind": "Unsupported inventory field kind: {kind}.",
        "json_invalid": "{name} must be valid JSON.",
        "json_object": "{name} must be a JSON object.",
        "payload_unknown": "Unknown payload field(s): {fields}.",
        "payload_required": "Payload field «{name}» is required.",
        "payload_kind": "Payload field «{name}» must be a {kind}.",
        "filter_not_allowed": "Field «{name}» is not filterable.",
        "grant_ok": "Granted {capability} on «{namespace}» to «{profile}».",
        "search_empty": "No memory records matched «{query}».",
        "search_header": "Found {count} record(s) for «{query}»:",
        "search_item": "{index}. {text}\n    {type} · {identity}",
        "prefetch_header": "Durable memory:",
        "pending_empty": "There are no pending memory changes.",
        "pending_header": "Pending memory changes:",
        "pending_item": "{index}. {operation} {type} «{identity}»: {text}\n    Approve: /durable-memory approve --request-id {id}\n    Reject:  /durable-memory reject --request-id {id}",
        "proposed_pending": "Proposed {operation} of {type} «{identity}». Waiting for approval.\nRequest: {id}\nApprove: /durable-memory approve --request-id {id}\nReject:  /durable-memory reject --request-id {id}",
        "proposed_approved": "Saved {type} «{identity}».",
        "human_decision_title": "Approve memory change?",
        "human_decision_body": "Review this durable-memory request.\n\nOperation: {operation}\nType: {type}\nIdentity: {identity}\nText: {text}\nRequest: {id}",
        "human_decision_unavailable": "Inline approval is unavailable ({error}). The request remains pending and can be resolved with the commands above.",
        "decided_approved_create": "Approved. Saved {type} «{identity}».",
        "decided_approved_update": "Approved. Updated {type} «{identity}».",
        "decided_approved_delete": "Approved. Removed {type} «{identity}».",
        "decided_rejected": "Rejected the {operation} of {type} «{identity}».",
        "decided_expired": "This approval request has expired and was not applied.",
        "decided_superseded": "This change is out of date and was not applied. «{identity}» has already changed.",
        "decided_already": "This request was already {status}.",
        "usage": (
            "Usage:\n"
            "  /durable-memory doctor\n"
            "  /durable-memory migrate\n"
            "  /durable-memory migration-status\n"
            "  /durable-memory bootstrap-profile --slug <profile> --runtime-role <postgres-role>\n"
            "  /durable-memory namespaces\n"
            "  /durable-memory create-namespace --slug <name> --kind shared\n"
            "  /durable-memory grant --namespace <name> --profile <name> --capability read|propose|approve|admin\n"
            "  /durable-memory search --query <text> [--namespace <name>]\n"
            "  /durable-memory propose --operation create|update|delete --type <type> --identity <key> --text <text> [--namespace <name>] [--record-id <id>] [--replace true|false]\n"
            "  /durable-memory pending\n"
            "  /durable-memory approve --request-id <id>\n"
            "  /durable-memory reject --request-id <id>"
        ),
    },
    "ru": {
        "unsafe_runtime_invalid": "DURABLE_MEMORY_DANGER_ALLOW_UNSAFE_RUNTIME должен быть true или false.",
        "unsafe_runtime_warning": "ОПАСНЫЙ РЕЖИМ: разрешена привилегированная роль БД. Её права позволяют обходить изоляцию профилей и согласование. Записи через плагин по-прежнему следуют политике согласования.",
        "invalid_args": "Не получилось прочитать аргументы: {error}",
        "unknown_action": "Неизвестное действие «{action}».\n{usage}",
        "unknown_profile": "Профиль «{slug}» не найден.",
        "unknown_namespace": "Пространство «{slug}» не найдено.",
        "unknown_record": "Запись памяти не найдена.",
        "unknown_request": "Заявка на подтверждение не найдена.",
        "unknown_operation": "Операция должна быть create, update или delete.",
        "namespace_exists": "Пространство «{slug}» уже существует.",
        "private_namespace_owned": "Личные пространства создаются автоматически для каждого профиля.",
        "private_namespace_reserved": "Общие пространства не могут использовать зарезервированный префикс profile:.",
        "private_namespace_taken": "Slug личного пространства занят другим пространством.",
        "namespace_kind_invalid": "Тип пространства: private или shared.",
        "capability_invalid": "Доступ: read, propose, approve или admin.",
        "grant_admin_only": "Выдавать доступ может только администратор пространства.",
        "missing_capability": "У вас нет права «{capability}» в пространстве «{namespace}».",
        "create_has_record_id": "Создание не может ссылаться на существующую запись.",
        "mutation_needs_record_id": "Для изменения и удаления нужен --record-id.",
        "record_wrong_namespace": "Эта запись принадлежит другому пространству.",
        "record_not_active": "Менять можно только активные записи.",
        "identity_taken": "Активная запись {type} «{identity}» уже есть.",
        "decision_invalid": "Нужно выбрать approve или reject.",
        "postgres_unimplemented": "Хранилище PostgreSQL ещё не подключено. Для локальных тестов укажите DURABLE_MEMORY_STORE=memory.",
        "database_url_missing": "Для PostgreSQL-хранилища укажите DURABLE_MEMORY_DATABASE_URL.",
        "migration_url_missing": "Для миграций укажите DURABLE_MEMORY_MIGRATION_DATABASE_URL.",
        "migrations_applied": "База готова. Применено миграций: {count}.",
        "migration_status": "Миграции: применено {applied} из {total}.",
        "usage_bootstrap_profile": "Используйте /durable-memory bootstrap-profile --slug <профиль> --runtime-role <postgres-роль>",
        "profile_bootstrapped": "Профиль «{slug}» привязан к PostgreSQL-роли «{role}».",
        "store_invalid": "DURABLE_MEMORY_STORE должен быть memory или postgres.",
        "profile_empty": "Имя профиля памяти не должно быть пустым.",
        "ttl_invalid": "Срок подтверждения должен быть положительным числом секунд.",
        "policy_invalid": "Политика подтверждения для {name}: require, auto или deny.",
        "usage_search": "Поиск: /durable-memory search --query <текст>",
        "usage_create_namespace": "Создание пространства: /durable-memory create-namespace --slug <имя> --kind shared",
        "usage_grant": "Выдача доступа: /durable-memory grant --namespace <имя> --profile <имя> --capability read",
        "usage_propose": "Предложение изменения: /durable-memory propose --operation create --type fact --identity <ключ> --text <текст> [--replace true|false]",
        "usage_create_identity": "Для создания записи нужен --identity.",
        "usage_decide": "Используйте /durable-memory {command} --request-id <id>",
        "namespace_required": "Укажите --namespace <имя>.",
        "expected_revision_int": "--expected-revision должен быть числом.",
        "replace_bool": "--replace должен быть true или false.",
        "ranked_cursor_unsupported": "Ранжированный текстовый поиск не поддерживает курсоры без явной сортировки по схеме.",
        "schema_search_namespace_required": "Фильтры и сортировка по схеме требуют явного пространства.",
        "identity_immutable": "Идентификатор записи нельзя изменить или удалить.",
        "unexpected_argument": "Неожиданный аргумент: {token}",
        "option_duplicate": "Параметр указан больше одного раза: {token}",
        "option_missing_value": "Не хватает значения для {token}.",
        "operation_denied": "Политика профиля запрещает операцию {operation}.",
        "capability_read": "чтение",
        "capability_propose": "предложение",
        "capability_approve": "подтверждение",
        "capability_admin": "администрирование",
        "operation_create": "создание",
        "operation_update": "изменение",
        "operation_delete": "удаление",
        "kind_private": "личное",
        "kind_shared": "общее",
        "status_pending": "ожидает",
        "status_approved": "подтверждено",
        "status_rejected": "отклонено",
        "status_expired": "истекло",
        "status_superseded": "устарело",
        "store_memory": "в памяти (временное)",
        "store_postgres": "PostgreSQL",
        "decision_approve": "approve",
        "decision_reject": "reject",
        "doctor": "Durable memory использует {store} для профиля «{profile}».\nПодтверждение: создание {create}, изменение {update}, удаление {delete}.\nPostgreSQL: {postgres}.",
        "postgres_connected": "подключён",
        "postgres_not_connected": "не подключён",
        "doctor_ephemeral": "Это хранилище только для тестов и не переживает перезапуск.",
        "namespaces_empty": "Для профиля «{profile}» нет видимых пространств.",
        "namespaces_header": "Пространства профиля «{profile}»:",
        "namespaces_item": "{index}. {slug} ({kind}{owner})",
        "namespaces_owner": ", владелец",
        "namespace_created": "Создано {kind} пространство «{slug}».",
        "usage_create_inventory": "Создание инвентаря: --type <имя> и --fields <JSON>.",
        "inventory_exists": "Инвентарь «{type}» уже существует.",
        "inventory_definition_immutable": "Определения инвентарей создаются только через create-inventory.",
        "inventories_found": "Найдено определений инвентарей: {count}.",
        "fields_json_invalid": "Поля инвентаря должны быть валидным JSON.",
        "fields_json_object": "Поля инвентаря должны быть непустым JSON-объектом.",
        "inventory_field_invalid": "Для каждого поля инвентаря нужно объектное определение.",
        "inventory_field_kind": "Неподдерживаемый тип поля инвентаря: {kind}.",
        "json_invalid": "{name} должен быть валидным JSON.",
        "json_object": "{name} должен быть JSON-объектом.",
        "payload_unknown": "Неизвестные поля payload: {fields}.",
        "payload_required": "Поле payload «{name}» обязательно.",
        "payload_kind": "Поле payload «{name}» должно иметь тип {kind}.",
        "filter_not_allowed": "По полю «{name}» нельзя фильтровать.",
        "grant_ok": "Профилю «{profile}» выдано право «{capability}» в пространстве «{namespace}».",
        "search_empty": "По запросу «{query}» ничего не найдено.",
        "search_header": "Найдено записей: {count} по запросу «{query}»:",
        "search_item": "{index}. {text}\n    {type} · {identity}",
        "prefetch_header": "Durable memory:",
        "pending_empty": "Нет изменений, ожидающих подтверждения.",
        "pending_header": "Изменения, ожидающие подтверждения:",
        "pending_item": "{index}. {operation} {type} «{identity}»: {text}\n    Подтвердить: /durable-memory approve --request-id {id}\n    Отклонить:   /durable-memory reject --request-id {id}",
        "proposed_pending": "Предложено: {operation} {type} «{identity}». Ждёт подтверждения.\nЗаявка: {id}\nПодтвердить: /durable-memory approve --request-id {id}\nОтклонить:   /durable-memory reject --request-id {id}",
        "proposed_approved": "Сохранено: {type} «{identity}».",
        "human_decision_title": "Подтвердить изменение памяти?",
        "human_decision_body": "Проверьте заявку durable-memory.\n\nОперация: {operation}\nТип: {type}\nИдентификатор: {identity}\nТекст: {text}\nЗаявка: {id}",
        "human_decision_unavailable": "Подтверждение кнопкой недоступно ({error}). Заявка осталась в очереди; используйте команды выше.",
        "decided_approved_create": "Подтверждено. Сохранено: {type} «{identity}».",
        "decided_approved_update": "Подтверждено. Обновлено: {type} «{identity}».",
        "decided_approved_delete": "Подтверждено. Удалено: {type} «{identity}».",
        "decided_rejected": "Отклонено: {operation} {type} «{identity}».",
        "decided_expired": "Срок этой заявки истёк, изменение не применено.",
        "decided_superseded": "Заявка устарела и не применена. «{identity}» уже изменилась.",
        "decided_already": "Эта заявка уже в статусе «{status}».",
        "usage": (
            "Использование:\n"
            "  /durable-memory doctor\n"
            "  /durable-memory migrate\n"
            "  /durable-memory migration-status\n"
            "  /durable-memory bootstrap-profile --slug <профиль> --runtime-role <postgres-роль>\n"
            "  /durable-memory namespaces\n"
            "  /durable-memory create-namespace --slug <имя> --kind shared\n"
            "  /durable-memory grant --namespace <имя> --profile <имя> --capability read|propose|approve|admin\n"
            "  /durable-memory search --query <текст> [--namespace <имя>]\n"
            "  /durable-memory propose --operation create|update|delete --type <тип> --identity <ключ> --text <текст> [--namespace <имя>] [--record-id <id>] [--replace true|false]\n"
            "  /durable-memory pending\n"
            "  /durable-memory approve --request-id <id>\n"
            "  /durable-memory reject --request-id <id>"
        ),
    },
}


def t(key: str, **values: Any) -> str:
    """Translate a plugin message using Hermes' process-wide language setting."""
    language = _language()
    template = _MESSAGES.get(language, _MESSAGES["en"]).get(key)
    if template is None:
        template = _MESSAGES["en"][key]
    return template.format(**values)


def _language() -> str:
    try:
        from agent.i18n import get_language

        language = str(get_language() or "").strip().lower()
        if language.startswith("ru"):
            return "ru"
        if language:
            return "en"
    except Exception:
        pass
    value = (os.environ.get("HERMES_LANGUAGE") or "en").strip().lower()
    if value.startswith("ru"):
        return "ru"
    return "en"
