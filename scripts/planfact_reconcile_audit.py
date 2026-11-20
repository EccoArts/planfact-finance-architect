#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Read-only CLI-утилита сверки балансов и ревизии учёта ПланФакта.

Поддерживает работу как с живым REST API ПланФакта (`https://api.planfact.io`),
так и с офлайн-снимком данных (`--snapshot-json`). Строго read-only:
разрешены только HTTP GET и ограниченный список read-only POST-эндпоинтов.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import math
import os
import random
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

# Безопасная настройка UTF-8 для Windows PowerShell и перенаправленных потоков.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

BASE_URL = "https://api.planfact.io"
ALLOWED_POST_PATHS = frozenset({
    "/api/v1/operations/list",
    "/api/v2/dashboardpro/accountbalance",
    "/api/v1/dashboards/accountbalance",
})
RETRY_HTTP_CODES = frozenset({429, 502, 503, 504})
UNDISTRIBUTED_CATEGORY_TYPES = frozenset({
    "IncomeUndistributed",
    "OutcomeUndistributed",
})
OTHER_CATEGORY_RE = re.compile(
    r"(?:^|\b)(?:проч(?:ее|ие|ий|ая|их)|иное|другие\s+(?:расходы|доходы)|other|misc(?:ellaneous)?)(?:\b|$)",
    re.IGNORECASE | re.UNICODE,
)
BALANCE_TOLERANCE = 0.01


class PlanFactError(Exception):
    """Базовое исключение утилиты."""


class PlanFactConfigError(PlanFactError):
    """Ошибка конфигурации или входных файлов."""


class ReadOnlyViolationError(PlanFactError):
    """Попытка выполнить мутирующий HTTP-запрос."""


class PlanFactApiError(PlanFactError):
    """Ошибка HTTP или бизнес-ошибка API ПланФакта."""


def mask_api_key(api_key: Optional[str]) -> str:
    """Маскирует API-ключ для безопасного вывода в логи."""
    if not api_key:
        return "<empty>"
    return "pf_***"


def _strip_key_from_text(text: str, api_key: Optional[str]) -> str:
    if api_key and api_key in text:
        return text.replace(api_key, "pf_***")
    return text


def load_env_file(env_path: Path) -> Dict[str, str]:
    """Читает пары KEY=VALUE из .env файла без сторонних зависимостей."""
    if not env_path.is_file():
        raise PlanFactConfigError(f"Файл окружения не найден: {env_path}")
    result: Dict[str, str] = {}
    for raw_line in env_path.read_text(encoding="utf-8-sig").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].strip()
        if "=" not in line:
            continue
        key, val = line.split("=", 1)
        key = key.strip()
        val = val.strip()
        if len(val) >= 2 and ((val[0] == val[-1] == '"') or (val[0] == val[-1] == "'")):
            val = val[1:-1]
        result[key] = val
    return result


def resolve_api_key(env_file: Optional[str]) -> str:
    """Получает PLANFACT_API_KEY из окружения или --env-file."""
    if env_file:
        env_vars = load_env_file(Path(env_file))
        if env_vars.get("PLANFACT_API_KEY"):
            return env_vars["PLANFACT_API_KEY"].strip()
    env_key = os.environ.get("PLANFACT_API_KEY", "").strip()
    if env_key:
        return env_key
    raise PlanFactConfigError(
        "Не задан PLANFACT_API_KEY (передайте через переменную окружения или --env-file)."
    )


