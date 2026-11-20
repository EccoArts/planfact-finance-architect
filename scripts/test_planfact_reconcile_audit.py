#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Unit-тесты для planfact_reconcile_audit.py."""

from __future__ import annotations

import csv
import datetime as dt
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import planfact_reconcile_audit as pfa  # noqa: E402


def _make_account(
    account_id: int,
    title: str,
    remainder: float,
    acct: str = "",
    currency: str = "RUB",
    active: bool = True,
    is_undistributed: bool = False,
    start_date: str | None = "2026-01-01T00:00:00",
    start_val: float | None = 100000.0,
) -> dict:
    return {
        "accountId": account_id,
        "title": title,
        "longTitle": title,
        "accountType": "Electronic",
        "accountAcct": acct,
        "currencyCode": currency,
        "remainder": remainder,
        "active": active,
        "isUndistributed": is_undistributed,
        "startingRemainderDate": start_date,
        "startingRemainderValue": start_val,
        "company": {"companyId": 1, "title": "ООО «Тест»"},
    }


def _make_op(
    op_id: int,
    account_id: int,
    op_type: str,
    value: float,
    op_date: str = "2026-11-10T10:00:00",
    is_committed: bool = True,
    calc_date: str | None = None,
    is_calc_committed: bool = True,
    category_id: int | None = 10,
    category_title: str = "Операционные расходы",
    category_acc_type: str | None = None,
    import_log_id: int | None = 100,
    external_id: str | None = None,
    doc_number: str | None = None,
    bound_move_id: int | None = None,
    parts: list | None = None,
    correct_remainder: float | None = None,
    comment: str = "",
    contragent_id: int | None = 50,
    currency: str = "RUB",
) -> dict:
    cat_obj = None
    if category_id is not None:
        cat_obj = {
            "operationCategoryId": category_id,
            "title": category_title,
            "accountCategoryType": category_acc_type,
        }
    return {
        "operationId": op_id,
        "operationType": op_type,
        "operationDate": op_date,
        "calculationPeriodDate": calc_date or op_date,
        "isCommitted": is_committed,
        "isCalculationCommitted": is_calc_committed,
        "value": value,
        "valueInUserCurrency": value,
        "correctRemainder": correct_remainder,
        "account": {"accountId": account_id, "title": f"Счёт {account_id}"},
        "accountCurrency": {"currencyCode": currency},
        "contrAgent": {"contrAgentId": contragent_id, "title": "КА"} if contragent_id else None,
        "operationCategory": cat_obj,
        "operationParts": parts or [],
        "boundMoveOperationId": bound_move_id,
        "importLogId": import_log_id,
        "externalId": external_id,
        "documentNumber": doc_number,
        "comment": comment,
    }


