# planfact-finance-architect

> **English summary:** An AI agent skill and standalone zero-dependency Python 3 CLI auditor for [PlanFact](https://planfact.io) (`api.planfact.io` v1/v2) management accounting. It enforces a strict **Balance Reconciliation Gate** (`delta = 0.00 ₽` against official bank statements before any profit or dividend calculations), documents 1C `.txt` import and accrual accounting workflows (Cash Flow / P&L / Balance Sheet), and detects duplicates, broken internal transfers (`Move`), undistributed entries, and overdue plans in strict read-only mode.

Инженерно-методологический навык (Agent Skill) и read-only CLI-ревизор для аудита, наведения порядка и постановки управленческого учёта в сервисе «ПланФакт» (`planfact.io`, REST API v1/v2).

---

## Что такое `Balance Reconciliation Gate`

Навык вводит жёсткое правило № 0: **запрещено рассчитывать чистую прибыль, маржинальность, EBITDA, точку безубыточности и сумму доступных дивидендов**, пока остатки по всем счетам в «ПланФакте» не сверены до копейки (`delta = 0,00 ₽`) с реальными выписками на конец вчерашнего закрытого дня (`23:59:59`).

Сверка идёт по двум контурам:
1. **Ручной контур** — отчёт «Движение денег по дням», бинарный поиск первого дня расхождения, импорт официальной выписки «для 1С (`*.txt`, UTF-8)» и разбор статусов окна Предпросмотра (`Существует`, `Новая`, `Нет в выписке`, `Изменена`).
2. **Автоматический контур** — CLI-скрипт `scripts/planfact_reconcile_audit.py` на чистом Python 3 stdlib в строгом read-only режиме сверяет счета с эталоном владельца, ищет дубли, разорванные перемещения (`Move`), неразнесённые статьи и просроченные планы (`!`).

---

## 7 шагов приведения «ПланФакта» в порядок

1. **Инвентаризация юрлиц и счетов** — перевод ошибочных счетов контрагентов кнопкой «Это не мой счёт», удаление дублей бизнес-карт и схлопывание овернайт-счетов правилами для депозитов.
2. **Сверка остатков до копейки (`delta = 0,00 ₽`)** — проверка начального остатка (дата строго раньше первой операции, без овердрафтов) и прогон выписок `1C (*.txt)` за весь период.
3. **Ремонт `Перемещений` и выводов эквайринга / маркетплейсов** — объединение парных списаний и поступлений, перевод выводов с транзитных счетов WB, Ozon, ЮKassa, Т-Кассы и Robokassa в тип `Перемещение` с отдельным учётом комиссий.
4. **Пересборка справочника статей** — 5 системных типов (`Доходы`, `Расходы`, `Активы`, `Обязательства`, `Капитал`), 3 вида деятельности ДДС и единая модель затрат (`Переменные / Постоянные` либо `Прямые / Косвенные`).
5. **Обнуление «Не разобрано» и зачистка «Прочего»** — разбор всех неразнесённых операций в `0` и сокращение доли «Прочего» до `<= 3–5%` расходов с закреплением в правилах импорта.
6. **Выравнивание ОПиУ по методу начисления** — разделение кредитов на тело (`Обязательства`) и проценты (`Расходы`), отделение зарплаты руководителя от дивидендов, очистка от транзитного НДС и обеспечительных залогов, начисление ФОТ, амортизации и себестоимости запасов последним днём месяца.
7. **Зачистка просроченных планов `!` и закрытие периода** — актуализация Платёжного календаря и включение запрета редактирования исторических данных в правах пользователей.

---

## Структура репозитория

| Путь | Назначение |
| :--- | :--- |
| `SKILL.md` | Главная инструкция навыка: протокол сверки, 7 шагов, инварианты API и 12-пунктовый `Exit-Gate`. |
| `references/01-balance-reconciliation-playbook.md` | Опросник собственника, матрица 12 причин расхождений, бинарный поиск в ДДС, работа с банками, WB/Ozon и эквайрингом. |
| `references/02-accounting-methodology-dds-pnl-balance.md` | Методология 5 типов статей, каскад ОПиУ, шаблоны для 3 архетипов бизнеса, метод начисления, НДС, ФОТ и Сделки. |
| `references/03-planfact-rest-api-v1-v2.md` | Спецификация REST API v1/v2 (`api.planfact.io`): квоты, `Single-Write Lock` (`429`), пагинация `limit=100`, модели и руководство по CLI. |
| `scripts/planfact_reconcile_audit.py` | Read-only Python CLI-ревизор (без внешних зависимостей, только stdlib Python 3.10+). |
| `scripts/test_planfact_reconcile_audit.py` | Модульные тесты движка сверки и 6 детекторов учётных аномалий. |
| `templates/actual_balances.example.json` | Образец JSON-эталона фактических остатков от собственника / финансиста. |
| `templates/actual_balances.example.csv` | Табличный шаблон CSV для заполнения остатков в Excel или Google Таблицах. |

---

## Установка навыка в ИИ-агента

Склонируйте репозиторий в каталог навыков вашей среды (на уровне проекта или глобально):

- **Claude Code:**
  ```bash
  git clone https://github.com/<owner>/planfact-finance-architect.git .claude/skills/planfact-finance-architect
  # или глобально: ~/.claude/skills/planfact-finance-architect
  ```
- **Antigravity:**
  ```bash
  git clone https://github.com/<owner>/planfact-finance-architect.git .agents/skills/planfact-finance-architect
  ```
- **Cursor:**
  ```bash
  git clone https://github.com/<owner>/planfact-finance-architect.git .cursor/skills/planfact-finance-architect
  ```
- **Codex CLI:**
  ```bash
  git clone https://github.com/<owner>/planfact-finance-architect.git .codex/skills/planfact-finance-architect
  # или глобально: ~/.codex/skills/planfact-finance-architect
  ```

---

## Запуск CLI-ревизора (`planfact_reconcile_audit.py`)

Скрипт работает на **Python 3.10+** без сторонних библиотек. На уровне HTTP-клиента разрешены только `GET` и три безопасных read-only `POST`-эндпоинта (`/api/v1/operations/list`, `/api/v2/dashboardpro/accountbalance`, `/api/v1/dashboards/accountbalance`), ключ `PLANFACT_API_KEY` маскируется в логах как `pf_***`.

1. **Выгрузка бланка активных счетов для заполнения собственником:**
   ```bash
   export PLANFACT_API_KEY="pf_your_api_key"
   python scripts/planfact_reconcile_audit.py export-template \
     --as-of 2026-11-19 \
     --out ./actual_balances.csv
   ```
   *(Либо передайте `--env-file .env` вместо экспорта переменной окружения.)*

2. **Полная сверка счетов и 6-контурный аудит:**
   ```bash
   python scripts/planfact_reconcile_audit.py audit \
     --env-file .env \
     --actual-balances ./actual_balances.csv \
     --as-of 2026-11-19 \
     --ops-from 2026-01-01 \
     --ops-to 2026-11-19 \
     --save-snapshot ./audit_report/snapshot.json \
     --out-dir ./audit_report/
   ```
   На выходе появятся `audit_summary.md` и `audit_findings.json`. Повторный анализ без траты квоты API выполняется с флагом `--snapshot-json ./audit_report/snapshot.json`.

3. **Запуск автотестов:**
   ```bash
   python -B -m unittest discover -s scripts -p "test_*.py" -v
   ```

---

## Лицензия

Проект распространяется под лицензией [MIT](LICENSE).
