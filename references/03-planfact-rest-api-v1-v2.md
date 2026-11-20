# Инженерный справочник REST API «ПланФакта» (`api.planfact.io` v1 и v2)

Полная техническая спецификация интеграции с публичным REST API сервиса «ПланФакт» (выверена по схеме OpenAPI от `05.11.2026`, `136` эндпоинтов, и опыту промышленных интеграций).

---

## 1. Подключение, авторизация, контракты ответов и коды ошибок

### 1.1. Базовые адреса и выпуск ключа `X-ApiKey`
- **Базовые URL:**
  - `https://api.planfact.io/api/v1` — основной контур справочников, денежных операций, сделок, Баланса и журналов аудита;
  - `https://api.planfact.io/api/v2` — актуальные начисления (`operations/accrual`), отчёты `reports/dds` и `reports/opu`, а также аналитика `dashboardpro/*`.
- **Получение ключа:** личный кабинет `https://app.planfact.io/manage/security-settings` (`Настройки → API ПланФакта`). Владелец аккаунта предварительно включает сотруднику флаг «Разрешить доступ к API» в разделе `Настройки → Пользователи`.
- **Наследование прав (`STOP-RULE`):** ключ `X-ApiKey` наследует все ролевые и проектные ограничения пользователя, который его выпустил. Если пользователю ограничили видимость проектов или счетов, API вернёт урезанный остаток и откажет в формировании Баланса. Для интеграций и ревизии выпускай ключ под полными правами Владельца / Администратора.
- **Обязательные заголовки каждого HTTP-запроса:**
  ```http
  X-ApiKey: <PLANFACT_API_KEY>
  Accept: application/json
  Content-Type: application/json
  ```
  *(В спецификации вендора прямо зафиксировано: без заголовка `Content-Type: application/json` корректная обработка запросов сервером не гарантируется даже для чтения.)*

### 1.2. Единая обёртка ответов `CustomResponse<T>` и списки `ListModel<T>`
Любой ответ API (одиночная сущность, список или расчёт) упакован в конверт `CustomResponse<T>`:

```json
{
  "data": {
    "items": [],
    "total": 0,
    "deletedItems": null,
    "totalDeleted": 0
  },
  "isSuccess": true,
  "errorMessage": null,
  "errorCode": "None",
  "errorCodeTitle": "None",
  "errors": null
}
```

- **Проверка бизнес-успеха:** статус `HTTP 200 OK` означает лишь доставку ответа транспортным уровнем. Бизнес-ошибки валидации приходят со статусом `200` и полем `"isSuccess": false`. Клиент обязан проверять `payload.get("isSuccess") is True`.
- **Типовые коды `errorCode` (`BusinessExceptionStatusCodeEnum`):**
  - `NotEnoughApiPermission` / `AccountAccessDenied` — нехватка прав пользователя ключа;
  - `OperationNotFound` — запрошен несуществующий или удалённый `operationId`;
  - `OperationCommitInFuture` — попытка передать `"isCommitted": true` для кассовой операции с датой в будущем;
  - `OperationNotDistributedValue` / `InvalidOperationParts` — сумма элементов сплита `items[]` расходится с общей суммой операции либо нарушена структура частей;
  - `OperationsAlreadyConsolidate` — операции уже объединены в `Перемещение`;
  - `RulesForOperationCategoryExist` / `RulesForAccountExist` — попытка удалить статью или счёт, к которым привязаны правила импорта (обходится флагом `?ignoreRules=true`).
- **Списочная модель `ListModel<T>`:** содержит массив `items`, полный размер выборки `total` (используется в цикле пагинации), а при передаче `filter.changesFromDate` дополнительно заполняет `deletedItems: [{ "entityId": 105, "deleteDate": "..." }]` и `totalDeleted`.

### 1.3. Коды HTTP-статусов

