"""Отложенный корпус (holdout v3): только проверка, по нему ничего не подбирается.

Правило работы — README эталона («Правило работы с holdout»). Launch id 8101–8112.
Тесты: Kotest, ScalaTest, TestNG + Feign, Karate, Newman, Cypress, TestCafe, Nightwatch,
Espresso, MSTest, Minitest, ExUnit, Robot Framework, Behave, Python unittest, Go testing,
cargo test, Vitest, Mocha. Логи: Apache httpd, MongoDB JSON, Redis, Cassandra, ClickHouse,
Airflow, PHP, IBM MQ, Quarkus, WildFly, WebLogic, log4net, Serilog CLEF, Sidekiq, env_logger,
uvicorn, Akka, kubectl events, HAProxy, RabbitMQ, консоль Chrome, Python logging с номером
строки; общие логи сервисов с rqUID/operUID, correlationId и traceparent.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from typing import Any

from eval.corpus import Case, LaunchBuilder, java_trace

_DAY = 1_791_936_000  # 2026-10-14 00:00:00 UTC
_ESC = "\x1b"


def _hex(seed: str, size: int = 32) -> str:
    return hashlib.sha256(seed.encode()).hexdigest()[:size]


def _hms(sec: int) -> str:
    hours, rest = divmod(sec, 3600)
    minutes, seconds = divmod(rest, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


def _jl(record: dict[str, Any]) -> str:
    return json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n"


def _at(hours: int, minutes: int, seconds: int = 0) -> int:
    return hours * 3600 + minutes * 60 + seconds


# --------------------------------------------------------------------------------------------
# log_formats_v3: причина видна только в логе, по группе на формат


def _httpd_log(sec: int, port: int) -> str:
    stamp = f"Wed Oct 14 {_hms(sec)}"
    return (
        "[Wed Oct 14 09:12:04.118204 2026] [mpm_event:notice] [pid 1:tid 140245] AH00489: "
        "Apache/2.4.62 (Unix) configured -- resuming normal operations\n"
        "[Wed Oct 14 09:12:04.118377 2026] [core:notice] [pid 1:tid 140245] AH00094: "
        "Command line: 'httpd -D FOREGROUND'\n"
        f"[{stamp}.402551 2026] [proxy:error] [pid 9:tid 140101] (111)Connection refused: "
        "AH00957: http: attempt to connect to 10.44.3.21:8090 (rates-backend.uat.internal:8090) "
        "failed\n"
        f"[{stamp}.402613 2026] [proxy_http:error] [pid 9:tid 140101] [client 10.44.1.7:{port}] "
        "AH01114: HTTP: failed to make connection to backend: rates-backend.uat.internal\n"
    )


def _mongo_log(sec: int, conn: int, account: str) -> str:
    def stamp(ms: int) -> dict[str, str]:
        return {"$date": f"2026-10-14T{_hms(sec)}.{ms:03d}+00:00"}

    return "".join([
        _jl({"t": stamp(118), "s": "I", "c": "NETWORK", "id": 22943, "ctx": "listener",
             "msg": "Connection accepted",
             "attr": {"remote": "10.44.6.12:40112", "connectionId": conn,
                      "connectionCount": 37}}),
        _jl({"t": stamp(121), "s": "I", "c": "NETWORK", "id": 51800, "ctx": f"conn{conn}",
             "msg": "client metadata",
             "attr": {"remote": "10.44.6.12:40112", "client": f"conn{conn}",
                      "doc": {"driver": {"name": "mongo-scala-driver", "version": "5.1.4"}}}}),
        _jl({"t": stamp(388), "s": "E", "c": "QUERY", "id": 4625501, "ctx": f"conn{conn}",
             "msg": "Plan executor error during find command",
             "attr": {"error": {"code": 292,
                                "codeName": "QueryExceededMemoryLimitNoDiskUseAllowed",
                                "errmsg": "Executor error during find command :: caused by :: "
                                          "Sort exceeded memory limit of 104857600 bytes, but "
                                          "did not opt in to external sorting."},
                      "stats": {"stage": "SORT", "nReturned": 0,
                                "totalDataSizeSorted": 104858012},
                      "cmd": {"find": "operations", "filter": {"accountId": account},
                              "sort": {"bookedAt": -1}, "$db": "statements"}}}),
        _jl({"t": stamp(392), "s": "I", "c": "NETWORK", "id": 22944, "ctx": f"conn{conn}",
             "msg": "Connection ended",
             "attr": {"remote": "10.44.6.12:40112", "connectionId": conn,
                      "connectionCount": 36}}),
    ])


def _redis_log(sec: int) -> str:
    return (
        "1:C 14 Oct 2026 09:00:00.512 # oO0OoO0OoO0Oo Redis is starting oO0OoO0OoO0Oo\n"
        "1:C 14 Oct 2026 09:00:00.512 # Redis version=7.2.5, bits=64, commit=00000000, "
        "modified=0, pid=1, just started\n"
        "1:M 14 Oct 2026 09:00:00.514 * Ready to accept connections tcp\n"
        f"1:M 14 Oct 2026 {_hms(sec)}.003 * 10000 changes in 60 seconds. Saving...\n"
        f"1:M 14 Oct 2026 {_hms(sec)}.004 # Can't save in background: fork: Cannot allocate "
        "memory\n"
        f"1:M 14 Oct 2026 {_hms(sec + 6)}.010 * 10000 changes in 60 seconds. Saving...\n"
        f"1:M 14 Oct 2026 {_hms(sec + 6)}.011 # Can't save in background: fork: Cannot "
        "allocate memory\n"
    )


def _cassandra_log(sec: int, account: str, token: str) -> str:
    query = f"SELECT * FROM ledger.entries WHERE account_id = {account} LIMIT 5000"
    return (
        "INFO  [main] 2026-10-14 08:55:12,004 StorageService.java:2943 - Node /10.44.7.3:7000 "
        "state jump to NORMAL\n"
        "INFO  [main] 2026-10-14 08:55:12,310 CassandraDaemon.java:782 - Startup complete\n"
        f"WARN  [ReadStage-2] 2026-10-14 {_hms(sec)},882 ReadCommand.java:601 - Read 1200 live "
        f"rows and 98422 tombstone cells for query {query}; token {token} "
        "(see tombstone_warn_threshold)\n"
        f"ERROR [ReadStage-3] 2026-10-14 {_hms(sec + 1)},104 StorageProxy.java:2104 - Scanned "
        f"over 100001 tombstones during query '{query}' (last scanned row token was {token} "
        f"and partion key was (({account}))); query aborted\n"
    )


def _clickhouse_log(sec: int, query_id: str, day: str) -> str:
    return (
        "2026.10.14 08:30:00.115201 [ 612 ] {} <Information> Application: Ready for "
        "connections.\n"
        f"2026.10.14 {_hms(sec)}.774012 [ 734 ] {{{query_id}}} <Error> executeQuery: Code: 60. "
        "DB::Exception: Table analytics.sessions_daily does not exist. (UNKNOWN_TABLE) "
        "(version 24.8.4.13 (official build)) (from 10.44.9.17:51920) (in query: SELECT day, "
        f"count() FROM analytics.sessions_daily WHERE day = '{day}' GROUP BY day), Stack trace "
        "(when copying this message, always include the lines below):\n"
        "\n"
        "0. DB::Exception::Exception(DB::Exception::MessageMasked&&, int, bool) @ "
        "0x000000000d2f1a3b\n"
        "1. DB::IDatabase::getTable(String const&, std::shared_ptr<DB::Context const>) const @ "
        "0x0000000011a7c2e4\n"
    )


def _airflow_log(dag: str, task: str) -> str:
    run = "scheduled__2026-10-13T07:00:00+00:00"
    site = "/home/airflow/.local/lib/python3.11/site-packages/airflow"
    return (
        "[2026-10-14, 07:00:01 UTC] {local_task_job_runner.py:123} INFO - ::group::Pre task "
        "execution logs\n"
        "[2026-10-14, 07:00:01 UTC] {taskinstance.py:2612} INFO - Dependencies all met for "
        f"dep_context=requeueable deps ti=<TaskInstance: {dag}.{task} {run} [queued]>\n"
        "[2026-10-14, 07:00:02 UTC] {taskinstance.py:2865} INFO - Starting attempt 1 of 1\n"
        "[2026-10-14, 07:00:03 UTC] {taskinstance.py:3311} ERROR - Task failed with exception\n"
        "Traceback (most recent call last):\n"
        f'  File "{site}/models/taskinstance.py", line 767, in _execute_task\n'
        "    result = _execute_callable(context=context, **execute_callable_kwargs)\n"
        f'  File "{site}/hooks/base.py", line 83, in get_connection\n'
        "    conn = Connection.get_connection_from_secrets(conn_id)\n"
        f'  File "{site}/models/connection.py", line 519, in get_connection_from_secrets\n'
        "    raise AirflowNotFoundException(f\"The conn_id `{conn_id}` isn't defined\")\n"
        "airflow.exceptions.AirflowNotFoundException: The conn_id `dwh_postgres` isn't "
        "defined\n"
        "[2026-10-14, 07:00:03 UTC] {taskinstance.py:1225} INFO - Marking task as FAILED. "
        f"dag_id={dag}, task_id={task}, run_id={run}\n"
    )


def _php_log(minute: int) -> str:
    return (
        f"[14-Oct-2026 10:{minute:02d}:12 UTC] PHP Warning:  Undefined array key \"vat_rate\" "
        "in /var/www/app/src/Report/TurnoverRow.php on line 41\n"
        f"[14-Oct-2026 10:{minute:02d}:13 UTC] PHP Fatal error:  Allowed memory size of "
        "134217728 bytes exhausted (tried to allocate 20480 bytes) in "
        "/var/www/app/vendor/phpoffice/phpspreadsheet/src/PhpSpreadsheet/Cell/Cell.php on "
        "line 214\n"
    )


def _mq_log(sec: int, pid: str) -> str:
    hours, rest = divmod(sec, 3600)
    clock = f"{hours % 12 or 12:02d}:{rest // 60:02d}:{rest % 60:02d}"
    return (
        f"10/14/2026 {clock} AM - Process({pid}) User(mqm) Program(amqrmppa)\n"
        "                    Host(mq-clr-01) Installation(Installation1)\n"
        "                    VRMF(9.3.0.15) QMgr(QM.CLR.UAT)\n"
        f"                    Time(2026-10-14T{_hms(sec)}.611Z)\n"
        "                    RemoteHost(10.52.7.14)\n"
        "                    CommentInsert1(PAYHUB.SVRCONN)\n"
        "                    CommentInsert2(svc_payhub)\n"
        "                    CommentInsert3(10.52.7.14)\n"
        "\n"
        "AMQ9777E: Channel was blocked\n"
        "\n"
        "EXPLANATION:\n"
        "The inbound channel 'PAYHUB.SVRCONN' was blocked from address '10.52.7.14' because\n"
        "the active values of the channel matched a record configured with USERSRC(NOACCESS).\n"
        "The active values of the channel were 'CLNTUSER(svc_payhub) ADDRESS(10.52.7.14)'.\n"
        "ACTION:\n"
        "Contact the systems administrator, who should examine the channel authentication\n"
        "records to ensure that the correct settings have been configured.\n"
        "----- cmcxmsgs.c : 1417 -------------------------------------------------------\n"
    )


def log_formats_v3() -> Case:
    b = LaunchBuilder(8101, "ift nightly regress #8101")

    for index, client in enumerate(["corporate", "retail"]):
        b.add_failure(
            "httpd-backend-refused", cause="rates-backend-refused", category="окружение",
            name=f"Rates for {client} clients",
            full_name=f"FX Rates API / {client} / Status code is 200",
            message="expected response to have status code 200 but got 503",
            step=f"GET /api/v1/rates/{client}",
            log=_httpd_log(_at(10, 3, 17 + index * 9), 52144 + index * 46),
            log_name="httpd-error.log",
            evidence=["AH01114: HTTP: failed to make connection to backend: "
                      "rates-backend.uat.internal"])

    for index, (account, count) in enumerate([("40817810500000004411", 214),
                                              ("40817810500000004629", 87)]):
        b.add_failure(
            "mongo-sort-limit", cause="statement-sort-memory", category="приложение",
            name=f"Statement for 90 days lists all operations of {account[-4:]}",
            full_name="ru.finplat.statements.e2e.StatementSpec",
            message=f"0 did not equal {count}",
            trace=java_trace(
                f"org.scalatest.exceptions.TestFailedException: 0 did not equal {count}",
                ["org.scalatest.matchers.MatchersHelper$.indicateFailure(MatchersHelper.scala:392)",
                 "org.scalatest.matchers.should.Matchers$AnyShouldWrapper.shouldEqual"
                 "(Matchers.scala:6919)",
                 "ru.finplat.statements.e2e.StatementSpec.$anonfun$new$3"
                 "(StatementSpec.scala:44)"]),
            step="GET /api/v2/statements?period=P90D",
            log=_mongo_log(_at(10, 5, 41 + index * 12), 4418 + index * 3, account),
            log_name="mongod.log",
            evidence=["Sort exceeded memory limit of 104857600 bytes, but did not opt in to "
                      "external sorting."])

    for index, name in enumerate(["refresh extends session", "refresh rotates token"]):
        b.add_failure(
            "redis-bgsave-fork", cause="redis-bgsave-fork-oom", category="окружение",
            name=f"test {name} (Sessions.ApiTest)", full_name=f"Sessions.ApiTest.test {name}",
            message="Assertion with == failed\ncode:  assert conn.status == 200\n"
                    "left:  500\nright: 200",
            trace=f"test/sessions/api_test.exs:{57 + index * 14}: (test)\n",
            step="PUT /api/sessions/:id/refresh",
            log=_redis_log(_at(10, 6, 2 + index * 20)), log_name="redis.log",
            evidence=["Can't save in background: fork: Cannot allocate memory"])

    for index, (account, token) in enumerate([("40817810900000000731", "-8817294715"),
                                              ("40817810900000000958", "-1127730048")]):
        url = (f"http://ledger-query.uat.internal:8080/api/v2/accounts/{account}/entries"
               "?from=2026-01-01")
        b.add_failure(
            "cassandra-tombstones", cause="ledger-tombstones", category="приложение",
            name=f"Ledger entries for account {account[-4:]}",
            full_name="classpath:ledger/entries.feature",
            message=f"status code was: 500, expected: 200, response time in milliseconds: "
                    f"{1270 + index * 33}, url: {url}, response: \n"
                    f'{{"status":500,"error":"Internal Server Error",'
                    f'"path":"/api/v2/accounts/{account}/entries"}}',
            trace="classpath:ledger/entries.feature:18\n",
            step="status 200",
            log=_cassandra_log(_at(10, 7, 15 + index * 21), account, token),
            log_name="cassandra-system.log",
            evidence=["Scanned over 100001 tombstones during query"])

    for index, day in enumerate(["2026-10-13", "2026-10-12"]):
        text = f"0 != 14 : rows in daily sessions report for {day}"
        b.add_failure(
            "clickhouse-unknown-table", cause="clickhouse-table-missing", category="приложение",
            name=f"test_daily_sessions_report_{day.replace('-', '_')}",
            full_name="tests.test_reports.DailyReportsTest",
            message=f"AssertionError: {text}",
            trace="Traceback (most recent call last):\n"
                  '  File "/builds/analytics/e2e/tests/test_reports.py", line 48, in '
                  "test_daily_sessions_report\n"
                  f'    self.assertEqual(len(rows), 14, "rows in daily sessions report for '
                  f'{day}")\n'
                  f"AssertionError: {text}\n",
            log=_clickhouse_log(_at(10, 8, 41 + index * 17), _hex(f"ch{index}", 36), day),
            log_name="clickhouse-server.err.log",
            evidence=["Table analytics.sessions_daily does not exist. (UNKNOWN_TABLE)"])

    for dag, task in [("nightly_settlement", "load_ledger"), ("nightly_fees", "load_fees")]:
        b.add_failure(
            "airflow-conn-missing", cause="airflow-conn-missing", category="окружение",
            name=f"DAG {dag} completes", full_name=f"features/dwh.feature: DAG {dag} completes",
            message=f"Assertion Failed: DAG run {dag}__2026-10-13 finished in state 'failed', "
                    "expected 'success'",
            step=f"Then the DAG run {dag} succeeds",
            log=_airflow_log(dag, task), log_name=f"{dag}.{task}.log",
            evidence=["The conn_id `dwh_postgres` isn't defined"])

    for index, report in enumerate(["turnover", "turnover-by-branch"]):
        b.add_failure(
            "php-memory-exhausted", cause="xlsx-export-memory", category="приложение",
            name=f"Download {report} report", full_name=f"ReportsCest:download{index}",
            message="Expected HTTP Status Code: 200 (OK). Actual Status Code: 500 "
                    "(Internal Server Error)",
            step=f"I send GET \"/api/v1/reports/{report}.xlsx\"",
            log=_php_log(9 + index), log_name="php-fpm-error.log",
            evidence=["Allowed memory size of 134217728 bytes exhausted"])

    for index, order in enumerate(["PO-2026-55102", "PO-2026-55117"]):
        b.add_failure(
            "mq-channel-blocked", cause="mq-chlauth-block", category="окружение",
            name=f"PaymentIsSettledAfterWindow ({order})",
            full_name="Lumen.Clearing.Tests.ClearingFlowTests.PaymentIsSettledAfterWindow",
            message="Assert.AreEqual failed. Expected:<Settled>. Actual:<Pending>. "
                    f"Payment {order} state after clearing window",
            trace="   at Lumen.Clearing.Tests.ClearingFlowTests.PaymentIsSettledAfterWindow() "
                  f"in /src/tests/ClearingFlowTests.cs:line {88 + index}\n",
            log=_mq_log(_at(10, 10, 5 + index * 40), f"3311.{27 + index}"),
            log_name="AMQERR01.LOG",
            evidence=["AMQ9777E: Channel was blocked"])

    return b.build("log_formats_v3")


# --------------------------------------------------------------------------------------------
# log_traps_v3: настоящая ошибка рядом с похожими на ошибку строками


def _quarkus_limits_log(sec: int, client: int, thread: int, error_id: str) -> str:
    def at(offset: int, ms: int) -> str:
        return f"2026-10-14 {_hms(sec + offset)},{ms:03d}"

    exec_thread = f"executor-thread-{thread}"
    return (
        f"{at(-3, 14)} INFO  [io.quarkus] (main) limits-svc 3.4.1 on JVM (powered by Quarkus "
        "3.15.1) started in 2.114s. Listening on: http://0.0.0.0:8080\n"
        f"{at(-3, 15)} INFO  [io.quarkus] (main) Installed features: [agroal, cdi, "
        "hibernate-orm, jdbc-postgresql, rest, smallrye-health]\n"
        f"{at(0, 101)} INFO  [io.qua.htt.access-log] (executor-thread-1) 10.44.2.8 - - "
        '"GET /api/v1/error-catalog?lang=ru HTTP/1.1" 200 1532\n'
        f"{at(0, 233)} INFO  [com.fin.lim.LimitCheckResource] ({exec_thread}) check started "
        f"client={client} errorCount=0 lastError=none\n"
        f"{at(0, 240)} WARN  [com.fin.lim.RatesClient] ({exec_thread}) rates cache refresh "
        "failed, using previous snapshot: jakarta.ws.rs.ProcessingException: RESTEASY004655: "
        "Unable to invoke request\n"
        "\tat org.jboss.resteasy.client.jaxrs.engines.ApacheHttpClient43Engine.invoke"
        "(ApacheHttpClient43Engine.java:282)\n"
        "\tat com.finplat.limits.RatesClient.refresh(RatesClient.java:57)\n"
        f"{at(0, 251)} DEBUG [com.fin.lim.ClientRepository] ({exec_thread}) handled exception "
        "while reading optional segment: jakarta.persistence.NoResultException: No entity "
        "found for query\n"
        f"{at(0, 262)} ERROR [io.qua.ver.htt.run.QuarkusErrorHandler] ({exec_thread}) HTTP "
        f"Request to /api/v1/limits/check failed, error id: {error_id}-1: "
        "org.hibernate.exception.SQLGrammarException: JDBC exception executing SQL [select "
        "lr1_0.id,lr1_0.segment,lr1_0.max_amount from limit_rules_v2 lr1_0 where "
        'lr1_0.segment=?] [ERROR: relation "limit_rules_v2" does not exist\n'
        "  Position: 71] [n/a]\n"
        "\tat org.hibernate.exception.internal.SQLStateConversionDelegate.convert"
        "(SQLStateConversionDelegate.java:91)\n"
        "\tat com.finplat.limits.LimitRuleRepository.findBySegment"
        "(LimitRuleRepository.java:38)\n"
        f"{at(1, 5)} INFO  [io.qua.htt.access-log] (executor-thread-1) 10.44.2.8 - - "
        '"GET /q/health/ready HTTP/1.1" 200 46\n'
    )


def _uvicorn_log(member: int, port: int) -> str:
    def lvl(color: str, name: str, pad: str) -> str:
        return f"{_ESC}[{color}m{name}{_ESC}[0m:{pad}"

    info = lvl("32", "INFO", "     ")
    return (
        f"{info}Started server process [1]\n"
        f"{info}Waiting for application startup.\n"
        f"{info}Application startup complete.\n"
        f"{info}Uvicorn running on http://0.0.0.0:8000 (Press CTRL+C to quit)\n"
        f'{info}10.44.3.15:{port - 1} - "GET /v1/errors/recent?limit=20 HTTP/1.1" '
        f"{_ESC}[32m200 OK{_ESC}[0m\n"
        f"{lvl('33', 'WARNING', '  ')}Invalid HTTP request received.\n"
        f'{info}10.44.3.15:{port} - "GET /v1/members/{member}/tier HTTP/1.1" '
        f"{_ESC}[91m500 Internal Server Error{_ESC}[0m\n"
        f"{lvl('31', 'ERROR', '    ')}Exception in ASGI application\n"
        "Traceback (most recent call last):\n"
        '  File "/app/.venv/lib/python3.12/site-packages/uvicorn/protocols/http/'
        'httptools_impl.py", line 409, in run_asgi\n'
        "    result = await app(  # type: ignore[func-returns-value]\n"
        '  File "/app/loyalty/api/members.py", line 88, in get_tier\n'
        "    async with db.acquire() as conn:\n"
        '  File "/app/.venv/lib/python3.12/site-packages/asyncpg/pool.py", line 1024, in '
        "__aenter__\n"
        "    self.connection = await self.pool._acquire(self.timeout)\n"
        '  File "/app/.venv/lib/python3.12/site-packages/asyncpg/connect_utils.py", line 1102, '
        "in _connect_addr\n"
        "    return await __connect_addr(params, True, *args)\n"
        "asyncpg.exceptions.TooManyConnectionsError: sorry, too many clients already\n"
        f'{info}10.44.3.15:{port + 1} - "GET /v1/members/{member}/errors-history HTTP/1.1" '
        f"{_ESC}[32m200 OK{_ESC}[0m\n"
    )


def _log4net_fx_log(sec: int, deal: str, pair: str, age: int) -> str:
    def at(offset: int, ms: int) -> str:
        return f"2026-10-14 {_hms(sec + offset)},{ms:03d}"

    lines = [
        f"{at(-5, 101)} [1] INFO  Treasury.Host.Program [(null)] - Treasury.FxDesk 5.2.0 "
        "started, environment=UAT",
        f"{at(0, 330)} [14] INFO  Treasury.Fx.ErrorBudget [(null)] - error budget report: "
        "errors=0, warnings=2",
        f"{at(0, 512)} [14] WARN  Treasury.Fx.QuoteCache [(null)] - quote cache refresh failed, "
        "keeping previous snapshot",
        "System.Security.Authentication.AuthenticationException: Authentication failed because "
        "the remote party sent a TLS alert: 'HandshakeFailure'.",
        "   at System.Net.Security.SslStream.ForceAuthenticationAsync[TIOAdapter](Boolean "
        "receiveFirst, Byte[] reAuthenticationData, CancellationToken cancellationToken)",
        "   at Treasury.Fx.QuoteCache.RefreshAsync() in /src/Treasury.Fx/QuoteCache.cs:line 61",
        f"{at(0, 601)} [14] DEBUG Treasury.Fx.DealBooking [deal={deal}] - validating deal, "
        "errors: none",
        f"{at(0, 734)} [14] ERROR Treasury.Fx.DealBooking [deal={deal}] - Deal rejected: "
        f"quote {pair} is stale by {age} s (limit 60 s), source=MOEX-UAT",
        f"{at(1, 2)} [9] INFO  Treasury.Fx.Health [(null)] - /health/errors responded 200",
    ]
    return "\r\n".join(lines) + "\r\n"


def _sidekiq_noise(start: int, count: int, offset: int) -> str:
    classes = ("BalanceSnapshotJob", "CardLimitSyncJob", "RateAlertJob", "AuditFlushJob")
    lines = []
    for index in range(count):
        number = offset + index
        stamp = f"2026-10-14T{_hms(start + index // 4)}.{(number * 137) % 1000:03d}Z"
        head = (f"{stamp} pid=7 tid={(number % 9) + 2}k{number % 7} "
                f"class={classes[number % 4]} jid={_hex(f'jid{number}', 24)}")
        lines.append(f"{head} INFO: start\n")
        lines.append(f"{head} elapsed=0.{(number * 53) % 1000:03d} INFO: done\n")
    return "".join(lines)


def _sidekiq_statement_log(account: str, jid: str, part: int) -> str:
    error_at = f"2026-10-14T10:31:{12 + part:02d}"
    head = f"pid=7 tid=3c{part} class=StatementPdfJob jid={jid}"
    return (
        "2026-10-14T10:25:00.004Z pid=7 tid=2kq INFO: Booted Rails 7.2.1 application in "
        "production environment\n"
        "2026-10-14T10:25:00.006Z pid=7 tid=2kq INFO: Running in ruby 3.3.5 +YJIT\n"
        + _sidekiq_noise(_at(10, 25, 1), 800, part * 5000)
        + f"{error_at}.401Z {head} INFO: start\n"
        f"{error_at}.733Z {head} elapsed=0.332 INFO: fail\n"
        f'{error_at}.734Z pid=7 tid=3c{part} WARN: {{"context":"Job raised exception",'
        f'"job":{{"retry":3,"queue":"pdf","class":"StatementPdfJob","args":["{account}"],'
        f'"jid":"{jid}"}}}}\n'
        f"{error_at}.734Z pid=7 tid=3c{part} WARN: Prawn::Errors::IncompatibleStringEncoding: "
        "Your document includes text that's not compatible with the Windows-1252 character "
        "set.\n"
        f"{error_at}.734Z pid=7 tid=3c{part} WARN: /app/app/pdfs/statement_pdf.rb:57:in "
        "'StatementPdf#render_header'\n"
        + _sidekiq_noise(_at(10, 31, 14), 800, part * 5000 + 800)
    )


def _statement_page_attachments(account: str, request_id: str) -> list[tuple[str, str, str]]:
    request = json.dumps({"url": f"https://ib.uat.internal/api/statements?account={account}",
                          "method": "GET", "headers": {"X-Error-Policy": "strict"},
                          "errorPolicy": "strict"}, indent=2)
    console = (
        "https://ib.uat.internal/statements 0:0 Failed to load resource: the server responded "
        "with a status of 500 ()\n"
        "https://ib.uat.internal/assets/index-3c9a71.js 2:18044 [statements] retry 1/1 "
        "after error\n"
    )
    server = "".join([
        _jl({"@t": "2026-10-14T10:33:01.2210000Z", "@mt": "Request starting {Protocol} "
             "{Method} {Path}", "Protocol": "HTTP/1.1", "Method": "GET",
             "Path": "/api/statements", "RequestId": request_id}),
        _jl({"@t": "2026-10-14T10:33:01.2290000Z", "@mt": "Error budget check passed, "
             "{ErrorCount} errors in window", "ErrorCount": 0, "RequestId": request_id}),
        _jl({"@t": "2026-10-14T10:33:01.9020000Z", "@mt": "Statement query failed for account "
             "{AccountId}", "@l": "Error",
             "@x": "Oracle.ManagedDataAccess.Client.OracleException (0x80004005): ORA-01652: "
                   "unable to extend temp segment by 128 in tablespace TEMP\n"
                   "   at OracleInternal.ServiceObjects.OracleConnectionImpl.VerifyExecution("
                   "Int32& cursorId, Boolean bThrowArrayBindRelatedErrors)\n"
                   "   at Statements.Data.StatementRepository.QueryAsync(String accountId) in "
                   "/src/Statements.Data/StatementRepository.cs:line 74",
             "AccountId": account, "SourceContext": "Statements.Data.StatementRepository",
             "RequestId": request_id}),
    ])
    return [("request.json", request, "application/json"),
            ("browser-console.log", console, "text/plain"),
            ("statements-api.clef", server, "text/plain")]


def _env_logger_log(sec: int, batch: int, day: str) -> str:
    def at(offset: int) -> str:
        return f"2026-10-14T{_hms(sec + offset)}Z"

    host = "files.uat.internal"
    return (
        f"[{at(0)} INFO  ledger_sync] starting ledger-sync 0.14.2, "
        "error_dir=/var/lib/ledger-sync/errors\n"
        f"[{at(1)} DEBUG hyper_util::client::legacy::pool] reuse idle connection for "
        f'("https", {host})\n'
        f"[{at(1)} INFO  ledger_sync::http] GET https://{host}/api/errors?since={day} -> "
        "200 OK\n"
        f"[{at(2)} DEBUG ledger_sync::fetch] handled error, will retry: error sending request "
        f"for url (https://{host}/ledger/{day}.csv): connection closed before message "
        "completed\n"
        f"[{at(2)} WARN  ledger_sync::fetch] attempt 1/3 failed: error decoding response "
        "body: unexpected EOF during chunk size line; retrying in 500ms\n"
        f"[{at(3)} INFO  ledger_sync::fetch] downloaded ledger-{day}.csv (48211 rows)\n"
        f"[{at(3)} ERROR ledger_sync::worker] batch {batch} rejected: checksum mismatch for "
        f"ledger-{day}.csv (expected sha256 9f1c2e04, got 3b7a90d1)\n"
        f"[{at(4)} INFO  ledger_sync] errors=1 warnings=1, next run in 300s\n"
    )


def log_traps_v3() -> Case:
    b = LaunchBuilder(8102, "uat regress #8102")

    for index, client in enumerate([7781, 7790]):
        b.add_failure(
            "limits-relation-missing", cause="limits-migration-missing", category="приложение",
            name=f"limit check for client segment {index + 1}",
            full_name="tests/limits/check.spec.ts > limits > check",
            message="AssertionError: expected 500 to be 200 // Object.is equality",
            trace="AssertionError: expected 500 to be 200 // Object.is equality\n"
                  f"    at /builds/limits/api-tests/tests/limits/check.spec.ts:{31 + index}:30\n",
            step="POST /api/v1/limits/check",
            log=_quarkus_limits_log(_at(10, 20, 3 + index * 30), client, 2 + index,
                                    _hex(f"q{index}", 8) + "-77d0-4b8e-9b31-2f6a0c1d5e93"),
            log_name="limits-svc.log",
            evidence=['relation "limit_rules_v2" does not exist'])

    for index, member in enumerate([77120, 77131]):
        b.add_failure(
            "asyncpg-too-many-clients", cause="loyalty-db-connections", category="окружение",
            name=f"Member {member} Tier Is Returned", full_name="Loyalty.Members",
            message=f"Url: http://loyalty.uat.internal:8000/v1/members/{member}/tier "
                    "Expected status: 500 != 200",
            step="GET On Session",
            log=_uvicorn_log(member, 40212 + index * 7), log_name="loyalty-api.log",
            evidence=["asyncpg.exceptions.TooManyConnectionsError: sorry, too many clients "
                      "already"])

    for index, (deal, pair, age) in enumerate([("FX-88120", "EUR/RUB", 914),
                                               ("FX-88134", "USD/RUB", 921)]):
        b.add_failure(
            "fx-quote-stale", cause="fx-quote-stale", category="окружение",
            name=f"BookDealAtMarketRate ({pair})",
            full_name="Treasury.E2E.FxDeskTests.BookDealAtMarketRate",
            message="Assert.AreEqual failed. Expected:<Booked>. Actual:<Rejected>. FX deal "
                    "status",
            trace="   at Treasury.E2E.FxDeskTests.BookDealAtMarketRate(String pair) in "
                  "/src/Treasury.E2E/FxDeskTests.cs:line 47\n",
            step="Book FX deal",
            log=_log4net_fx_log(_at(10, 22, 5 + index * 25), deal, pair, age),
            log_name="fxdesk.log",
            evidence=[f"Deal rejected: quote {pair} is stale"])

    for index, account in enumerate(["40817810100000003301", "40817810100000003318"]):
        b.add_failure(
            "statement-pdf-encoding", cause="pdf-font-encoding", category="приложение",
            name=f"test_statement_pdf_is_ready_{index + 1}",
            full_name="StatementPdfTest#test_statement_pdf_is_ready",
            message=f"Statement PDF for account {account} should be ready.\n"
                    'Expected: "ready"\n  Actual: "failed"',
            trace=f"test/integration/statement_pdf_test.rb:{24 + index}:in "
                  "'StatementPdfTest#test_statement_pdf_is_ready'\n",
            step="Request account statement PDF",
            log=_sidekiq_statement_log(account, _hex(f"pdf{index}", 24), index),
            log_name="sidekiq.log",
            evidence=["Prawn::Errors::IncompatibleStringEncoding: Your document includes text "
                      "that's not compatible with the Windows-1252 character set."])

    for index, account in enumerate(["40817810300000002291", "40817810300000002305"]):
        b.add_failure(
            "oracle-temp-tablespace", cause="oracle-temp-tablespace", category="окружение",
            name=f"Statement rows are shown ({index + 1})",
            full_name="Statements / Statement rows are shown",
            message="The specified selector does not match any element in the DOM tree.\n\n"
                    " > | Selector('[data-testid=\"statement-row\"]')",
            step="Open statement page",
            logs=_statement_page_attachments(account, f"0HN7A1B2C3D4E:0000000{index + 2}"),
            evidence=["ORA-01652: unable to extend temp segment by 128 in tablespace TEMP"])

    for index, (batch, day) in enumerate([(4471, "2026-10-13"), (4472, "2026-10-12")]):
        message = (f"thread 'sync::imports_daily_ledger_{index + 1}' panicked at "
                   f"tests/sync.rs:{74 + index * 9}:9:\n"
                   "assertion `left == right` failed: ledger import status\n"
                   '  left: "Rejected"\n right: "Imported"')
        b.add_failure(
            "ledger-checksum", cause="ledger-checksum-mismatch", category="данные",
            name=f"sync::imports_daily_ledger_{index + 1}", full_name="ledger_sync::tests::sync",
            message=message,
            log=_env_logger_log(_at(10, 35 + index * 5), batch, day), log_name="ledger-sync.log",
            evidence=[f"checksum mismatch for ledger-{day}.csv"])

    return b.build("log_traps_v3")


# --------------------------------------------------------------------------------------------
# same_symptom_causes_v3: одинаковые сообщение и шаг, причины только в логах


def _wls(sec: int, severity: str, subsystem: str, thread: str, msg_id: str, text: str) -> str:
    hours, rest = divmod(sec, 3600)
    ms = (sec * 37) % 1000
    stamp = (f"Oct 14, 2026 {hours % 12 or 12}:{rest // 60:02d}:{rest % 60:02d},{ms:03d} "
             f"{'AM' if hours < 12 else 'PM'} UTC")
    value = {"Error": 8, "Warning": 16, "Notice": 32, "Info": 64}[severity]
    return (f"####<{stamp}> <{severity}> <{subsystem}> <wls-trn-01> <TransfersServer1> "
            f"<{thread}> <<WLS Kernel>> <> <{_hex(f'ecid{sec}', 8)}-5c1e-4b7a-9f02-"
            f"{_hex(f'ecid{sec}', 12)}-0000012a> <{(_DAY + sec) * 1000 + ms}> "
            f"<[severity-value: {value}] [rid: 0] [partition-id: 0] [partition-name: DOMAIN] > "
            f"<{msg_id}> <{text}>\n")


def _wls_thread(number: int) -> str:
    return (f"[ACTIVE] ExecuteThread: '{number}' for queue: 'weblogic.kernel.Default "
            "(self-tuning)'")


def _transfers_log(kind: str, sec: int, transfer: str, thread: int) -> str:
    lines = [
        _wls(sec - 600, "Notice", "WebLogicServer", "main", "BEA-000360",
             "The server started in RUNNING mode."),
        _wls(sec - 30, "Info", "Health", _wls_thread(3), "BEA-310002",
             "61% of the total memory in the server is free."),
        _wls(sec, "Info", "TransfersApp", _wls_thread(thread), "BEA-000000",
             f"TransferResource: create transfer {transfer} accepted"),
    ]
    if kind == "db":
        lines += [
            _wls(sec + 2, "Error", "JDBC", _wls_thread(thread), "BEA-001129",
                 'Received exception while creating connection for pool "TransfersDS": IO '
                 "Error: The Network Adapter could not establish the connection "
                 f"(CONNECTION_ID={_hex(f'cid{transfer}', 22)}==)"),
            _wls(sec + 2, "Warning", "JDBC", _wls_thread(thread), "BEA-001156",
                 "Stack trace associated with message 001129 follows: \n\n"
                 "java.sql.SQLRecoverableException: IO Error: The Network Adapter could not "
                 "establish the connection\n"
                 "\tat oracle.jdbc.driver.T4CConnection.logon(T4CConnection.java:891)\n"
                 "\tat weblogic.jdbc.common.internal.ConnectionEnvFactory.makeConnection"
                 "(ConnectionEnvFactory.java:356)"),
        ]
    elif kind == "lock":
        lines += [
            _wls(sec + 1, "Warning", "TransfersApp", _wls_thread(thread), "BEA-000000",
                 f"LimitService: waiting for TRANSFERS.PKG_LIMITS for transfer {transfer}"),
            _wls(sec + 31, "Error", "TransfersApp", _wls_thread(thread), "BEA-000000",
                 f"LimitService: transfer {transfer} failed: ORA-04021: timeout occurred while "
                 "waiting to lock object TRANSFERS.PKG_LIMITS"),
        ]
    else:
        lines += [
            _wls(sec + 30, "Error", "TransfersApp", _wls_thread(thread), "BEA-000000",
                 f"AmlScreeningClient: screening request for transfer {transfer} timed out after "
                 "30000 ms (https://aml-screen.uat.internal/api/v1/screen)"),
        ]
    lines.append(_wls(sec + 40, "Warning", "WebLogicServer", _wls_thread(1), "BEA-000337",
                      f"[STUCK] ExecuteThread: '{thread}' for queue: 'weblogic.kernel.Default "
                      "(self-tuning)' has been busy for \"612\" seconds working on the request "
                      "\"Http Request Information: weblogic.servlet.internal.ServletRequestImpl"
                      "[POST /v2/transfers]\", which is more than the configured time "
                      "(StuckThreadMaxTime) of \"600\" seconds."))
    return "".join(lines)


def _payorders_shared_log(ops: list[tuple[str, str]]) -> str:
    """Общий лог сервиса платёжных поручений: операции тестов и чужие операции."""
    lines = [
        "2026-10-14 10:44:00,010 [1] INFO  PayOrders.Api.Program [(null)] - PayOrders.Api "
        "7.1.3 started on http://+:8080",
        "2026-10-14 10:44:30,000 [7] INFO  PayOrders.Jobs.OutboxRelay [(null)] - relayed 0 "
        "messages, 0 errors",
    ]
    errors = {
        "data": 'Confirm failed: ORA-12899: value too large for column "PAY"."ORDERS"."PURPOSE" '
                "(actual: {n}, maximum: 210)",
        "key": "Confirm failed: signing service rejected request: key alias 'pay-sign-2025' "
               "expired on 2026-10-01",
        "other": "Confirm failed: payer account 40702810700000009911 is closed",
    }
    for index, (corr, kind) in enumerate(ops):
        stamp = f"2026-10-14 10:45:{index * 4 + 1:02d}"
        thread = 20 + index % 5
        lines.append(f"{stamp},{110 + index:03d} [{thread}] INFO  PayOrders.Api.ConfirmHandler "
                     f"[corr={corr}] - confirm payment order requested")
        if kind == "ok":
            lines.append(f"{stamp},{480 + index:03d} [{thread}] INFO  "
                         f"PayOrders.Api.ConfirmHandler [corr={corr}] - order confirmed")
        else:
            text = errors[kind].replace("{n}", str(212 + index))
            lines.append(f"{stamp},{480 + index:03d} [{thread}] ERROR "
                         f"PayOrders.Api.ConfirmHandler [corr={corr}] - {text}")
    lines.append("2026-10-14 10:46:00,002 [7] ERROR PayOrders.Jobs.OutboxRelay [(null)] - "
                 "broker rabbit-uat-2 unreachable, will retry in 30 s")
    return "\n".join(lines) + "\n"


def _k8s_events_oom(pod: str, pid: int) -> str:
    return (
        "LAST SEEN   TYPE      REASON       OBJECT                                MESSAGE\n"
        f"6m12s       Normal    Pulled       pod/{pod}   Container image "
        '"registry.uat.internal/statements-api:2.31.0" already present on machine\n'
        f"6m11s       Normal    Started      pod/{pod}   Started container statements-api\n"
        "2m03s       Warning   OOMKilling   node/uat-worker-07                    Memory cgroup "
        f"out of memory: Killed process {pid} (python3) total-vm:2843120kB, "
        "anon-rss:1048200kB, file-rss:10240kB, shmem-rss:0kB, UID:1000 pgtables:2412kB "
        "oom_score_adj:986\n"
        f"2m01s       Warning   BackOff      pod/{pod}   Back-off restarting failed container "
        f"statements-api in pod {pod}_statements(5f1e0c2a-9d3b-4c71-8e55-0a3b9c7d2e10)\n"
    )


def _filesgw_panic_log(port: int, goroutine: int) -> str:
    return (
        "2026/10/14 10:49:58 files-gw 1.9.0 listening on :8080\n"
        f"2026/10/14 10:50:03 http: panic serving 10.44.5.20:{port}: runtime error: slice "
        "bounds out of range [:-1]\n"
        f"goroutine {goroutine} [running]:\n"
        "net/http.(*conn).serve.func1()\n"
        "\t/usr/local/go/src/net/http/server.go:1947 +0xbe\n"
        "panic({0x9c1a20?, 0xc0003a2f30?})\n"
        "\t/usr/local/go/src/runtime/panic.go:785 +0x132\n"
        "filesgw/internal/multipart.(*Reader).boundary(...)\n"
        "\t/src/internal/multipart/reader.go:118 +0x2d4\n"
        "filesgw/internal/upload.(*Handler).ServeHTTP(0xc0001a6000, {0xb2e7c0, 0xc0004c2000}, "
        "0xc0003f8000)\n"
        "\t/src/internal/upload/handler.go:64 +0x1a5\n"
    )


def _aiohttp_trace(test: str, line: int) -> str:
    site = "/builds/statements/e2e/.venv/lib/python3.12/site-packages/aiohttp"
    return (
        "Traceback (most recent call last):\n"
        f'  File "/builds/statements/e2e/tests/test_upload.py", line {line}, in {test}\n'
        "    resp = await self.client.upload(path)\n"
        '  File "/builds/statements/e2e/support/client.py", line 44, in upload\n'
        "    async with self._session.post(url, data=form) as resp:\n"
        f'  File "{site}/client.py", line 1197, in __aenter__\n'
        "    self._resp = await self._coro\n"
        f'  File "{site}/client_reqrep.py", line 1059, in start\n'
        "    message, payload = await protocol.read()  # type: ignore[union-attr]\n"
        f'  File "{site}/streams.py", line 643, in read\n'
        "    await self._waiter\n"
        "aiohttp.client_exceptions.ServerDisconnectedError: Server disconnected\n"
    )


def same_symptom_causes_v3() -> Case:
    b = LaunchBuilder(8103, "ift regress transfers #8103")

    timeout = ('transfer_test.go:88: create transfer: Post "http://transfers.uat.internal:8080/'
               'v2/transfers": context deadline exceeded (Client.Timeout exceeded while awaiting '
               "headers)")
    transfers = [
        ("transfers-db", "transfers-db-unreachable", "db",
         ['Received exception while creating connection for pool "TransfersDS": IO Error: The '
          "Network Adapter could not establish the connection"]),
        ("transfers-db", "transfers-db-unreachable", "db", []),
        ("limits-package-lock", "limits-package-lock", "lock",
         ["ORA-04021: timeout occurred while waiting to lock object TRANSFERS.PKG_LIMITS"]),
        ("aml-timeout", "aml-screening-slow", "aml",
         ["screening request for transfer", "timed out after 30000 ms "
          "(https://aml-screen.uat.internal/api/v1/screen)"]),
        ("transfers-db", "transfers-db-unreachable", "db", []),
        ("limits-package-lock", "limits-package-lock", "lock", []),
        ("aml-timeout", "aml-screening-slow", "aml", []),
        ("limits-package-lock", "limits-package-lock", "lock", []),
    ]
    for index, (group, cause, kind, evidence) in enumerate(transfers):
        transfer = f"TR-2026-{30411 + index * 7}"
        b.add_failure(
            group, cause=cause, category="окружение",
            name=f"TestCreateTransfer/{['sbp', 'card', 'swift', 'internal'][index % 4]}_{index}",
            full_name="transfers/e2e.TestCreateTransfer",
            message=timeout, step="Create transfer",
            log=_transfers_log(kind, _at(10, 40, index * 50), transfer, 7 + index),
            log_name="TransfersServer1.log", evidence=evidence)

    corr = [_hex(f"corr{index}", 8) + "-0b6a-4c55-9a1e-" + _hex(f"corr{index}", 12)
            for index in range(7)]
    shared = _payorders_shared_log([(corr[0], "data"), (corr[1], "ok"), (corr[2], "key"),
                                    (corr[3], "other"), (corr[4], "data"), (corr[5], "key"),
                                    (corr[6], "ok")])
    for index, (op, group, cause, category, evidence) in enumerate([
        (0, "purpose-too-long", "purpose-too-long", "данные",
         'ORA-12899: value too large for column "PAY"."ORDERS"."PURPOSE"'),
        (2, "sign-key-expired", "sign-key-expired", "окружение",
         "key alias 'pay-sign-2025' expired on 2026-10-01"),
        (4, "purpose-too-long", "purpose-too-long", "данные",
         'ORA-12899: value too large for column "PAY"."ORDERS"."PURPOSE"'),
        (5, "sign-key-expired", "sign-key-expired", "окружение",
         "key alias 'pay-sign-2025' expired on 2026-10-01"),
    ]):
        response = json.dumps({
            "type": "https://tools.ietf.org/html/rfc9110#section-15.6.1",
            "title": "An error occurred while processing your request.", "status": 500,
            "correlationId": corr[op]}, indent=2)
        b.add_failure(
            group, cause=cause, category=category,
            name=f"ConfirmPaymentOrder_{['Budget', 'Tax', 'Customs', 'Salary'][index]}",
            full_name="PayOrders.E2E.ConfirmTests.ConfirmPaymentOrder",
            message="Assert.AreEqual failed. Expected:<201>. Actual:<500>. "
                    "POST /api/payment-orders",
            trace="   at PayOrders.E2E.ConfirmTests.ConfirmPaymentOrder(String kind) in "
                  "/src/PayOrders.E2E/ConfirmTests.cs:line 52\n",
            step="Confirm payment order",
            log=shared, log_name="payorders-api.log",
            logs=[("response.json", response, "application/json")],
            evidence=[evidence])

    for index, (group, cause, test) in enumerate([
        ("statements-oom", "statements-api-oom", "test_upload_mt940"),
        ("filesgw-panic", "files-gw-multipart-panic", "test_upload_camt053"),
        ("statements-oom", "statements-api-oom", "test_upload_1c_format"),
        ("filesgw-panic", "files-gw-multipart-panic", "test_upload_csv"),
    ]):
        if group == "statements-oom":
            log = _k8s_events_oom("statements-api-5c8d7f9b6-k2xwq", 31877 + index)
            log_name, evidence = "k8s-events.txt", "Memory cgroup out of memory: Killed process"
        else:
            log = _filesgw_panic_log(39122 + index, 2211 + index * 40)
            log_name, evidence = "files-gw.log", "runtime error: slice bounds out of range [:-1]"
        b.add_failure(
            group, cause=cause, category="приложение",
            name=test, full_name=f"tests.test_upload.UploadTest.{test}",
            message="aiohttp.client_exceptions.ServerDisconnectedError: Server disconnected",
            trace=_aiohttp_trace(test, 61 + index * 12), step="Upload statement file",
            log=log, log_name=log_name, evidence=[evidence])

    return b.build("same_symptom_causes_v3")


# --------------------------------------------------------------------------------------------
# hosts_locators_v3: один шаг и одна обёртка, различаются хост или локатор


def _feign_trace(host: str, path: str, test_class: str, method: str, line: int) -> str:
    return java_trace(
        f"feign.RetryableException: Connection refused executing POST http://{host}:8080{path}",
        ["feign.FeignException.errorExecuting(FeignException.java:300)",
         "feign.SynchronousMethodHandler.executeAndDecode(SynchronousMethodHandler.java:105)",
         "feign.SynchronousMethodHandler.invoke(SynchronousMethodHandler.java:53)",
         "feign.ReflectiveFeign$FeignInvocationHandler.invoke(ReflectiveFeign.java:104)",
         "jdk.proxy2/jdk.proxy2.$Proxy41.post(Unknown Source)",
         "ru.finplat.e2e.support.PartnerGateway.call(PartnerGateway.java:42)",
         f"ru.finplat.e2e.{test_class}.{method}({test_class.split('.')[-1]}.java:{line})",
         "java.base/jdk.internal.reflect.DirectMethodHandleAccessor.invoke"
         "(DirectMethodHandleAccessor.java:103)",
         "org.testng.internal.invokers.MethodInvocationHelper.invokeMethod"
         "(MethodInvocationHelper.java:139)"])


def _netty_trace(host: str, address: str, spec: str, line: int) -> str:
    return java_trace(
        "io.netty.channel.AbstractChannel$AnnotatedConnectException: Connection refused: "
        f"{host}/{address}",
        ["io.netty.channel.unix.Errors.newConnectException0(Errors.java:166)",
         "io.netty.channel.unix.Errors.handleConnectErrno(Errors.java:131)",
         "io.netty.channel.epoll.AbstractEpollChannel$AbstractEpollUnsafe.finishConnect"
         "(AbstractEpollChannel.java:691)",
         "ru.finplat.e2e.support.ServiceCall.invoke(ServiceCall.kt:31)",
         f"ru.finplat.e2e.{spec}$1$1.invokeSuspend({spec.split('.')[-1]}.kt:{line})"])


def _fetch_trace(url: str, address: str, test_file: str, line: int) -> str:
    return (
        f"FetchError: request to {url} failed, reason: connect ECONNREFUSED {address}\n"
        "    at ClientRequest.<anonymous> (node_modules/node-fetch/lib/index.js:1505:11)\n"
        "    at ClientRequest.emit (node:events:519:28)\n"
        "    at apiCall (support/apiCall.js:18:9)\n"
        f"    at Context.<anonymous> (test/{test_file}:{line}:24)\n"
    )


def _cypress_get(locator: str, spec: str, line: int) -> tuple[str, str]:
    message = (f"AssertionError: Timed out retrying after 10000ms: Expected to find element: "
               f"`{locator}`, but never found it.")
    trace = (f"{message}\n"
             "    at Context.eval (webpack://ib-e2e/./cypress/support/commands.ts:14:13)\n"
             f"    at Context.eval (webpack://ib-e2e/./cypress/e2e/{spec}:{line}:8)\n")
    return message, trace


def _espresso(view_id: int, name: str, test: str, line: int) -> tuple[str, str]:
    message = ("androidx.test.espresso.NoMatchingViewException: No views in hierarchy found "
               f"matching: view.getId() is <{view_id}/com.lumen.mobile:id/{name}>")
    trace = (f"{message}\n\nView Hierarchy:\n"
             "+>DecorView{id=-1, visibility=VISIBLE, width=1080, height=2400, has-focus=false, "
             "has-focusable=true, has-window-focus=true, is-clickable=false, is-enabled=true, "
             "is-focused=false, is-focusable=false, is-layout-requested=false, "
             "is-selected=false, layout-params=LayoutParams{(0,0)(fillxfill) ty=BASE_APPLICATION"
             "}, tag=null, root-is-layout-requested=false, has-input-connection=false, x=0.0, "
             "y=0.0, child-count=3}\n"
             "\tat dalvik.system.VMStack.getThreadStackTrace(Native Method)\n"
             "\tat androidx.test.espresso.base.DefaultFailureHandler.getUserFriendlyError"
             "(DefaultFailureHandler.java:92)\n"
             "\tat androidx.test.espresso.ViewInteraction.perform(ViewInteraction.java:262)\n"
             "\tat com.lumen.mobile.e2e.support.Screen.tap(Screen.kt:22)\n"
             f"\tat com.lumen.mobile.e2e.{test}(TransferTest.kt:{line})\n")
    return message, trace


def hosts_locators_v3() -> Case:
    b = LaunchBuilder(8104, "ift e2e #8104")

    for host, cause, path, cases in [
        ("kyc-adapter.ift.local", "kyc-adapter-down", "/v1/checks",
         [("onboarding.OnboardingIT", "verifyClient", 77),
          ("onboarding.OnboardingIT", "verifyPassportData", 93)]),
        ("scoring-adapter.ift.local", "scoring-adapter-down", "/v1/scores",
         [("loans.LoanApplicationIT", "scoreApplicant", 58),
          ("loans.LoanApplicationIT", "scoreCoBorrower", 71)]),
    ]:
        for test_class, method, line in cases:
            b.add_failure(
                f"feign-{cause}", cause=cause, category="окружение",
                name=method, full_name=f"ru.finplat.e2e.{test_class}.{method}",
                message=f"Connection refused executing POST http://{host}:8080{path}",
                trace=_feign_trace(host, path, test_class, method, line),
                step="Вызвать партнёрский шлюз",
                evidence=[f"Connection refused executing POST http://{host}:8080{path}"])

    for host, address, cause, spec, line in [
        ("geo-coder.ift.local", "10.61.2.8:9000", "geo-coder-down", "delivery.AddressSpec", 27),
        ("tariff-engine.ift.local", "10.61.2.19:9100", "tariff-engine-down",
         "tariffs.TariffSpec", 40),
    ]:
        b.add_failure(
            f"vertx-{cause}", cause=cause, category="окружение",
            name=f"{spec.split('.')[-1]} resolves data", full_name=f"ru.finplat.e2e.{spec}",
            message=f"Connection refused: {host}/{address}",
            trace=_netty_trace(host, address, spec, line), step="Request downstream service",
            evidence=[f"Connection refused: {host}/{address}"])

    for url, address, cause, tests in [
        ("https://cards-issuer.ift.local/api/v3/cards", "10.61.4.12:443", "cards-issuer-down",
         [("cards/issue.test.js", "issues virtual card", 33),
          ("cards/issue.test.js", "issues plastic card", 51)]),
        ("https://push-gw.ift.local/api/v1/send", "10.61.4.30:443", "push-gw-down",
         [("push/send.test.js", "sends transaction push", 20),
          ("push/send.test.js", "sends marketing push", 38)]),
    ]:
        for test_file, title, line in tests:
            b.add_failure(
                f"fetch-{cause}", cause=cause, category="окружение",
                name=title, full_name=f"test/{test_file} {title}",
                message=f"request to {url} failed, reason: connect ECONNREFUSED {address}",
                trace=_fetch_trace(url, address, test_file, line), step="call backend",
                evidence=[f"connect ECONNREFUSED {address}"])

    for locator, tests in [
        ('[data-cy="loan-offer-card"]', [("shows personal offer", 22), ("shows top-up offer", 41)]),
        ('[data-cy="loan-calculator"]', [("recalculates payment", 63), ("changes term", 80)]),
    ]:
        for title, line in tests:
            message, trace = _cypress_get(locator, "loans/offers.cy.ts", line)
            b.add_failure(
                f"cy-{locator.split('=')[1].strip(chr(34) + ']')}", cause=None,
                name=title, full_name=f"cypress/e2e/loans/offers.cy.ts Loan offers {title}",
                message=message, trace=trace, step="Open loan offers")

    for view_id, view, test, line in [
        (2131296451, "btn_transfer_confirm", "transfers.TransferTest.confirmTransfer", 51),
        (2131296388, "card_balance_value", "transfers.TransferTest.showsBalanceAfter", 77),
    ]:
        message, trace = _espresso(view_id, view, test, line)
        b.add_failure(
            f"espresso-{view}", cause=None, name=test.split(".")[-1],
            full_name=f"com.lumen.mobile.e2e.{test}", message=message, trace=trace,
            step="Tap on view")

    return b.build("hosts_locators_v3")


# --------------------------------------------------------------------------------------------
# shared_assert_bg_v3: общий ассерт, разные ошибки, фоновые ошибки других сервисов


def _akka(sec: int, ms: int, level: str, dispatcher: int, path: str, text: str) -> str:
    hours, rest = divmod(sec, 3600)
    return (f"[{level}] [10/14/2026 {hours:02d}:{rest // 60:02d}:{rest % 60:02d}.{ms:03d}] "
            f"[cards-akka.actor.default-dispatcher-{dispatcher}] [akka://cards/{path}] {text}\n")


_BG_SHARED = (
    _akka(_at(11, 0), 512, "ERROR", 3, "user/audit-publisher",
          "Audit publish failed: topic cards.audit not available (broker kafka-2.ift.local:9093)")
    + _akka(_at(11, 0), 700, "WARN", 4, "system/sharding/cardRegion",
            "Retry request for shard [17] homes from coordinator. [1] buffered messages.")
    + _akka(_at(11, 1), 20, "ERROR", 6, "user/push-notifier",
            "Push provider returned 429 Too Many Requests, quota exhausted for app "
            "lumen-mobile-uat")
)

_BG_OTHER = [
    _akka(_at(11, 1, 6), 104, "ERROR", 2, "user/ldap-sync",
          "Directory sync failed: LDAP bind to ldaps://dir.ift.local:636 rejected (49 - "
          "Invalid Credentials)"),
    _akka(_at(11, 1, 7), 233, "ERROR", 8, "user/metrics",
          "Metrics push to http://pushgw.ift.local:9091 failed: 503 Service Unavailable")
    + _akka(_at(11, 1, 7), 240, "WARN", 8, "user/metrics", "Dropping 1200 samples"),
    _akka(_at(11, 1, 8), 15, "WARN", 5, "user/card-cache", "Cache eviction took 1840 ms")
    + _akka(_at(11, 1, 8), 510, "ERROR", 9, "user/statement-scheduler",
            "Scheduled job statements-nightly failed: lock statements-nightly held by "
            "cards-1 since 2026-10-14T02:00:00Z"),
]


def _cards_log(sec: int, card: str, actor: int, line: str, background: str) -> str:
    return (
        _akka(_at(10, 59), 1, "INFO", 1, "user", "Cards service 2.12.4 started, cluster "
              "members: [akka://cards@10.70.1.4:25520]")
        + background
        + _akka(sec, 118, "INFO", actor, f"user/activation/act-{card}",
                f"Activation requested for card **** {card}")
        + _akka(sec, 403, "ERROR", actor, f"user/activation/act-{card}", line)
    )


def shared_assert_bg_v3() -> Case:
    b = LaunchBuilder(8105, "ift cards regress #8105")
    message = 'expected:<"ACTIVE"> but was:<"INACTIVE">'

    def add(group: str, cause: str, category: str, card: str, index: int, line: str,
            background: str, evidence: str) -> None:
        b.add_failure(
            group, cause=cause, category=category,
            name=f"activates card {card}", full_name="ru.finplat.cards.e2e.ActivationSpec",
            message=message,
            trace=java_trace(f"io.kotest.assertions.AssertionFailedError: {message}",
                             ["ru.finplat.cards.e2e.ActivationSpec$1$2.invokeSuspend"
                              f"(ActivationSpec.kt:{48 + index})",
                              "io.kotest.core.spec.style.scopes.FunSpecRootScope$test$1"
                              ".invokeSuspend(FunSpecRootScope.kt:84)"]),
            step="Activate card",
            log=_cards_log(_at(11, 2, index * 13), card, 10 + index, line, background),
            log_name="cards-service.log", evidence=[evidence])

    for index, card in enumerate(["4417", "4425", "4433"]):
        add("hsm-key", "hsm-key-not-loaded", "окружение", card, index,
            f"Activation of card **** {card} failed: HSM error 0x21 key PVK-UAT-2026 is not "
            "loaded in partition CARDS", _BG_SHARED,
            "HSM error 0x21 key PVK-UAT-2026 is not loaded")
    for index, (card, client) in enumerate([("7710", "C-551204"), ("7728", "C-551233")]):
        add("watchlist", "test-client-watchlist", "данные", card, 3 + index,
            f"Activation of card **** {card} blocked: client {client} matched watchlist WL-117 "
            "(sanctions screening)", _BG_SHARED, "matched watchlist WL-117")
    for index, card in enumerate(["2290", "2304"]):
        add("limit-profile", "limit-profile-missing", "данные", card, 5 + index,
            f"Activation of card **** {card} failed: limit profile LP-DEBIT-STD-2 not found "
            "for product DEBIT_CLASSIC", _BG_OTHER[index],
            "limit profile LP-DEBIT-STD-2 not found")
    for index, card in enumerate(["3301", "3319", "3327"]):
        add("pin-set-event", "pin-set-event-missing", "приложение", card, 7 + index,
            f"Activation of card **** {card} rejected: transition ISSUED -> ACTIVE requires "
            "event PIN_SET, none received", _BG_OTHER[index],
            "transition ISSUED -> ACTIVE requires event PIN_SET, none received")

    return b.build("shared_assert_bg_v3")


# --------------------------------------------------------------------------------------------
# one_problem_noisy_v3: одна причина за разными симптомами, шумные логи


def _fxquote_log(index: int, sec: int, quote: str, pair: str, noise: list[str]) -> str:
    def at(offset: int) -> str:
        return f"2026-10-14 {_hms(sec + offset)},{(index * 211 + offset * 37) % 1000:03d}"

    poller = f"feed-poller-{1 + index % 3}"
    worker = f"ThreadPoolExecutor-0_{index % 4}"
    line_no = (204, 231)[index % 2]
    lines = [
        f"INFO     {at(-60)} [MainThread] fxquote.app:31 fx-quote 4.8.2 started, "
        "feeds=['moex-uat', 'partner-feed']",
        f"ERROR    {at(-20)} [{poller}] fxquote.feeds.partner:118 Feed refresh failed: [SSL: "
        "CERTIFICATE_VERIFY_FAILED] certificate verify failed: certificate has expired "
        "(_ssl.c:1006) url=https://rates.partner-feed.lan/v3/spot",
        *(f"ERROR    {at(-10 + n)} [{worker}] {text}" for n, text in enumerate(noise)),
        f"INFO     {at(0)} [{worker}] fxquote.api:77 POST /v2/quotes pair={pair} "
        f"request_id={_hex(f'req{index}', 16)}",
        f"ERROR    {at(1)} [{worker}] fxquote.quotes:{line_no} Quote {quote} rejected: no fresh "
        f"{pair} rate (age {16180 + index * 31} s, limit 120 s)",
        f"ERROR    {at(40)} [{poller}] fxquote.feeds.partner:118 Feed refresh failed: [SSL: "
        "CERTIFICATE_VERIFY_FAILED] certificate verify failed: certificate has expired "
        "(_ssl.c:1006) url=https://rates.partner-feed.lan/v3/spot",
    ]
    return "\n".join(lines) + "\n"


_FX_NOISE = [
    ["audit.sink:44 Failed to flush 12 audit records: SlowDown (bucket audit-uat)"],
    ["mail.digest:90 SMTP 421 4.7.0 Try again later, digest for desk-7 postponed",
     "audit.sink:44 Failed to flush 3 audit records: SlowDown (bucket audit-uat)"],
    [],
    ["limits.sync:15 Limits snapshot is 2 versions behind, resync scheduled"],
    ["metrics.push:22 push to pushgw.ift.local:9091 failed: HTTP 503"],
    ["clients.cache:61 client 88120 not found in cache"],
    ["audit.sink:44 Failed to flush 7 audit records: SlowDown (bucket audit-uat)",
     "metrics.push:22 push to pushgw.ift.local:9091 failed: HTTP 503"],
]


def one_problem_noisy_v3() -> Case:
    b = LaunchBuilder(8106, "uat fx regress #8106")
    cause, category = "fx-feed-cert-expired", "окружение"
    evidence = ("certificate has expired (_ssl.c:1006) "
                "url=https://rates.partner-feed.lan/v3/spot")

    robot = [("EUR/RUB", "q-88120"), ("USD/RUB", "q-88173"), ("CNY/RUB", "q-88190"),
             ("EUR/USD", None)]
    for index, (pair, quote) in enumerate(robot):
        log = (_fxquote_log(index, _at(11, 10 + index * 2), quote, pair, _FX_NOISE[index])
               if quote else None)
        b.add_failure(
            "fx-quote-status", cause=cause, category=category,
            name=f"Quote Is Confirmed For {pair}", full_name="Fx Quotes.Spot Quotes",
            message="Quote status: REJECTED != CONFIRMED", step="Request FX quote",
            log=log, log_name="fx-quote.log", evidence=[evidence] if quote else [])

    for index, (pair, quote) in enumerate([("EUR/RUB", "q-88211"), ("GBP/RUB", "q-88240")]):
        b.add_failure(
            "fx-quote-client-422", cause=cause, category=category,
            name=f"CreateQuoteViaApi ({pair})",
            full_name="Fx.E2E.QuoteApiTests.CreateQuoteViaApi",
            message="Refit.ApiException: Response status code does not indicate success: 422 "
                    "(Unprocessable Entity).",
            trace="Refit.ApiException: Response status code does not indicate success: 422 "
                  "(Unprocessable Entity).\n"
                  "   at Refit.RequestBuilderImplementation.<>c__DisplayClass14_0`2."
                  "<<BuildCancellableTaskFuncForMethod>b__0>d.MoveNext() in "
                  "/_/Refit/RequestBuilderImplementation.cs:line 288\n"
                  "   at Fx.E2E.Support.FxQuoteClient.CreateAsync(QuoteRequest request) in "
                  "/src/Fx.E2E/Support/FxQuoteClient.cs:line 29\n"
                  "   at Fx.E2E.QuoteApiTests.CreateQuoteViaApi(String pair) in "
                  f"/src/Fx.E2E/QuoteApiTests.cs:line {41 + index * 18}\n",
            step="POST /v2/quotes",
            log=_fxquote_log(4 + index, _at(11, 20 + index * 3), quote, pair,
                             _FX_NOISE[4 + index]),
            log_name="fx-quote.log", evidence=[evidence])

    for index, (pair, quote) in enumerate([("EUR/RUB", "q-88302"), ("USD/RUB", "q-88317")]):
        b.add_failure(
            "fx-quote-ui-wait", cause=cause, category=category,
            name=f"Desk shows quote amount for {pair}",
            full_name="tests/desk/quote.js Desk shows quote amount",
            message='Timed out while waiting for element <[data-test="fx-quote-amount"]> to be '
                    'present for 10000 milliseconds. - expected "present" but got: "not present" '
                    f"({10040 + index * 17}ms)",
            step="Open FX desk quote panel",
            log=_fxquote_log(6 + index, _at(11, 30 + index * 4), quote, pair,
                             _FX_NOISE[6 - index]),
            log_name="fx-quote.log", evidence=[evidence])

    return b.build("one_problem_noisy_v3")


# --------------------------------------------------------------------------------------------
# one_problem_resources_v3: одна причина, разные ресурсы в сообщении


def _notif_events(pod: str) -> str:
    return (
        "LAST SEEN   TYPE      REASON    OBJECT                                     MESSAGE\n"
        f"4m          Normal    Pulled    pod/{pod}      Container image "
        '"registry.uat.internal/notif-dispatcher:1.18.0" already present on machine\n'
        f'3m58s       Warning   Failed    pod/{pod}      Error: secret "fcm-credentials" not '
        "found\n"
        f"12s         Normal    Pulled    pod/{pod}      Container image "
        '"registry.uat.internal/notif-dispatcher:1.18.0" already present on machine\n'
    )


def _smsgw_log(sec: int, user: str) -> str:
    def at(offset: int, ms: int) -> str:
        return f"[2026-10-14 {_hms(sec + offset)},{ms:03d}]"

    return (
        f"{at(-5, 118)} INFO SMPP session sms-uat-1 connecting to smsc.uat.internal:2775 "
        "(ru.finplat.smsgw.smpp.SessionManager)\n"
        f"{at(-5, 231)} ERROR SMPP bind_transceiver failed for system_id=finplat_uat: "
        "command_status=0x0000000E (ESME_RINVPASWD) (ru.finplat.smsgw.smpp.SessionManager)\n"
        f"{at(0, 640)} INFO OTP requested for {user}, channel=sms "
        "(ru.finplat.smsgw.OtpSender)\n"
        f"{at(0, 642)} WARN OTP message for {user} queued, no bound SMPP session "
        "(ru.finplat.smsgw.OtpSender)\n"
        f"{at(25, 231)} ERROR SMPP bind_transceiver failed for system_id=finplat_uat: "
        "command_status=0x0000000E (ESME_RINVPASWD) (ru.finplat.smsgw.smpp.SessionManager)\n"
    )


def _labels_log(order: str, jid: str, sec: int) -> str:
    stamp = f"2026-10-14T{_hms(sec)}"
    head = f"pid=12 tid=8h{jid[:2]} class=ShippingLabelJob jid={jid}"
    return (
        f"{stamp}.511Z {head} INFO: start\n"
        f"{stamp}.602Z {head} elapsed=0.091 INFO: fail\n"
        f'{stamp}.603Z pid=12 tid=8h{jid[:2]} WARN: {{"context":"Job raised exception",'
        f'"job":{{"retry":5,"queue":"labels","class":"ShippingLabelJob","args":["{order}"],'
        f'"jid":"{jid}"}}}}\n'
        f"{stamp}.603Z pid=12 tid=8h{jid[:2]} WARN: Carriers::AuthError: carrier API rejected "
        "label request: 401 Unauthorized (api key ck_uat_7Q2m expired on 2026-10-13)\n"
        f"{stamp}.603Z pid=12 tid=8h{jid[:2]} WARN: /app/app/services/carriers/client.rb:41:in "
        "'Carriers::Client#create_label'\n"
    )


def _chrome_console(asset: str) -> str:
    return (
        f"GET https://ib.ift.local/assets/{asset}.css net::ERR_ABORTED 404 (Not Found)\n"
        f"GET https://ib.ift.local/assets/{asset}.js net::ERR_ABORTED 404 (Not Found)\n"
        f"Uncaught (in promise) Error: Unable to preload CSS for /assets/{asset}.css\n"
        "    at HTMLLinkElement.<anonymous> (https://ib.ift.local/assets/index-4c22e1a9.js:1:"
        "1288)\n"
    )


def one_problem_resources_v3() -> Case:
    b = LaunchBuilder(8107, "ift regress #8107")

    for index, (address, pod) in enumerate([
        ("10.62.8.14", "notif-dispatcher-6c9f7d8b4-tq2lm"),
        ("10.62.9.3", "notif-dispatcher-6c9f7d8b4-8xw7d"),
        ("10.62.11.27", "notif-dispatcher-6c9f7d8b4-vb5nk"),
    ]):
        b.add_failure(
            "notif-pods-refused", cause="notif-dispatcher-secret-missing", category="окружение",
            name=f"TestSendPush/{['card_debit', 'login_alert', 'transfer_in'][index]}",
            full_name="notify/e2e.TestSendPush",
            message=f"notify_test.go:{41 + index}: send push: rpc error: code = Unavailable "
                    'desc = connection error: desc = "transport: Error while dialing: dial tcp '
                    f'{address}:9090: connect: connection refused"',
            step="Send push notification", log=_notif_events(pod), log_name="k8s-events.txt",
            evidence=['Error: secret "fcm-credentials" not found'])

    for index, user in enumerate(["k.orlova", "d.mironov", "a.zueva"]):
        message = f"OTP for user '{user}' was not received within 60 s"
        b.add_failure(
            "otp-not-received", cause="smpp-bind-password", category="окружение",
            name=f"login with OTP as {user}", full_name="ru.finplat.auth.e2e.OtpLoginSpec",
            message=message,
            trace=java_trace(f"io.kotest.assertions.AssertionFailedError: {message}",
                             ["ru.finplat.auth.e2e.OtpLoginSpec$1$1.invokeSuspend"
                              f"(OtpLoginSpec.kt:{33 + index})"]),
            step="Wait for OTP", log=_smsgw_log(_at(11, 20, index * 40), user),
            log_name="sms-gateway.log",
            evidence=["bind_transceiver failed for system_id=finplat_uat: "
                      "command_status=0x0000000E (ESME_RINVPASWD)"])

    for index, order in enumerate(["A-77120", "A-77134", "A-77141"]):
        jid = _hex(f"label{order}", 24)
        b.add_failure(
            "order-not-shipped", cause="carrier-api-key-expired", category="окружение",
            name=f"test_ships_order_{index + 1}", full_name="ShippingFlowTest#test_ships_order",
            message=f'Order {order} should be shipped.\nExpected: "shipped"\n'
                    '  Actual: "awaiting_label"',
            trace=f"test/system/shipping_flow_test.rb:{19 + index * 6}:in "
                  "'ShippingFlowTest#test_ships_order'\n",
            step="Ship order", log=_labels_log(order, jid, _at(11, 22, index * 31)),
            log_name="sidekiq.log",
            evidence=["carrier API rejected label request: 401 Unauthorized"])

    cause = "stand-dns-failure"
    for index, (host, doc) in enumerate([("docs-store.ift.local", "/api/v1/docs/D-4410"),
                                         ("audit-trail.ift.local", "/api/v1/events?doc=D-4410")]):
        b.add_failure(
            "dns-go-servfail", cause=cause, category="окружение",
            name=f"TestFetchDocument/{['body', 'audit'][index]}",
            full_name="docs/e2e.TestFetchDocument",
            message=f'docs_test.go:{29 + index * 8}: fetch: Get "https://{host}{doc}": dial tcp: '
                    f"lookup {host} on 10.62.0.10:53: server misbehaving",
            step="Fetch document", evidence=["on 10.62.0.10:53: server misbehaving"])
    for index, host in enumerate(["kyc-files.ift.local", "bonus-api.ift.local"]):
        b.add_failure(
            "dns-node-notfound", cause=cause, category="окружение",
            name=["downloads KYC file", "reads bonus balance"][index],
            full_name=f"test/{['kyc', 'bonus'][index]}.test.js",
            message=f"RequestError: getaddrinfo ENOTFOUND {host}",
            trace=f"RequestError: getaddrinfo ENOTFOUND {host}\n"
                  "    at ClientRequest.<anonymous> (node_modules/got/dist/source/core/index.js:"
                  "792:61)\n"
                  "    at GetAddrInfoReqWrap.onlookupall [as oncomplete] (node:dns:120:26)\n",
            step="call service", evidence=[f"getaddrinfo ENOTFOUND {host}"])
    b.add_failure(
        "dns-rust-lookup", cause=cause, category="окружение",
        name="rates::history_is_loaded", full_name="fx_history::tests::rates",
        message="thread 'rates::history_is_loaded' panicked at tests/rates.rs:22:10:\n"
                "called `Result::unwrap()` on an `Err` value: reqwest::Error { kind: Request, "
                'url: "https://fx-history.ift.local/api/rates", source: '
                'hyper_util::client::legacy::Error(Connect, ConnectError("dns error", Custom { '
                'kind: Uncategorized, error: "failed to lookup address information: Try again" '
                "})) }",
        evidence=['ConnectError("dns error"', "failed to lookup address information: Try again"])

    asset = "DepositCalculator-7b31c0e2"
    for index, locator in enumerate(['[data-cy="deposit-rate"]', '[data-cy="deposit-term"]',
                                     '[data-cy="deposit-submit"]']):
        message, trace = _cypress_get(locator, "deposits/calculator.cy.ts", 18 + index * 15)
        b.add_failure(
            "deposit-page-not-loaded", cause="deposit-bundle-missing", category="приложение",
            name=["shows rate", "changes term", "submits application"][index],
            full_name="cypress/e2e/deposits/calculator.cy.ts Deposit calculator",
            message=message, trace=trace, step="Open deposit calculator",
            logs=[("browser-console.log", _chrome_console(asset), "text/plain")],
            evidence=[f"{asset}.js net::ERR_ABORTED 404 (Not Found)"])

    return b.build("one_problem_resources_v3")


# --------------------------------------------------------------------------------------------
# signatures_kb_v3: близкие тексты разных проблем, причины с несколькими симптомами


def _wildfly(sec: int, ms: int, level: str, category: str, thread: str, text: str) -> str:
    return f"{_hms(sec)},{ms:03d} {level:<5} [{category}] ({thread}) {text}\n"


def _loans_log(sec: int, task: int, body: str) -> str:
    return (
        _wildfly(sec - 300, 4, "INFO", "org.jboss.as", "Controller Boot Thread",
                 "WFLYSRV0025: WildFly Full 31.0.1.Final (WildFly Core 23.0.3.Final) started "
                 "in 8412ms - Started 612 of 838 services")
        + _wildfly(sec, 101, "INFO", "com.meridian.loans.api.ApplicationResource",
                   f"default task-{task}", "application received")
        + body
    )


def _python_report_log(sec: int, day: str) -> str:
    return (
        f"INFO     2026-10-14 {_hms(sec)},004 [MainThread] reports.app:22 insurance-reports 1.6.0 "
        "started\n"
        f"INFO     2026-10-14 {_hms(sec + 3)},118 [worker-1] reports.insurance:51 build report "
        f"day={day}\n"
        f"ERROR    2026-10-14 {_hms(sec + 3)},301 [worker-1] reports.insurance:77 Report build "
        "failed: (psycopg.errors.UndefinedColumn) column loan_offers.insurance_type does not "
        "exist\n"
        "LINE 1: SELECT loan_offers.id, loan_offers.insurance_type, loan_offers.premium FROM "
        "loan_offers ...\n"
        "                               ^\n"
        "[SQL: SELECT loan_offers.id, loan_offers.insurance_type, loan_offers.premium FROM "
        "loan_offers WHERE loan_offers.issued_on = %(day)s]\n"
    )


def _s3_denied(sec: int, task: int, key: str) -> str:
    return _wildfly(sec + 1, 412, "ERROR", "com.meridian.loans.docs.DocumentStore",
                    f"default task-{task}",
                    f"S3 PutObject to bucket loan-docs-uat failed (key={key}): Access Denied "
                    f"(Service: S3, Status Code: 403, Request ID: {_hex(key, 16).upper()})")


def signatures_kb_v3() -> Case:
    b = LaunchBuilder(8108, "ift loans regress #8108")
    declined = "AssertionError: expected 'DECLINED' to be 'APPROVED' // Object.is equality"

    def vitest(title: str, spec: str, line: int, message: str) -> dict[str, str]:
        return {"name": title, "full_name": f"tests/{spec} > {title}", "message": message,
                "trace": f"{message}\n    at /builds/loans/api-tests/tests/{spec}:{line}:41\n"}

    for index, app in enumerate(["LA-50112", "LA-50131"]):
        b.add_failure(
            "scoring-model", cause="scoring-model-not-deployed", category="окружение",
            step="Submit loan application",
            log=_loans_log(_at(10, 30, index * 20), 4 + index, _wildfly(
                _at(10, 30, index * 20 + 1), 114, "ERROR",
                "com.meridian.loans.scoring.ModelRegistry", f"default task-{4 + index}",
                "Scoring model SCR-2026-Q4 is not deployed, fallback policy DECLINE_ALL applied "
                f"to application {app}")),
            log_name="loans-server.log",
            evidence=["Scoring model SCR-2026-Q4 is not deployed"],
            **vitest(f"approves cash loan {index + 1}", "loans/cash.spec.ts", 27 + index * 9,
                     declined))

    bureau_logs = [
        _loans_log(_at(10, 32), 9,
                   _wildfly(_at(10, 32, 20), 3, "WARN", "com.meridian.loans.bureau.BureauClient",
                            "default task-9", "Credit bureau request for LA-50150 timed out "
                            "after 20000 ms (bureau=NBKI-UAT)")
                   + _wildfly(_at(10, 32, 20), 9, "INFO",
                              "com.meridian.loans.decision.DecisionEngine", "default task-9",
                              "application LA-50150 decision DECLINED: bureau report unavailable")),
        _wildfly(_at(10, 33, 1), 220, "INFO", "com.meridian.loans.bureau.BureauPool",
                 "bureau-pool-3", "pool stats: active=8 idle=0 waiting=14")
        + _wildfly(_at(10, 33, 21), 518, "ERROR", "com.meridian.loans.bureau.BureauClient",
                   "bureau-pool-3", "Bureau NBKI-UAT did not respond within 20000 ms, request "
                   "BR-99812 for LA-50177 abandoned")
        + _wildfly(_at(10, 33, 21), 521, "WARN", "com.meridian.loans.bureau.BureauPool",
                   "bureau-pool-3", "circuit nbki-uat half-open, 3 of 5 recent calls failed")
        + _wildfly(_at(10, 33, 21), 530, "INFO", "com.meridian.loans.decision.DecisionEngine",
                   "default task-2", "application LA-50177 decision DECLINED: bureau report "
                   "unavailable"),
        "10.44.20.7:53122 [14/Oct/2026:10:34:02.118] fe_bureau be_bureau/nbki-uat 0/0/1/-1/20002 "
        '504 198 - - sH-- 31/31/14/14/0 0/0 "POST /nbki/v2/report HTTP/1.1"\n'
        "10.44.20.7:53140 [14/Oct/2026:10:34:05.440] fe_bureau be_bureau/nbki-uat 0/0/1/-1/20001 "
        '504 198 - - sH-- 30/30/13/13/0 0/0 "POST /nbki/v2/report HTTP/1.1"\n',
    ]
    for index, log in enumerate(bureau_logs):
        b.add_failure(
            "bureau-timeout", cause="bureau-timeout", category="окружение",
            step="Submit loan application", log=log,
            log_name=["loans-server.log", "loans-server.log", "haproxy.log"][index],
            evidence=["decision DECLINED: bureau report unavailable"] if index == 0 else
            (["Bureau NBKI-UAT did not respond within 20000 ms"] if index == 1 else []),
            **vitest(f"approves card loan {index + 1}", "loans/card.spec.ts", 19 + index * 7,
                     declined))

    b.add_failure(
        "applicant-age", cause="applicant-underage", category="данные",
        step="Submit loan application",
        log=_loans_log(_at(10, 35), 3, _wildfly(
            _at(10, 35, 1), 77, "INFO", "com.meridian.loans.decision.PolicyRules",
            "default task-3", "Applicant age 17 is below minimum 18 for product CONSUMER_CASH, "
            "application LA-50190 DECLINED")),
        log_name="loans-server.log",
        evidence=["Applicant age 17 is below minimum 18 for product CONSUMER_CASH"],
        **vitest("approves student loan", "loans/student.spec.ts", 14, declined))

    s3 = "S3 PutObject to bucket loan-docs-uat failed"
    uploaded = "AssertionError: expected 'FAILED' to be 'UPLOADED' // Object.is equality"
    for index, key in enumerate(["LA-50201/income-2025.pdf", "LA-50214/income-2025.pdf"]):
        sec = _at(10, 40, index * 15)
        b.add_failure(
            "upload-status-failed", cause="doc-storage-access-denied", category="окружение",
            step="Upload income certificate",
            log=_loans_log(sec, 11 + index, _s3_denied(sec, 11 + index, key)),
            log_name="loans-server.log", evidence=[s3],
            **vitest(f"uploads income certificate {index + 1}", "docs/upload.spec.ts",
                     33 + index * 6, uploaded))
    for index, key in enumerate(["LA-50230/passport.jpg", "LA-50236/passport.jpg"]):
        sec = _at(10, 41, index * 15)
        b.add_failure(
            "upload-ui-success-missing", cause="doc-storage-access-denied", category="окружение",
            name=f"Passport copy is attached ({index + 1})",
            full_name="Documents / Passport copy is attached",
            message="The specified selector does not match any element in the DOM tree.\n\n"
                    " > | Selector('.upload-status--success')",
            step="Attach passport copy",
            log=_loans_log(sec, 14 + index, _s3_denied(sec, 14 + index, key)),
            log_name="loans-server.log", evidence=[s3])
    for index, key in enumerate(["LA-50251/contract.pdf", "LA-50266/contract.pdf"]):
        sec = _at(10, 42, index * 15)
        message = ('Server error(POST http://loan-docs.ift.local/api/v1/documents: 500 Internal '
                   'Server Error. Text: "{"code":"STORAGE_UNAVAILABLE"}"')
        b.add_failure(
            "upload-api-500", cause="doc-storage-access-denied", category="окружение",
            name=f"uploads signed contract {index + 1}",
            full_name="ru.meridian.loans.e2e.DocumentsApiSpec", message=message,
            trace=java_trace(f"io.ktor.client.plugins.ServerResponseException: {message}",
                             ["io.ktor.client.plugins.DefaultResponseValidationKt$addDefault"
                              "ResponseValidation$1$1.invokeSuspend(DefaultResponseValidation.kt"
                              ":54)",
                              "ru.meridian.loans.e2e.DocumentsApiSpec$1$1.invokeSuspend"
                              f"(DocumentsApiSpec.kt:{40 + index * 11})"]),
            step="Upload document via API",
            log=_loans_log(sec, 17 + index, _s3_denied(sec, 17 + index, key)),
            log_name="loans-server.log", evidence=[s3])
    for index, app in enumerate(["LA-50280", "LA-50293"]):
        sec = _at(10, 44, index * 15)
        b.add_failure(
            "upload-eicar", cause="test-file-eicar", category="данные",
            step="Upload income certificate",
            log=_loans_log(sec, 3 + index, _wildfly(
                sec + 1, 77, "WARN", "com.meridian.loans.docs.AvScanner", f"default task-{3 + index}",
                f"File {app}/income-2024.pdf rejected by antivirus: Win.Test.EICAR_HDB-1 FOUND")),
            log_name="loans-server.log",
            evidence=["rejected by antivirus: Win.Test.EICAR_HDB-1 FOUND"],
            **vitest(f"uploads last year income certificate {index + 1}", "docs/upload.spec.ts",
                     61 + index * 6, uploaded))

    column = "ERROR: column lo1_0.insurance_type does not exist"

    def hibernate_body(sec: int, task: int) -> str:
        return (
            _wildfly(sec + 1, 300, "WARN", "org.hibernate.engine.jdbc.spi.SqlExceptionHelper",
                     f"default task-{task}", "SQL Error: 0, SQLState: 42703")
            + _wildfly(sec + 1, 301, "ERROR", "org.hibernate.engine.jdbc.spi.SqlExceptionHelper",
                       f"default task-{task}", column)
            + "  Position: 87\n"
        )

    for index in range(2):
        sec = _at(10, 50, index * 20)
        b.add_failure(
            "insurance-offer-500", cause="insurance-type-migration", category="приложение",
            step="Create insurance offer",
            log=_loans_log(sec, 21 + index, hibernate_body(sec, 21 + index)),
            log_name="loans-server.log", evidence=[column],
            **vitest(f"creates life insurance offer {index + 1}", "insurance/offer.spec.ts",
                     22 + index * 8, "AssertionError: expected 500 to be 201 // Object.is "
                                     "equality"))
    for index, day in enumerate(["2026-10-13", "2026-10-12"]):
        b.add_failure(
            "insurance-report-empty", cause="insurance-type-migration", category="приложение",
            name=f"Insurance report for {day}", full_name="features/reports.feature",
            message=f"Assertion Failed: insurance report for {day} has 0 rows, expected > 0",
            step="Then the insurance report has rows",
            log=_python_report_log(_at(10, 52, index * 30), day), log_name="reports.log",
            evidence=["(psycopg.errors.UndefinedColumn) column loan_offers.insurance_type does "
                      "not exist"])
    sec = _at(10, 54)
    b.add_failure(
        "insurance-offer-ui", cause="insurance-type-migration", category="приложение",
        name="Insurance offer is shown", full_name="Insurance / Insurance offer is shown",
        message="The specified selector does not match any element in the DOM tree.\n\n"
                " > | Selector('[data-testid=\"insurance-offer\"]')",
        step="Open insurance offer", log=_loans_log(sec, 25, hibernate_body(sec, 25)),
        log_name="loans-server.log", evidence=[column])

    return b.build("signatures_kb_v3")


# --------------------------------------------------------------------------------------------
# retries_v3: повторы Cypress, связь через historyId, historyKey и testCaseId + parameters


def _cy_request(url: str, status: str) -> str:
    return (f"cy.request() failed on:\n\n{url}\n\nThe response we received from your web server "
            f"was:\n\n  > {status}\n\nThis was considered a failure because the status code "
            "was not `2xx` or `3xx`.")


def retries_v3() -> Case:
    b = LaunchBuilder(8109, "policy-portal e2e #8109")
    api = "https://polis-gw.ift.local/api/v2"
    env = [{"name": "stand", "value": "ift"}]
    quote_500 = _cy_request(f"{api}/quotes", "500: Internal Server Error")

    def result(name: str, status: str, message: str | None, step: str, extra: dict[str, Any],
               hidden: bool = False) -> int:
        return b.add_result(name=name, status=status, message=message, step=step, hidden=hidden,
                            full_name=f"cypress/e2e/policy/{name.split(' ')[0]}.cy.ts {name}",
                            extra=extra)

    name = "quote shows premium"
    link = {"historyId": _hex("h-premium"), "testCaseId": 12001}
    result(name, "failed", "AssertionError: Timed out retrying after 4000ms: Expected to find "
           'element: `[data-cy="quote-premium"]`, but never found it.', "Calculate quote", link,
           hidden=True)
    passed = result(name, "passed", None, "Calculate quote", link)
    b.expect_passed_after_retry(passed)

    name = "quote for new driver"
    link = {"historyKey": _hex("k-new-driver", 40)}
    first = result(name, "failed", quote_500, "Calculate quote", link, hidden=True)
    second = result(name, "failed", quote_500, "Calculate quote", link, hidden=True)
    final = b.add_failure("quote-api-500", cause=None, name=name, message=quote_500,
                          step="Calculate quote", extra=link,
                          full_name="cypress/e2e/policy/quote.cy.ts quote for new driver")
    b.expect_attempts(final, [(first, True), (second, True)])

    name = "policy is issued"
    link = {"historyId": _hex("h-issue"), "testCaseId": 12003}
    first = result(name, "failed", "AssertionError: Timed out retrying after 4000ms: expected "
                   "'<span.policy-status>' to contain text 'Issued', but the text was "
                   "'Processing'", "Issue contract and wait", link, hidden=True)
    final = b.add_failure("issue-502", cause=None, name=name,
                          message=_cy_request(f"{api}/policies/issue", "502: Bad Gateway"),
                          step="Issue contract and wait", extra=link,
                          full_name="cypress/e2e/policy/issue.cy.ts policy is issued")
    b.expect_attempts(final, [(first, False)])

    for product in ["OSAGO", "KASKO"]:
        name = "quote by product"
        link = {"testCaseId": 12004, "parameters": [{"name": "product", "value": product}],
                "environment": env}
        attempt = result(name, "failed", quote_500, "Calculate quote", link, hidden=True)
        final = b.add_failure("quote-api-500", cause=None, name=name, message=quote_500,
                              step="Calculate quote", extra=link,
                              full_name="cypress/e2e/policy/quote.cy.ts quote by product")
        b.expect_attempts(final, [(attempt, True)])

    name = "status turns issued"
    status_message = ("AssertionError: Timed out retrying after 4000ms: expected 'Processing' to "
                      "equal 'Issued'")
    link = {"historyId": _hex("h-status"), "testCaseId": 12005}
    attempt = result(name, "failed", None, "Wait for policy status", link, hidden=True)
    b.add_detail(attempt, message=status_message)
    final = b.add_failure("status-processing", cause=None, name=name, message=status_message,
                          step="Wait for policy status", extra=link,
                          full_name="cypress/e2e/policy/status.cy.ts status turns issued")
    b.expect_attempts(final, [(attempt, True)])

    name = "pay premium via SBP"
    sbp_503 = _cy_request(f"{api}/payments/sbp", "503: Service Unavailable")
    link = {"historyId": _hex("h-sbp"), "testCaseId": 12006}
    attempts = [result(name, "failed", sbp_503, "Pay premium", link, hidden=True)
                for _ in range(6)]
    final = b.add_failure("sbp-503", cause=None, name=name, message=sbp_503, step="Pay premium",
                          extra=link, full_name="cypress/e2e/policy/pay.cy.ts pay premium via SBP")
    b.expect_attempts(final, [(attempt, True) for attempt in attempts])

    name = "agent sees commission"
    commission = ("AssertionError: Timed out retrying after 4000ms: Expected to find element: "
                  '`[data-cy="agent-commission"]`, but never found it.')
    for _ in range(2):
        b.add_result(name=name, status="failed", message=commission, step="Open agent cabinet",
                     hidden=True)
    final = b.add_failure("commission-missing", cause=None, name=name, message=commission,
                          step="Open agent cabinet", extra={"historyId": _hex("h-commission")},
                          full_name="cypress/e2e/policy/agent.cy.ts agent sees commission")
    b.expect_attempts(final, [])

    name = "quote for young driver"
    final = b.add_failure("quote-api-500", cause=None, name=name, message=quote_500,
                          step="Calculate quote",
                          extra={"historyId": _hex("h-young"), "testCaseId": 12008},
                          full_name="cypress/e2e/policy/quote.cy.ts quote for young driver")
    b.expect_attempts(final, [])

    return b.build("retries_v3")


# --------------------------------------------------------------------------------------------
# retries_env_v3: без historyId и historyKey, связь только testCaseId + parameters + environment


def _karate_status(code: int, expected: int, host: str, path: str, body: str, ms: int) -> str:
    return (f"status code was: {code}, expected: {expected}, response time in milliseconds: "
            f"{ms}, url: https://{host}{path}, response: \n{body}")


def retries_env_v3() -> Case:
    b = LaunchBuilder(8110, "acquiring regress #8110")

    def env(stand: str) -> list[dict[str, str]]:
        return [{"name": "stand", "value": stand}]

    def extra(case_id: int, params: list[dict[str, str]], stand: str) -> dict[str, Any]:
        return {"testCaseId": case_id, "parameters": params, "environment": env(stand)}

    def hidden(name: str, message: str, link: dict[str, Any]) -> int:
        return b.add_result(name=name, status="failed", message=message, hidden=True,
                            full_name=f"classpath:acquiring/{name}.feature", extra=link)

    terminal = [{"name": "terminal", "value": "T-5501"}]
    settle = "settle"
    settle_503 = _karate_status(503, 200, "acquiring.ift-1.local", "/api/v1/terminals/T-5501/"
                                "settle", '{"error":"settlement window closed"}', 30012)
    attempt = hidden(settle, settle_503, extra(9101, terminal, "ift-1"))
    final = b.add_failure("settle-503", cause=None, name=settle, message=settle_503,
                          full_name="classpath:acquiring/settle.feature",
                          extra=extra(9101, terminal, "ift-1"))
    b.expect_attempts(final, [(attempt, True)])
    final = b.add_failure(
        "settle-401", cause=None, name=settle,
        message=_karate_status(401, 200, "acquiring.ift-2.local", "/api/v1/terminals/T-5501/"
                               "settle", '{"error":"invalid_token"}', 41),
        full_name="classpath:acquiring/settle.feature", extra=extra(9101, terminal, "ift-2"))
    b.expect_attempts(final, [])

    refund = "refund"
    hidden(refund, _karate_status(503, 200, "acquiring.ift-1.local", "/api/v1/refunds",
                                  '{"error":"upstream unavailable"}', 30007),
           extra(9102, [], "ift-1"))
    final = b.add_failure(
        "refund-409", cause=None, name=refund,
        message=_karate_status(409, 200, "acquiring.ift-2.local", "/api/v1/refunds",
                               '{"error":"refund already exists"}', 88),
        full_name="classpath:acquiring/refund.feature", extra=extra(9102, [], "ift-2"))
    b.expect_attempts(final, [])

    convert = "convert"
    rate_zero = "match failed: EQUALS\n  $.rate | not equal (NUMBER:NUMBER)\n  0\n  92.15"
    rub = [{"name": "currency", "value": "RUB"}]
    attempt = hidden(convert, rate_zero, extra(9103, rub, "ift-1"))
    final = b.add_failure("fx-rate-zero", cause=None, name=convert, message=rate_zero,
                          full_name="classpath:acquiring/convert.feature",
                          extra=extra(9103, rub, "ift-1"))
    b.expect_attempts(final, [(attempt, True)])
    usd = [{"name": "currency", "value": "USD"}]
    attempt = hidden(convert, rate_zero.replace("92.15", "1.0"), extra(9103, usd, "ift-1"))
    final = b.add_failure(
        "fx-convert-500", cause=None, name=convert,
        message=_karate_status(500, 200, "acquiring.ift-1.local", "/api/v1/fx/convert",
                               '{"error":"internal"}', 120),
        full_name="classpath:acquiring/convert.feature", extra=extra(9103, usd, "ift-1"))
    b.expect_attempts(final, [(attempt, False)])

    payout = "payout"
    hidden(payout, _karate_status(504, 200, "acquiring.ift-2.local", "/api/v1/payouts",
                                  '{"error":"gateway timeout"}', 60001),
           extra(9104, [], "ift-2"))
    passed = b.add_result(name=payout, status="passed", extra=extra(9104, [], "ift-2"),
                          full_name="classpath:acquiring/payout.feature")
    b.expect_passed_after_retry(passed)

    return b.build("retries_env_v3")


# --------------------------------------------------------------------------------------------
# sparse_data_v3: падения без данных, только шаг, только лог; muted, hidden, passed


def _rabbit_log(sec: int, conn: str, port: int) -> str:
    stamp = f"2026-10-14 {_hms(sec)}.902114+00:00"
    return (
        "2026-10-14 12:00:00.101418+00:00 [info] <0.230.0> Server startup complete; 4 plugins "
        "started.\n"
        f"{stamp} [info] <0.{conn}.0> accepting AMQP connection 10.44.8.21:{port} -> "
        "10.44.8.5:5672\n"
        f"{stamp} [error] <0.{conn}.0> Channel error on connection <0.{conn}.0> "
        f"(10.44.8.21:{port} -> 10.44.8.5:5672, vhost: 'uat', user: 'loan-events'), channel 1:\n"
        f"{stamp} [error] <0.{conn}.0> operation basic.publish caused a channel exception "
        "not_found: no exchange 'loan.decisions.v2' in vhost 'uat'\n"
    )


def sparse_data_v3() -> Case:
    b = LaunchBuilder(8111, "mobile-bank smoke #8111")
    suite = "LumenBankUITests"

    for index, (test, status) in enumerate([("testOpenDepositsTab", "failed"),
                                            ("testChangeLanguage", "broken"),
                                            ("testLogoutClearsCache", "failed")]):
        b.add_failure(f"silent-{index + 1}", cause=None, name=test, status=status,
                      full_name=f"{suite}.SmokeTests.{test}")

    for test in ["testSignCreditAgreement", "testSignCardAgreement"]:
        b.add_failure("step-sign-agreement", cause=None, name=test,
                      full_name=f"{suite}.AgreementTests.{test}",
                      step="Подписать кредитный договор")
    b.add_failure("step-card-limits", cause=None, name="testCardLimitsScreen",
                  full_name=f"{suite}.CardTests.testCardLimitsScreen",
                  step="Open card limits screen")

    for index, test in enumerate(["testLoanDecisionPublished", "testLoanDecisionNotified"]):
        b.add_failure(
            "exchange-missing", cause="loan-exchange-missing", category="окружение",
            name=test, full_name=f"{suite}.LoanEventsTests.{test}",
            log=_rabbit_log(_at(12, 5, 11 + index * 9), str(4412 + index * 37), 50122 + index),
            log_name="rabbitmq.log",
            evidence=["no exchange 'loan.decisions.v2' in vhost 'uat'"])

    for test in ["testPushOnLogin", "testBiometricPrompt"]:
        b.add_result(name=test, status="failed", muted=True,
                     message="XCTAssertTrue failed - push banner did not appear",
                     full_name=f"{suite}.KnownIssuesTests.{test}")
    for test in ["testTransferToCard", "testPayMobile"]:
        b.add_result(name=test, status="failed", hidden=True,
                     message="Failed to get matching snapshot: No matches found for Elements "
                             'matching predicate \'"confirm_button" IN identifiers\'',
                     full_name=f"{suite}.PaymentsTests.{test}")
        b.add_result(name=test, status="passed", full_name=f"{suite}.PaymentsTests.{test}")
    for test in ["testLogin", "testBalanceShown", "testHistoryScroll"]:
        b.add_result(name=test, status="passed", full_name=f"{suite}.SmokeTests.{test}")
    b.add_result(name="testApplePay", status="skipped", full_name=f"{suite}.PaymentsTests"
                 ".testApplePay")

    return b.build("sparse_data_v3")


# --------------------------------------------------------------------------------------------
# operation_logs_v3: общие логи сервисов, операции помечены rqUID/operUID и traceparent


def _payhub_line(sec: int, ms: int, level: str, category: str, thread: str,
                 mdc: str | None, text: str) -> str:
    ids = f"[{mdc}] " if mdc else ""
    return (f"2026-10-14 {_hms(sec)},{ms:03d} {level:<5} [ru.finplat.payhub.{category}] "
            f"({thread}) {ids}{text}\n")


def _payhub_log(ops: list[dict[str, Any]]) -> str:
    lines = [
        "2026-10-14 12:20:00,412 INFO  [org.jboss.as] (Controller Boot Thread) WFLYSRV0025: "
        "WildFly Full 31.0.1.Final (WildFly Core 23.0.3.Final) started in 8412ms\n",
        _payhub_line(_at(12, 20, 1), 5, "INFO", "Bootstrap", "ServerService Thread Pool -- 74",
                     None, "payhub 3.12.0 started, profile=ift, abs=abs-adapter.ift.local"),
        _payhub_line(_at(12, 25), 2, "INFO", "jobs.RatesRefresh", "EJB default - 3", None,
                     "currency rates refreshed: 42 pairs"),
    ]
    for index, op in enumerate(ops):
        sec = _at(12, 30, index * 9)
        thread = f"default task-{7 + index % 6}"
        mdc = f"rqUID={op['rq']}, operUID={op['oper']}"
        lines.append(_payhub_line(sec, 104, "INFO", "transfer.TransferResource", thread, mdc,
                                  f"Transfer request received: amount={op['amount']} RUB, "
                                  f"payer={op['payer']}"))
        outcome = op["outcome"]
        if outcome == "arrest":
            lines.append(_payhub_line(sec + 1, 220, "ERROR", "transfer.TransferProcessor",
                                      thread, mdc, "Transfer rejected by ABS: payer account "
                                      f"{op['payer']} is under arrest (ABS-4013)"))
        elif outcome == "abs-timeout":
            lines.append(_payhub_line(sec + 15, 31, "ERROR", "abs.AbsClient", thread, mdc,
                                      "ABS call createPayment timed out after 15000 ms, "
                                      "operation marked REJECTED"))
        elif outcome == "abs-5001":
            lines.append(_payhub_line(sec + 1, 87, "ERROR", "abs.AbsClient", thread, mdc,
                                      "ABS returned error ABS-5001 for createPayment, operation "
                                      "marked FAILED"))
        elif outcome == "ok":
            lines.append(_payhub_line(sec + 1, 302, "INFO", "transfer.TransferProcessor",
                                      thread, mdc, f"Transfer executed, ABS document "
                                      f"{op['doc']}"))
        elif outcome == "limit":
            lines.append(_payhub_line(sec + 1, 140, "WARN", "transfer.LimitGuard", thread, mdc,
                                      "Daily limit is 90% used for payer "
                                      f"{op['payer']}"))
            lines.append(_payhub_line(sec + 1, 302, "INFO", "transfer.TransferProcessor",
                                      thread, mdc, f"Transfer executed, ABS document "
                                      f"{op['doc']}"))
        if index == 3:
            lines.append(_payhub_line(_at(12, 30, 30), 1, "ERROR", "jobs.Reconciliation",
                                      "EJB default - 5", None, "Reconciliation job failed: SFTP "
                                      "sftp-recon.ift.local:22 auth failed for user recon_uat"))
    return "".join(lines)


def _abs_clef(ops: list[dict[str, Any]]) -> str:
    lines = [_jl({"@t": "2026-10-14T12:20:03.0000000Z", "@mt": "Abs.Adapter {Version} started",
                  "Version": "2.4.0", "SourceContext": "Abs.Adapter.Program"})]
    for index, op in enumerate(ops):
        stamp = f"2026-10-14T{_hms(_at(12, 30, index * 9 + 1))}.{100 + index:03d}0000Z"
        request = f"0HN7B{index:02d}KQ3:00000001"
        if op["outcome"] == "abs-5001":
            lines.append(_jl({"@t": stamp, "@mt": "Limit check failed for {OperUID}: {Reason}",
                              "@l": "Error", "OperUID": op["oper"],
                              "Reason": "daily limit table LIM_DAY_0042 not found",
                              "ErrorCode": "ABS-5001", "RequestId": request,
                              "SourceContext": "Abs.Adapter.Limits"}))
        elif op["outcome"] == "abs-timeout":
            lines.append(_jl({"@t": stamp, "@mt": "Core banking call {Method} for {OperUID} "
                              "took {Elapsed} ms", "@l": "Warning", "Method": "createPayment",
                              "OperUID": op["oper"], "Elapsed": 15020 + index,
                              "RequestId": request, "SourceContext": "Abs.Adapter.Core"}))
        elif op["outcome"] == "arrest":
            lines.append(_jl({"@t": stamp, "@mt": "Payment {OperUID} rejected with {ErrorCode}",
                              "@l": "Warning", "OperUID": op["oper"], "ErrorCode": "ABS-4013",
                              "RequestId": request, "SourceContext": "Abs.Adapter.Payments"}))
        else:
            lines.append(_jl({"@t": stamp, "@mt": "Payment {OperUID} created as {Document}",
                              "OperUID": op["oper"], "Document": op.get("doc", "-"),
                              "RequestId": request, "SourceContext": "Abs.Adapter.Payments"}))
    lines.append(_jl({"@t": "2026-10-14T12:32:00.0000000Z", "@mt": "Health probe {Status}",
                      "Status": "Degraded", "@l": "Warning",
                      "SourceContext": "Abs.Adapter.Health"}))
    return "".join(lines)


def _notify_log(ops: list[tuple[str, str]]) -> str:
    lines = ["[notify-gw] 2026-10-14 12:20:02.004 INFO started notify-gw 0.9.3, providers=[mts, "
             "beeline]\n"]
    for index, (traceparent, outcome) in enumerate(ops):
        stamp = f"2026-10-14 {_hms(_at(12, 31, index * 7))}"
        phone = f"+7900***{1201 + index * 13}"
        lines.append(f"[notify-gw] {stamp}.118 INFO sms queued (traceparent={traceparent} "
                     f"phone={phone})\n")
        if outcome == "sender":
            lines.append(f"[notify-gw] {stamp}.402 ERROR sms send failed: provider responded "
                         "403: sender name FINPLAT is not registered for route mts "
                         f"(traceparent={traceparent} phone={phone})\n")
        elif outcome == "blacklist":
            lines.append(f"[notify-gw] {stamp}.377 ERROR sms send failed: phone {phone} is in "
                         f"provider stop-list (traceparent={traceparent})\n")
        else:
            lines.append(f"[notify-gw] {stamp}.290 INFO sms delivered (traceparent={traceparent} "
                         f"phone={phone})\n")
    lines.append("[notify-gw] 2026-10-14 12:33:00.000 WARN provider beeline latency p99=2.4s\n")
    return "".join(lines)


def operation_logs_v3() -> Case:
    b = LaunchBuilder(8112, "payhub ift regress #8112")

    def op(n: int, outcome: str, amount: str = "1500.00") -> dict[str, Any]:
        return {"rq": _hex(f"rq{n}"), "oper": _hex(f"oper{n}"), "outcome": outcome,
                "amount": amount, "payer": f"408178102000000{4400 + n * 71:05d}",
                "doc": f"0042-{118200 + n}"}

    ops = [op(0, "ok"), op(1, "arrest"), op(2, "abs-timeout"), op(3, "arrest"), op(4, "ok"),
           op(5, "abs-5001"), op(6, "abs-timeout"), op(7, "limit"), op(8, "abs-5001"),
           op(9, "arrest")]
    payhub = _payhub_log(ops)
    abs_log = _abs_clef(ops)
    traces = [f"00-{_hex(f'trace{n}')}-{_hex(f'span{n}', 16)}-01" for n in range(5)]
    notify = _notify_log([(traces[0], "ok"), (traces[1], "sender"), (traces[2], "blacklist"),
                          (traces[3], "sender"), (traces[4], "ok")])

    def json_request(rq: str) -> tuple[str, str, str]:
        return ("request.json", json.dumps({
            "method": "POST", "url": "https://payhub.ift.local/api/v2/transfers",
            "headers": {"rqUID": rq, "Content-Type": "application/json"},
            "body": {"amount": "1500.00", "currency": "RUB"}}, indent=2), "application/json")

    def json_response(o: dict[str, Any], status: str) -> tuple[str, str, str]:
        return ("response.json", json.dumps({
            "status": 200, "headers": {"rqUID": o["rq"]},
            "body": {"operUID": o["oper"], "status": status}}, indent=2), "application/json")

    def xml_request(rq: str) -> tuple[str, str, str]:
        return ("request.xml", (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<TransferRq xmlns="urn:finplat:payhub:v2">\n'
            f"  <RqUID>{rq}</RqUID>\n  <RqTm>2026-10-14T12:30:00+03:00</RqTm>\n"
            "  <Amount currency=\"RUB\">1500.00</Amount>\n</TransferRq>\n"), "application/xml")

    rejected = 'expected:<"EXECUTED"> but was:<"REJECTED">'

    def kotest(message: str, spec: str, line: int, extra: str = "") -> str:
        return java_trace(f"io.kotest.assertions.AssertionFailedError: {message}",
                          [f"ru.finplat.payhub.e2e.{spec}$1$1.invokeSuspend({spec}.kt:{line})",
                           "io.kotest.core.spec.style.scopes.FunSpecRootScope$test$1"
                           ".invokeSuspend(FunSpecRootScope.kt:84)"]) + extra

    def transfer(group: str, cause: str | None, category: str, title: str, message: str,
                 line: int, attachments: list[tuple[str, str, str]],
                 evidence: list[str], with_abs: bool = False) -> None:
        logs = list(attachments)
        if with_abs:
            logs.append(("abs-adapter.clef", abs_log, "text/plain"))
        b.add_failure(group, cause=cause, category=category, name=title,
                      full_name="ru.finplat.payhub.e2e.TransferSpec", message=message,
                      trace=kotest(message, "TransferSpec", line), step="Execute transfer",
                      log=payhub, log_name="payhub.log", logs=logs, evidence=evidence)

    transfer("account-arrested", "payer-account-arrested", "данные", "transfer from salary "
             "account", rejected, 41, [json_request(ops[1]["rq"]),
                                       json_response(ops[1], "REJECTED")],
             [f"payer account {ops[1]['payer']} is under arrest (ABS-4013)"])
    transfer("account-arrested", "payer-account-arrested", "данные", "transfer via SOAP",
             rejected, 58, [xml_request(ops[9]["rq"])],
             [f"payer account {ops[9]['payer']} is under arrest (ABS-4013)"])
    transfer("abs-timeout", "abs-create-payment-timeout", "окружение", "transfer to own card",
             rejected, 73, [json_request(ops[2]["rq"]), json_response(ops[2], "REJECTED")],
             ["ABS call createPayment timed out after 15000 ms"])
    transfer("abs-timeout", "abs-create-payment-timeout", "окружение", "transfer to deposit",
             rejected, 90, [json_response(ops[6], "REJECTED")],
             ["ABS call createPayment timed out after 15000 ms"])
    amount = 'expected:<"1500.00"> but was:<"1500.0">'
    transfer("amount-format", "test-amount-format", "тест", "transfer amount is echoed",
             amount, 104, [json_request(ops[7]["rq"]), json_response(ops[7], "EXECUTED")],
             [amount, f"Transfer executed, ABS document {ops[7]['doc']}"])
    transfer("op-not-in-log", None, "неизвестно", "transfer by phone number", rejected, 121,
             [json_request(_hex("rq-lost")), ("response.json", json.dumps(
                 {"status": 200, "headers": {"rqUID": _hex("rq-lost")},
                  "body": {"status": "REJECTED"}}, indent=2), "application/json")], [])
    failed = 'expected:<"EXECUTED"> but was:<"FAILED">'
    transfer("abs-limit-table", "abs-limit-table-missing", "окружение", "transfer above "
             "100 000", failed, 139, [json_request(ops[5]["rq"])],
             ["daily limit table LIM_DAY_0042 not found"], with_abs=True)
    transfer("abs-limit-table", "abs-limit-table-missing", "окружение", "transfer to "
             "another bank", failed, 152, [json_response(ops[8], "FAILED")],
             ["daily limit table LIM_DAY_0042 not found"], with_abs=True)

    sms = "SMS confirmation code was not delivered within 30 s"
    response = json.dumps({"status": 202, "headers": {"traceparent": traces[1]},
                           "body": {"state": "CODE_SENT"}}, indent=2)
    b.add_failure(
        "sms-sender", cause="sms-sender-not-registered", category="окружение",
        name="confirm transfer by SMS", full_name="ru.finplat.payhub.e2e.ConfirmationSpec",
        message=sms, trace=kotest(sms, "ConfirmationSpec", 37), step="Wait for SMS code",
        log=notify, log_name="notify-gw.log",
        logs=[("response.json", response, "application/json")],
        evidence=["sender name FINPLAT is not registered for route mts"])
    b.add_failure(
        "sms-sender", cause="sms-sender-not-registered", category="окружение",
        name="confirm card binding by SMS", full_name="ru.finplat.payhub.e2e.ConfirmationSpec",
        message=sms,
        trace=kotest(sms, "ConfirmationSpec", 55,
                     "Caused by: ru.finplat.e2e.support.SmsWaitTimeout: no SMS for "
                     f"traceparent={traces[3]} after 30 s\n"
                     "\tat ru.finplat.e2e.support.SmsInbox.await(SmsInbox.kt:48)\n"),
        step="Wait for SMS code", log=notify, log_name="notify-gw.log",
        logs=[("payhub.log", payhub, "text/plain")],
        evidence=["sender name FINPLAT is not registered for route mts"])

    return b.build("operation_logs_v3")


CASES: dict[str, Callable[[], Case]] = {
    "log_formats_v3": log_formats_v3,
    "log_traps_v3": log_traps_v3,
    "same_symptom_causes_v3": same_symptom_causes_v3,
    "hosts_locators_v3": hosts_locators_v3,
    "shared_assert_bg_v3": shared_assert_bg_v3,
    "one_problem_noisy_v3": one_problem_noisy_v3,
    "one_problem_resources_v3": one_problem_resources_v3,
    "signatures_kb_v3": signatures_kb_v3,
    "retries_v3": retries_v3,
    "retries_env_v3": retries_env_v3,
    "sparse_data_v3": sparse_data_v3,
    "operation_logs_v3": operation_logs_v3,
}