class TestActualBalancesParsingAndMatching(unittest.TestCase):
    def test_parse_json_and_csv_and_match_priorities(self) -> None:
        accounts = [
            _make_account(1, "Основной р/с Т-Банк", 150000.0, acct="40702810100000123456"),
            _make_account(2, "Резервный р/с Альфа", 50000.0, acct="40702810900000654321"),
            _make_account(3, "Касса офис", 12500.50, acct="", currency="RUB"),
        ]

        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            json_path = tmp / "actual.json"
            json_path.write_text(
                json.dumps(
                    [
                        {"account_id": 1, "actual_balance": 150000.0},
                        {"account_acct": "40702 810 9 0000 0654321", "actualBalance": "50 000,00"},
                        {"title": "  касса   офис ", "currencyCode": "RUB", "actualBalance": 12500.50},
                        {"accountId": 999, "title": "Пустая строка", "actualBalance": ""},
                    ],
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            rows_json, skipped = pfa.load_actual_balances(json_path)
            self.assertEqual(len(rows_json), 3)
            self.assertEqual(skipped, 1)

            recon_json = pfa.check_balance_reconciliation(accounts, rows_json, [], [], [], [])
            statuses = [item["status"] for item in recon_json["items"]]
            self.assertEqual(statuses, ["MATCH", "MATCH", "MATCH"])
            self.assertEqual([item["accountId"] for item in recon_json["items"]], [1, 2, 3])

            csv_path = tmp / "actual.csv"
            csv_path.write_text(
                "\ufeffaccountId;accountAcct;title;currencyCode;actualBalance\n"
                "1;;Основной р/с Т-Банк;RUB;150 000,00\n"
                ";40702810900000654321;;RUB;50000.00\n"
                ";;Касса Офис;RUB;12500.50\n",
                encoding="utf-8",
            )
            rows_csv, skipped_csv = pfa.load_actual_balances(csv_path)
            self.assertEqual(len(rows_csv), 3)
            self.assertEqual(skipped_csv, 0)
            recon_csv = pfa.check_balance_reconciliation(accounts, rows_csv, [], [], [], [])
            self.assertTrue(all(item["status"] == "MATCH" for item in recon_csv["items"]))


class TestRootCauseHintEngine(unittest.TestCase):
    def test_hints_for_duplicate_overdue_move_and_starting_remainder(self) -> None:
        accounts = [
            _make_account(10, "Счёт дубль", 115000.0, start_date="2026-01-01T00:00:00", start_val=100000.0),
            _make_account(20, "Счёт просрочка", 85000.0, start_date=None, start_val=None),
            _make_account(30, "Счёт перемещение", 60000.0, start_date="2026-01-01T00:00:00", start_val=10000.0),
            _make_account(40, "Счёт получатель", 25000.0),
        ]
        ops = [
            # Дубль на счёте 10 на 15000 (ручной + импорт)
            _make_op(101, 10, "Income", 15000.0, "2026-11-05T10:00:00", import_log_id=None),
            _make_op(102, 10, "Income", 15000.0, "2026-11-05T12:00:00", import_log_id=777),
            # Просроченный план на счёте 20 на 5000 + correctRemainder на 202
            _make_op(201, 20, "Outcome", 5000.0, "2026-11-01T10:00:00", is_committed=False),
            _make_op(202, 20, "Income", 1000.0, "2026-11-02T10:00:00", correct_remainder=85000.0),
            # Несхлопнутое перемещение со счёта 30 на счёт 40 на 12000
            _make_op(301, 30, "Outcome", 12000.0, "2026-11-08T10:00:00"),
            _make_op(302, 40, "Income", 12000.0, "2026-11-09T10:00:00"),
        ]
        actual_rows = [
            {"rowIndex": 1, "accountId": 10, "accountAcct": "", "title": "", "currencyCode": "RUB", "actualBalance": 100000.0},
            {"rowIndex": 2, "accountId": 20, "accountAcct": "", "title": "", "currencyCode": "RUB", "actualBalance": 80000.0},
            {"rowIndex": 3, "accountId": 30, "accountAcct": "", "title": "", "currencyCode": "RUB", "actualBalance": 48000.0},
            {"rowIndex": 4, "accountId": 40, "accountAcct": "", "title": "", "currencyCode": "RUB", "actualBalance": 25000.0},
        ]

        report = pfa.run_audit(
            snapshot={"accounts": accounts, "operations": ops, "categories": [], "undistributedCount": 0},
            actual_rows=actual_rows,
            as_of="2026-11-20",
        )
        items_by_acc = {it["accountId"]: it for it in report["checks"]["balance_reconciliation"]["items"]}

        # Счёт 10: delta = -15000 -> DUPLICATE_EXACT_DELTA с operationIds [101, 102]
        self.assertEqual(items_by_acc[10]["status"], "MISMATCH")
        self.assertEqual(items_by_acc[10]["delta"], -15000.0)
        hint_types_10 = {h["type"]: h["operationIds"] for h in items_by_acc[10]["hints"]}
        self.assertIn("DUPLICATE_EXACT_DELTA", hint_types_10)
        self.assertEqual(hint_types_10["DUPLICATE_EXACT_DELTA"], [101, 102])

        # Счёт 20: delta = -5000 -> OVERDUE_PLAN_EXACT_DELTA ([201]), MISSING_STARTING_REMAINDER_DATE, CORRECT_REMAINDER_PRESENT ([202])
        hint_types_20 = {h["type"]: h["operationIds"] for h in items_by_acc[20]["hints"]}
        self.assertIn("OVERDUE_PLAN_EXACT_DELTA", hint_types_20)
        self.assertEqual(hint_types_20["OVERDUE_PLAN_EXACT_DELTA"], [201])
        self.assertIn("MISSING_STARTING_REMAINDER_DATE", hint_types_20)
        self.assertIn("CORRECT_REMAINDER_PRESENT", hint_types_20)
        self.assertEqual(hint_types_20["CORRECT_REMAINDER_PRESENT"], [202])

        # Счёт 30: delta = -12000 -> BROKEN_OR_UNMERGED_MOVE_EXACT_DELTA ([301, 302])
        hint_types_30 = {h["type"]: h["operationIds"] for h in items_by_acc[30]["hints"]}
        self.assertIn("BROKEN_OR_UNMERGED_MOVE_EXACT_DELTA", hint_types_30)
        self.assertEqual(hint_types_30["BROKEN_OR_UNMERGED_MOVE_EXACT_DELTA"], [301, 302])


class TestAccountingChecks(unittest.TestCase):
    def test_duplicates_manual_plus_import_and_exact(self) -> None:
        ops = [
            _make_op(1, 10, "Outcome", 4500.0, "2026-11-10T09:00:00", import_log_id=None),
            _make_op(2, 10, "Outcome", 4500.0, "2026-11-11T15:00:00", import_log_id=333),
            _make_op(3, 10, "Income", 9900.0, "2026-11-12T10:00:00", external_id="ext-42"),
            _make_op(4, 10, "Income", 9900.0, "2026-11-12T10:00:00", external_id="ext-42"),
        ]
        dups = pfa.check_duplicate_operations(ops, dup_window_days=2)
        reasons = {d["reason"]: d for d in dups}
        self.assertIn("manual_plus_import", reasons)
        self.assertEqual(reasons["manual_plus_import"]["operationIds"], [1, 2])
        self.assertEqual(reasons["manual_plus_import"]["confidence"], "HIGH")
        self.assertIn("exact_external_id", reasons)
        self.assertEqual(reasons["exact_external_id"]["operationIds"], [3, 4])

    def test_broken_moves_and_unmerged_candidates(self) -> None:
        accounts_by_id = {
            1: _make_account(1, "Р/с Основной", 100000.0),
            2: _make_account(2, "Корпоративная карта", 20000.0),
        }
        ops = [
            # Сирота Move
            _make_op(10, 1, "Move", 5000.0, bound_move_id=None),
            # Асимметрия isCommitted + расхождение суммы в одной валюте
            _make_op(11, 1, "Move", 10000.0, is_committed=True, bound_move_id=12),
            _make_op(12, 2, "Move", 9800.0, is_committed=False, bound_move_id=11),
            # Несхлопнутые Outcome + Income
            _make_op(21, 1, "Outcome", 25000.0, "2026-11-14T10:00:00"),
            _make_op(22, 2, "Income", 25000.0, "2026-11-15T10:00:00"),
        ]
        findings = pfa.check_broken_or_unmerged_moves(ops, accounts_by_id)
        subtypes = {f["subtype"]: f for f in findings}
        self.assertIn("ORPHAN_MOVE", subtypes)
        self.assertIn("COMMIT_ASYMMETRY", subtypes)
        self.assertIn("AMOUNT_MISMATCH_SAME_CURRENCY", subtypes)
        self.assertIn("UNMERGED_MOVE_CANDIDATE", subtypes)
        self.assertEqual(subtypes["UNMERGED_MOVE_CANDIDATE"]["operationIds"], [21, 22])

    def test_undistributed_other_bloat_and_overdue_plans(self) -> None:
        categories = [
            {"operationCategoryId": 1, "title": "Выручка", "accountCategoryType": None},
            {"operationCategoryId": 2, "title": "Аренда", "accountCategoryType": None},
            {"operationCategoryId": 3, "title": "Прочие расходы", "accountCategoryType": None},
            {"operationCategoryId": 4, "title": "Не разобрано (расход)", "accountCategoryType": "OutcomeUndistributed"},
        ]
        cats_by_id = {c["operationCategoryId"]: c for c in categories}
        ops = [
            # Неразнесённая без категории
            _make_op(1, 10, "Income", 1000.0, category_id=None),
            # Сплит: одна часть OutcomeUndistributed, сумма частей != value (400 + 500 != 1000)
            _make_op(
                2,
                10,
                "Outcome",
                1000.0,
                category_id=None,
                parts=[
                    {"operationCategory": categories[1], "value": 400.0, "valueInUserCurrency": 400.0},
                    {"operationCategory": categories[3], "value": 500.0, "valueInUserCurrency": 500.0},
                ],
            ),
            # Прочие расходы = 1500 из общих 10000 (15.0% > 5%)
            _make_op(3, 10, "Outcome", 7600.0, category_id=2, category_title="Аренда"),
            _make_op(4, 10, "Outcome", 1500.0, category_id=3, category_title="Прочие расходы"),
            # Просроченный кассовый план и просроченное начисление
            _make_op(5, 10, "Outcome", 3000.0, op_date="2026-11-01T10:00:00", is_committed=False, is_calc_committed=True),
            _make_op(6, 10, "Income", 4000.0, op_date="2026-11-18T10:00:00", is_committed=True, calc_date="2026-11-10T10:00:00", is_calc_committed=False),
        ]

        undist = pfa.check_undistributed_operations(ops, cats_by_id, reported_undistributed_count=2)
        undist_subtypes = [it["subtype"] for it in undist["items"]]
        self.assertIn("UNDISTRIBUTED_OPERATION", undist_subtypes)
        self.assertIn("UNDISTRIBUTED_PART", undist_subtypes)
        self.assertIn("PARTS_SUM_MISMATCH", undist_subtypes)

        other = pfa.check_other_category_bloat(ops, cats_by_id, other_limit_pct=5.0)
        self.assertEqual(len(other["warnings"]), 1)
        self.assertEqual(other["warnings"][0]["operationType"], "Outcome")
        self.assertEqual(other["metrics"]["Outcome"]["otherPercent"], 15.0)

        overdue = pfa.check_overdue_plans(ops, dt.date(2026, 11, 20))
        overdue_subtypes = {ov["subtype"]: ov for ov in overdue}
        self.assertIn("OVERDUE_CASH_PLAN", overdue_subtypes)
        self.assertEqual(overdue_subtypes["OVERDUE_CASH_PLAN"]["operationId"], 5)
        self.assertIn("OVERDUE_ACCRUAL_PLAN", overdue_subtypes)
        self.assertEqual(overdue_subtypes["OVERDUE_ACCRUAL_PLAN"]["operationId"], 6)


class TestReadOnlyGuardAndCLI(unittest.TestCase):
    def test_read_only_client_blocks_mutations_and_masks_key(self) -> None:
        client = pfa.PlanFactReadOnlyClient(api_key="secret_planfact_token_12345")
        self.assertEqual(pfa.mask_api_key("secret_planfact_token_12345"), "pf_***")
        with self.assertRaises(pfa.ReadOnlyViolationError):
            client.request("PUT", "/api/v1/accounts/1", body={"title": "hack"})
        with self.assertRaises(pfa.ReadOnlyViolationError):
            client.request("DELETE", "/api/v1/operations/1")
        with self.assertRaises(pfa.ReadOnlyViolationError):
            client.request("POST", "/api/v1/operations/income", body={})

    def test_example_templates_are_valid(self) -> None:
        templates_dir = SCRIPT_DIR.parent / "templates"
        rows_json, skipped_j = pfa.load_actual_balances(templates_dir / "actual_balances.example.json")
        rows_csv, skipped_c = pfa.load_actual_balances(templates_dir / "actual_balances.example.csv")
        self.assertEqual(len(rows_json), 3)
        self.assertEqual(len(rows_csv), 3)
        self.assertEqual(skipped_j, 0)
        self.assertEqual(skipped_c, 0)

    def test_api_error_on_is_success_false_and_quota_warning(self) -> None:
        client = pfa.PlanFactReadOnlyClient(api_key="pf_secret_xyz")
        fake_headers = {"X-Quota-Remaining": "120", "X-RateLimit-Limit": "60"}
        fake_resp = mock.MagicMock()
        fake_resp.__enter__.return_value = fake_resp
        fake_resp.headers = fake_headers
        fake_resp.read.return_value = json.dumps({
            "data": None,
            "isSuccess": False,
            "errorCode": "NotEnoughApiPermission",
            "errorMessage": "Forbidden for pf_secret_xyz",
        }).encode("utf-8")

        with mock.patch("urllib.request.urlopen", return_value=fake_resp), \
             mock.patch("sys.stderr", new_callable=io.StringIO) as err_buf:
            with self.assertRaises(pfa.PlanFactApiError) as ctx:
                client.request("GET", "/api/v1/accounts")
        self.assertIn("pf_***", str(ctx.exception))
        self.assertNotIn("pf_secret_xyz", str(ctx.exception))
        self.assertIn("X-Quota-Remaining=120", err_buf.getvalue())
        self.assertNotIn("pf_secret_xyz", err_buf.getvalue())

    def test_cli_export_template_and_audit_end_to_end(self) -> None:
        snapshot = {
            "accounts": [
                _make_account(101, "Расчётный счёт", 500000.0, acct="40702810100000123456"),
                _make_account(102, "Служебный", 0.0, is_undistributed=True),
                _make_account(103, "Архивный", 0.0, active=False),
            ],
            "operations": [
                _make_op(1, 101, "Income", 500000.0, "2026-11-05T10:00:00", category_id=1, category_title="Выручка"),
            ],
            "categories": [
                {"operationCategoryId": 1, "title": "Выручка", "accountCategoryType": None},
            ],
            "undistributedCount": 0,
        }

        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            snap_path = tmp / "snapshot.json"
            snap_path.write_text(json.dumps(snapshot, ensure_ascii=False), encoding="utf-8")

            tpl_json = tmp / "tpl.json"
            tpl_csv = tmp / "tpl.csv"

            with mock.patch("sys.stdout", new_callable=io.StringIO):
                rc1 = pfa.main(["export-template", "--snapshot-json", str(snap_path), "--out", str(tpl_json), "--as-of", "2026-11-20"])
                rc2 = pfa.main(["export-template", "--snapshot-json", str(snap_path), "--out", str(tpl_csv), "--as-of", "2026-11-20", "--prefill-actual"])
            self.assertEqual(rc1, 0)
            self.assertEqual(rc2, 0)

            exported = json.loads(tpl_json.read_text(encoding="utf-8"))
            self.assertEqual(len(exported), 1)
            self.assertEqual(exported[0]["accountId"], 101)
            self.assertIsNone(exported[0]["actualBalance"])

            with tpl_csv.open("r", encoding="utf-8-sig") as fh:
                csv_rows = list(csv.DictReader(fh))
            self.assertEqual(len(csv_rows), 1)
            self.assertEqual(csv_rows[0]["actualBalance"], "500000.0")

            # Чистый аудит -> exit code 0
            out_clean = tmp / "out_clean"
            with mock.patch("sys.stdout", new_callable=io.StringIO) as buf:
                rc_clean = pfa.main([
                    "audit",
                    "--snapshot-json", str(snap_path),
                    "--actual-balances", str(tpl_csv),
                    "--as-of", "2026-11-20",
                    "--out-dir", str(out_clean),
                    "--json",
                ])
            self.assertEqual(rc_clean, 0)
            clean_json = json.loads(buf.getvalue())
            self.assertEqual(clean_json["summary"]["status"], "CLEAN")
            self.assertTrue((out_clean / "audit_summary.md").is_file())
            self.assertTrue((out_clean / "audit_findings.json").is_file())

            # Аудит с расхождением -> exit code 2
            bad_actual = tmp / "bad_actual.json"
            bad_actual.write_text(
                json.dumps([{"accountId": 101, "actualBalance": 490000.0}], ensure_ascii=False),
                encoding="utf-8",
            )
            out_fail = tmp / "out_fail"
            with mock.patch("sys.stdout", new_callable=io.StringIO):
                rc_fail = pfa.main([
                    "audit",
                    "--snapshot-json", str(snap_path),
                    "--actual-balances", str(bad_actual),
                    "--as-of", "2026-11-20",
                    "--out-dir", str(out_fail),
                ])
            self.assertEqual(rc_fail, 2)
            md_text = (out_fail / "audit_summary.md").read_text(encoding="utf-8")
            self.assertIn("MISMATCH", md_text)


if __name__ == "__main__":
    unittest.main()