| HTTP-код | Причина возврата | Действие клиента |
| :---: | :--- | :--- |
| **`200 OK`** | Штатный ответ сервера. | Проверить `isSuccess == true`; при `false` поднять исключение с `errorCode` и `errorMessage`. |
| **`401 Unauthorized`** | Пустой, отозванный или повреждённый `X-ApiKey`. | Остановить ретраи, проверить переменную `PLANFACT_API_KEY` (в логах маскировать как `pf_***`). |
| **`403 Forbidden`** | Нет доступа к методу/объекту **ИЛИ исчерпана месячная квота API-запросов**. | Проверить заголовок `X-Quota-Remaining`: если `0` — дождаться `X-Quota-Reset` или расширить тариф. |
| **`429 Too Many Requests`** | **(1)** Превышен минутный лимит RPM **ИЛИ (2)** нарушена блокировка параллельной записи (`> 1` write-запроса одновременно). | Выполнить повтор с экспоненциальным backoff (`1 с → 2 с → 4 с` + случайный jitter), сериализовав запись через per-tenant мьютекс. |
| **`500 / 502 / 503 / 504`** | Временная недоступность шлюза или внутренняя ошибка сервера. | На `502 / 503 / 504` и сетевые таймауты сделать до 3 повторов с backoff; на `500` залогировать тело без утечки ключа. |

---

## 2. Квоты, Single-Write Lock, пагинация и правила `PUT`

### 2.1. Заголовки контроля лимитов и квот
В каждом HTTP-ответе сервер возвращает телеметрию расхода тарифного пакета:
- `X-RateLimit-Limit` — допустимое число запросов в минуту (RPM);
- `X-Quota-Limit` — общий месячный лимит запросов аккаунта;
- `X-Quota-Used` — израсходовано запросов с начала месяца;
- `X-Quota-Remaining` — остаток запросов до конца месяца (рекомендуется сигнализировать при `< 500`);
- `X-Quota-Reset` — Unix Timestamp обнуления квоты в начале следующего месяца.

### 2.2. Однопоточная запись на бизнес (`Single-Write Lock`)
С `10.03.2025` платформа ввела строгую сериализацию мутаций: внутри одного бизнеса одновременно исполняется **максимум один запрос `POST`, `PUT` или `DELETE`** (за исключением read-only списков `POST .../list` и расчётных отчётов). Отправка второго мутирующего запроса до получения ответа на первый немедленно завершается ошибкой `HTTP 429`.

Эталонный паттерн защиты асинхронного клиента:
```python
import asyncio

_TENANT_WRITE_LOCKS: dict[str, asyncio.Lock] = {}

def get_tenant_lock(api_key: str) -> asyncio.Lock:
    tenant_fingerprint = api_key[:12]
    if tenant_fingerprint not in _TENANT_WRITE_LOCKS:
        _TENANT_WRITE_LOCKS[tenant_fingerprint] = asyncio.Lock()
    return _TENANT_WRITE_LOCKS[tenant_fingerprint]
```
В распределённых микросервисах с несколькими воркерами локальный `asyncio.Lock` заменяется на распределённый Redis-лок (`SET pf:write_lock:{company_id} <token> NX EX 30`) либо выделенную очередь RabbitMQ с `prefetch=1` на тенанта.

### 2.3. Пагинация и инкрементальная синхронизация (без вебхуков)
- **Явный размер страницы `100`:** в схеме OpenAPI для `paging.limit` соседствуют формулировки `«от 1 до 100»` и `«по умолчанию 10000»`. Во избежание тихой обрезки выборки или ошибок валидации всегда передавай:
  - для `GET`-списков: `?paging.limit=100&paging.offset=0`, увеличивая `paging.offset += 100` пока `offset + len(items) < total`;
  - для `POST`-списков (`POST /api/v1/operations/list`, `contragents/list`, `deals/list`): query-параметры `?limit=100&offset=0&field=operationDate&direction=Asc` и JSON-фильтр в теле запроса.
- **Отслеживание дельт:** нативных исходящих вебхуков в API «ПланФакта» нет. Для фоновой синхронизации храни курсор времени и опрашивай справочники с параметром `filter.changesFromDate`, а операции — с `filter.lastModificationDateStart` / `filter.lastModificationDateEnd` (или `changesFromDate`), обрабатывая в том числе массив `deletedItems`.