def parse_float(value: Any) -> Optional[float]:
    """Безопасно преобразует число (включая строки с пробелами и запятой) во float."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        num = float(value)
        return num if math.isfinite(num) else None
    text = str(value).strip()
    if not text:
        return None
    cleaned = text.replace("\u00a0", "").replace(" ", "").replace(",", ".")
    try:
        num = float(cleaned)
    except ValueError:
        return None
    return num if math.isfinite(num) else None


def parse_iso_date(value: Any) -> Optional[dt.date]:
    """Извлекает дату YYYY-MM-DD из ISO-строки или даты DD.MM.YYYY."""
    if value is None:
        return None
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    text = str(value).strip()
    if not text:
        return None
    if len(text) >= 10 and text[4] == "-" and text[7] == "-":
        try:
            return dt.date.fromisoformat(text[:10])
        except ValueError:
            pass
    if len(text) == 10 and text[2] == "." and text[5] == ".":
        try:
            return dt.datetime.strptime(text, "%d.%m.%Y").date()
        except ValueError:
            pass
    return None


def date_str(value: Any) -> Optional[str]:
    """Возвращает нормализованную строку YYYY-MM-DD или None."""
    d = parse_iso_date(value)
    return d.isoformat() if d else None


def normalize_acct(value: Any) -> str:
    """Оставляет только цифры банковского счёта."""
    if value is None:
        return ""
    return re.sub(r"\D+", "", str(value))


def normalize_title(value: Any) -> str:
    """Нормализует название счёта/категории для сопоставления."""
    if value is None:
        return ""
    text = str(value).strip().lower().replace("ё", "е")
    text = re.sub(r"[«»\"'`]+", "", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _safe_int(value: Any) -> Optional[int]:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    text = str(value).strip()
    if not text:
        return None
    try:
        return int(text)
    except ValueError:
        return None


def _get_header_ci(headers: Any, name: str) -> Optional[str]:
    if not headers:
        return None
    target = name.lower()
    if hasattr(headers, "items"):
        for k, v in headers.items():
            if str(k).lower() == target:
                return str(v)
    return None


class PlanFactReadOnlyClient:
    """Строго read-only клиент REST API ПланФакта на стандартной библиотеке."""

    def __init__(
        self,
        api_key: str,
        base_url: str = BASE_URL,
        timeout: float = 30.0,
        max_retries: int = 4,
    ) -> None:
        if not api_key:
            raise PlanFactConfigError("API-ключ ПланФакта пуст.")
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._max_retries = max_retries
        self.last_rate_limits: Dict[str, Optional[int]] = {}
        self._quota_warned = False
        self._request_timestamps: List[float] = []

    @staticmethod
    def _assert_read_only(method: str, path: str) -> None:
        upper_method = method.upper()
        norm_path = "/" + path.lstrip("/")
        if upper_method == "GET":
            return
        if upper_method == "POST" and norm_path in ALLOWED_POST_PATHS:
            return
        raise ReadOnlyViolationError(
            f"Заблокирован небезопасный запрос {upper_method} {norm_path}: утилита работает строго в read-only режиме."
        )

    def _pace_rpm(self) -> None:
        rpm_limit = self.last_rate_limits.get("rate_limit_rpm")
        if not rpm_limit or rpm_limit <= 0:
            return
        now = time.monotonic()
        self._request_timestamps = [ts for ts in self._request_timestamps if now - ts < 60.0]
        if len(self._request_timestamps) >= max(1, rpm_limit - 2):
            sleep_for = max(0.2, 60.0 - (now - self._request_timestamps[0]) + 0.1)
            time.sleep(min(sleep_for, 10.0))

    def _update_rate_limits(self, headers: Any) -> None:
        rpm = _safe_int(_get_header_ci(headers, "X-RateLimit-Limit"))
        q_limit = _safe_int(_get_header_ci(headers, "X-Quota-Limit"))
        q_used = _safe_int(_get_header_ci(headers, "X-Quota-Used"))
        q_rem = _safe_int(_get_header_ci(headers, "X-Quota-Remaining"))
        q_reset = _safe_int(_get_header_ci(headers, "X-Quota-Reset"))
        if rpm is not None:
            self.last_rate_limits["rate_limit_rpm"] = rpm
        if q_limit is not None:
            self.last_rate_limits["quota_limit"] = q_limit
        if q_used is not None:
            self.last_rate_limits["quota_used"] = q_used
        if q_rem is not None:
            self.last_rate_limits["quota_remaining"] = q_rem
            if q_rem < 500 and not self._quota_warned:
                self._quota_warned = True
                print(
                    f"[WARN] Низкий остаток месячной квоты API ПланФакта: X-Quota-Remaining={q_rem} (ключ {mask_api_key(self._api_key)})",
                    file=sys.stderr,
                )
        if q_reset is not None:
            self.last_rate_limits["quota_reset"] = q_reset

    def request(
        self,
        method: str,
        path: str,
        query: Optional[List[Tuple[str, Any]]] = None,
        body: Optional[Dict[str, Any]] = None,
    ) -> Any:
        """Выполняет read-only HTTP-запрос с retry/backoff и проверкой isSuccess."""
        self._assert_read_only(method, path)
        norm_path = "/" + path.lstrip("/")
        url = f"{self._base_url}{norm_path}"
        if query:
            pairs = [(k, str(v)) for k, v in query if v is not None]
            if pairs:
                url = f"{url}?{urllib.parse.urlencode(pairs, doseq=True)}"

        data_bytes: Optional[bytes] = None
        if body is not None:
            data_bytes = json.dumps(body, ensure_ascii=False).encode("utf-8")

        headers = {
            "X-ApiKey": self._api_key,
            "Accept": "application/json",
            "Content-Type": "application/json",
        }

        for attempt in range(self._max_retries + 1):
            self._pace_rpm()
            req = urllib.request.Request(url, data=data_bytes, headers=headers, method=method.upper())
            self._request_timestamps.append(time.monotonic())
            try:
                with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                    self._update_rate_limits(resp.headers)
                    raw_text = resp.read().decode("utf-8", errors="replace")
                    payload = self._parse_json_response(raw_text, norm_path)
                    self._check_business_success(payload, norm_path)
                    return payload
            except urllib.error.HTTPError as exc:
                self._update_rate_limits(exc.headers)
                err_body = ""
                try:
                    err_body = exc.read().decode("utf-8", errors="replace")
                except Exception:
                    pass
                if exc.code in RETRY_HTTP_CODES and attempt < self._max_retries:
                    delay = (1.0 * (2 ** attempt)) + random.uniform(0.0, 0.35)
                    print(
                        f"[WARN] HTTP {exc.code} на {method.upper()} {norm_path} (ключ {mask_api_key(self._api_key)}), повтор через {delay:.1f}с...",
                        file=sys.stderr,
                    )
                    time.sleep(delay)
                    continue
                if exc.code in (401, 403):
                    raise PlanFactApiError(
                        f"Ошибка авторизации/доступа HTTP {exc.code} на {norm_path} (ключ {mask_api_key(self._api_key)}): {_strip_key_from_text(err_body[:300], self._api_key)}"
                    ) from exc
                raise PlanFactApiError(
                    f"HTTP {exc.code} на {method.upper()} {norm_path}: {_strip_key_from_text(err_body[:300], self._api_key)}"
                ) from exc
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                if attempt < self._max_retries:
                    delay = (1.0 * (2 ** attempt)) + random.uniform(0.0, 0.35)
                    print(
                        f"[WARN] Сетевой сбой на {method.upper()} {norm_path}: {_strip_key_from_text(str(exc), self._api_key)}, повтор через {delay:.1f}с...",
                        file=sys.stderr,
                    )
                    time.sleep(delay)
                    continue
                raise PlanFactApiError(
                    f"Сетевая ошибка на {method.upper()} {norm_path}: {_strip_key_from_text(str(exc), self._api_key)}"
                ) from exc

        raise PlanFactApiError(f"Исчерпаны попытки запроса {method.upper()} {norm_path}")

    def _parse_json_response(self, raw_text: str, norm_path: str) -> Any:
        try:
            return json.loads(raw_text) if raw_text else {}
        except json.JSONDecodeError as exc:
            raise PlanFactApiError(
                f"Невалидный JSON в ответе {norm_path}: {_strip_key_from_text(raw_text[:200], self._api_key)}"
            ) from exc

    def _check_business_success(self, payload: Any, norm_path: str) -> None:
        if isinstance(payload, dict) and payload.get("isSuccess") is False:
            code = payload.get("errorCode") or payload.get("errorCodeTitle") or "UnknownError"
            msg = payload.get("errorMessage") or ""
            errors = payload.get("errors")
            raise PlanFactApiError(
                f"Ошибка API ПланФакта на {norm_path}: code={code}, message={_strip_key_from_text(str(msg), self._api_key)}, errors={errors}"
            )

    @staticmethod
    def _unwrap_data(payload: Any) -> Any:
        if isinstance(payload, dict) and "data" in payload:
            return payload["data"]
        return payload

    def paginate_get(
        self,
        path: str,
        query: Optional[List[Tuple[str, Any]]] = None,
        limit: int = 100,
    ) -> List[Dict[str, Any]]:
        """Листает GET-список с параметрами paging.limit=100 и paging.offset."""
        items: List[Dict[str, Any]] = []
        offset = 0
        base_query = list(query or [])
        while True:
            page_query = base_query + [("paging.limit", limit), ("paging.offset", offset)]
            payload = self.request("GET", path, query=page_query)
            data = self._unwrap_data(payload)
            if isinstance(data, list):
                items.extend(data)
                break
            if not isinstance(data, dict):
                break
            batch = data.get("items") or []
            total = _safe_int(data.get("total"))
            items.extend(batch)
            if not batch or (total is not None and offset + len(batch) >= total) or len(batch) < limit:
                break
            offset += len(batch)
        return items

    def paginate_post_list(
        self,
        path: str,
        body: Optional[Dict[str, Any]] = None,
        limit: int = 100,
        field: str = "OperationDate",
        direction: str = "Asc",
    ) -> List[Dict[str, Any]]:
        """Листает POST .../list с query-параметрами limit=100 и offset."""
        items: List[Dict[str, Any]] = []
        offset = 0
        payload_body = dict(body or {})
        while True:
            query: List[Tuple[str, Any]] = [
                ("offset", offset),
                ("limit", limit),
                ("field", field),
                ("direction", direction),
            ]
            payload = self.request("POST", path, query=query, body=payload_body)
            data = self._unwrap_data(payload)
            if not isinstance(data, dict):
                break
            batch = data.get("items") or []
            total = _safe_int(data.get("total"))
            items.extend(batch)
            if not batch or (total is not None and offset + len(batch) >= total) or len(batch) < limit:
                break
            offset += len(batch)
        return items

    def fetch_dashboard_balances(self, as_of: str) -> Dict[int, float]:
        """Пытается получить остатки счетов на конкретную дату --as-of."""
        body = {
            "currentDate": f"{as_of}T23:59:59",
            "periodStartDate": f"{as_of}T00:00:00",
            "periodEndDate": f"{as_of}T23:59:59",
            "standardPeriod": "Day",
        }
        payload = None
        for endpoint in ("/api/v2/dashboardpro/accountbalance", "/api/v1/dashboards/accountbalance"):
            try:
                payload = self.request("POST", endpoint, body=body)
                break
            except PlanFactApiError:
                continue
        if not payload:
            return {}
        data = self._unwrap_data(payload)
        if not isinstance(data, dict):
            return {}
        result: Dict[int, float] = {}
        for item in data.get("items") or []:
            acc_id = _safe_int(item.get("accountId") or (item.get("account") or {}).get("accountId"))
            total_val = parse_float(item.get("total"))
            if acc_id is not None and total_val is not None:
                result[acc_id] = total_val
        return result

    def fetch_snapshot(
        self,
        ops_from: Optional[str] = None,
        ops_to: Optional[str] = None,
        as_of: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Загружает все необходимые данные ПланФакта в режиме read-only."""
        companies = self.paginate_get("/api/v1/companies")
        accounts = self.paginate_get("/api/v1/accounts")
        categories = self.paginate_get("/api/v1/operationcategories")

        undist_resp = self.request("GET", "/api/v1/operations/get-undistributed-operation-count")
        undist_raw = self._unwrap_data(undist_resp)
        undistributed_count = _safe_int(undist_raw) if not isinstance(undist_raw, dict) else _safe_int(undist_raw.get("count"))

        op_filter: Dict[str, Any] = {}
        if ops_from:
            op_filter["operationDateStart"] = f"{ops_from}T00:00:00"
        if ops_to:
            op_filter["operationDateEnd"] = f"{ops_to}T23:59:59"
        operations = self.paginate_post_list("/api/v1/operations/list", body=op_filter)

        dashboard_balances: Dict[int, float] = {}
        if as_of:
            dashboard_balances = self.fetch_dashboard_balances(as_of)

        companies_by_id = {_safe_int(c.get("companyId")): c for c in companies if _safe_int(c.get("companyId")) is not None}
        for acc in accounts:
            if not acc.get("company") and _safe_int(acc.get("companyId")) in companies_by_id:
                comp = companies_by_id[_safe_int(acc.get("companyId"))]
                acc["company"] = {"companyId": comp.get("companyId"), "title": comp.get("title")}

        snapshot: Dict[str, Any] = {
            "accounts": accounts,
            "operations": operations,
            "categories": categories,
            "undistributedCount": undistributed_count if undistributed_count is not None else 0,
        }
        if dashboard_balances:
            snapshot["dashboardBalances"] = {str(k): v for k, v in dashboard_balances.items()}
        return snapshot


