"""Safe failure details: codes and SQLSTATE out, SQL text and parameters never."""
import sqlalchemy as sa

from app.clhear.platform import failures
from tests.test_migration_concurrency import real_postgresql  # noqa: F401


def _sqlalchemy_error(engine, statement):
    try:
        with engine.begin() as conn:
            conn.exec_driver_sql(statement)
    except sa.exc.DBAPIError as exc:
        return exc
    raise AssertionError("statement was expected to fail")


def test_redaction_removes_sql_parameters_urls_and_secrets():
    raw = ("(psycopg.errors.InFailedSqlTransaction) current transaction is aborted\n"
           "[SQL: SELECT l1_sources.search_units.id FROM l1_sources.search_units WHERE source_id = %(source_id_1)s]\n"
           "[parameters: {'source_id_1': 150}]\n(Background on this error at: https://sqlalche.me/e/20/2j85)")
    text = failures.redact(raw)
    assert text == "(psycopg.errors.InFailedSqlTransaction) current transaction is aborted"
    assert "SELECT" not in text and "150" not in text and "sqlalche.me" not in text
    assert failures.redact("fetch https://publisher.example/rules/2210?token=abc failed; password=hunter2") == \
        "fetch <url> failed; password=<redacted>"
    assert failures.redact("A firm must 'pay due regard to the interests of its customers and treat them fairly' now") == \
        "A firm must '<redacted>' now"
    assert failures.redact("dsn postgresql+psycopg://user:pw@host/db is set") == "dsn <dsn> is set"


def test_first_cause_is_kept_apart_from_follow_on_transaction_errors():
    class InFailedSqlTransaction(Exception):
        sqlstate = "25P02"

    class UndefinedTable(Exception):
        sqlstate = "42P01"

    first = UndefinedTable('relation "search_units_fts" does not exist')
    try:
        try:
            raise first
        except UndefinedTable as inner:
            raise InFailedSqlTransaction("current transaction is aborted") from inner
    except InFailedSqlTransaction as outer:
        detail = failures.describe(outer, source="finra/rule/2210", worker="l1", stage="persistence", attempt=1)
    assert detail["error_type"] == "InFailedSqlTransaction"
    assert detail["error_code"] == "UndefinedTable" and detail["sqlstate"] == "42P01"
    assert detail["first_cause"]["error_code"] == "UndefinedTable"
    assert [e["error_code"] for e in detail["follow_on"]] == ["InFailedSqlTransaction"]
    assert detail["aborted_transaction"] is True
    assert detail["source"] == "finra/rule/2210" and detail["stage"] == "persistence" and detail["attempt"] == 1
    assert "search_units_fts" in detail["message"]  # a short identifier is kept; text and parameters are not


def test_sqlite_errors_carry_codes_without_sqlstate(engine):
    exc = _sqlalchemy_error(engine, "SELECT * FROM no_such_table")
    detail = failures.describe(exc)
    assert detail["error_type"] == "OperationalError" and detail["error_code"] == "OperationalError"
    assert detail["sqlstate"] is None and "[SQL" not in detail["message"] and "parameters" not in detail["message"]


def test_real_postgresql_sqlstate_and_aborted_transaction_are_reported(real_postgresql):
    engine = real_postgresql
    exc = _sqlalchemy_error(engine, "SELECT * FROM l1_sources.no_such_table")
    detail = failures.describe(exc)
    assert detail["error_code"] == "UndefinedTable" and detail["sqlstate"] == "42P01"
    assert "SELECT" not in detail["message"]
    with engine.connect() as conn:
        with conn.begin():
            try:
                conn.exec_driver_sql("SELECT * FROM l1_sources.no_such_table")
            except sa.exc.DBAPIError:
                pass
            try:
                conn.exec_driver_sql("SELECT 1")
            except sa.exc.DBAPIError as follow_on:
                detail = failures.describe(follow_on)
            conn.rollback()
    assert detail["error_type"] == "InternalError" and detail["aborted_transaction"] is True
    assert detail["chain"][0]["error_code"] == "InFailedSqlTransaction" and detail["chain"][0]["sqlstate"] == "25P02"


def test_task_failure_summary_is_bounded_and_carries_codes_only():
    tasks = [{"task_id": f"t{i}", "source_key": f"finra/rule/{i}", "status": "retrying", "attempt": 1, "error": "[SQL: secret]",
              "summary": {"status": "failed", "failure": {"error_type": "ProgrammingError", "error_code": "InFailedSqlTransaction",
                                                          "sqlstate": "25P02", "stage": "persistence", "message": "text with 'a quoted passage from a publisher'",
                                                          "first_cause": {"error_code": "UndefinedTable"}, "follow_on": [], "aborted_transaction": True}}}
             for i in range(40)]
    tasks.append({"task_id": "done", "source_key": "finra/rule/x", "status": "completed", "summary": {"status": "unchanged"}})
    rows = failures.task_failure_summary(tasks, worker="l1")
    assert len(rows) == failures.MAX_SUMMARY and all(r["status"] == "retrying" for r in rows)
    assert rows[0] == {"source": "finra/rule/0", "worker": "l1", "task": "t0", "status": "retrying", "attempt": 1, "stage": "persistence",
                       "duration_ms": None, "error_type": "ProgrammingError", "error_code": "InFailedSqlTransaction", "sqlstate": "25P02",
                       "first_cause": "UndefinedTable", "follow_on": [], "aborted_transaction": True}
    assert "message" not in rows[0] and "quoted passage" not in str(rows)