### 2.4. Ловушка `PUT` (`Full-Replace`) и устаревшие URL старой справки
1. **`PUT` перезаписывает объект целиком:** если отправить в `PUT /api/v1/accounts/{accountId}` только `{"remainder": 1000}`, все опущенные поля сбросятся или вызовут ошибку валидации. Более того, в ответе `GET` связанные сущности приходят вложенными объектами (`"accountGroup": {"accountGroupId": 7}`, `"projectGroup": {"projectGroupId": 11}`), тогда как `PUT` ожидает плоские поля (`"accountGroupId": 7`, `"projectGroupId": 11`). Всегда выполняй цикл: `GET сущность → развернуть вложенные ссылки в плоские *Id → пропатчить целевое поле → отправить PUT` (для принудительного сброса `externalId` на ряде методов передаётся специальная строка `"ExternalId"`).
2. **Актуальные пути OpenAPI против устаревших HTML-алиасов:** в старых статьях документации встречались несуществующие пути `/incomeoperations`, `/outcomeoperations`, `/moveoperations`, `/accrualoperations`, а также вымышленный фильтр `filter.dateFrom`. Используй строго верифицированные пути OpenAPI: `/api/v1/operations/income`, `/api/v1/operations/outcome`, `/api/v1/operations/move`, `/api/v2/operations/accrual` и фильтры дат `operationDateStart` / `operationDateEnd`.

---

## 3. Полная карта ключевых эндпоинтов (v1 и v2)

### 3.1. Счета, остатки, валюты и юрлица

| Метод и путь | Назначение и ключевые параметры |
| :--- | :--- |
| `GET /api/v1/companies` | Список собственных юрлиц (`companyId`, `title`, `companyInn`, `companyKpp`, `active`, `existRestrictions`). |
| `GET/PUT/DELETE /api/v1/companies/{companyId}` и `POST /api/v1/companies` | Чтение и управление карточками юрлиц (`?forceUpdate` в `PUT`, `?ignoreRules` в `DELETE`). |
| `GET /api/v1/accounts` | Список счетов (`filter.agentId`, `filter.accountType` [`Cash`, `Card`, `IndividualCard`, `Electronic`], `filter.accountAcct`, `filter.currencyCode`, `filter.active`, `paging.*`). |
| `GET /api/v1/accounts/{accountId}` | Полная карточка одного счёта (`AccountModel`). |
| `POST /api/v1/accounts` / `PUT /api/v1/accounts/{accountId}` / `DELETE /api/v1/accounts/{accountId}` | Создание, полное обновление (включая корректировку `remainder` / `remainderDate` или `startingRemainderValue` / `startingRemainderDate`, флаг `?ignoreMarketplaceIntegrationsCheck`) и удаление счёта (`?ignoreRules`, `?forceDelete`). |
| `GET/POST/PUT/DELETE /api/v1/accounts/groups` | Управление группами счетов. |
| `POST /api/v2/dashboardpro/accountbalance` *(и fallback `POST /api/v1/dashboards/accountbalance`)* | Расчёт фактических и плановых остатков на дату `currentDate` и по дням периода `[periodStartDate, periodEndDate]` (`totalValuesByDays[]`, индикатор кассового разрыва `cashGap`). |
| `GET /api/v1/businessmetrics/accountbalance` | Быстрый срез текущих балансов счетов. |
| `GET /api/v1/bizinfos/accountshistory` | Подневная история изменений счетов (`dateFrom`, `dateTo` → `factValue`, `planValue`, `notComittedCount`, `accountCurrencyUserCurrencyRate`). |
| `GET /api/v1/currencies` и `GET /api/v1/currencies/history` | Справочник валют и архив курсов ЦБ РФ. |

### 3.2. Операции, начисления, сплиты и перемещения