def load_snapshot_json(path: Path) -> Dict[str, Any]:
    """Загружает офлайн-снимок данных ПланФакта."""
    if not path.is_file():
        raise PlanFactConfigError(f"Файл снимка не найден: {path}")
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as exc:
        raise PlanFactConfigError(f"Невалидный JSON в файле снимка {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise PlanFactConfigError(f"Снимок {path} должен быть JSON-объектом.")
    return {
        "accounts": list(data.get("accounts") or []),
        "operations": list(data.get("operations") or []),
        "categories": list(data.get("categories") or []),
        "undistributedCount": _safe_int(data.get("undistributedCount")),
        "dashboardBalances": data.get("dashboardBalances") or {},
    }


def _extract_row_field(row: Dict[str, Any], aliases: Sequence[str]) -> Any:
    lowered = {str(k).strip().lower(): v for k, v in row.items()}
    for alias in aliases:
        key = alias.lower()
        if key in lowered and lowered[key] is not None:
            val = lowered[key]
            if isinstance(val, str) and not val.strip():
                continue
            return val
    return None


def _normalize_actual_row(raw: Dict[str, Any], index: int) -> Tuple[Optional[Dict[str, Any]], bool]:
    """Нормализует строку фактических остатков. Возвращает (row, skipped_empty)."""
    bal_raw = _extract_row_field(raw, ("actualBalance", "actual_balance", "balance", "amount"))
    if bal_raw is None:
        return None, True
    actual_balance = parse_float(bal_raw)
    if actual_balance is None:
        raise PlanFactConfigError(
            f"Некорректное число actualBalance в строке #{index + 1}: {bal_raw!r}"
        )
    account_id = _safe_int(_extract_row_field(raw, ("accountId", "account_id")))
    account_acct = normalize_acct(_extract_row_field(raw, ("accountAcct", "account_acct", "accountNumber", "account_number")))
    title = _extract_row_field(raw, ("title", "name_hint", "nameHint", "accountTitle", "account_title"))
    currency = _extract_row_field(raw, ("currencyCode", "currency_code", "currency"))
    as_of_val = date_str(_extract_row_field(raw, ("asOfDate", "as_of_date")))
    notes = _extract_row_field(raw, ("notes", "comment"))
    return {
        "rowIndex": index + 1,
        "accountId": account_id,
        "accountAcct": account_acct,
        "title": str(title).strip() if title is not None else "",
        "currencyCode": str(currency).strip().upper() if currency is not None else None,
        "actualBalance": round(actual_balance, 2),
        "asOfDate": as_of_val,
        "notes": str(notes).strip() if notes is not None else "",
    }, False


def load_actual_balances(path: Path) -> Tuple[List[Dict[str, Any]], int]:
    """Читает фактические остатки из JSON или CSV. Возвращает (rows, skipped_empty_count)."""
    if not path.is_file():
        raise PlanFactConfigError(f"Файл фактических остатков не найден: {path}")
    suffix = path.suffix.lower()
    raw_rows: List[Dict[str, Any]] = []

    if suffix == ".json":
        try:
            payload = json.loads(path.read_text(encoding="utf-8-sig"))
        except json.JSONDecodeError as exc:
            raise PlanFactConfigError(f"Невалидный JSON в {path}: {exc}") from exc
        if isinstance(payload, list):
            raw_rows = [r for r in payload if isinstance(r, dict)]
        elif isinstance(payload, dict):
            raw_rows = [r for r in (payload.get("items") or payload.get("accounts") or []) if isinstance(r, dict)]
        else:
            raise PlanFactConfigError(f"Ожидался JSON-массив или объект в {path}")
    elif suffix == ".csv":
        text = path.read_text(encoding="utf-8-sig")
        if not text.strip():
            return [], 0
        first_line = text.splitlines()[0]
        delimiter = ";" if first_line.count(";") > first_line.count(",") else ","
        reader = csv.DictReader(text.splitlines(), delimiter=delimiter)
        raw_rows = list(reader)
    else:
        raise PlanFactConfigError(
            f"Неподдерживаемое расширение файла фактических остатков: {path.suffix} (нужен .json или .csv)"
        )

    parsed: List[Dict[str, Any]] = []
    skipped_empty = 0
    for idx, raw in enumerate(raw_rows):
        row, is_empty = _normalize_actual_row(raw, idx)
        if is_empty:
            skipped_empty += 1
        elif row is not None:
            parsed.append(row)
    return parsed, skipped_empty


def export_template_rows(
    snapshot: Dict[str, Any],
    as_of: str,
    include_archived: bool = False,
    prefill_actual: bool = False,
) -> List[Dict[str, Any]]:
    """Формирует строки шаблона сверки из активных неслужебных счетов."""
    dashboard_balances = snapshot.get("dashboardBalances") or {}
    rows: List[Dict[str, Any]] = []
    for acc in snapshot.get("accounts") or []:
        if acc.get("isUndistributed"):
            continue
        if not include_archived and acc.get("active") is False:
            continue
        acc_id = _safe_int(acc.get("accountId"))
        company_obj = acc.get("company") if isinstance(acc.get("company"), dict) else {}
        pf_bal = parse_float(dashboard_balances.get(str(acc_id))) if acc_id is not None and str(acc_id) in dashboard_balances else parse_float(acc.get("remainder"))
        pf_bal_rounded = round(pf_bal, 2) if pf_bal is not None else 0.0
        rows.append({
            "accountId": acc_id,
            "title": acc.get("title") or "",
            "companyTitle": company_obj.get("title") or acc.get("companyTitle") or "",
            "accountType": acc.get("accountType") or "",
            "accountAcct": acc.get("accountAcct") or "",
            "currencyCode": (acc.get("currencyCode") or "RUB").upper(),
            "planfactBalance": pf_bal_rounded,
            "actualBalance": pf_bal_rounded if prefill_actual else None,
            "asOfDate": as_of,
            "notes": "",
        })
    return rows


def write_template_file(rows: List[Dict[str, Any]], out_path: Path, fmt: Optional[str] = None) -> None:
    """Сохраняет шаблон actual_balances в JSON или CSV."""
    target_fmt = (fmt or out_path.suffix.lstrip(".")).lower()
    if target_fmt not in ("json", "csv"):
        raise PlanFactConfigError(f"Неподдерживаемый формат шаблона: {target_fmt} (ожидается json или csv)")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    if target_fmt == "json":
        out_path.write_text(json.dumps(rows, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return

    fieldnames = [
        "accountId",
        "title",
        "companyTitle",
        "accountType",
        "accountAcct",
        "currencyCode",
        "planfactBalance",
        "actualBalance",
        "asOfDate",
        "notes",
    ]
    with out_path.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            csv_row = dict(row)
            if csv_row["actualBalance"] is None:
                csv_row["actualBalance"] = ""
            writer.writerow(csv_row)


def _op_id(op: Dict[str, Any]) -> Optional[int]:
    return _safe_int(op.get("operationId"))


def _op_account_id(op: Dict[str, Any]) -> Optional[int]:
    acc = op.get("account")
    if isinstance(acc, dict) and acc.get("accountId") is not None:
        return _safe_int(acc.get("accountId"))
    return _safe_int(op.get("accountId"))


def _op_account_title(op: Dict[str, Any], accounts_by_id: Dict[int, Dict[str, Any]]) -> str:
    acc = op.get("account")
    if isinstance(acc, dict) and acc.get("title"):
        return str(acc.get("title"))
    acc_id = _op_account_id(op)
    if acc_id is not None and acc_id in accounts_by_id:
        return str(accounts_by_id[acc_id].get("title") or f"Счёт #{acc_id}")
    return f"Счёт #{acc_id}" if acc_id is not None else "Неизвестный счёт"


def _op_currency(op: Dict[str, Any], accounts_by_id: Dict[int, Dict[str, Any]]) -> str:
    cur = op.get("accountCurrency")
    if isinstance(cur, dict) and cur.get("currencyCode"):
        return str(cur["currencyCode"]).upper()
    if op.get("currencyCode"):
        return str(op["currencyCode"]).upper()
    acc_id = _op_account_id(op)
    if acc_id is not None and acc_id in accounts_by_id:
        return str(accounts_by_id[acc_id].get("currencyCode") or "RUB").upper()
    return "RUB"


def _op_contragent_id(op: Dict[str, Any]) -> Optional[int]:
    ca = op.get("contrAgent")
    if isinstance(ca, dict) and ca.get("contrAgentId") is not None:
        return _safe_int(ca.get("contrAgentId"))
    return _safe_int(op.get("contrAgentId"))


def _op_value(op: Dict[str, Any]) -> float:
    val = parse_float(op.get("value"))
    return round(val, 2) if val is not None else 0.0


def _op_value_user_currency(op: Dict[str, Any]) -> float:
    val = parse_float(op.get("valueInUserCurrency"))
    if val is not None:
        return round(val, 2)
    return _op_value(op)


def _filter_operations(
    operations: Sequence[Dict[str, Any]],
    ops_from: Optional[str],
    ops_to: Optional[str],
) -> List[Dict[str, Any]]:
    d_from = parse_iso_date(ops_from) if ops_from else None
    d_to = parse_iso_date(ops_to) if ops_to else None
    if not d_from and not d_to:
        return list(operations)
    filtered: List[Dict[str, Any]] = []
    for op in operations:
        op_d = parse_iso_date(op.get("operationDate"))
        if op_d is None:
            continue
        if d_from and op_d < d_from:
            continue
        if d_to and op_d > d_to:
            continue
        filtered.append(op)
    return filtered


def _category_info(
    cat_obj: Any,
    cat_id_fallback: Any,
    categories_by_id: Dict[int, Dict[str, Any]],
) -> Tuple[Optional[int], str, Optional[str]]:
    cat_id: Optional[int] = None
    title = ""
    acc_cat_type: Optional[str] = None
    if isinstance(cat_obj, dict):
        cat_id = _safe_int(cat_obj.get("operationCategoryId"))
        title = str(cat_obj.get("title") or "")
        acc_cat_type = cat_obj.get("accountCategoryType")
    if cat_id is None:
        cat_id = _safe_int(cat_id_fallback)
    if cat_id is not None and cat_id in categories_by_id:
        ref = categories_by_id[cat_id]
        if not title:
            title = str(ref.get("title") or "")
        if not acc_cat_type:
            acc_cat_type = ref.get("accountCategoryType")
    return cat_id, title, acc_cat_type


# ---------------------------------------------------------------------------
# Проверка 2: Дубли операций
# ---------------------------------------------------------------------------

def check_duplicate_operations(
    operations: Sequence[Dict[str, Any]],
    dup_window_days: int = 2,
) -> List[Dict[str, Any]]:
    """Находит точные и эвристические дубли операций."""
    findings: List[Dict[str, Any]] = []
    seen_pairs: Set[Tuple[int, int]] = set()

    def _register_pair(op1: Dict[str, Any], op2: Dict[str, Any], reason: str, confidence: str, severity: str) -> None:
        id1, id2 = _op_id(op1), _op_id(op2)
        if id1 is None or id2 is None or id1 == id2:
            return
        pair_key = (min(id1, id2), max(id1, id2))
        if pair_key in seen_pairs:
            return
        seen_pairs.add(pair_key)
        findings.append({
            "check": "duplicate_operations",
            "severity": severity,
            "confidence": confidence,
            "reason": reason,
            "accountId": _op_account_id(op1),
            "operationType": op1.get("operationType"),
            "value": _op_value(op1),
            "operationIds": [pair_key[0], pair_key[1]],
            "dates": [date_str(op1.get("operationDate")), date_str(op2.get("operationDate"))],
            "importLogIds": [op1.get("importLogId"), op2.get("importLogId")],
        })

    # 1. Точные дубли по externalId (в рамках одного счёта и типа).
    by_external: Dict[Tuple[Optional[int], Any, str], List[Dict[str, Any]]] = {}
    by_docnum: Dict[Tuple[Optional[int], Any, str, str, float], List[Dict[str, Any]]] = {}
    for op in operations:
        acc_id = _op_account_id(op)
        op_type = op.get("operationType")
        ext_id = str(op.get("externalId") or "").strip()
        if ext_id:
            by_external.setdefault((acc_id, op_type, ext_id), []).append(op)
        doc_num = str(op.get("documentNumber") or "").strip()
        d_str = date_str(op.get("operationDate")) or ""
        val = _op_value(op)
        if doc_num and d_str:
            by_docnum.setdefault((acc_id, op_type, doc_num, d_str, val), []).append(op)

    for group in by_external.values():
        if len(group) > 1:
            for i in range(len(group)):
                for j in range(i + 1, len(group)):
                    _register_pair(group[i], group[j], "exact_external_id", "HIGH", "CRITICAL")

    for group in by_docnum.values():
        if len(group) > 1:
            for i in range(len(group)):
                for j in range(i + 1, len(group)):
                    _register_pair(group[i], group[j], "exact_document_number", "HIGH", "CRITICAL")

    # 2. Эвристические дубли в окне ±dup_window_days.
    buckets: Dict[Tuple[Optional[int], Any, float], List[Tuple[dt.date, Dict[str, Any]]]] = {}
    for op in operations:
        op_d = parse_iso_date(op.get("operationDate"))
        if op_d is None:
            continue
        key = (_op_account_id(op), op.get("operationType"), _op_value(op))
        buckets.setdefault(key, []).append((op_d, op))

    for entries in buckets.values():
        if len(entries) < 2:
            continue
        entries.sort(key=lambda item: (item[0], _op_id(item[1]) or 0))
        for i in range(len(entries)):
            d1, op1 = entries[i]
            for j in range(i + 1, len(entries)):
                d2, op2 = entries[j]
                if (d2 - d1).days > dup_window_days:
                    break
                imp1 = op1.get("importLogId") is not None
                imp2 = op2.get("importLogId") is not None
                ca1, ca2 = _op_contragent_id(op1), _op_contragent_id(op2)
                c1 = normalize_title(op1.get("comment"))
                c2 = normalize_title(op2.get("comment"))

                if imp1 != imp2:
                    _register_pair(op1, op2, "manual_plus_import", "HIGH", "CRITICAL")
                elif ca1 is not None and ca1 == ca2 and op1.get("isCommitted") == op2.get("isCommitted"):
                    _register_pair(op1, op2, "same_contragent_window", "HIGH", "CRITICAL")
                elif c1 and c2 and len(c1) >= 6 and len(c2) >= 6 and (c1[:12] == c2[:12]):
                    _register_pair(op1, op2, "similar_comment_window", "MEDIUM", "WARN")
                elif d1 == d2 and op1.get("isCommitted") == op2.get("isCommitted"):
                    _register_pair(op1, op2, "same_day_amount_account", "MEDIUM", "WARN")

    return findings


# ---------------------------------------------------------------------------
# Проверка 3: Битые и несхлопнутые перемещения
# ---------------------------------------------------------------------------

def check_broken_or_unmerged_moves(
    operations: Sequence[Dict[str, Any]],
    accounts_by_id: Dict[int, Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Находит сироты Move, асимметрии ног Move и кандидатов на склейку Outcome+Income."""
    findings: List[Dict[str, Any]] = []
    ops_by_id = {_op_id(op): op for op in operations if _op_id(op) is not None}
    checked_move_pairs: Set[Tuple[int, int]] = set()

    for op in operations:
        if op.get("operationType") != "Move":
            continue
        oid = _op_id(op)
        if oid is None:
            continue
        bound_id = _safe_int(op.get("boundMoveOperationId"))
        bound_obj = op.get("boundMoveOperation") if isinstance(op.get("boundMoveOperation"), dict) else None
        if bound_id is None and bound_obj is not None:
            bound_id = _safe_int(bound_obj.get("operationId"))

        if bound_id is None or bound_id not in ops_by_id:
            findings.append({
                "check": "broken_or_unmerged_moves",
                "subtype": "ORPHAN_MOVE",
                "severity": "CRITICAL",
                "operationIds": [oid],
                "boundMoveOperationId": bound_id,
                "accountIds": [_op_account_id(op)],
                "value": _op_value(op),
                "message": (
                    f"Операция Move #{oid} без парной ноги (boundMoveOperationId={bound_id})."
                ),
            })
            continue

        pair_key = (min(oid, bound_id), max(oid, bound_id))
        if pair_key in checked_move_pairs:
            continue
        checked_move_pairs.add(pair_key)
        peer = ops_by_id[bound_id]

        acc1, acc2 = _op_account_id(op), _op_account_id(peer)
        cur1, cur2 = _op_currency(op, accounts_by_id), _op_currency(peer, accounts_by_id)
        val1, val2 = _op_value(op), _op_value(peer)
        d1, d2 = parse_iso_date(op.get("operationDate")), parse_iso_date(peer.get("operationDate"))

        if bool(op.get("isCommitted")) != bool(peer.get("isCommitted")):
            findings.append({
                "check": "broken_or_unmerged_moves",
                "subtype": "COMMIT_ASYMMETRY",
                "severity": "CRITICAL",
                "operationIds": [pair_key[0], pair_key[1]],
                "accountIds": [acc1, acc2],
                "value": val1,
                "message": (
                    f"Асимметрия подтверждения в Move #{pair_key[0]}/#{pair_key[1]}: "
                    f"isCommitted={op.get('isCommitted')} vs {peer.get('isCommitted')}."
                ),
            })

        if acc1 is not None and acc1 == acc2:
            findings.append({
                "check": "broken_or_unmerged_moves",
                "subtype": "SAME_ACCOUNT_MOVE",
                "severity": "CRITICAL",
                "operationIds": [pair_key[0], pair_key[1]],
                "accountIds": [acc1, acc2],
                "value": val1,
                "message": f"Move #{pair_key[0]}/#{pair_key[1]} привязан к одному и тому же счёту #{acc1}.",
            })

        comment_text = f"{op.get('comment') or ''} {peer.get('comment') or ''}".lower()
        has_fee_hint = ("комисс" in comment_text) or ("fee" in comment_text)
        if cur1 == cur2 and abs(val1 - val2) > BALANCE_TOLERANCE and not has_fee_hint:
            findings.append({
                "check": "broken_or_unmerged_moves",
                "subtype": "AMOUNT_MISMATCH_SAME_CURRENCY",
                "severity": "CRITICAL",
                "operationIds": [pair_key[0], pair_key[1]],
                "accountIds": [acc1, acc2],
                "values": [val1, val2],
                "value": val1,
                "message": (
                    f"Расхождение сумм в Move #{pair_key[0]}/#{pair_key[1]} ({cur1}): {val1:.2f} vs {val2:.2f}."
                ),
            })

        if d1 and d2 and abs((d2 - d1).days) > 3:
            findings.append({
                "check": "broken_or_unmerged_moves",
                "subtype": "DATE_GAP_GT_3_DAYS",
                "severity": "WARN",
                "operationIds": [pair_key[0], pair_key[1]],
                "accountIds": [acc1, acc2],
                "value": val1,
                "daysGap": abs((d2 - d1).days),
                "message": (
                    f"Разрыв дат > 3 дней ({abs((d2 - d1).days)} дн.) в Move #{pair_key[0]}/#{pair_key[1]}."
                ),
            })

    # Кандидаты на склейку Outcome (A) + Income (B) в одной валюте на одну сумму в окне 0..3 дня.
    outcomes: List[Tuple[dt.date, Dict[str, Any]]] = []
    incomes_by_key: Dict[Tuple[str, float], List[Tuple[dt.date, Dict[str, Any]]]] = {}
    for op in operations:
        op_d = parse_iso_date(op.get("operationDate"))
        if op_d is None:
            continue
        t = op.get("operationType")
        if t == "Outcome":
            outcomes.append((op_d, op))
        elif t == "Income":
            key = (_op_currency(op, accounts_by_id), _op_value(op))
            incomes_by_key.setdefault(key, []).append((op_d, op))

    outcomes.sort(key=lambda x: (x[0], _op_id(x[1]) or 0))
    for k in incomes_by_key:
        incomes_by_key[k].sort(key=lambda x: (x[0], _op_id(x[1]) or 0))

    used_income_ids: Set[int] = set()
    for d_out, out_op in outcomes:
        out_id = _op_id(out_op)
        out_acc = _op_account_id(out_op)
        if out_id is None or out_acc is None:
            continue
        key = (_op_currency(out_op, accounts_by_id), _op_value(out_op))
        for d_in, in_op in incomes_by_key.get(key, ()):
            in_id = _op_id(in_op)
            in_acc = _op_account_id(in_op)
            if in_id is None or in_id in used_income_ids or in_acc is None or in_acc == out_acc:
                continue
            delta_days = (d_in - d_out).days
            if 0 <= delta_days <= 3:
                used_income_ids.add(in_id)
                findings.append({
                    "check": "broken_or_unmerged_moves",
                    "subtype": "UNMERGED_MOVE_CANDIDATE",
                    "severity": "WARN",
                    "operationIds": [out_id, in_id],
                    "accountIds": [out_acc, in_acc],
                    "value": _op_value(out_op),
                    "currencyCode": key[0],
                    "daysGap": delta_days,
                    "message": (
                        f"Кандидат на склейку в Move: Outcome #{out_id} ({_op_account_title(out_op, accounts_by_id)}) "
                        f"→ Income #{in_id} ({_op_account_title(in_op, accounts_by_id)}) на {key[1]:.2f} {key[0]} (лаг {delta_days} дн.)."
                    ),
                })
                break

    return findings


# ---------------------------------------------------------------------------
# Проверка 4: Неразнесённые операции и битые сплиты
# ---------------------------------------------------------------------------

def check_undistributed_operations(
    operations: Sequence[Dict[str, Any]],
    categories_by_id: Dict[int, Dict[str, Any]],
    reported_undistributed_count: Optional[int] = None,
) -> Dict[str, Any]:
    """Ищет операции без статьи или на служебных статьях, а также расхождения operationParts."""
    items: List[Dict[str, Any]] = []
    for op in operations:
        op_type = op.get("operationType")
        if op_type not in ("Income", "Outcome"):
            continue
        oid = _op_id(op)
        parts = op.get("operationParts") or []
        if parts:
            part_sum = 0.0
            bad_parts = 0
            for idx, part in enumerate(parts):
                p_val = parse_float(part.get("value")) or 0.0
                part_sum += p_val
                cat_id, _, acc_cat_type = _category_info(
                    part.get("operationCategory"), part.get("operationCategoryId"), categories_by_id
                )
                if cat_id is None or acc_cat_type in UNDISTRIBUTED_CATEGORY_TYPES:
                    bad_parts += 1
                    items.append({
                        "check": "undistributed_operations",
                        "subtype": "UNDISTRIBUTED_PART",
                        "severity": "CRITICAL",
                        "operationId": oid,
                        "partIndex": idx,
                        "accountId": _op_account_id(op),
                        "operationType": op_type,
                        "value": round(p_val, 2),
                        "accountCategoryType": acc_cat_type,
                    })
            op_val = _op_value(op)
            if abs(part_sum - op_val) > BALANCE_TOLERANCE:
                items.append({
                    "check": "undistributed_operations",
                    "subtype": "PARTS_SUM_MISMATCH",
                    "severity": "CRITICAL",
                    "operationId": oid,
                    "accountId": _op_account_id(op),
                    "operationType": op_type,
                    "value": op_val,
                    "partsSum": round(part_sum, 2),
                })
            if bad_parts == 0:
                continue
        else:
            cat_id, _, acc_cat_type = _category_info(
                op.get("operationCategory"), op.get("operationCategoryId"), categories_by_id
            )
            if cat_id is None or acc_cat_type in UNDISTRIBUTED_CATEGORY_TYPES:
                items.append({
                    "check": "undistributed_operations",
                    "subtype": "UNDISTRIBUTED_OPERATION",
                    "severity": "CRITICAL",
                    "operationId": oid,
                    "accountId": _op_account_id(op),
                    "operationType": op_type,
                    "value": _op_value(op),
                    "accountCategoryType": acc_cat_type,
                })

    return {
        "items": items,
        "detectedCount": len({it["operationId"] for it in items if it.get("operationId") is not None}),
        "reportedUndistributedCount": reported_undistributed_count,
    }


# ---------------------------------------------------------------------------
# Проверка 5: Раздутие статьи «Прочее»
# ---------------------------------------------------------------------------

def _is_other_category(title: str) -> bool:
    return bool(title and OTHER_CATEGORY_RE.search(title.strip()))


def check_other_category_bloat(
    operations: Sequence[Dict[str, Any]],
    categories_by_id: Dict[int, Dict[str, Any]],
    other_limit_pct: float = 5.0,
) -> Dict[str, Any]:
    """Считает долю фактических доходов и расходов в статьях группы «Прочее»."""
    totals = {"Income": 0.0, "Outcome": 0.0}
    other_totals = {"Income": 0.0, "Outcome": 0.0}
    other_ops: Dict[str, Set[int]] = {"Income": set(), "Outcome": set()}
    category_breakdown: Dict[str, Dict[str, float]] = {"Income": {}, "Outcome": {}}

    for op in operations:
        if op.get("isCommitted") is not True:
            continue
        op_type = op.get("operationType")
        if op_type not in ("Income", "Outcome"):
            continue
        oid = _op_id(op)
        parts = op.get("operationParts") or []
        if parts:
            for part in parts:
                p_val = parse_float(part.get("valueInUserCurrency"))
                if p_val is None:
                    p_val = parse_float(part.get("value")) or 0.0
                totals[op_type] += p_val
                _, cat_title, _ = _category_info(
                    part.get("operationCategory"), part.get("operationCategoryId"), categories_by_id
                )
                if _is_other_category(cat_title):
                    other_totals[op_type] += p_val
                    if oid is not None:
                        other_ops[op_type].add(oid)
                    category_breakdown[op_type][cat_title] = round(
                        category_breakdown[op_type].get(cat_title, 0.0) + p_val, 2
                    )
        else:
            val = _op_value_user_currency(op)
            totals[op_type] += val
            _, cat_title, _ = _category_info(
                op.get("operationCategory"), op.get("operationCategoryId"), categories_by_id
            )
            if _is_other_category(cat_title):
                other_totals[op_type] += val
                if oid is not None:
                    other_ops[op_type].add(oid)
                category_breakdown[op_type][cat_title] = round(
                    category_breakdown[op_type].get(cat_title, 0.0) + val, 2
                )

    warnings: List[Dict[str, Any]] = []
    metrics: Dict[str, Any] = {}
    for op_type in ("Outcome", "Income"):
        total = round(totals[op_type], 2)
        other_sum = round(other_totals[op_type], 2)
        pct = round((other_sum / total) * 100.0, 2) if total > 0 else 0.0
        metrics[op_type] = {
            "totalActual": total,
            "otherAmount": other_sum,
            "otherPercent": pct,
            "exceedsLimit": pct > other_limit_pct,
            "categories": category_breakdown[op_type],
            "operationIds": sorted(other_ops[op_type]),
        }
        if pct > other_limit_pct:
            label = "расходов" if op_type == "Outcome" else "доходов"
            warnings.append({
                "check": "other_category_bloat",
                "severity": "WARN",
                "operationType": op_type,
                "otherAmount": other_sum,
                "totalActual": total,
                "otherPercent": pct,
                "limitPercent": other_limit_pct,
                "operationIds": sorted(other_ops[op_type]),
                "message": (
                    f"Доля статей «Прочее» в фактических {label} составляет {pct:.2f}% "
                    f"({other_sum:.2f} из {total:.2f}), что выше порога {other_limit_pct:.2f}%."
                ),
            })

    return {"limitPercent": other_limit_pct, "metrics": metrics, "warnings": warnings}


# ---------------------------------------------------------------------------
# Проверка 6: Просроченные планы (касса и начисления)
# ---------------------------------------------------------------------------

def _age_bucket(days: int) -> str:
    if days <= 7:
        return "1-7d"
    if days <= 30:
        return "8-30d"
    return ">30d"


def check_overdue_plans(
    operations: Sequence[Dict[str, Any]],
    as_of_date: dt.date,
) -> List[Dict[str, Any]]:
    """Находит просроченные неподтверждённые кассовые операции и начисления."""
    findings: List[Dict[str, Any]] = []
    for op in operations:
        oid = _op_id(op)
        acc_id = _op_account_id(op)
        op_date = parse_iso_date(op.get("operationDate"))
        if op.get("isCommitted") is False and op_date is not None and op_date < as_of_date:
            overdue_days = (as_of_date - op_date).days
            findings.append({
                "check": "overdue_plans",
                "subtype": "OVERDUE_CASH_PLAN",
                "severity": "CRITICAL",
                "operationId": oid,
                "accountId": acc_id,
                "operationType": op.get("operationType"),
                "value": _op_value(op),
                "date": op_date.isoformat(),
                "overdueDays": overdue_days,
                "ageBucket": _age_bucket(overdue_days),
            })

        calc_date = parse_iso_date(op.get("calculationPeriodDate"))
        if op.get("isCalculationCommitted") is False and calc_date is not None and calc_date < as_of_date:
            overdue_days = (as_of_date - calc_date).days
            findings.append({
                "check": "overdue_plans",
                "subtype": "OVERDUE_ACCRUAL_PLAN",
                "severity": "WARN",
                "operationId": oid,
                "accountId": acc_id,
                "operationType": op.get("operationType"),
                "value": _op_value(op),
                "date": calc_date.isoformat(),
                "overdueDays": overdue_days,
                "ageBucket": _age_bucket(overdue_days),
            })
    return findings


# ---------------------------------------------------------------------------
# Проверка 1: Сверка балансов + Root-Cause Hint Engine
# ---------------------------------------------------------------------------

def _match_actual_to_account(
    actual_row: Dict[str, Any],
    accounts: Sequence[Dict[str, Any]],
) -> Optional[Dict[str, Any]]:
    # Приоритет 1: accountId
    if actual_row.get("accountId") is not None:
        target_id = actual_row["accountId"]
        for acc in accounts:
            if _safe_int(acc.get("accountId")) == target_id:
                return acc

    # Приоритет 2: очищенный 20-значный (или >=10 цифр) accountAcct
    target_acct = actual_row.get("accountAcct") or ""
    if len(target_acct) >= 10:
        for acc in accounts:
            if normalize_acct(acc.get("accountAcct")) == target_acct:
                return acc

    # Приоритет 3: нормализованный title + currencyCode
    target_title = normalize_title(actual_row.get("title"))
    target_cur = actual_row.get("currencyCode")
    if target_title:
        candidates = []
        for acc in accounts:
            t1 = normalize_title(acc.get("title"))
            t2 = normalize_title(acc.get("longTitle"))
            if target_title not in (t1, t2):
                continue
            acc_cur = str(acc.get("currencyCode") or "RUB").upper()
            if target_cur and acc_cur != target_cur:
                continue
            candidates.append(acc)
        if candidates:
            return candidates[0]

    return None


def _build_root_cause_hints(
    acc: Dict[str, Any],
    delta: float,
    operations: Sequence[Dict[str, Any]],
    dup_findings: Sequence[Dict[str, Any]],
    move_findings: Sequence[Dict[str, Any]],
    overdue_findings: Sequence[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Root-Cause Hint Engine: объясняет дельту actual - pf по операциям счёта."""
    hints: List[Dict[str, Any]] = []
    acc_id = _safe_int(acc.get("accountId"))
    abs_delta = round(abs(delta), 2)
    acc_ops = [op for op in operations if _op_account_id(op) == acc_id]

    # (а) Дубль операции ровно на |delta|
    dup_op_ids: List[int] = []
    for dup in dup_findings:
        if dup.get("accountId") == acc_id and abs(float(dup.get("value", 0.0)) - abs_delta) <= BALANCE_TOLERANCE:
            dup_op_ids.extend(dup.get("operationIds") or [])
    if dup_op_ids:
        uniq_ids = sorted(set(dup_op_ids))
        hints.append({
            "type": "DUPLICATE_EXACT_DELTA",
            "operationIds": uniq_ids,
            "message": f"Найден вероятный дубль операции на сумму {abs_delta:.2f} (operationId: {uniq_ids}).",
        })

    # (б) Просроченная неподтверждённая плановая операция ровно на |delta|
    overdue_ids: List[int] = []
    for ov in overdue_findings:
        if (
            ov.get("subtype") == "OVERDUE_CASH_PLAN"
            and ov.get("accountId") == acc_id
            and abs(float(ov.get("value", 0.0)) - abs_delta) <= BALANCE_TOLERANCE
            and ov.get("operationId") is not None
        ):
            overdue_ids.append(int(ov["operationId"]))
    if overdue_ids:
        uniq_ids = sorted(set(overdue_ids))
        hints.append({
            "type": "OVERDUE_PLAN_EXACT_DELTA",
            "operationIds": uniq_ids,
            "message": f"Найдена просроченная неподтверждённая операция на {abs_delta:.2f} (operationId: {uniq_ids}).",
        })

    # (в) Нога несхлопнутого/битого перемещения ровно на |delta|
    move_ids: List[int] = []
    for mv in move_findings:
        if acc_id in (mv.get("accountIds") or []) and abs(float(mv.get("value", 0.0)) - abs_delta) <= BALANCE_TOLERANCE:
            move_ids.extend(mv.get("operationIds") or [])
    if move_ids:
        uniq_ids = sorted(set(move_ids))
        hints.append({
            "type": "BROKEN_OR_UNMERGED_MOVE_EXACT_DELTA",
            "operationIds": uniq_ids,
            "message": f"Найдено битое или несхлопнутое перемещение на {abs_delta:.2f} (operationId: {uniq_ids}).",
        })

    # (г) Нулевой/пустой startingRemainderDate, дата старта >= первой операции, либо correctRemainder
    start_date_raw = acc.get("startingRemainderDate")
    start_date = parse_iso_date(start_date_raw)
    start_val = parse_float(acc.get("startingRemainderValue"))
    if start_date is None:
        hints.append({
            "type": "MISSING_STARTING_REMAINDER_DATE",
            "operationIds": [],
            "message": "У счёта не задана дата начального остатка (startingRemainderDate=null).",
        })
    else:
        op_dates = [d for d in (parse_iso_date(o.get("operationDate")) for o in acc_ops) if d is not None]
        if op_dates and start_date >= min(op_dates):
            hints.append({
                "type": "STARTING_DATE_NOT_BEFORE_FIRST_OP",
                "operationIds": [],
                "message": (
                    f"Дата начального остатка ({start_date.isoformat()}) не раньше первой операции ({min(op_dates).isoformat()})."
                ),
            })
        if (start_val is None or abs(start_val) <= BALANCE_TOLERANCE) and acc_ops and abs(delta) > BALANCE_TOLERANCE:
            # Мягкая подсказка только если нет более точных совпадений или начальный остаток нулевой
            hints.append({
                "type": "ZERO_STARTING_REMAINDER",
                "operationIds": [],
                "message": f"Начальный остаток счёта равен 0.00 (startingRemainderDate={start_date.isoformat()}) — проверьте сальдо на дату старта учёта.",
            })

    correct_ops = [op for op in acc_ops if op.get("correctRemainder") is not None]
    if correct_ops:
        corr_ids = [int(_op_id(op)) for op in correct_ops if _op_id(op) is not None]
        hints.append({
            "type": "CORRECT_REMAINDER_PRESENT",
            "operationIds": corr_ids,
            "message": f"Найдены операции с ручной корректировкой остатка correctRemainder (operationId: {corr_ids}).",
        })

    return hints


def check_balance_reconciliation(
    accounts: Sequence[Dict[str, Any]],
    actual_rows: Optional[Sequence[Dict[str, Any]]],
    operations: Sequence[Dict[str, Any]],
    dup_findings: Sequence[Dict[str, Any]],
    move_findings: Sequence[Dict[str, Any]],
    overdue_findings: Sequence[Dict[str, Any]],
    include_archived: bool = False,
    dashboard_balances: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Сверяет остатки ПланФакта с фактическими остатками пользователя."""
    if actual_rows is None:
        return {"performed": False, "items": []}

    eligible_accounts = [
        acc for acc in accounts
        if not acc.get("isUndistributed") and (include_archived or acc.get("active") is not False)
    ]
    all_non_service = [acc for acc in accounts if not acc.get("isUndistributed")]
    dash_map = {str(k): parse_float(v) for k, v in (dashboard_balances or {}).items()}

    def _pf_balance(acc: Dict[str, Any]) -> float:
        aid = _safe_int(acc.get("accountId"))
        if aid is not None and str(aid) in dash_map and dash_map[str(aid)] is not None:
            return round(float(dash_map[str(aid)]), 2)
        val = parse_float(acc.get("remainder"))
        return round(val, 2) if val is not None else 0.0

    items: List[Dict[str, Any]] = []
    matched_account_ids: Set[int] = set()

    for row in actual_rows:
        matched = _match_actual_to_account(row, eligible_accounts) or _match_actual_to_account(row, all_non_service)
        if matched is None:
            items.append({
                "status": "UNMATCHED_ACTUAL",
                "severity": "CRITICAL",
                "accountId": row.get("accountId"),
                "title": row.get("title") or row.get("accountAcct") or f"Строка #{row['rowIndex']}",
                "currencyCode": row.get("currencyCode") or "RUB",
                "planfactBalance": None,
                "actualBalance": row["actualBalance"],
                "delta": None,
                "hints": [{
                    "type": "ACCOUNT_NOT_FOUND_IN_PF",
                    "operationIds": [],
                    "message": "Счёт из файла фактических остатков не найден в ПланФакте.",
                }],
            })
            continue

        aid = _safe_int(matched.get("accountId"))
        if aid is not None:
            matched_account_ids.add(aid)
        pf_bal = _pf_balance(matched)
        actual_bal = float(row["actualBalance"])
        delta = round(actual_bal - pf_bal, 2)
        is_match = abs(delta) <= BALANCE_TOLERANCE
        hints: List[Dict[str, Any]] = []
        if not is_match:
            hints = _build_root_cause_hints(
                matched, delta, operations, dup_findings, move_findings, overdue_findings
            )

        items.append({
            "status": "MATCH" if is_match else "MISMATCH",
            "severity": "OK" if is_match else "CRITICAL",
            "accountId": aid,
            "title": matched.get("title") or row.get("title") or "",
            "accountAcct": matched.get("accountAcct") or row.get("accountAcct") or "",
            "currencyCode": (matched.get("currencyCode") or row.get("currencyCode") or "RUB").upper(),
            "planfactBalance": pf_bal,
            "actualBalance": actual_bal,
            "delta": delta,
            "hints": hints,
        })

    for acc in eligible_accounts:
        aid = _safe_int(acc.get("accountId"))
        if aid is not None and aid in matched_account_ids:
            continue
        pf_bal = _pf_balance(acc)
        if abs(pf_bal) > BALANCE_TOLERANCE:
            items.append({
                "status": "UNMATCHED_PF_ACTIVE",
                "severity": "WARN",
                "accountId": aid,
                "title": acc.get("title") or "",
                "accountAcct": acc.get("accountAcct") or "",
                "currencyCode": (acc.get("currencyCode") or "RUB").upper(),
                "planfactBalance": pf_bal,
                "actualBalance": None,
                "delta": None,
                "hints": [{
                    "type": "MISSING_IN_ACTUAL_BALANCES",
                    "operationIds": [],
                    "message": "Активный счёт с ненулевым остатком в ПланФакте не передан в файле фактических остатков.",
                }],
            })

    return {"performed": True, "items": items}


# ---------------------------------------------------------------------------
# Сборка отчёта и рендеринг
# ---------------------------------------------------------------------------

def run_audit(
    snapshot: Dict[str, Any],
    actual_rows: Optional[List[Dict[str, Any]]],
    as_of: str,
    ops_from: Optional[str] = None,
    ops_to: Optional[str] = None,
    dup_window_days: int = 2,
    other_limit_pct: float = 5.0,
    include_archived: bool = False,
    skipped_empty_actual: int = 0,
) -> Dict[str, Any]:
    """Запускает все 6 детерминированных проверок и возвращает структуру отчёта."""
    as_of_date = parse_iso_date(as_of) or dt.date.today()
    accounts = snapshot.get("accounts") or []
    accounts_by_id = {_safe_int(a.get("accountId")): a for a in accounts if _safe_int(a.get("accountId")) is not None}
    categories = snapshot.get("categories") or []
    categories_by_id = {
        _safe_int(c.get("operationCategoryId")): c
        for c in categories
        if _safe_int(c.get("operationCategoryId")) is not None
    }
    operations = _filter_operations(snapshot.get("operations") or [], ops_from, ops_to)

    dup_findings = check_duplicate_operations(operations, dup_window_days=dup_window_days)
    move_findings = check_broken_or_unmerged_moves(operations, accounts_by_id)
    undist_result = check_undistributed_operations(
        operations, categories_by_id, reported_undistributed_count=snapshot.get("undistributedCount")
    )
    other_result = check_other_category_bloat(
        operations, categories_by_id, other_limit_pct=other_limit_pct
    )
    overdue_findings = check_overdue_plans(operations, as_of_date)
    recon_result = check_balance_reconciliation(
        accounts=accounts,
        actual_rows=actual_rows,
        operations=operations,
        dup_findings=dup_findings,
        move_findings=move_findings,
        overdue_findings=overdue_findings,
        include_archived=include_archived,
        dashboard_balances=snapshot.get("dashboardBalances"),
    )

    critical_count = 0
    warn_count = 0

    for item in recon_result.get("items") or []:
        if item.get("severity") == "CRITICAL":
            critical_count += 1
        elif item.get("severity") == "WARN":
            warn_count += 1

    for group in (dup_findings, move_findings, undist_result["items"], other_result["warnings"], overdue_findings):
        for item in group:
            sev = item.get("severity")
            if sev == "CRITICAL":
                critical_count += 1
            elif sev == "WARN":
                warn_count += 1

    # Если API вернул ненулевой undistributedCount, а в выборке операций они не попались (например, за пределами ops-from)
    reported_undist = undist_result.get("reportedUndistributedCount")
    if reported_undist is not None and reported_undist > 0 and not undist_result["items"]:
        critical_count += 1

    status = "FAIL" if critical_count > 0 else ("WARN" if warn_count > 0 else "CLEAN")
    return {
        "meta": {
            "asOfDate": as_of_date.isoformat(),
            "opsFrom": ops_from,
            "opsTo": ops_to,
            "dupWindowDays": dup_window_days,
            "otherLimitPct": other_limit_pct,
            "accountsAnalyzed": len(accounts),
            "operationsAnalyzed": len(operations),
            "actualRowsLoaded": len(actual_rows) if actual_rows is not None else 0,
            "skippedEmptyActualRows": skipped_empty_actual,
        },
        "summary": {
            "status": status,
            "criticalCount": critical_count,
            "warnCount": warn_count,
            "balanceMismatches": sum(1 for i in recon_result["items"] if i["status"] == "MISMATCH"),
            "duplicateClusters": len(dup_findings),
            "brokenOrUnmergedMoves": len(move_findings),
            "undistributedIssues": len(undist_result["items"]),
            "otherCategoryWarnings": len(other_result["warnings"]),
            "overduePlans": len(overdue_findings),
        },
        "checks": {
            "balance_reconciliation": recon_result,
            "duplicate_operations": dup_findings,
            "broken_or_unmerged_moves": move_findings,
            "undistributed_operations": undist_result,
            "other_category_bloat": other_result,
            "overdue_plans": overdue_findings,
        },
    }


def render_markdown_report(report: Dict[str, Any]) -> str:
    """Формирует Markdown-отчёт ревизии и сверки."""
    meta = report["meta"]
    summary = report["summary"]
    checks = report["checks"]
    lines: List[str] = [
        f"# Ревизия и сверка ПланФакта на {meta['asOfDate']}",
        "",
        f"- **Статус:** `{summary['status']}` (критических: {summary['criticalCount']}, предупреждений: {summary['warnCount']})",
        f"- **Счетов в базе:** {meta['accountsAnalyzed']}, **операций в выборке:** {meta['operationsAnalyzed']}",
        "",
        "## 1. Сверка остатков счетов (`balance_reconciliation`)",
        "",
    ]

    recon = checks["balance_reconciliation"]
    if not recon["performed"]:
        lines.append("Файл `--actual-balances` не передан — сверка с реальными остатками не выполнялась.")
    elif not recon["items"]:
        lines.append("Нет счетов для сверки.")
    else:
        lines.extend([
            "| Счёт | ID | Валюта | ПланФакт | Факт | Дельта | Статус |",
            "| :--- | ---: | :---: | ---: | ---: | ---: | :---: |",
        ])
        for it in recon["items"]:
            pf_s = f"{it['planfactBalance']:.2f}" if it["planfactBalance"] is not None else "—"
            ac_s = f"{it['actualBalance']:.2f}" if it["actualBalance"] is not None else "—"
            dl_s = f"{it['delta']:+.2f}" if it["delta"] is not None else "—"
            lines.append(
                f"| {it['title']} | {it.get('accountId') or '—'} | {it['currencyCode']} | {pf_s} | {ac_s} | {dl_s} | `{it['status']}` |"
            )
        mismatch_items = [it for it in recon["items"] if it.get("hints")]
        if mismatch_items:
            lines.extend(["", "### Подсказки по причинам расхождений (Root-Cause Hint Engine)", ""])
            for it in mismatch_items:
                lines.append(f"- **{it['title']}** (ID `{it.get('accountId')}`, статус `{it['status']}`):")
                for hint in it["hints"]:
                    lines.append(f"  - [{hint['type']}] {hint['message']}")

    lines.extend(["", "## 2. Дубли операций (`duplicate_operations`)", ""])
    dups = checks["duplicate_operations"]
    if not dups:
        lines.append("Дубли операций не обнаружены.")
    else:
        for d in dups:
            lines.append(
                f"- `{d['severity']}` ({d['confidence']}, `{d['reason']}`): "
                f"счёт `{d['accountId']}`, {d['operationType']} на {d['value']:.2f}, "
                f"`operationIds`: {d['operationIds']}, даты: {d['dates']}."
            )

    lines.extend(["", "## 3. Перемещения (`broken_or_unmerged_moves`)", ""])
    moves = checks["broken_or_unmerged_moves"]
    if not moves:
        lines.append("Битых или несхлопнутых перемещений не обнаружено.")
    else:
        for m in moves:
            lines.append(f"- `{m['severity']}` (`{m['subtype']}`): {m['message']}")

    lines.extend(["", "## 4. Неразнесённые операции (`undistributed_operations`)", ""])
    undist = checks["undistributed_operations"]
    if not undist["items"]:
        lines.append(
            f"Неразнесённых операций и битых сплитов не обнаружено (API `undistributedCount`: {undist.get('reportedUndistributedCount')})."
        )
    else:
        lines.append(
            f"Найдено проблем: {len(undist['items'])} (уникальных операций: {undist['detectedCount']}, API `undistributedCount`: {undist.get('reportedUndistributedCount')})."
        )
        for u in undist["items"][:50]:
            lines.append(
                f"- `{u['subtype']}`: `operationId={u['operationId']}`, сумма {u['value']:.2f}, статья `{u.get('accountCategoryType')}`."
            )

    lines.extend(["", "## 5. Доля категории «Прочее» (`other_category_bloat`)", ""])
    other = checks["other_category_bloat"]
    for op_type, label in (("Outcome", "Расходы"), ("Income", "Доходы")):
        m = other["metrics"][op_type]
        lines.append(
            f"- **{label}:** {m['otherAmount']:.2f} из {m['totalActual']:.2f} ({m['otherPercent']:.2f}%, порог {other['limitPercent']:.2f}%)."
        )
    for w in other["warnings"]:
        lines.append(f"- `WARN`: {w['message']} (`operationIds`: {w['operationIds']})")

    lines.extend(["", "## 6. Просроченные планы (`overdue_plans`)", ""])
    overdue = checks["overdue_plans"]
    if not overdue:
        lines.append("Просроченных плановых операций и начислений не обнаружено.")
    else:
        for ov in overdue[:50]:
            lines.append(
                f"- `{ov['severity']}` (`{ov['subtype']}`, {ov['ageBucket']}): "
                f"`operationId={ov['operationId']}`, счёт `{ov['accountId']}`, дата {ov['date']}, сумма {ov['value']:.2f} (просрочка {ov['overdueDays']} дн.)."
            )

    lines.append("")
    return "\n".join(lines)


def render_stdout_summary(report: Dict[str, Any]) -> str:
    """Краткая сводка результатов ревизии для вывода в консоль."""
    s = report["summary"]
    m = report["meta"]
    lines = [
        f"ПланФакт-ревизия на {m['asOfDate']} | Статус: {s['status']}",
        f"Критичных проблем: {s['criticalCount']} | Предупреждений: {s['warnCount']}",
        f"- Расхождения остатков (MISMATCH): {s['balanceMismatches']}",
        f"- Кластеры дублей: {s['duplicateClusters']}",
        f"- Битые/несхлопнутые перемещения: {s['brokenOrUnmergedMoves']}",
        f"- Неразнесённые/битые сплиты: {s['undistributedIssues']}",
        f"- Предупреждения по «Прочее»: {s['otherCategoryWarnings']}",
        f"- Просроченные планы: {s['overduePlans']}",
    ]
    return "\n".join(lines)


def _acquire_snapshot(args: argparse.Namespace) -> Dict[str, Any]:
    if getattr(args, "snapshot_json", None):
        return load_snapshot_json(Path(args.snapshot_json))
    api_key = resolve_api_key(getattr(args, "env_file", None))
    client = PlanFactReadOnlyClient(api_key=api_key)
    snapshot = client.fetch_snapshot(
        ops_from=getattr(args, "ops_from", None),
        ops_to=getattr(args, "ops_to", None),
        as_of=getattr(args, "as_of", None),
    )
    save_path = getattr(args, "save_snapshot", None)
    if save_path:
        out_file = Path(save_path)
        out_file.parent.mkdir(parents=True, exist_ok=True)
        out_file.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return snapshot


def _cmd_export_template(args: argparse.Namespace) -> int:
    as_of = date_str(args.as_of) or dt.date.today().isoformat()
    snapshot = _acquire_snapshot(args)
    rows = export_template_rows(
        snapshot=snapshot,
        as_of=as_of,
        include_archived=args.include_archived,
        prefill_actual=args.prefill_actual,
    )
    out_path = Path(args.out)
    write_template_file(rows, out_path, fmt=args.format)
    print(f"Шаблон выгружен: {out_path} (счетов: {len(rows)}, дата сверки: {as_of})")
    return 0


def _cmd_audit(args: argparse.Namespace) -> int:
    as_of = date_str(args.as_of) or dt.date.today().isoformat()
    snapshot = _acquire_snapshot(args)

    actual_rows: Optional[List[Dict[str, Any]]] = None
    skipped_empty = 0
    if args.actual_balances:
        actual_rows, skipped_empty = load_actual_balances(Path(args.actual_balances))

    report = run_audit(
        snapshot=snapshot,
        actual_rows=actual_rows,
        as_of=as_of,
        ops_from=args.ops_from,
        ops_to=args.ops_to,
        dup_window_days=args.dup_window_days,
        other_limit_pct=args.other_limit_pct,
        include_archived=args.include_archived,
        skipped_empty_actual=skipped_empty,
    )

    if args.out_dir:
        out_dir = Path(args.out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "audit_findings.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        (out_dir / "audit_summary.md").write_text(
            render_markdown_report(report),
            encoding="utf-8",
        )

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(render_stdout_summary(report))
        if args.out_dir:
            print(f"Отчёты сохранены в: {args.out_dir}")

    if report["summary"]["criticalCount"] > 0 and not args.no_fail_on_findings:
        return 2
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="planfact_reconcile_audit.py",
        description="Read-only сверка балансов и ревизия учёта ПланФакта.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_exp = sub.add_parser("export-template", help="Выгрузить шаблон счетов для ввода фактических остатков.")
    p_exp.add_argument("--out", required=True, help="Путь к выходному файлу (.json или .csv).")
    p_exp.add_argument("--format", choices=("json", "csv"), help="Явный формат вывода (по умолчанию определяется по расширению).")
    p_exp.add_argument("--snapshot-json", help="Путь к офлайн-снимку данных ПланФакта.")
    p_exp.add_argument("--env-file", help="Путь к .env файлу с PLANFACT_API_KEY.")
    p_exp.add_argument("--as-of", help="Целевая дата сверки (YYYY-MM-DD).")
    p_exp.add_argument("--include-archived", action="store_true", help="Включить архивные счета.")
    p_exp.add_argument("--prefill-actual", action="store_true", help="Предзаполнить actualBalance значением из ПланФакта.")

    p_aud = sub.add_parser("audit", help="Выполнить ревизию учёта и сверку балансов.")
    p_aud.add_argument("--snapshot-json", help="Путь к офлайн-снимку данных ПланФакта.")
    p_aud.add_argument("--save-snapshot", help="Сохранить загруженный снимок с API в JSON.")
    p_aud.add_argument("--env-file", help="Путь к .env файлу с PLANFACT_API_KEY.")
    p_aud.add_argument("--actual-balances", help="Путь к файлу фактических остатков (.json или .csv).")
    p_aud.add_argument("--as-of", help="Целевая дата сверки и отсечки просрочки (YYYY-MM-DD).")
    p_aud.add_argument("--ops-from", help="Фильтр операций с даты (YYYY-MM-DD).")
    p_aud.add_argument("--ops-to", help="Фильтр операций по дату (YYYY-MM-DD).")
    p_aud.add_argument("--dup-window-days", type=int, default=2, help="Окно поиска эвристических дублей в днях (по умолчанию 2).")
    p_aud.add_argument("--other-limit-pct", type=float, default=5.0, help="Порог доли статей «Прочее» в %% (по умолчанию 5.0).")
    p_aud.add_argument("--include-archived", action="store_true", help="Учитывать архивные счета при сверке.")
    p_aud.add_argument("--out-dir", help="Папка для сохранения audit_summary.md и audit_findings.json.")
    p_aud.add_argument("--json", action="store_true", help="Вывести JSON-отчёт в stdout.")
    p_aud.add_argument("--no-fail-on-findings", action="store_true", help="Всегда возвращать код 0 при наличии расхождений.")

    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "export-template":
            return _cmd_export_template(args)
        if args.command == "audit":
            return _cmd_audit(args)
        parser.print_help()
        return 1
    except PlanFactError as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