| Метод и путь | Назначение и особенности |
| :--- | :--- |
| `POST /api/v1/operations/list` | **Основной метод выборки операций** с передачей `OperationListRequest` в JSON-теле и query-пагинацией `offset`, `limit=100`, `field`, `direction`. Выдерживает длинные массивы ID без переполнения URL. |
| `GET /api/v1/operations` | Выборка операций через query-параметры `filter.operationDateStart`, `filter.operationDateEnd`, `filter.calculationPeriodDateStart/End`, `filter.accountId`, `filter.operationType`, `filter.isCommitted`, `paging.*`. |
| `GET /api/v1/operations/{operationId}` | Чтение одной операции со всеми частями `operationParts[]` и связкой `boundMoveOperation`. |
| `POST /api/v1/operations/income` и `PUT /api/v1/operations/income/{operationId}` | Создание / полная замена `Поступления` (`IncomeOutcomeOperationCreationRequest`). |
| `POST /api/v1/operations/outcome` и `PUT /api/v1/operations/outcome/{operationId}` | Создание / полная замена `Выплаты` (`IncomeOutcomeOperationCreationRequest`). |
| `POST /api/v1/operations/move` и `PUT /api/v1/operations/move/{operationId}` | Создание / полная замена `Перемещения`. Возвращает массив из **двух** связанных `OperationModel` (`[debitOp, creditOp]`). |
| `POST /api/v2/operations/accrual` и `PUT /api/v2/operations/accrual/{operationId}` | Актуальный эндпоинт v2 для создания/обновления неденежных `Начислений` (`debitPart` и `creditPart`; версия `v1/operations/accrual` устарела). |
| `PUT /api/v1/operations/consolidate` | Схлопывание пары отдельных операций (`incomeOperationId`, `outcomeOperationId`) в единое `Перемещение`. |
| `PUT /api/v1/operations/split/{operationId}` | Разъединение `Перемещения` обратно на независимые `Выплату` и `Поступление`. |
| `PUT /api/v1/operations/change-type/{operationId}` | Смена системного типа существующей операции. |
| `DELETE /api/v1/operations/{operationId}` | Универсальное удаление операции любого типа по ID. |
| `GET /api/v1/operations/get-undistributed-operation-count` | Контрольный скаляр `data: int32` — число доходов и расходов на системных статьях «Не разобрано». |
| `GET /api/v1/operations/dates-summary` | Диапазон дат базы (`earlyOperationDate`, `lateOperationDate`, `earlyUncommittedOperationDate`, `earlyUncommittedCalculationDate`). |
| `GET/POST /api/v1/operations/summary` | Агрегированные суммы и счётчики по фильтру выборки. |
| `GET /api/v1/operations/origin` и `GET/PUT/PATCH /api/v1/operations/{operationId}/origin` | Доступ к эталонным банковским реквизитам импортированной строки (`originDate`, `originValue`, `originDocumentNumber`, `originPayerAcct`, `originRecipientAcct`). |
| `POST/PUT /api/v1/operations/shipment` и `POST/PUT /api/v1/operations/supply` | Проведение товарных/актовых `Отгрузок` и `Поставок` внутри сделок. |

### 3.3. Статьи, контрагенты, проекты, сделки, отчёты и логи

| Контур | Ключевые эндпоинты |
| :--- | :--- |
| **Статьи учёта** | `GET /api/v1/operationcategories`, `GET /api/v1/operationcategories/group-list`, `GET/POST/PUT/DELETE /api/v1/operationcategories/{id}`. |
| **Контрагенты** | `GET /api/v1/contragents`, `POST /api/v1/contragents/list`, `POST /api/v1/contragents/calculated` (расчёт ДЗ/КЗ `balanceFact`), `GET/POST/PUT/DELETE /api/v1/contragents/{id}`, `/groups`. |
| **Проекты** | `GET /api/v1/projects`, `GET /api/v1/projects/calculations` (`Cashflow`, `IncomeExpensesByOperationDate`, `IncomeExpensesByCalculationDate`), `GET/POST/PUT/DELETE /api/v1/projects/{id}`, `/groups`. |
| **Сделки** | `GET /api/v1/deals`, `POST /api/v1/deals/list`, `POST /api/v1/deals/calculated` (`dealValue`, `paidValue`, `providedValue`, `profit`, `profitability`), `GET/POST/PUT /api/v1/deals/selling` и `/purchase`, `PUT /api/v1/deals/bind/{dealId}`. |
| **Управленческие отчёты** | `POST /api/v2/reports/dds` (ДДС по статьям, проектам, сделкам), `POST /api/v2/reports/opu` (ОПиУ: кассовый метод или метод начисления `"isCalculation": true`), `POST /api/v1/accounting/balance/table` (Баланс: `assets` и `liabilities`), `POST /api/v2/dashboardpro/*`. |
| **Журналы аудита** | `GET /api/v1/importlogs` (логи загрузок выписок), `GET /api/v1/businesses` (действия пользователей), `POST /api/v1/entity-history/list` и `GET /api/v1/entity-history/{entityId}`. |

> **Ограничение по автоправилам импорта:** модель `RuleViewModel` присутствует в JSON-схемах OpenAPI, но не привязана ни к одному публичному URL-пути — читать, создавать или запускать автоправила по API нельзя, и сами правила к API-операциям не применяются.

---

## 4. Схемы ключевых моделей и эталонные JSON-запросы

### 4.1. Модель счёта (`AccountModel`) и безопасная мутация `PUT`
Ключевые поля ответа `GET /api/v1/accounts/{accountId}`:
- `accountId` (`int32`), `title`, `longTitle`, `companyId`, `accountType` (`Cash` — наличный, `Card` — безналичный р/с, `IndividualCard` — карта физлица, `Electronic` — электронный/транзитный), `currencyCode` (`RUB`, `USD`, `EUR`, `CNY`);
- `remainder` (`double` — текущий фактический остаток), `startingRemainderValue` (`double | null`), `startingRemainderDate` (`date-time | null`);
- `accountAcct` (20-значный номер счёта), `accountBik`, `accountBank`, `correspondentAcct`;
- `active` (`bool`), `isUndistributed` (`bool` — технический счёт), `isConsolidatedDeposit` (`bool` — сводный депозит овернайтов), `accountAliases[]`, `bankImportIntegrationTokenId`, `marketplaceApiIntegration`, `accountGroup` (`{ "accountGroupId": ... }`).

**Пример безопасного обновления счёта (`GET -> маппинг -> PUT`):**
```json
PUT /api/v1/accounts/10101
{
  "title": "Основной р/с Т-Банк",
  "longTitle": "Расчётный счёт ООО «Альфа Трейд» в АО ТБанк",
  "description": "Основной операционный счёт",
  "companyId": 42,
  "accountType": "Card",
  "accountAcct": "40702810100000123456",
  "correspondentAcct": "30101810145250000974",
  "accountBik": "044525974",
  "accountBank": "АО «ТБанк»",
  "currencyCode": "RUB",
  "active": true,
  "externalId": null,
  "accountGroupId": 7,
  "headerPosition": 1,
  "remainder": 1540200.50,
  "remainderDate": "2026-11-19"
}
```
*(Если над API стоит внешний кэш, после успешного `PUT` принудительно инвалидируй ключи `accounts`, `balances` и `operations` тенанта.)*

### 4.2. Модель операции (`OperationModel`) и нативный сплит `items[]`
При создании расхода или дохода с разбивкой (например, выделение НДС или деление чека между статьями/проектами/датами начисления) **запрещено плодить несколько отдельных операций** — иначе банковская сверка развалится. Передавай массив `items[]` в одном запросе:

```json
POST /api/v1/operations/outcome
{
  "accountId": 10101,
  "operationDate": "2026-11-19",
  "isCommitted": true,
  "comment": "Оплата по счёту № 48 за серверное ПО (в т. ч. НДС 20%)",
  "documentNumber": "481",
  "items": [
    {
      "value": 100000.00,
      "operationCategoryId": 310,
      "contrAgentId": 884,
      "projectId": 19,
      "calculationDate": "2026-11-30",
      "isCalculationCommitted": true
    },
    {
      "value": 20000.00,
      "operationCategoryId": 512,
      "contrAgentId": 884,
      "projectId": null,
      "calculationDate": "2026-11-19",
      "isCalculationCommitted": true
    }
  ]
}
```
При чтении через `GET /api/v1/operations/{id}` или `POST /api/v1/operations/list` части возвращаются в поле `operationParts[]`. Признак неразнесённой статьи в `operationCategory` или `operationParts[].operationCategory` — значение `accountCategoryType` равное `"IncomeUndistributed"` либо `"OutcomeUndistributed"` (или `operationCategory == null`).

### 4.3. Создание `Перемещения` (`Move`) с комиссией и разрывом дат
Внутренний перевод между собственными счетами или вывод с транзитного счёта эквайринга/МП оформляется единым вызовом `POST /api/v1/operations/move`. Если сумма зачисления меньше суммы списания (`admissionValue < debitingValue` в одной валюте), разница автоматически трактуется как банковская комиссия за перевод:

```json
POST /api/v1/operations/move
{
  "debitingAccountId": 10103,
  "debitingDate": "2026-11-18",
  "debitingValue": 300000.00,
  "admissionAccountId": 10101,
  "admissionDate": "2026-11-19",
  "admissionValue": 297000.00,
  "isCommitted": true,
  "comment": "Вывод средств с транзитного счёта эквайринга за вычетом комиссии 1%"
}
```
В ответе возвращаются два объекта `OperationModel` с `"operationType": "Move"`, связанные перекрёстными полями `boundMoveOperationId` и `boundMoveOperation`.

### 4.4. Создание неденежного `Начисления` (`Accrual v2`)
Используется для начисления амортизации, ФОТ последним днём месяца, списания запасов и ввода начальных долгов без движения по денежным счетам:

```json
POST /api/v2/operations/accrual
{
  "calculationDate": "2026-11-30",
  "isCalculationCommitted": true,
  "value": 45000.00,
  "comment": "Ежемесячная амортизация серверного оборудования за ноябрь 2026",
  "debitPart": {
    "operationCategoryId": 340,
    "projectId": null,
    "contrAgentId": null
  },
  "creditPart": {
    "operationCategoryId": 610,
    "projectId": null,
    "contrAgentId": null
  }
}
```

### 4.5. Запросы к управленческим отчётам (`DDS`, `OPU`, `Balance`, `AccountBalance`)

```json
// 1. Проверка подневных остатков и кассовых разрывов: POST /api/v2/dashboardpro/accountbalance
{
  "currentDate": "2026-11-19",
  "periodStartDate": "2026-11-01",
  "periodEndDate": "2026-11-30",
  "accountIds": null,
  "standardPeriod": "Day"
}

// 2. Отчёт ОПиУ по методу начисления: POST /api/v2/reports/opu
{
  "periodStartDate": "2026-01-01",
  "periodEndDate": "2026-11-30",
  "standardPeriod": "Month",
  "isCalculation": true
}
```
Аналогично `POST /api/v2/reports/dds` строит кассовый ДДС в разрезе статей, проектов или сделок, а `POST /api/v1/accounting/balance/table` возвращает табличное представление Активов (`assets`) и Пассивов (`liabilities`) на отчётную дату.

---

## 5. Руководство по CLI-ревизору `scripts/planfact_reconcile_audit.py`

Утилита [`scripts/planfact_reconcile_audit.py`](../scripts/planfact_reconcile_audit.py) автоматизирует `Balance Reconciliation Gate` и глубокую диагностику учётных ошибок без сторонних зависимостей (чистый Python 3 stdlib).

### 5.1. Архитектура безопасности (`Strict Read-Only`)
- Клиент на уровне кода блокирует любые мутирующие глаголы (`PUT`, `PATCH`, `DELETE`) и разрешает только `GET` и три безопасных списочных/расчётных `POST`-пути: `/api/v1/operations/list`, `/api/v2/dashboardpro/accountbalance`, `/api/v1/dashboards/accountbalance` (любая иная попытка выбрасывает `ReadOnlyViolationError`).
- Ключ читается из переменной окружения `PLANFACT_API_KEY` либо из файла `--env-file` и принудительно маскируется в логах и текстах исключений строкой `pf_***`.
- Встроены чтение квот `X-Quota-Remaining`, троттлинг под `X-RateLimit-Limit`, автоматический ретрай с экспоненциальным backoff и jitter на `429 / 502 / 503 / 504` и честная пагинация по `100` записей.

### 5.2. Двухшаговый рабочий цикл CLI

#### Шаг 1. Выгрузка шаблона активных счетов для собственника (`export-template`)
Чтобы собственник не переписывал названия и номера счетов вручную, выгрузи готовый бланк прямо из его аккаунта «ПланФакта» (в `.csv` для Excel/Google Таблиц или в `.json`):

```powershell
python scripts/planfact_reconcile_audit.py export-template `
  --env-file .env `
  --as-of 2026-11-19 `
  --out ./actual_balances_2026-11-19.csv
```
Флаги команды `export-template`:
- `--out` *(обязательный)* — путь к создаваемому шаблону (`.json` или `.csv`);
- `--format` — явный выбор `json` или `csv`;
- `--as-of` — целевая дата сверки `YYYY-MM-DD` (конец вчерашнего дня);
- `--include-archived` — включить архивные счета;
- `--prefill-actual` — предзаполнить колонку `actualBalance` текущим остатком из «ПланФакта» (по умолчанию оставляется пустой, чтобы финансист вписал независимую цифру из банковской выписки);
- `--snapshot-json` — работа по ранее сохранённому офлайн-снимку без обращения к сети.

Образцы заполненных эталонов лежат в [`templates/actual_balances.example.json`](../templates/actual_balances.example.json) и [`templates/actual_balances.example.csv`](../templates/actual_balances.example.csv) (ключевые поля: `accountId`, `title`, `companyTitle`, `accountType`, `accountAcct`, `currencyCode`, `actualBalance`, `asOfDate`, `notes`).

#### Шаг 2. Запуск полной сверки и 6-контурного аудита (`audit`)
После того как собственник вписал реальные остатки на конец вчерашнего дня в колонку `actualBalance`, запусти ревизию:

```powershell
python scripts/planfact_reconcile_audit.py audit `
  --env-file .env `
  --actual-balances ./actual_balances_2026-11-19.csv `
  --as-of 2026-11-19 `
  --ops-from 2026-01-01 `
  --ops-to 2026-11-19 `
  --dup-window-days 2 `
  --other-limit-pct 5.0 `
  --save-snapshot ./audit_out/pf_snapshot.json `
  --out-dir ./audit_out/
```

Если файл `--actual-balances` ещё не заполнен, команду `audit` можно запустить без него для первичной технической диагностики дублей, битых перемещений, неразнесённых статей и просроченных планов (при этом `Balance Reconciliation Gate` останется закрытым до подачи эталонных остатков). Повторные прогоны удобно выполнять мгновенно и без расхода квоты API через `--snapshot-json ./audit_out/pf_snapshot.json`.

### 5.3. Шесть автоматических проверок, выходные файлы и коды возврата

| № | Модуль проверки в `report["checks"]` | Что именно детектирует скрипт |
| :---: | :--- | :--- |
| **1** | **`balance_reconciliation`** | Сопоставляет строки эталона со счетами «ПланФакта» по приоритету `accountId → accountAcct → title + currencyCode`. Вычисляет дельту с точностью `0,01`: `MATCH` (`|delta| <= 0,01`), `MISMATCH` (`|delta| > 0,01`), `UNMATCHED_ACTUAL` (счёт есть у владельца, отсутствует в ПФ), `UNMATCHED_PF_ACTIVE` (активный счёт в ПФ с ненулевым остатком забыт при опросе владельца). Дополнительно проверяет `startingRemainderDate >= первой операции` и следы `correctRemainder`. |
| **2** | **`duplicate_operations`** | Находит точные дубли по `externalId` и `(accountId, operationType, documentNumber, date, value)`, а также эвристические кластеры в окне `±dup-window-days` (классическая пара «ручной ввод `importLogId == null` + банковский импорт `importLogId != null`»). |
| **3** | **`broken_or_unmerged_moves`** | Ловит сирот `Move` без второй ноги (`boundMoveOperationId == null`), петли перевода со счёта на тот же самый счёт, асимметрию подтверждения (`одна нога факт, вторая план`), расхождение сумм в одной валюте и несхлопнутые пары `Outcome + Income` между своими счетами (кандидаты на `consolidate`). |
| **4** | **`undistributed_operations`** | Сверяет фактический список операций без статьи или на `IncomeUndistributed` / `OutcomeUndistributed` со скаляром `get-undistributed-operation-count`, проверяет сплиты `operationParts[]` на несовпадение суммы частей с `operation.value` и подсвечивает служебные флаги `isUndistributed`. |
| **5** | **`other_category_bloat`** | Считает долю фактических расходов и доходов, сваленных в статьи «Прочее / Иное / Другие расходы», и выдаёт предупреждение при превышении порога `--other-limit-pct` (по умолчанию `5,0%`). |
| **6** | **`overdue_plans`** | Находит все неподтверждённые кассовые планы (`isCommitted == false`, `operationDate < as_of`) и неподтверждённые плановые начисления (`isCalculationCommitted == false`, `calculationPeriodDate < as_of`), группируя их по корзинам просрочки (`1–7`, `8–30`, `> 30` дней). |

**Выходные артефакты и коды завершения:**
- В каталоге `--out-dir` сохраняются человекочитаемый отчёт **`audit_summary.md`** и полный машиночитаемый дамп **`audit_findings.json`** (флаг `--json` выводит JSON напрямую в `stdout`).
- Коды возврата процесса:
  - `0` — критических расхождений не обнаружено (либо передан флаг `--no-fail-on-findings`);
  - `2` — найдены критические ошибки (`criticalCount > 0`: расхождение баланса `MISMATCH`, точные дубли, битые перемещения, неразнесённые операции);
  - `1` — ошибка конфигурации, входного файла или сетевого/авторизационного ответа API.
- Для самопроверки движка сверки запусти модульные тесты:
  ```powershell
  python -B -m unittest scripts/test_planfact_reconcile_audit.py -v
  ```
