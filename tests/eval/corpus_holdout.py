"""Отложенный корпус (holdout v4): только проверка, по нему ничего не подбирается.

Правило работы — README эталона («Правило работы с holdout»): сценарии не правятся под
результат, каждый просмотр результатов — запись в журнале. Launch id — 9101–9112 (выданный
диапазон 9101–9199).

Фреймворки тестов: Cucumber-JVM (Hamcrest, AssertJ), Gauge (Java и Python), Pester, Hurl,
Jasmine + Puppeteer, CodeceptJS, Behat/Mink, Reqnroll (FluentAssertions, Shouldly),
clojure.test, Catch2, GoogleTest, AVA, Flutter integration_test, Bats.

Форматы логов: Tarantool, журнал ошибок MySQL, hclog, bunyan, NLog, Loguru, Envoy (свой
журнал и журнал доступа), Elixir Logger (Phoenix), tinylog, NestJS Logger с ANSI-цветами,
spdlog, стандартный ``log`` Go, slf4j-simple, winston JSON, NATS, журнал ошибок SQL Server,
Jetty, MDC в квадратных скобках, JSON-записи с полями операции, ``ключ=значение``.
"""

from __future__ import annotations

from collections.abc import Callable

from eval.corpus import Case, LaunchBuilder, java_trace, noise_lines

HAMCREST_200_500 = "\nExpected: is <200>\n     but: was <500>"


def _cucumber_trace(exception: str, steps_class: str, method: str, line: int,
                    feature: str, step_text: str, feature_line: int) -> str:
    return java_trace(exception, [
        "org.hamcrest.MatcherAssert.assertThat(MatcherAssert.java:20)",
        f"{steps_class}.{method}({steps_class.rsplit('.', 1)[-1]}.java:{line})",
        f"✽.{step_text}(classpath:features/{feature}:{feature_line})",
    ])


# --- log_formats_v4 ------------------------------------------------------------------------


def _tarantool_log(i: int) -> str:
    sid = f"5e{i}c0a91f7d2"
    return (
        f"2026-10-27 02:13:5{i}.902 [4127] main/103/http.server I> "
        f"session lookup sid={sid} ttl=900\n"
        f"2026-10-27 02:13:5{i}.911 [4127] main/105/checkpoint_daemon I> "
        "scheduled next checkpoint for 2026-10-27 03:00:00\n"
        f"2026-10-27 02:14:0{i}.118 [4127] main/11{i}/http.server I> "
        f"POST /api/v1/sessions/{sid}/renew\n"
        f"2026-10-27 02:14:0{i}.120 [4127] main/11{i}/http.server E> "
        "ER_MEMORY_ISSUE: Failed to allocate 16384 bytes in slab allocator for memtx_tuple\n"
        f"2026-10-27 02:14:0{i}.121 [4127] main/11{i}/http.server W> "
        "renew failed, responding 500\n"
        f"2026-10-27 02:14:0{i}.530 [4127] main/104/gc I> wal/00000000000004127{i}.xlog removed\n"
    )


def _mysql_log(i: int) -> str:
    conn = 210 + i
    return (
        "2026-10-26T23:41:07.551204Z 0 [System] [MY-010116] [Server] /usr/sbin/mysqld "
        "(mysqld 8.0.36) starting as process 1\n"
        "2026-10-26T23:41:09.002118Z 0 [Warning] [MY-010068] [Server] CA certificate ca.pem "
        "is self signed.\n"
        "2026-10-26T23:41:09.310442Z 0 [System] [MY-010931] [Server] /usr/sbin/mysqld: ready "
        "for connections. Version: '8.0.36'  socket: '/var/run/mysqld/mysqld.sock'  port: 3306"
        "  MySQL Community Server - GPL.\n"
        f"2026-10-27T02:2{i}:13.118372Z {conn} [ERROR] [MY-012592] [InnoDB] Operating system "
        "error number 28 in a file operation.\n"
        f"2026-10-27T02:2{i}:13.118401Z {conn} [ERROR] [MY-012596] [InnoDB] Error number 28 "
        "means 'No space left on device'\n"
        f"2026-10-27T02:2{i}:13.118420Z {conn} [Warning] [MY-012145] [InnoDB] Error while "
        "writing 1048576 zeroes to ./payroll/bonus_accruals.ibd starting at offset 46137344\n"
        f"2026-10-27T02:2{i}:13.121009Z {conn} [ERROR] [MY-012888] [InnoDB] Cannot extend "
        "tablespace payroll/bonus_accruals by 64 pages\n"
    )


def _hclog_log(i: int) -> str:
    account = f"4081781057000012345{i}"
    return (
        "2026-10-27T02:31:40.004+0300 [INFO]  statement-render: starting worker pool: size=4\n"
        "2026-10-27T02:31:40.019+0300 [INFO]  statement-render.templates: loaded: count=12 "
        "dir=/opt/templates\n"
        f"2026-10-27T02:31:4{i}.901+0300 [INFO]  statement-render.http: request: method=GET "
        f"path=/v1/statements/{account}/pdf\n"
        f"2026-10-27T02:31:4{i}.902+0300 [ERROR] statement-render.pdf: render failed: "
        'template=stmt_v7.tmpl error="font PT-Sans-Regular.ttf not found in /opt/fonts"\n'
        f"2026-10-27T02:31:4{i}.903+0300 [INFO]  statement-render.http: response: status=500 "
        "duration=4.1ms\n"
    )


def _bunyan_log(i: int) -> str:
    doc = f"DF-11882{i}"
    host = '"hostname":"docflow-api-5f8b7c9d4-mx2kp","pid":1'
    return (
        f'{{"name":"docflow-api",{host},"level":30,"msg":"upload started",'
        f'"docId":"{doc}","size":184220,"time":"2026-10-27T02:40:1{i}.201Z","v":0}}\n'
        f'{{"name":"docflow-api",{host},"level":50,"err":{{"message":"The AWS Access Key Id '
        'you provided does not exist in our records.","name":"InvalidAccessKeyId",'
        '"stack":"InvalidAccessKeyId: The AWS Access Key Id you provided does not exist in '
        'our records.\\n    at throwDefaultError (/app/node_modules/@smithy/smithy-client/'
        'dist-cjs/index.js:867:20)","code":"InvalidAccessKeyId"},"msg":"storage put failed",'
        f'"docId":"{doc}","bucket":"docflow-ift2","time":"2026-10-27T02:40:1{i}.388Z","v":0}}\n'
        f'{{"name":"docflow-api",{host},"level":30,"msg":"document marked REJECTED",'
        f'"docId":"{doc}","time":"2026-10-27T02:40:1{i}.390Z","v":0}}\n'
    )


def _nlog_log(i: int) -> str:
    deposit = f"4230581040000007712{i}"
    return (
        f"2026-10-27 02:44:1{i}.1120|INFO|Deposits.Api.Controllers.AccrualController|"
        f"Accrual preview requested for deposit {deposit}\n"
        f"2026-10-27 02:44:1{i}.1188|WARN|Deposits.Api.Calendar.ProductionCalendar|"
        "Year 2026 is not loaded, daysInYear defaults to 0\n"
        f"2026-10-27 02:44:1{i}.1290|ERROR|Deposits.Api.Services.InterestService|"
        f"Interest accrual failed for deposit {deposit} System.DivideByZeroException: "
        "Attempted to divide by zero.\n"
        "   at System.Decimal.DecCalc.VarDecDiv(DecCalc& d1, DecCalc& d2)\n"
        "   at Deposits.Api.Services.InterestService.DailyRate(Decimal annualRate, Int32 "
        "daysInYear) in /src/Deposits.Api/Services/InterestService.cs:line 41\n"
        "   at Deposits.Api.Services.InterestService.Accrue(Deposit deposit, DateOnly date) "
        "in /src/Deposits.Api/Services/InterestService.cs:line 27\n"
        f"2026-10-27 02:44:1{i}.1302|INFO|Deposits.Api.Middleware.RequestLog|"
        "Request finished HTTP/1.1 POST /api/accruals/preview - 500\n"
    )


def _loguru_log(i: int) -> str:
    return (
        f"2026-10-27 02:51:0{i}.890 | INFO     | atm_locator.api:nearest:58 - "
        f"nearest requested lat=55.70{i} lon=37.52{i} radius=2000\n"
        f"2026-10-27 02:51:0{i}.897 | DEBUG    | atm_locator.cache:get:31 - cache miss "
        f"key=geo:55.70{i}:37.52{i}\n"
        f"2026-10-27 02:51:0{i}.904 | ERROR    | atm_locator.providers.geocoder:lookup:142 - "
        "Upstream geocoder rejected request: HTTP 429 Too Many Requests "
        "(quota 'free-tier' exhausted)\n"
        f"2026-10-27 02:51:0{i}.905 | WARNING  | atm_locator.api:nearest:71 - responding 502 "
        "to caller\n"
    )


def _envoy_log(i: int) -> str:
    conn, stream = 910 + i, 1490 + i
    tags = f'[Tags: "ConnectionId":"{conn}","StreamId":"{stream}"]'
    return (
        f"[2026-10-27 02:58:3{i}.509][27][debug][router] [source/common/router/router.cc:732]"
        f" {tags} cluster 'fx_pricer' match for URL '/v2/fx/rates'\n"
        f"[2026-10-27 02:58:3{i}.511][27][debug][router] [source/common/router/router.cc:1222]"
        f" {tags} upstream reset: reset reason: connection failure, transport failure reason:"
        " delayed connect error: 111\n"
        f"[2026-10-27 02:58:3{i}.511][27][debug][http] [source/common/http/conn_manager_impl.cc"
        f":1810] {tags} encoding headers via codec (end_stream=false):\n"
        "':status', '503'\n"
        f'[2026-10-27T02:58:3{i}.512Z] "GET /v2/fx/rates?base=RUB HTTP/1.1" 503 UF,URX 0 91 3 '
        f'- "10.80.3.1{i}" "fx-smoke/1.4" "5b1e0c1a-77d2-4c1e-9b0e-0a3f5c1d2e{i}f" '
        '"fx-pricer.ift2.bank.local" "10.81.6.20:8443"\n'
    )


def _phoenix_log(i: int) -> str:
    rid = f"F5k2LbQx9rT{i}"
    return (
        f"02:12:4{i}.201 request_id={rid} [info] POST /api/subscriptions\n"
        f"02:12:4{i}.236 request_id={rid} [error] ** (Postgrex.Error) ERROR 23503 "
        '(foreign_key_violation) insert or update on table "subscriptions" violates foreign '
        'key constraint "subscriptions_plan_id_fkey"\n'
        "\n"
        "    table: subscriptions\n"
        "    constraint: subscriptions_plan_id_fkey\n"
        "\n"
        'Key (plan_id)=(SMS_PLUS_2026) is not present in table "plans".\n'
        f"02:12:4{i}.237 request_id={rid} [info] Sent 500 in 36ms\n"
    )


def log_formats_v4() -> Case:
    b = LaunchBuilder(9101, "ift2 regress-nightly #3318", first_result_id=9_101_000)
    for i, name in enumerate(["Продление сессии клиента ИБ", "Продление сессии после смены ПИН"]):
        b.add_failure(
            "tarantool-memtx", cause="session-store-memtx-limit", category="окружение",
            name=name, full_name=f"Сессии ИБ: {name}", message=HAMCREST_200_500,
            trace=_cucumber_trace("java.lang.AssertionError: " + HAMCREST_200_500,
                                  "ru.bank.ib.steps.SessionSteps", "renewStatusIs", 64,
                                  "sessions.feature", "сервис сессий отвечает 200", 14 + i),
            step="Продлить сессию клиента", log=_tarantool_log(i), log_name="session-store.log",
            evidence=["Failed to allocate 16384 bytes in slab allocator for memtx_tuple"])
    for i, name in enumerate(["Начисление премии за квартал", "Начисление разовой премии"]):
        b.add_failure(
            "mysql-disk-full", cause="payroll-db-disk-full", category="окружение",
            name=name, message="Expected 201, but got 500.",
            trace=(f"at $resp.StatusCode | Should -Be 201, /builds/hr/payroll-tests/"
                   f"Bonus.Tests.ps1:{37 + 12 * i}\n"
                   f"at <ScriptBlock>, /builds/hr/payroll-tests/Bonus.Tests.ps1:{37 + 12 * i}"),
            step="Начислить премию сотруднику", log=_mysql_log(i), log_name="mysqld-error.log",
            evidence=["Error number 28 means 'No space left on device'"])
    for i, name in enumerate(["pdf выписки за месяц", "pdf выписки за квартал"]):
        b.add_failure(
            "hclog-font", cause="statement-font-missing", category="окружение",
            name=name, full_name=f"tests/statements/pdf.hurl: {name}",
            message=(f"error: Assert status code\n  --> tests/statements/pdf.hurl:{4 + 6 * i}:6\n"
                     f"   |\n {4 + 6 * i} | HTTP 200\n   |      ^^^ actual value is <500>\n   |"),
            step="GET /v1/statements/{account}/pdf", log=_hclog_log(i),
            log_name="statement-render.log",
            evidence=["font PT-Sans-Regular.ttf not found in /opt/fonts"])
    for i, name in enumerate(["загрузка скана договора", "загрузка доверенности"]):
        b.add_failure(
            "bunyan-s3-key", cause="docflow-s3-access-key-invalid", category="окружение",
            name=name, full_name=f"docflow upload {name}",
            message="Expected 'REJECTED' to equal 'STORED'.",
            trace=("Error: Expected 'REJECTED' to equal 'STORED'.\n    at <Jasmine>\n"
                   f"    at UserContext.<anonymous> (/e2e/specs/docflow/upload.spec.js:{58 + i}:35)"),
            step="Загрузить документ в архив", log=_bunyan_log(i), log_name="docflow-api.log",
            evidence=["The AWS Access Key Id you provided does not exist in our records."])
    for i, name in enumerate(["Предпросмотр начисления процентов",
                              "Предпросмотр капитализации"]):
        b.add_failure(
            "nlog-calendar", cause="production-calendar-2026-missing", category="данные",
            name=name,
            message=("Expected response.StatusCode to be HttpStatusCode.OK {value: 200}, but "
                     "found HttpStatusCode.InternalServerError {value: 500}."),
            trace=("   at FluentAssertions.Execution.LateBoundTestFramework.Throw(String message)\n"
                   "   at Deposits.Specs.Steps.AccrualSteps.ThenTheResponseIsOk() in "
                   f"/src/Deposits.Specs/Steps/AccrualSteps.cs:line {57 + 9 * i}"),
            step="Запросить предпросмотр начисления", log=_nlog_log(i),
            log_name="deposits-api.log",
            evidence=["Year 2026 is not loaded, daysInYear defaults to 0",
                      "System.DivideByZeroException: Attempted to divide by zero."])
    for i, test in enumerate(["nearest-atm-test", "nearest-atm-with-cash-in-test"]):
        b.add_failure(
            "loguru-quota", cause="geocoder-quota-exhausted", category="окружение",
            name=test, full_name=f"atm-locator.api-test/{test}",
            message=(f"FAIL in ({test}) (atm_locator_test.clj:{41 + 13 * i})\n"
                     "expected: (= 200 (:status resp))\n  actual: (not (= 200 502))"),
            step="GET /api/atm/nearest", log=_loguru_log(i), log_name="atm-locator.log",
            evidence=["Upstream geocoder rejected request: HTTP 429 Too Many Requests"])
    for i, name in enumerate(["FxGateway rates RUB", "FxGateway rates CNY"]):
        b.add_failure(
            "envoy-uf", cause="fx-pricer-down", category="окружение",
            name=name,
            message=(f"/src/tests/fx_gateway_smoke.cpp:{48 + 10 * i}: FAILED:\n"
                     "  REQUIRE( resp.status == 200 )\nwith expansion:\n  503 == 200"),
            step="Получить курсы через шлюз", log=_envoy_log(i), log_name="envoy.log",
            evidence=["upstream reset: reset reason: connection failure, transport failure "
                      "reason: delayed connect error: 111"])
    for i, name in enumerate(["Подключение SMS-информирования",
                              "Подключение SMS-информирования для карты MIR"]):
        b.add_failure(
            "phoenix-fk", cause="sms-plan-reference-missing", category="данные",
            name=name, message="Current response status code is 500, but 201 expected.",
            trace=("#0 /app/vendor/behat/mink/src/WebAssert.php(794): Behat\\Mink\\WebAssert->"
                   "assert(false, 'Current respons...')\n"
                   "#1 /app/vendor/behat/mink/src/WebAssert.php(120): Behat\\Mink\\WebAssert->"
                   "assertResponseStatus(201)\n"
                   f"#2 /app/features/bootstrap/SmsContext.php({88 + 4 * i}): "
                   "Behat\\Mink\\WebAssert->statusCodeEquals(201)"),
            step="Подключить пакет SMS-информирования", log=_phoenix_log(i),
            log_name="sms-info.log",
            evidence=['Key (plan_id)=(SMS_PLUS_2026) is not present in table "plans".'])
    return b.build("log_formats_v4")


# --- log_traps_v4 --------------------------------------------------------------------------

_LIMITS = "ru.bank.cardlimits"


def _tinylog(time: str, thread: str, level: str, where: str, text: str) -> str:
    return f"2026-10-27 03:05:{time} [{thread}] {level}: {_LIMITS}.{where} {text}\n"


def _limits_log(i: int) -> str:
    card = f"220220******44{17 + i}"
    traps = [
        _tinylog("10.880", "main", "INFO", "Bootstrap.start()",
                 "Dictionary loaded from https://dict.ift2.bank.local/api/v1/error-codes?lang=ru"
                 " (412 entries)"),
        _tinylog("11.002", "http-worker-3", "INFO", "web.AccessLog.log()",
                 "GET /api/v1/limits/errors/summary 200 errorCount=0 failed=0"),
        _tinylog("11.110", "http-worker-3", "INFO", "Validator.validate()",
                 "Validation passed: 0 errors, 0 warnings"),
        _tinylog("11.150", "rates-refresh", "WARN", "rates.RateClient.fetch()",
                 "attempt 1/3 failed, retrying in 200 ms"),
        "java.net.SocketTimeoutException: Connect timed out\n"
        "\tat java.base/sun.nio.ch.NioSocketImpl.timedFinishConnect(NioSocketImpl.java:546)\n"
        f"\tat {_LIMITS}.rates.RateClient.fetch(RateClient.java:77)\n",
        _tinylog("11.380", "rates-refresh", "INFO", "rates.RateClient.fetch()",
                 "attempt 2/3 succeeded, 148 rates"),
    ]
    if i:
        traps = [
            _tinylog("10.870", "main", "INFO", "Bootstrap.start()",
                     "Feature flags: error-page-v2=on, strict-errors=off"),
            _tinylog("11.020", "http-worker-2", "INFO", "web.AccessLog.log()",
                     'GET /api/v1/error-log/export?from=2026-10-26 200 body={"lastError":null}'),
            _tinylog("11.240", "audit-flush", "WARN", "audit.AuditPublisher.flush()",
                     "publish failed, batch kept for next flush"),
            "java.io.IOException: Broken pipe\n"
            "\tat java.base/sun.nio.ch.SocketDispatcher.write0(Native Method)\n"
            f"\tat {_LIMITS}.audit.AuditPublisher.flush(AuditPublisher.java:93)\n",
            _tinylog("11.300", "audit-flush", "INFO", "audit.AuditPublisher.flush()",
                     "retry ok, 37 events published"),
        ]
    return "".join(traps) + "".join([
        _tinylog("11.402", f"http-worker-{7 + i}", "DEBUG", "cache.LimitCache.get()",
                 "cache miss handled, loading from DB"),
        f"{_LIMITS}.cache.CacheMissException: no entry for card {card}\n"
        f"\tat {_LIMITS}.cache.LimitCache.get(LimitCache.java:52)\n",
        _tinylog("11.415", f"http-worker-{7 + i}", "ERROR", "LimitService.check()",
                 f"Limit check failed for card {card}"),
        f"{_LIMITS}.rules.RuleConfigException: rule ATM_DAILY_WITHDRAWAL has no currency "
        "configured for product MIR_DEBIT_PREMIUM\n"
        f"\tat {_LIMITS}.rules.RuleEngine.resolve(RuleEngine.java:118)\n"
        f"\tat {_LIMITS}.LimitService.check(LimitService.java:64)\n",
        _tinylog("11.417", f"http-worker-{7 + i}", "INFO", "web.AccessLog.log()",
                 "POST /api/v1/card-limits/verify 500 errorCode=LIMIT_RULE_CONFIG"),
    ])


def _nest_ansi_log() -> str:
    def line(time: str, color: str, level: str, ctx: str, text: str) -> str:
        return (f"\x1b[{color}m[Nest] 31  - \x1b[39m10/27/2026, {time} AM \x1b[{color}m"
                f"{level:>7}\x1b[39m \x1b[33m[{ctx}]\x1b[39m \x1b[{color}m{text}\x1b[39m\n")
    return "".join([
        line("3:08:09", "32", "LOG", "NestFactory", "Starting Nest application..."),
        line("3:08:09", "33", "WARN", "ConfigService",
             "ENV ERROR_REPORTING_URL is not set, using default"),
        line("3:08:10", "32", "LOG", "RoutesResolver", "PayeeController {/api/payees}:"),
        line("3:08:10", "32", "LOG", "RouterExplorer", "Mapped {/api/payees/errors, GET} route"),
        line("3:08:14", "32", "LOG", "PayeeController", "create payee inn=7707083893 bic=?"),
        line("3:08:14", "31", "ERROR", "ExceptionsHandler",
             "Cannot read properties of undefined (reading 'bic')"),
        "TypeError: Cannot read properties of undefined (reading 'bic')\n"
        "    at PayeeService.resolveBank (/app/dist/payees/payee.service.js:88:41)\n"
        "    at PayeeService.create (/app/dist/payees/payee.service.js:41:32)\n"
        "    at PayeeController.create (/app/dist/payees/payee.controller.js:27:35)\n",
        line("3:08:15", "32", "LOG", "HealthController", "health ok, errors=0"),
    ])


def _crlf_log() -> str:
    lines = [
        "2026-10-27 03:09:01.4410 INFO  Payees.Api.Startup Errors endpoint: "
        "https://payees.ift2.bank.local/errors (problem+json)",
        "2026-10-27 03:09:02.0120 INFO  Payees.Api.Validation.PayeeValidator Payee validation:"
        " no errors for inn=7704123456",
        "2026-10-27 03:09:02.0340 ERROR Payees.Api.Data.PayeeRepository Insert payee failed "
        "Microsoft.Data.SqlClient.SqlException (0x80131904): Cannot insert duplicate key row "
        "in object 'dbo.Payees' with unique index 'IX_Payees_ClientId_Inn'. The duplicate key "
        "value is (100442, 7704123456).",
        "   at Microsoft.Data.SqlClient.SqlConnection.OnError(SqlException exception, Boolean "
        "breakConnection, Action`1 wrapCloseInAction)",
        "   at Payees.Api.Data.PayeeRepository.InsertAsync(Payee payee) in "
        "C:\\build\\src\\Payees.Api\\Data\\PayeeRepository.cs:line 58",
        "ClientConnectionId:5b0f7c1e-2a44-4b8e-9d1c-31e0a7c6f2d9",
        "Error Number:2601,State:1,Class:14",
        "2026-10-27 03:09:02.0360 INFO  Payees.Api.Middleware.RequestLog POST /api/payees "
        "responded 409 in 31 ms",
    ]
    return "\r\n".join(lines) + "\r\n"


def _spdlog_long(i: int) -> str:
    def ts(n: int) -> str:
        sec = n // 10
        return (f"[2026-10-27 03:{10 + sec // 60:02d}:{sec % 60:02d}."
                f"{(n % 10) * 100 + i:03d}] [quotes] ")

    def tick(n: int) -> str:
        if n % 400 == 7:
            return f"[warning] slow consumer risk-calc lag={900 + n % 300} msgs"
        return (f"[info] tick USD/RUB bid=96.{4100 + n % 97:04d} ask=96.{4160 + n % 89:04d} "
                f"seq={880000 + n} lat_us={40 + n % 23} channel=MOEX-FX-1")

    def after(n: int) -> str:
        return (f"[info] tick CNY/RUB bid=13.{2100 + n % 71:04d} ask=13.{2140 + n % 67:04d} "
                f"seq={510000 + n} lat_us={38 + n % 19} channel=MOEX-FX-2")

    head = noise_lines(ts, 1600, tick)
    middle = (
        f"{ts(1600)}[error] snapshot rejected: sequence gap 884120 -> 884127 on channel "
        "MOEX-FX-1\n"
        f"{ts(1601)}[critical] feed MOEX-FX-1 halted, quotes frozen at last snapshot\n"
        f"{ts(1602)}[info] quote state USD/RUB -> STALE\n"
    )
    tail = noise_lines(lambda n: ts(1603 + n), 1600, after)
    return head + middle + tail


def _settlement_attachments() -> list[tuple[str, str, str]]:
    exchange = (
        "POST https://clearing.ift2.bank.local/api/v1/batches HTTP/1.1\n"
        "Content-Type: application/json\n\n"
        '{"batchId":"B-20261027-031","items":42,"errors":[]}\n\n'
        "HTTP/1.1 202 Accepted\nContent-Type: application/json\n\n"
        '{"batchId":"B-20261027-031","status":"QUEUED","errorCode":null}\n\n'
        "GET https://clearing.ift2.bank.local/api/v1/batches/B-20261027-031 HTTP/1.1\n\n"
        "HTTP/1.1 200 OK\nContent-Type: application/json\n\n"
        '{"batchId":"B-20261027-031","status":"FAILED","errorCode":"DELIVERY"}\n'
    )
    access = (
        '[2026-10-27T03:15:20.114Z] "POST /api/v1/batches HTTP/1.1" 202 - 51 63 12 11 '
        '"10.80.3.40" "gauge-python/1.6" "c2f1a7e0-1b44-4f0e-8a9a-6d1e2b3c4d5e" '
        '"clearing.ift2.bank.local" "10.81.7.9:8080"\n'
        '[2026-10-27T03:15:24.530Z] "GET /api/v1/batches/B-20261027-031 HTTP/1.1" 200 - 0 70 '
        '4 3 "10.80.3.40" "gauge-python/1.6" "0e9d3b21-6c7a-4d2f-9f10-77a1c2b3d4e5" '
        '"clearing.ift2.bank.local" "10.81.7.9:8080"\n'
    )
    worker = (
        "2026/10/27 03:15:20 worker.go:97: batch B-20261027-031 accepted, 42 items\n"
        "2026/10/27 03:15:21 render.go:55: CLR_20261027_031.xml written, 18342 bytes\n"
        "2026/10/27 03:15:22 sftp.go:61: dial sftp-clearing.ift2.bank.local:22\n"
        "2026/10/27 03:15:22 worker.go:184: settle batch B-20261027-031 failed: sftp upload "
        "/outbox/CLR_20261027_031.xml: ssh: handshake failed: ssh: unable to authenticate, "
        "attempted methods [none publickey], no supported methods remain\n"
        "2026/10/27 03:15:22 worker.go:191: batch B-20261027-031 -> FAILED (DELIVERY)\n"
        "2026/10/27 03:15:30 janitor.go:40: removed 3 temp files\n"
    )
    return [("http-exchange.txt", exchange, "text/plain"),
            ("gateway-access.log", access, "text/plain"),
            ("settlement-worker.log", worker, "text/plain")]


def log_traps_v4() -> Case:
    b = LaunchBuilder(9102, "ift2 cards+payments regress #1207", first_result_id=9_102_000)
    for i, name in enumerate(["Снятие наличных MIR Premium в пределах лимита",
                              "Снятие наличных MIR Premium в другом городе"]):
        message = "[ATM withdrawal allowed] \nExpecting value to be true but was false"
        b.add_failure(
            "limits-rule-currency", cause="limit-rule-currency-missing", category="данные",
            name=name, message=message,
            trace=java_trace("java.lang.AssertionError: " + message, [
                "ru.bank.e2e.limits.LimitSteps.withdrawalAllowed(LimitSteps.java:73)",
                "com.thoughtworks.gauge.execution.MethodExecutor.execute(MethodExecutor.java:38)",
            ]),
            step="Проверить лимит на снятие наличных", log=_limits_log(i),
            log_name="card-limits.log",
            evidence=["rule ATM_DAILY_WITHDRAWAL has no currency configured for product "
                      "MIR_DEBIT_PREMIUM"])
    b.add_failure(
        "nest-ansi-bic", cause="payee-bank-lookup-undefined", category="приложение",
        name="payees › создание получателя по ИНН и БИК",
        message="Difference (- actual, + expected):\n\n- 500\n+ 201",
        trace="t.is(res.status, 201)\n    at file:///e2e/test/payees.test.js:34:4",
        step="POST /api/payees", log=_nest_ansi_log(), log_name="payees-gw.log",
        evidence=["Cannot read properties of undefined (reading 'bic')",
                  "at PayeeService.resolveBank (/app/dist/payees/payee.service.js:88:41)"])
    b.add_failure(
        "crlf-duplicate-payee", cause="payee-test-data-duplicate", category="данные",
        name="Добавление получателя юрлица",
        message=("response.StatusCode\n    should be\nHttpStatusCode.Created\n    but was\n"
                 "HttpStatusCode.Conflict"),
        trace=("   at Payees.Specs.Steps.PayeeSteps.ThenPayeeIsCreated() in "
               "C:\\build\\tests\\Payees.Specs\\Steps\\PayeeSteps.cs:line 49"),
        step="Добавить получателя платежа", log=_crlf_log(), log_name="payees-api.log",
        evidence=["Cannot insert duplicate key row in object 'dbo.Payees' with unique index "
                  "'IX_Payees_ClientId_Inn'"])
    for i, name in enumerate(["QuotesIT.UsdRubQuoteIsLive", "QuotesIT.UsdRubSpreadWithinLimit"]):
        b.add_failure(
            "spdlog-long-feed-gap", cause="fx-feed-sequence-gap", category="окружение",
            name=name,
            message=(f"/src/tests/quotes_it.cc:{57 + 21 * i}: Failure\n"
                     "Expected equality of these values:\n  quote.state\n"
                     '    Which is: "STALE"\n  "LIVE"'),
            step="Дождаться котировки USD/RUB", log=_spdlog_long(i), log_name="quote-engine.log",
            evidence=["snapshot rejected: sequence gap 884120 -> 884127 on channel MOEX-FX-1",
                      "feed MOEX-FX-1 halted, quotes frozen at last snapshot"])
    b.add_failure(
        "sftp-auth-third-attachment", cause="clearing-sftp-key-rejected", category="окружение",
        name="Отправка клирингового пакета",
        message="AssertionError: settlement batch status: 'FAILED' != 'SENT'",
        trace=('  File "/e2e/step_impl/settlement.py", line 64, in wait_batch_sent\n'
               "    assert batch[\"status\"] == \"SENT\", f\"settlement batch status: "
               "{batch['status']!r} != 'SENT'\"\n"
               "AssertionError: settlement batch status: 'FAILED' != 'SENT'"),
        step="Дождаться отправки пакета", logs=_settlement_attachments(),
        evidence=["ssh: unable to authenticate, attempted methods [none publickey], no "
                  "supported methods remain"])
    return b.build("log_traps_v4")


# --- same_symptom_log_causes_v4 ------------------------------------------------------------

_OKHTTP_TIMEOUT = java_trace("java.net.SocketTimeoutException: timeout", [
    "okhttp3.internal.http2.Http2Stream$StreamTimeout.newTimeoutException(Http2Stream.kt:675)",
    "okhttp3.internal.http2.Http2Stream$StreamTimeout.exitAndThrowIfTimedOut(Http2Stream.kt:684)",
    "okhttp3.internal.http2.Http2Stream.takeHeaders(Http2Stream.kt:143)",
    "okhttp3.internal.connection.RealCall.execute(RealCall.kt:154)",
    "ru.bank.e2e.sbp.SbpClient.send(SbpClient.java:58)",
    "ru.bank.e2e.sbp.SbpSteps.sendTransfer(SbpSteps.java:41)",
])
_STATUS_MESSAGE = '[transfer status] \nexpected: "ACCEPTED"\n but was: "REJECTED"'
_STATUS_TRACE = java_trace("java.lang.AssertionError: " + _STATUS_MESSAGE, [
    "ru.bank.e2e.sbp.SbpSteps.statusIs(SbpSteps.java:77)",
    "com.thoughtworks.gauge.execution.MethodExecutor.execute(MethodExecutor.java:38)",
])


def _sbp_log(minute: int, tr: str, body: list[str]) -> str:
    head = (
        f"2026/10/27 04:{minute:02d}:01 health.go:22: GET /healthz 200\n"
        f"2026/10/27 04:{minute:02d}:05 config.go:61: limits reloaded, version 2026.10.3\n"
        f"2026/10/27 04:{minute:02d}:11 server.go:88: POST /api/v1/c2c/transfers from "
        f"10.80.3.52 id={tr}\n"
        f"2026/10/27 04:{minute:02d}:11 nspk.go:131: -> nspk-stub POST /api/c2c/transfer "
        f"id={tr}\n"
    )
    return head + "".join(f"2026/10/27 04:{minute:02d}:{line}\n" for line in body)


def same_symptom_log_causes_v4() -> Case:
    b = LaunchBuilder(9103, "ift2 sbp regress #884", first_result_id=9_103_000)
    step_send, step_status = "Отправить перевод по номеру телефона", "Проверить статус перевода"
    for i, name in enumerate(["Перевод 1500 ₽ клиенту другого банка",
                              "Перевод с карты MIR клиенту другого банка",
                              "Перевод себе в другой банк"]):
        tr = f"TR-77310{i}"
        b.add_failure(
            "sbp-timeout-nspk-slow", cause="nspk-stub-slow", category="окружение",
            name=name, message="java.net.SocketTimeoutException: timeout",
            trace=_OKHTTP_TIMEOUT, step=step_send, log_name="sbp-adapter.log",
            log=_sbp_log(2 + i, tr, [
                f"36 nspk.go:139: nspk-stub did not answer within 25s (POST /api/c2c/transfer "
                f"id={tr}), giving up",
                f"36 server.go:112: id={tr} responded 504 after 25004ms",
            ]),
            evidence=["nspk-stub did not answer within 25s"])
    for i, name in enumerate(["Перевод по QR-коду ТСП", "Перевод юрлицу по QR-коду"]):
        tr = f"TR-77320{i}"
        b.add_failure(
            "sbp-timeout-open-files", cause="sbp-adapter-fd-exhausted", category="окружение",
            name=name, message="java.net.SocketTimeoutException: timeout",
            trace=_OKHTTP_TIMEOUT, step=step_send, log_name="sbp-adapter.log",
            log=_sbp_log(10 + i, tr, [
                "12 server.go:3285: http: Accept error: accept tcp [::]:8080: accept4: too many "
                "open files; retrying in 1s",
                "13 server.go:3285: http: Accept error: accept tcp [::]:8080: accept4: too many "
                "open files; retrying in 1s",
                "14 store.go:44: mssql: open conn: dial tcp 10.81.9.40:1433: socket: too many "
                "open files",
            ]),
            evidence=["accept4: too many open files"])
    for i, name in enumerate(["Повторный перевод по шаблону", "Перевод по шаблону с комиссией"]):
        tr = f"TR-77330{i}"
        b.add_failure(
            "sbp-timeout-db-lock", cause="transfers-db-lock-timeout", category="приложение",
            name=name, message="java.net.SocketTimeoutException: timeout",
            trace=_OKHTTP_TIMEOUT, step=step_send, log_name="sbp-adapter.log",
            log=_sbp_log(20 + i, tr, [
                f"12 store.go:69: begin tran save transfer id={tr}",
                f"42 store.go:73: save transfer id={tr}: mssql: Lock request time out period "
                "exceeded.",
                f"42 server.go:112: id={tr} responded 500 after 30011ms",
            ]),
            evidence=["mssql: Lock request time out period exceeded."])
    for i, name in enumerate(["Четвёртый перевод за пять минут", "Серия переводов на 1 ₽"]):
        tr = f"TR-77340{i}"
        b.add_failure(
            "sbp-rejected-antifraud", cause="antifraud-velocity-test-client", category="данные",
            name=name, message=_STATUS_MESSAGE, trace=_STATUS_TRACE, step=step_status,
            log_name="sbp-adapter.log",
            log=_sbp_log(30 + i, tr, [
                f"12 antifraud.go:52: id={tr} denied by antifraud: rule VELOCITY_P2P_5M "
                f"({4 + i} transfers in 5m, max 3)",
                f"12 server.go:112: id={tr} status REJECTED",
            ]),
            evidence=["denied by antifraud: rule VELOCITY_P2P_5M"])
    for i, (name, phone) in enumerate([("Перевод по номеру нового клиента", "+7900*****41"),
                                       ("Перевод по номеру из адресной книги", "+7916*****07")]):
        tr = f"TR-77350{i}"
        b.add_failure(
            "sbp-rejected-payee-unknown", cause="sbp-payee-not-registered", category="данные",
            name=name, message=_STATUS_MESSAGE, trace=_STATUS_TRACE, step=step_status,
            log_name="sbp-adapter.log",
            log=_sbp_log(40 + i, tr, [
                f"13 nspk.go:151: nspk-stub reject id={tr}: code=RJCT reason=AC03 creditor "
                f"{phone} is not registered in SBP",
                f"13 server.go:112: id={tr} status REJECTED",
            ]),
            evidence=["code=RJCT reason=AC03"])
    return b.build("same_symptom_log_causes_v4")


# --- hosts_and_locators_v4 -----------------------------------------------------------------


def _hc5_failure(host: str, ip: str, port: int) -> tuple[str, str]:
    message = f"Connect to http://{host}:{port} [{host}/{ip}] failed: Connection refused"
    trace = java_trace(f"org.apache.hc.client5.http.HttpHostConnectException: {message}", [
        "java.base/sun.nio.ch.Net.pollConnect(Native Method)",
        "org.apache.hc.client5.http.impl.io.DefaultHttpClientConnectionOperator.connect("
        "DefaultHttpClientConnectionOperator.java:158)",
        "org.apache.hc.client5.http.impl.classic.InternalHttpClient.doExecute("
        "InternalHttpClient.java:170)",
        "ru.bank.e2e.core.http.ServiceGateway.call(ServiceGateway.java:88)",
        "ru.bank.e2e.steps.CommonSteps.request(CommonSteps.java:41)",
    ])
    return message, trace


def _dotnet_refused(host: str) -> tuple[str, str]:
    message = f"System.Net.Http.HttpRequestException: Connection refused ({host}:443)"
    trace = (
        " ---> System.Net.Sockets.SocketException (111): Connection refused\n"
        "   at System.Net.Sockets.Socket.AwaitableSocketAsyncEventArgs.ThrowException("
        "SocketError error, CancellationToken cancellationToken)\n"
        "   at System.Net.Http.HttpConnectionPool.ConnectToTcpHostAsync(String host, Int32 port,"
        " HttpRequestMessage initialRequest, Boolean async, CancellationToken cancellationToken)"
        "\n   --- End of inner exception stack trace ---\n"
        "   at Bank.E2E.Core.Http.ApiClient.SendAsync(HttpRequestMessage request) in "
        "/src/Bank.E2E.Core/Http/ApiClient.cs:line 64\n"
        "   at Bank.E2E.Specs.Steps.ApiSteps.WhenISendRequest(String method, String path) in "
        "/src/Bank.E2E.Specs/Steps/ApiSteps.cs:line 31"
    )
    return message, trace


def _puppeteer_timeout(selector: str, spec: str, line: int) -> tuple[str, str]:
    message = (f"TimeoutError: Waiting for selector `{selector}` failed: Waiting failed: "
               "30000ms exceeded")
    trace = (
        f"{message}\n"
        "    at new WaitTask (/e2e/node_modules/puppeteer-core/lib/cjs/puppeteer/common/"
        "WaitTask.js:50:34)\n"
        "    at BasePage.waitVisible (/e2e/src/pages/base.page.ts:22:21)\n"
        f"    at UserContext.<anonymous> (/e2e/src/specs/{spec}:{line}:16)"
    )
    return message, trace


def _codecept_click(text: str, line: int) -> tuple[str, str]:
    message = f'Clickable element "{text}" was not found by text|CSS|XPath'
    trace = (
        f"Error: {message}\n"
        "    at findClickable (/e2e/node_modules/codeceptjs/lib/helper/Puppeteer.js:2771:11)\n"
        "    at BasePage.press (/e2e/pages/base.page.js:19:7)\n"
        f"    at Test.<anonymous> (/e2e/tests/cards_test.js:{line}:5)"
    )
    return message, trace


def hosts_and_locators_v4() -> Case:
    b = LaunchBuilder(9104, "ift2 smoke after deploy #57", first_result_id=9_104_000)
    java = [
        ("acct-ledger", "10.81.2.11", 8080, "Остатки по счетам клиента"),
        ("acct-ledger", "10.81.2.11", 8080, "Обороты по счёту за период"),
        ("client-profile", "10.81.2.27", 8443, "Профиль клиента по ИНН"),
        ("client-profile", "10.81.2.27", 8443, "Контакты клиента"),
        ("cashback-engine", "10.81.2.34", 8080, "Начисленный кешбэк за месяц"),
    ]
    for service, ip, port, name in java:
        message, trace = _hc5_failure(f"{service}.ift2.bank.local", ip, port)
        b.add_failure(
            f"hc5-{service}", cause=f"{service}-unreachable", category="окружение",
            name=name, message=message, trace=trace, step="Выполнить запрос к сервису",
            evidence=[message])
    for service, name in [("doc-archive", "Получение архивной справки"),
                          ("doc-archive", "Поиск документа по номеру"),
                          ("notary-gw", "Проверка доверенности у нотариуса")]:
        message, trace = _dotnet_refused(f"{service}.ift2.bank.local")
        b.add_failure(
            f"dotnet-{service}", cause=f"{service}-unreachable", category="окружение",
            name=name, message=message, trace=trace, step="Вызвать API через общий клиент",
            evidence=[f"Connection refused ({service}.ift2.bank.local:443)"])
    ui = [
        ("swift-iban", '[data-role="iban-input"]', "swift/payment.spec.ts", 31,
         "SWIFT-перевод в евро"),
        ("swift-iban", '[data-role="iban-input"]', "swift/payment.spec.ts", 58,
         "SWIFT-перевод в юанях"),
        ("deposit-close", "#deposit-close-confirm", "deposits/close.spec.ts", 24,
         "Досрочное закрытие вклада"),
        ("deposit-close", "#deposit-close-confirm", "deposits/close.spec.ts", 47,
         "Закрытие вклада в день окончания"),
        ("statement-period", ".statement-filter__period", "statements/filter.spec.ts", 19,
         "Фильтр выписки по периоду"),
    ]
    for key, selector, spec, line, name in ui:
        message, trace = _puppeteer_timeout(selector, spec, line)
        b.add_failure(
            f"pptr-{key}", cause=f"ui-{key}-not-rendered", category="неизвестно",
            name=name, message=message, trace=trace, step="Дождаться элемента на странице",
            evidence=[message])
    for key, text, line, name in [("card-reissue", "Перевыпустить карту", 12,
                                   "Перевыпуск карты при утере"),
                                  ("card-reissue", "Перевыпустить карту", 33,
                                   "Перевыпуск карты по сроку"),
                                  ("certificate-order", "Заказать справку", 54,
                                   "Справка о наличии карты")]:
        message, trace = _codecept_click(text, line)
        b.add_failure(
            f"codecept-{key}", cause=f"ui-{key}-button-missing", category="неизвестно",
            name=name, message=message, trace=trace, step="Нажать кнопку в разделе карт",
            evidence=[message])
    return b.build("hosts_and_locators_v4")


# --- shared_assert_background_v4 -----------------------------------------------------------

_CONVEYOR_MESSAGE = "\nexpected: 200\n but was: 500"


def _slf4j(time: str, thread: str, level: str, logger: str, text: str) -> str:
    return f"2026-10-27 05:{time} +0300 [{thread}] {level} {logger} - {text}\n"


_BACKGROUND_A = "".join([
    _slf4j("00:58:004", "metrics-publisher", "ERROR", "ru.bank.platform.metrics.StatsdPublisher",
           "send batch to metrics-relay.ift2.bank.local:8125 failed: "
           "java.net.PortUnreachableException: ICMP Port Unreachable"),
    _slf4j("00:59:410", "audit-sink-1", "ERROR", "ru.bank.platform.audit.AuditSink",
           "dropped 14 events: queue capacity 1000 reached"),
    _slf4j("01:00:002", "toggle-refresh", "ERROR", "ru.bank.platform.toggles.ToggleClient",
           "refresh failed: HTTP 503 from toggles.ift2.bank.local, keeping 41 cached flags"),
])
_BACKGROUND_B = "".join([
    _slf4j("00:57:120", "mail-outbox", "ERROR", "ru.bank.platform.mail.OutboxSender",
           "SMTP 421 4.7.0 Try again later (smtp-relay.ift2.bank.local)"),
    _slf4j("00:59:731", "cache-warmup", "ERROR", "ru.bank.conveyor.cache.BranchCache",
           "warmup skipped: branch directory returned 0 rows"),
])
_BACKGROUND_C = "".join([
    _slf4j("01:01:015", "sms-callback-2", "ERROR", "ru.bank.platform.sms.CallbackHandler",
           "unknown delivery report id=DR-55120871, ignored"),
    _slf4j("01:01:240", "scheduler-1", "ERROR", "ru.bank.conveyor.jobs.ArchiveJob",
           "archive of applications older than 180 days failed: share //fs-archive/credit "
           "is not mounted"),
])


def _conveyor_log(background: str, app: str, body: str) -> str:
    start = _slf4j("01:12:101", "http-nio-8080-exec-3", "INFO",
                   "ru.bank.conveyor.web.ApplicationController",
                   f"POST /api/v2/applications app={app}")
    return background + start + body + _slf4j(
        "01:12:950", "http-nio-8080-exec-3", "INFO", "ru.bank.conveyor.web.ApplicationController",
        f"app={app} responded 500")


def shared_assert_background_v4() -> Case:
    b = LaunchBuilder(9105, "ift2 credit-conveyor regress #412", first_result_id=9_105_000)
    trace = java_trace("java.lang.AssertionError: " + _CONVEYOR_MESSAGE, [
        "ru.bank.e2e.credit.ApplicationSteps.statusIs(ApplicationSteps.java:58)",
        "✽.заявка принята со статусом 200(classpath:features/credit/application.feature:21)",
    ])
    step = "Подать кредитную заявку"
    for i, name in enumerate(["Кредит наличными зарплатному клиенту",
                              "Кредит наличными с поручителем",
                              "Рефинансирование кредита другого банка"]):
        app = f"CA-2026-1041{i}"
        body = (_slf4j("01:12:640", "http-nio-8080-exec-3", "ERROR",
                       "ru.bank.conveyor.steps.BureauStep", f"bureau call failed app={app}")
                + "javax.net.ssl.SSLHandshakeException: PKIX path validation failed: "
                "java.security.cert.CertPathValidatorException: validity check failed\n"
                "\tat java.base/sun.security.ssl.Alert.createSSLException(Alert.java:130)\n"
                "\tat ru.bank.conveyor.bureau.BureauClient.report(BureauClient.java:97)\n"
                "Caused by: java.security.cert.CertificateExpiredException: NotAfter: "
                "Mon Oct 26 23:59:59 MSK 2026\n")
        b.add_failure(
            "conveyor-bureau-cert", cause="bureau-client-cert-expired", category="окружение",
            name=name, message=_CONVEYOR_MESSAGE, trace=trace, step=step,
            log=_conveyor_log(_BACKGROUND_A, app, body), log_name="credit-conveyor.log",
            evidence=["CertificateExpiredException: NotAfter: Mon Oct 26 23:59:59 MSK 2026"])
    for i, (name, street) in enumerate([
            ("Заявка клиента с длинным адресом", "Проспект Маршала Жукова, д. 7"),
            ("Заявка клиента из Подмосковья", "Посёлок Вешки, Липкинское шоссе")]):
        app = f"CA-2026-1042{i}"
        body = (_slf4j("01:12:480", "http-nio-8080-exec-3", "ERROR",
                       "ru.bank.conveyor.store.ApplicantStore", f"save applicant failed app={app}")
                + "com.microsoft.sqlserver.jdbc.SQLServerException: String or binary data would "
                "be truncated in table 'credit.dbo.applicant_addresses', column 'street'. "
                f"Truncated value: '{street[:21]}'.\n"
                "\tat com.microsoft.sqlserver.jdbc.SQLServerException.makeFromDatabaseError("
                "SQLServerException.java:265)\n"
                "\tat ru.bank.conveyor.store.ApplicantStore.save(ApplicantStore.java:71)\n")
        b.add_failure(
            "conveyor-street-truncated", cause="applicant-street-column-too-short",
            category="приложение", name=name, message=_CONVEYOR_MESSAGE, trace=trace, step=step,
            log=_conveyor_log(_BACKGROUND_A, app, body), log_name="credit-conveyor.log",
            evidence=["String or binary data would be truncated in table "
                      "'credit.dbo.applicant_addresses', column 'street'"])
    for i, name in enumerate(["Кредит наличными на 24 месяца", "Кредит наличными на 24 месяца "
                              "с пониженной ставкой"]):
        app = f"CA-2026-1043{i}"
        body = _slf4j("01:12:330", "http-nio-8080-exec-3", "ERROR",
                      "ru.bank.conveyor.product.ProductResolver",
                      f"app={app}: no active product for code CONS_CASH_24 on 2026-10-27")
        b.add_failure(
            "conveyor-product-missing", cause="cons-cash-24-product-inactive", category="данные",
            name=name, message=_CONVEYOR_MESSAGE, trace=trace, step=step,
            log=_conveyor_log(_BACKGROUND_B, app, body), log_name="credit-conveyor.log",
            evidence=["no active product for code CONS_CASH_24 on 2026-10-27"])
    for i, (name, background) in enumerate([
            ("Автокредит с залогом", _BACKGROUND_A),
            ("Ипотека на вторичное жильё", _BACKGROUND_B + _BACKGROUND_C)]):
        app = f"CA-2026-1044{i}"
        body = (_slf4j("01:12:801", "http-nio-8080-exec-3", "ERROR",
                       "ru.bank.conveyor.steps.CollateralStep",
                       f"app={app}: valuation-svc timed out after 15000 ms "
                       "(POST /v1/valuations)"))
        b.add_failure(
            "conveyor-valuation-timeout", cause="valuation-svc-timeout", category="окружение",
            name=name, message=_CONVEYOR_MESSAGE, trace=trace, step=step,
            log=_conveyor_log(background, app, body), log_name="credit-conveyor.log",
            evidence=["valuation-svc timed out after 15000 ms"])
    return b.build("shared_assert_background_v4")


# --- one_problem_noisy_v4 ------------------------------------------------------------------

_ES_BLOCK = ("index [person-registry-v3] blocked by: [TOO_MANY_REQUESTS/12/disk usage exceeded "
             "flood-stage watermark, index has read-only-allow-delete block]")


def _winston(level: str, message: str, ts: str, **fields: str) -> str:
    extra = "".join(f',"{key}":"{value}"' for key, value in fields.items())
    return f'{{"level":"{level}","message":"{message}"{extra},"timestamp":"{ts}"}}\n'


def _registry_log(pod: str, rid: str, minute: int, route: str, frame: str,
                  noise: list[str]) -> str:
    ts = f"2026-10-27T05:{minute:02d}"
    error = (f"ResponseError: cluster_block_exception: [cluster_block_exception] Reason: "
             f"{_ES_BLOCK};")
    stack = (f"{error}\\n    at SniffingTransport.request (/app/node_modules/@elastic/transport/"
             f"lib/Transport.js:479:27)\\n    at async {frame}")
    return "".join([
        _winston("info", "person-registry listening on :3000", f"{ts}:00.004Z", pod=pod),
        *noise,
        _winston("info", route, f"{ts}:11.104Z", requestId=rid, pod=pod),
        _winston("error", "person update failed", f"{ts}:11.161Z", requestId=rid, pod=pod,
                 error=error, stack=stack),
        _winston("info", f"{route} 500 57ms", f"{ts}:11.162Z", requestId=rid, pod=pod),
    ])


def one_problem_noisy_v4() -> Case:
    b = LaunchBuilder(9106, "ift2 person-registry regress #95", first_result_id=9_106_000)
    cause, category = "person-index-read-only", "окружение"
    noise_a = [
        _winston("error", "exchange rates cache refresh failed: socket hang up",
                 "2026-10-27T05:11:02.310Z", pod="person-registry-7c9d8b5f6-k2x8q"),
        _winston("warn", "slow query on persons_by_phone took 1840ms",
                 "2026-10-27T05:11:05.002Z", pod="person-registry-7c9d8b5f6-k2x8q"),
    ]
    noise_b = [
        _winston("error", "geo-ip lookup failed: connect ETIMEDOUT 10.81.4.90:8080",
                 "2026-10-27T05:14:07.881Z", pod="person-registry-7c9d8b5f6-q7w2n"),
    ]
    noise_c = [
        _winston("error", "audit publish failed: 503 Service Unavailable",
                 "2026-10-27T05:17:01.551Z", pod="person-registry-7c9d8b5f6-k2x8q"),
        _winston("error", "exchange rates cache refresh failed: socket hang up",
                 "2026-10-27T05:17:02.310Z", pod="person-registry-7c9d8b5f6-k2x8q"),
    ]
    ava = "Difference (- actual, + expected):\n\n- 500\n+ 200"
    for name, line, log in [
        ("registry › смена фамилии клиента", 41,
         _registry_log("person-registry-7c9d8b5f6-k2x8q", "6f1c2a90-1d0e-4b7a-9c55-0e2f4a6b8c01",
                       11, "PATCH /api/persons/P-0041822",
                       "PersonService.update (/app/dist/persons/person.service.js:212:19)",
                       noise_a)),
        ("registry › смена телефона клиента", 67,
         _registry_log("person-registry-7c9d8b5f6-q7w2n", "a3d9e1f7-55b2-4c0e-8f1a-9b7c6d5e4f32",
                       14, "PATCH /api/persons/P-0041907",
                       "PersonService.update (/app/dist/persons/person.service.js:212:19)",
                       noise_b)),
        ("registry › смена адреса регистрации", 93, None),
    ]:
        b.add_failure(
            "registry-ava-500", cause=cause, category=category, name=name,
            message=ava, trace=f"t.is(res.status, 200)\n    at file:///e2e/test/registry/"
                               f"update.test.js:{line}:5",
            step="PATCH /api/persons/:id", log=log, log_name="person-registry.log",
            evidence=[_ES_BLOCK])
    for i, (name, rid, frame) in enumerate([
            ("Обновление паспортных данных клиента", "0b7e4c2d-9a18-4f63-b5e0-c1d2e3f4a5b6",
             "PersonService.updateDocument (/app/dist/persons/person.service.js:247:19)"),
            ("Обновление СНИЛС клиента", "e8f9a0b1-c2d3-4e5f-8a9b-0c1d2e3f4a5b",
             "PersonService.updateDocument (/app/dist/persons/person.service.js:247:19)")]):
        b.add_failure(
            "registry-retrofit-500", cause=cause, category=category, name=name,
            message="retrofit2.HttpException: HTTP 500 Internal Server Error",
            trace=java_trace("retrofit2.HttpException: HTTP 500 Internal Server Error", [
                "retrofit2.KotlinExtensions$await$2$2.onResponse(KotlinExtensions.kt:53)",
                "retrofit2.OkHttpCall$1.onResponse(OkHttpCall.java:161)",
                f"ru.bank.e2e.registry.RegistryApi.updateDocument(RegistryApi.kt:{44 + i})",
            ]),
            step="Обновить документ клиента",
            log=_registry_log("person-registry-7c9d8b5f6-k2x8q", rid, 17 + i,
                              "PUT /api/persons/P-00420" + str(15 + i) + "/documents", frame,
                              noise_c if i == 0 else noise_a),
            log_name="person-registry.log", evidence=[_ES_BLOCK])
    for i, (name, key) in enumerate([("Редактирование профиля: аватар", "profile_saved_banner"),
                                     ("Редактирование профиля: email", "profile_saved_banner")]):
        b.add_failure(
            "registry-flutter-banner", cause=cause, category=category, name=name,
            message=("Expected: exactly one matching candidate\n  Actual: _KeyWidgetFinder:"
                     f"<Found 0 widgets with key [<'{key}'>]: []>\n"
                     "   Which: means none were found but one was expected"),
            trace=("package:flutter_test/src/expect.dart 149:31     expect\n"
                   f"integration_test/profile_edit_test.dart {58 + 23 * i}:7  main.<fn>.<fn>"),
            step="Сохранить профиль в приложении",
            log=_registry_log("person-registry-7c9d8b5f6-q7w2n",
                              f"5c4b3a29-{i}e1d-4c0b-9a8f-7e6d5c4b3a2{i}", 20 + i,
                              "PATCH /api/persons/me",
                              "PersonService.patchSelf (/app/dist/persons/person.service.js:"
                              "305:11)", noise_b),
            log_name="person-registry.log", evidence=[_ES_BLOCK])
    return b.build("one_problem_noisy_v4")


# --- one_problem_resources_v4 --------------------------------------------------------------

_IB_ACCESS_LOG = "".join(
    f'[2026-10-27T05:40:{sec:02d}.{ms:03d}Z] "GET {path} HTTP/1.1" {status} - 0 {size} 2 1 '
    f'"10.80.3.61" "Mozilla/5.0 (X11; Linux x86_64) HeadlessChrome/129.0.0.0" '
    f'"{rid}" "ib-web.ift2.bank.local" "10.81.8.12:8080"\n'
    for sec, ms, path, status, size, rid in [
        (1, 102, "/", 200, 1843, "71c0d2e4-0a1b-4c2d-8e3f-5a6b7c8d9e01"),
        (1, 240, "/assets/app.3f9c2e1b.js", 404, 0, "71c0d2e4-0a1b-4c2d-8e3f-5a6b7c8d9e02"),
        (1, 241, "/assets/app.3f9c2e1b.css", 200, 48211, "71c0d2e4-0a1b-4c2d-8e3f-5a6b7c8d9e03"),
        (1, 260, "/assets/vendor.81d07a44.js", 200, 712004, "71c0d2e4-0a1b-4c2d-8e3f-5a6b7c8d9e04"),
        (19, 7, "/", 200, 1843, "71c0d2e4-0a1b-4c2d-8e3f-5a6b7c8d9e05"),
        (19, 131, "/assets/app.3f9c2e1b.js", 404, 0, "71c0d2e4-0a1b-4c2d-8e3f-5a6b7c8d9e06"),
        (19, 133, "/favicon.ico", 200, 4286, "71c0d2e4-0a1b-4c2d-8e3f-5a6b7c8d9e07"),
        (37, 552, "/", 200, 1843, "71c0d2e4-0a1b-4c2d-8e3f-5a6b7c8d9e08"),
        (37, 690, "/assets/app.3f9c2e1b.js", 404, 0, "71c0d2e4-0a1b-4c2d-8e3f-5a6b7c8d9e09"),
    ]
)


def one_problem_resources_v4() -> Case:
    b = LaunchBuilder(9107, "ift2 daily regress #2210", first_result_id=9_107_000)
    for i, (name, pod) in enumerate([("Выпуск виртуальной карты", "card-issuing-6b8d5f7c9-4kq2m"),
                                     ("Выпуск карты с индивидуальным дизайном",
                                      "card-issuing-6b8d5f7c9-9xv7t"),
                                     ("Выпуск дополнительной карты", "card-issuing-6b8d5f7c9-tq5wd")]):
        message = (f"Card issue failed: 500 from {pod}: "
                   '{"code":"HSM_SESSION_LOST","message":"HSM session is not established"}')
        b.add_failure(
            "card-issuing-hsm", cause="card-issuing-hsm-session-lost", category="окружение",
            name=name, message=message,
            trace=java_trace(f"ru.bank.e2e.cards.CardApiException: {message}", [
                "ru.bank.e2e.cards.CardApi.issue(CardApi.java:71)",
                f"ru.bank.e2e.cards.IssueSteps.issueCard(IssueSteps.java:{33 + 11 * i})",
            ]),
            step="Выпустить карту", evidence=["HSM session is not established"])
    for i, (name, user) in enumerate([("Вход в ИБ по логину и паролю", "qa.retail.017"),
                                      ("Вход в ИБ после смены устройства", "qa.retail.023"),
                                      ("Вход премиального клиента", "qa.premium.004")]):
        message = (f"Authentication rejected for test user {user}: PASSWORD_EXPIRED (password "
                   "changed 2026-07-27, max age 90 days)")
        b.add_failure(
            "test-users-expired", cause="test-users-password-expired", category="данные",
            name=name, message=message,
            trace=(f"Error: {message}\n    at AuthHelper.login (/e2e/support/auth.helper.js:37:13)\n"
                   f"    at UserContext.<anonymous> (/e2e/specs/login/login.spec.js:{12 + 9 * i}:5)"),
            step="Авторизоваться тестовым пользователем",
            evidence=["PASSWORD_EXPIRED (password changed 2026-07-27, max age 90 days)"])
    dns = [
        ("tests/smoke/fx-rates.hurl", "error: HTTP connection\n  --> tests/smoke/fx-rates.hurl:1:5\n"
         "   |\n 1 | GET https://fx-rates.ift2.bank.local/v1/rates?base=RUB\n"
         "   |     ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^ (6) Could not resolve host: "
         "fx-rates.ift2.bank.local\n   |", None, "GET /v1/rates"),
        ("smoke: branch locator responds", "(in test file test/smoke/branches.bats, line 14)\n"
         "  `[ \"$status\" -eq 0 ]' failed\n"
         "curl: (6) Could not resolve host: branch-locator.ift2.bank.local", None,
         "curl branch-locator /v1/branches"),
        ("Cards API health", "System.Net.Http.HttpRequestException: Name or service not known "
         "(cards-api.ift2.bank.local:443)",
         " ---> System.Net.Sockets.SocketException (0xFFFDFFFF): Name or service not known\n"
         "   at System.Net.Http.HttpConnectionPool.ConnectToTcpHostAsync(String host, Int32 port,"
         " HttpRequestMessage initialRequest, Boolean async, CancellationToken cancellationToken)"
         "\n   at Bank.Smoke.Specs.Steps.HealthSteps.WhenIRequestHealth(String service) in "
         "/src/Bank.Smoke.Specs/Steps/HealthSteps.cs:line 22", "Запросить health сервиса"),
        ("Cashdesk.Smoke: касса отвечает", "No such host is known. (cashdesk.ift2.bank.local:443)",
         "at Invoke-RestMethod -Uri \"https://cashdesk.ift2.bank.local/api/health\", "
         "C:\\build\\smoke\\Cashdesk.Tests.ps1:9\nat <ScriptBlock>, "
         "C:\\build\\smoke\\Cashdesk.Tests.ps1:9", "Invoke-RestMethod /api/health"),
    ]
    for name, message, trace, step in dns:
        b.add_failure(
            "stand-dns", cause="stand-dns-failure", category="окружение", name=name,
            message=message, trace=trace, step=step,
            evidence=["Could not resolve host: fx-rates.ift2.bank.local"])
    for i, (name, selector) in enumerate([("Главная страница ИБ: список счетов", ".accounts-list"),
                                          ("Главная страница ИБ: быстрый перевод",
                                           "#quick-transfer"),
                                          ("Главная страница ИБ: витрина предложений",
                                           ".offers-carousel")]):
        message = f'Element matching css "{selector}" not found.'
        b.add_failure(
            "ib-main-page-blank", cause="ib-web-bundle-missing", category="приложение",
            name=name, message=message,
            trace=(f"Behat\\Mink\\Exception\\ElementNotFoundException: {message}\n"
                   "#0 /app/vendor/behat/mink/src/WebAssert.php(519): Behat\\Mink\\WebAssert->"
                   "elementExists('css', '" + selector + "')\n"
                   f"#1 /app/features/bootstrap/MainPageContext.php({40 + 7 * i}): "
                   "Behat\\Mink\\WebAssert->elementExists('css', '" + selector + "')"),
            step="Открыть главную страницу интернет-банка", log=_IB_ACCESS_LOG,
            log_name="ib-web-envoy.log",
            evidence=['"GET /assets/app.3f9c2e1b.js HTTP/1.1" 404'])
    return b.build("one_problem_resources_v4")


# --- signatures_and_kb_v4 ------------------------------------------------------------------

_FIX_SESSION = "FIX.4.4:BRK1->MOEX"


def _fix_event(time: str, kind: str, text: str) -> str:
    return f"<20261027-{time}, {_FIX_SESSION}, {kind}> ({text})\n"


_FIX_GATEWAY_LOG = "".join([
    _fix_event("05:50:00", "event", f"Session {_FIX_SESSION} schedule is daily, "
               "07:00:00-UTC - 23:50:00-UTC"),
    _fix_event("05:50:00", "event", "Created session: " + _FIX_SESSION),
    *[line for minute in ("50", "51", "52", "53") for line in [
        _fix_event(f"05:{minute}:01", "event", "Initiated logon request"),
        _fix_event(f"05:{minute}:01", "outgoing", "8=FIX.4.4|9=71|35=A|34=17|49=BRK1|56=MOEX|"
                   "98=0|108=30|141=N|10=118|"),
        _fix_event(f"05:{minute}:01", "incoming", "8=FIX.4.4|9=104|35=5|34=1043|49=MOEX|"
                   "56=BRK1|58=MsgSeqNum too low, expecting 1042 but received 17|10=201|"),
        _fix_event(f"05:{minute}:01", "event", "Received logout request: MsgSeqNum too low, "
                   "expecting 1042 but received 17"),
        _fix_event(f"05:{minute}:01", "event", "Disconnecting: Logout received"),
    ]],
])


def _py_trace(path: str, line: int, func: str, code: str, error: str) -> str:
    return f'  File "{path}", line {line}, in {func}\n    {code}\n{error}'


def _rust_log(time: str, level: str, target: str, text: str) -> str:
    return f"2026-10-27T05:{time}Z {level:>5} {target}: {text}\n"


def _jetty(time: str, thread: str, text: str) -> str:
    return f"2026-10-27 05:{time}:WARN:oejs.HttpChannel:{thread}: {text}\n"


def signatures_and_kb_v4() -> Case:
    b = LaunchBuilder(9108, "ift2 broker regress #640", first_result_id=9_108_000)
    fix = [("fix-gateway.log", _FIX_GATEWAY_LOG, "text/plain")]
    filled = "AssertionError: order status: 'REJECTED' != 'FILLED'"
    filled_code = ("assert order[\"status\"] == \"FILLED\", "
                   "f\"order status: {order['status']!r} != 'FILLED'\"")
    fix_line = "MsgSeqNum too low, expecting 1042 but received 17"
    for i, name in enumerate(["Рыночная заявка на покупку SBER", "Рыночная заявка на продажу GAZP"]):
        b.add_failure(
            "broker-order-rejected-fix", cause="fix-session-seqnum-mismatch",
            category="окружение", name=name, message=filled,
            trace=_py_trace("/e2e/step_impl/orders.py", 48, "order_is_filled", filled_code,
                            filled),
            step="Выставить рыночную заявку", logs=fix, evidence=[fix_line])
    for i, (name, ticker) in enumerate([("Котировка SBER в стакане", "SBER"),
                                        ("Котировка LKOH в стакане", "LKOH")]):
        error = f"TimeoutError: no quote for {ticker} within 10 s"
        b.add_failure(
            "broker-quote-timeout-fix", cause="fix-session-seqnum-mismatch",
            category="окружение", name=name, message=error,
            trace=_py_trace("/e2e/step_impl/market_data.py", 31 + i, "wait_quote",
                            "raise TimeoutError(f\"no quote for {ticker} within {timeout} s\")",
                            error),
            step="Получить котировку инструмента", logs=fix, evidence=[fix_line])
    for i, name in enumerate(["Снятие лимитной заявки", "Снятие стоп-заявки"]):
        error = "AssertionError: cancel response: 409 != 200"
        b.add_failure(
            "broker-cancel-409-fix", cause="fix-session-seqnum-mismatch",
            category="окружение", name=name, message=error,
            trace=_py_trace("/e2e/step_impl/orders.py", 92, "cancel_order",
                            "assert resp.status_code == 200, f\"cancel response: "
                            "{resp.status_code} != 200\"", error),
            step="Снять заявку", logs=fix, evidence=[fix_line])
    for i, name in enumerate(["ГО по фьючерсу Si", "ГО по фьючерсу CNY"]):
        error = "AssertionError: initial margin: Decimal('0.00') != Decimal('15000.00')"
        b.add_failure(
            "risk-margin-zero", cause="risk-rates-snapshot-stale", category="данные",
            name=name, message=error,
            trace=_py_trace("/e2e/step_impl/risk.py", 57, "margin_is",
                            "assert margin == expected, f\"initial margin: {margin!r} != "
                            "{expected!r}\"", error),
            step="Рассчитать гарантийное обеспечение", log_name="risk-engine.log",
            log=(_rust_log(f"52:1{i}.004118", "INFO", "risk_engine::api",
                           f"margin request account=ACC-6610{i} instrument=Si-12.26")
                 + _rust_log(f"52:1{i}.004391", "WARN", "risk_engine::rates",
                             "fx snapshot is stale, falling back to zero coefficients age_h=26 "
                             "snapshot=2026-10-26T03:00:00Z")
                 + _rust_log(f"52:1{i}.004502", "INFO", "risk_engine::api",
                             f"margin computed account=ACC-6610{i} value=0.00")),
            evidence=["fx snapshot is stale, falling back to zero coefficients"])
    for i, name in enumerate(["Покупательная способность маржинального счёта",
                              "Покупательная способность после пополнения"]):
        error = "AssertionError: buying power: Decimal('0.00') != Decimal('250000.00')"
        b.add_failure(
            "risk-buying-power-zero", cause="risk-rates-snapshot-stale", category="данные",
            name=name, message=error,
            trace=_py_trace("/e2e/step_impl/risk.py", 81, "buying_power_is",
                            "assert power == expected, f\"buying power: {power!r} != "
                            "{expected!r}\"", error),
            step="Получить покупательную способность", log_name="risk-engine.log",
            log=(_rust_log(f"54:0{i}.118204", "INFO", "risk_engine::api",
                           f"buying power request account=ACC-6620{i}")
                 + _rust_log(f"54:0{i}.118377", "ERROR", "risk_engine::limits",
                             "buying power unavailable: rates snapshot 2026-10-26T03:00:00Z "
                             "expired (age 26h > 4h)")),
            evidence=["rates snapshot 2026-10-26T03:00:00Z expired (age 26h > 4h)"])
    for i, name in enumerate(["Рыночная заявка на ОФЗ 26238", "Заявка на ОФЗ 26238 с ИИС"]):
        order = f"ORD-90{41 + i}"
        b.add_failure(
            "broker-order-rejected-bond", cause="bond-not-tradable", category="данные",
            name=name, message=filled,
            trace=_py_trace("/e2e/step_impl/orders.py", 48, "order_is_filled", filled_code,
                            filled),
            step="Выставить рыночную заявку", log_name="broker-api.log",
            log=(_jetty(f"57:0{i}.104", "qtp1846274136-3" + str(i),
                        f"order {order} sent to exchange, instrument=SU26238RMFS4")
                 + f"2026-10-27 05:57:0{i}.311:INFO:r.b.b.ExecReportHandler:fix-in-1: order "
                 f"{order} rejected by exchange: 39=8 58=Instrument SU26238RMFS4 is not "
                 "available for trading in current session\n"),
            evidence=["Instrument SU26238RMFS4 is not available for trading in current session"])
    for i, account in enumerate(["ACC-77310", "ACC-77324"]):
        error = ("requests.exceptions.HTTPError: 502 Server Error: Bad Gateway for url: "
                 f"https://broker-api.ift2.bank.local/v1/portfolio/{account}")
        b.add_failure(
            "portfolio-502-oom", cause="portfolio-svc-heap-exhausted", category="приложение",
            name=f"Портфель клиента {account}", message=error,
            trace=_py_trace("/e2e/step_impl/portfolio.py", 22, "get_portfolio",
                            "resp.raise_for_status()", error),
            step="Получить портфель клиента", log_name="portfolio-svc.log",
            log=(_jetty(f"58:0{i}.118", f"qtp1846274136-4{i}",
                        f"handleException /v1/portfolio/{account} "
                        "java.lang.OutOfMemoryError: Java heap space")
                 + "java.lang.OutOfMemoryError: Java heap space\n"
                 "\tat java.base/java.util.Arrays.copyOf(Arrays.java:3537)\n"
                 "\tat ru.bank.broker.portfolio.PositionsAggregator.collect("
                 "PositionsAggregator.java:88)\n"),
            evidence=["java.lang.OutOfMemoryError: Java heap space"])
    for i, account in enumerate(["ACC-77410", "ACC-77455"]):
        error = ("requests.exceptions.HTTPError: 502 Server Error: Bad Gateway for url: "
                 f"https://broker-api.ift2.bank.local/v1/positions/{account}")
        b.add_failure(
            "positions-502-cache", cause="positions-cache-down", category="окружение",
            name=f"Позиции клиента {account}", message=error,
            trace=_py_trace("/e2e/step_impl/portfolio.py", 35, "get_positions",
                            "resp.raise_for_status()", error),
            step="Получить позиции клиента", log_name="positions-svc.log",
            log=(f"2026/10/27 05:59:1{i} server.go:71: GET /v1/positions/{account}\n"
                 f"2026/10/27 05:59:1{i} cache.go:52: tarantool positions-cache: dial tcp "
                 "10.81.9.71:3301: connect: connection refused\n"
                 f"2026/10/27 05:59:1{i} server.go:96: GET /v1/positions/{account} -> 502\n"),
            evidence=["tarantool positions-cache: dial tcp 10.81.9.71:3301: connect: "
                      "connection refused"])
    tax_error = "AssertionError: tax withheld: None != Decimal('1300.00')"
    tax_line = "no NDFL rate configured resident_status=NONRESIDENT_EAEU income_code=1530"
    tax_logs: list[tuple[str, list[tuple[str, str, str]]]] = [
        ("Купонный доход нерезидента ЕАЭС", [("tax-calc.log", (
            _rust_log("59:40.001002", "INFO", "tax_calc::server", "listening on [::]:8085")
            + _rust_log("59:41.220931", "INFO", "tax_calc::ndfl", "withhold deal=D-551021")
            + _rust_log("59:41.221344", "ERROR", "tax_calc::ndfl", f"{tax_line} deal=D-551021")),
            "text/plain")]),
        ("Дивиденды нерезидента ЕАЭС", [("tax-calc.log", (
            _rust_log("59:44.010443", "WARN", "tax_calc::cbr",
                      "key rate cache older than 1d, refreshing")
            + _rust_log("59:44.871203", "ERROR", "tax_calc::ndfl", f"{tax_line} deal=D-551088")
            + _rust_log("59:44.871399", "INFO", "tax_calc::server",
                        "POST /v1/ndfl/withhold 422 1ms")), "text/plain")]),
        ("Продажа облигаций нерезидентом ЕАЭС", [
            ("broker-api.log", _jetty("59:47.502", "qtp1846274136-52",
                                      "POST /v1/tax/withhold deal=D-551130 -> 422 from tax-calc"),
             "text/plain"),
            ("tax-calc.log", _rust_log("59:47.499870", "ERROR", "tax_calc::ndfl",
                                       f"{tax_line} deal=D-551130"), "text/plain")]),
    ]
    for name, logs in tax_logs:
        b.add_failure(
            "tax-ndfl-rate-missing", cause="ndfl-rate-missing-nonresident-eaeu",
            category="данные", name=name, message=tax_error,
            trace=_py_trace("/e2e/step_impl/tax.py", 64, "tax_withheld_is",
                            "assert tax == expected, f\"tax withheld: {tax!r} != "
                            "{expected!r}\"", tax_error),
            step="Проверить удержанный НДФЛ", logs=logs, evidence=[tax_line])
    return b.build("signatures_and_kb_v4")


# --- retries_v4, retries_env_v4 ------------------------------------------------------------

_RUN_START_MS = 1_793_080_800_000  # 2026-10-27 06:00 UTC


def _shouldly(expr: str, relation: str, expected: str, actual: str) -> str:
    return f"{expr}\n    {relation}\n{expected}\n    but was\n{actual}"


def _link(minute: int, *, history_id: str | None = None, history_key: str | None = None,
          test_case: int | None = None, params: dict[str, str] | None = None,
          env: dict[str, str] | None = None) -> dict[str, object]:
    extra: dict[str, object] = {"start": _RUN_START_MS + minute * 60_000}
    if history_id:
        extra["historyId"] = history_id
    if history_key:
        extra["historyKey"] = history_key
    if test_case is not None:
        extra["testCaseId"] = test_case
    if params is not None:
        extra["parameters"] = [{"name": key, "value": value} for key, value in params.items()]
    if env is not None:
        extra["environment"] = [{"name": key, "value": value} for key, value in env.items()]
    return extra


def _dotnet_frame(method: str, line: int) -> str:
    return (f"   at Bank.Retail.Specs.Steps.{method} in "
            f"/src/Bank.Retail.Specs/Steps/{method.split('.')[0]}.cs:line {line}")


def retries_v4() -> Case:
    b = LaunchBuilder(9109, "ift2 retail regress (retries=on) #771", first_result_id=9_109_000)
    env = {"stand": "ift2", "agent": "linux-runner-04"}

    # Прошёл после повтора.
    name = "Перевод между своими счетами"
    b.add_result(name=name, status="failed", hidden=True,
                 message="System.Net.Http.HttpRequestException: Connection refused "
                         "(transfers-gw.ift2.bank.local:443)",
                 extra=_link(1, history_id="7d1f0a9e3c5b4e21", test_case=60011, env=env))
    passed = b.add_result(name=name, status="passed",
                          extra=_link(3, history_id="7d1f0a9e3c5b4e21", test_case=60011, env=env))
    b.expect_passed_after_retry(passed)

    # Все попытки с той же ошибкой; связь только по historyKey.
    name = "Начисление кешбэка за покупки"
    login = ("Microsoft.Data.SqlClient.SqlException (0x80131904): Login failed for user "
             "'svc_cashback'.")
    attempts = [b.add_result(name=name, status="broken", hidden=True, message=login,
                             trace=_dotnet_frame("CashbackSteps.WhenAccrued()", 41),
                             extra=_link(2 + n, history_key="cashback.accrual.purchases"))
                for n in range(2)]
    final = b.add_failure(
        "cashback-db-login", cause="cashback-db-login-failed", category="окружение",
        name=name, status="broken", message=login,
        trace=_dotnet_frame("CashbackSteps.WhenAccrued()", 41), step="Начислить кешбэк",
        extra=_link(5, history_key="cashback.accrual.purchases"),
        evidence=["Login failed for user 'svc_cashback'."])
    b.expect_attempts(final, [(attempt, True) for attempt in attempts])

    # Попытка с другой ошибкой.
    name = "Выписка за год"
    timeout = ("System.Threading.Tasks.TaskCanceledException: The request was canceled due to "
               "the configured HttpClient.Timeout of 100 seconds elapsing.")
    attempt = b.add_result(name=name, status="broken", hidden=True, message=timeout,
                           extra=_link(4, history_id="2e9c7b1d0f6a4c88", test_case=60031,
                                       env=env))
    final = b.add_failure(
        "statement-empty", cause=None, name=name,
        message=_shouldly("statement.Lines.Count", "should be", "12", "0"),
        trace=_dotnet_frame("StatementSteps.ThenLinesCount(Int32 count)", 57),
        step="Проверить строки выписки",
        extra=_link(7, history_id="2e9c7b1d0f6a4c88", test_case=60031, env=env))
    b.expect_attempts(final, [(attempt, False)])

    # Параметризованный тест: связь только testCaseId + parameters + environment.
    rate_zero = _shouldly("rate.Value", "should be greater than", "0m", "0m")
    for n, currency in enumerate(["USD", "EUR"]):
        name = f"Курс конвертации [{currency}]"
        params = {"currency": currency}
        attempt = b.add_result(name=name, status="failed", hidden=True, message=rate_zero,
                               trace=_dotnet_frame("FxSteps.ThenRateIsPositive()", 33),
                               extra=_link(6 + n, test_case=60044, params=params, env=env))
        final = b.add_failure(
            "fx-rate-zero", cause=None, name=name, message=rate_zero,
            trace=_dotnet_frame("FxSteps.ThenRateIsPositive()", 33),
            step="Получить курс конвертации",
            extra=_link(9 + n, test_case=60044, params=params, env=env))
        b.expect_attempts(final, [(attempt, True)])

    # Ошибка попытки — только в ответе GET /api/testresult/{id}.
    name = "Выплата самозанятому"
    payout = _shouldly("payout.Status", "should be", '"Completed"', '"Failed"')
    attempt = b.add_result(name=name, status="failed", hidden=True,
                           extra=_link(8, history_id="b40c6e2a91d74f05", test_case=60052,
                                       env=env))
    b.add_detail(attempt, message=payout,
                 trace=_dotnet_frame("PayoutSteps.ThenPayoutCompleted()", 62))
    final = b.add_failure(
        "payout-failed", cause=None, name=name, message=payout,
        trace=_dotnet_frame("PayoutSteps.ThenPayoutCompleted()", 62),
        step="Дождаться выплаты",
        extra=_link(11, history_id="b40c6e2a91d74f05", test_case=60052, env=env))
    b.expect_attempts(final, [(attempt, True)])

    # Больше пяти попыток.
    name = "Выгрузка операций в 1С"
    ended = "System.IO.IOException: The response ended prematurely."
    attempts = [b.add_result(name=name, status="broken", hidden=True, message=ended,
                             trace=_dotnet_frame("ExportSteps.WhenExported()", 28),
                             extra=_link(10 + n, history_id="f61a3d8c2b0e4977",
                                         history_key="export.1c.operations", test_case=60063))
                for n in range(6)]
    final = b.add_failure(
        "export-1c-ended", cause=None, name=name, status="broken", message=ended,
        trace=_dotnet_frame("ExportSteps.WhenExported()", 28), step="Выгрузить операции",
        extra=_link(17, history_id="f61a3d8c2b0e4977", history_key="export.1c.operations",
                    test_case=60063))
    b.expect_attempts(final, [(attempt, True) for attempt in attempts])

    # Попытки без полей связи: связать их с финальным результатом нечем.
    name = "Лимиты по карте после перевыпуска"
    limits = _shouldly("card.Limits.Count", "should be", "3", "2")
    for n in range(2):
        b.add_result(name=name, status="failed", hidden=True, message=limits,
                     extra={"start": _RUN_START_MS + (12 + n) * 60_000})
    final = b.add_failure(
        "card-limits-count", cause=None, name=name, message=limits,
        trace=_dotnet_frame("CardSteps.ThenLimitsCount(Int32 count)", 75),
        step="Проверить лимиты карты",
        extra=_link(15, history_id="0c93e5a7d1b24f6e", test_case=60071, env=env))
    b.expect_attempts(final, [])

    for n, name in enumerate(["Открытие накопительного счёта", "Закрытие накопительного счёта"]):
        b.add_result(name=name, status="passed",
                     extra=_link(18 + n, history_id=f"5a8d2c{n}e7f9b1034", test_case=60080 + n,
                                 env=env))
    return b.build("retries_v4")


def _pester_strings(expected: str, actual: str) -> str:
    return ("Expected strings to be the same, but they were different.\n"
            f"Expected length: {len(expected)}\nActual length:   {len(actual)}\n"
            "Strings differ at index 0.\n"
            f"Expected: '{expected}'\nBut was:  '{actual}'")


def retries_env_v4() -> Case:
    b = LaunchBuilder(9110, "ift2 cashdesk cross-platform (retries=on) #58",
                      first_result_id=9_110_000)
    linux = {"os": "linux", "stand": "ift2"}
    windows = {"os": "windows", "stand": "ift2"}
    msk = {"region": "MSK"}

    name = "Оплата ЖКХ по QR [region=MSK]"
    pending = _pester_strings("PAID", "PENDING")
    trace = "at $payment.Status | Should -Be 'PAID', /builds/cashdesk/tests/QrPay.Tests.ps1:27"
    attempt = b.add_result(name=name, status="failed", hidden=True, message=pending, trace=trace,
                           extra=_link(1, test_case=50101, params=msk, env=linux))
    final_linux = b.add_failure(
        "qr-pay-pending", cause=None, name=name, message=pending, trace=trace,
        step="Оплатить квитанцию по QR", extra=_link(4, test_case=50101, params=msk, env=linux))
    b.expect_attempts(final_linux, [(attempt, True)])
    dns = "No such host is known. (qr-pay.ift2.bank.local:443)"
    final_windows = b.add_failure(
        "qr-pay-windows-dns", cause="windows-agent-dns-failure", category="окружение",
        name=name, message=dns,
        trace="at Invoke-RestMethod, C:\\build\\cashdesk\\tests\\QrPay.Tests.ps1:19",
        step="Оплатить квитанцию по QR",
        extra=_link(2, test_case=50101, params=msk, env=windows), evidence=[dns])
    b.expect_attempts(final_windows, [])

    rate_trace = "at $receipt.Rate | Should -Be {0}, /builds/cashdesk/tests/Receipt.Tests.ps1:44"
    usd = {"currency": "USD"}
    attempt = b.add_result(name="Курс в чеке [currency=USD]", status="failed", hidden=True,
                           message="Expected 96.41, but got 0.",
                           trace=rate_trace.format("96.41"),
                           extra=_link(3, test_case=50102, params=usd, env=linux))
    final = b.add_failure(
        "receipt-rate-null", cause=None, name="Курс в чеке [currency=USD]",
        message="Expected 96.41, but got $null.", trace=rate_trace.format("96.41"),
        step="Распечатать чек обмена валюты",
        extra=_link(6, test_case=50102, params=usd, env=linux))
    b.expect_attempts(final, [(attempt, False)])
    final = b.add_failure(
        "receipt-rate-null", cause=None, name="Курс в чеке [currency=CNY]",
        message="Expected 13.21, but got $null.", trace=rate_trace.format("13.21"),
        step="Распечатать чек обмена валюты",
        extra=_link(7, test_case=50102, params={"currency": "CNY"}, env=linux))
    b.expect_attempts(final, [])

    b.add_result(name="Печать чека возврата", status="failed", hidden=True,
                 message="Expected $true, but got $false.",
                 extra=_link(5, test_case=50103, params={}, env=linux))
    passed = b.add_result(name="Печать чека возврата", status="passed",
                          extra=_link(8, test_case=50103, params={}, env=linux))
    b.expect_passed_after_retry(passed)
    return b.build("retries_env_v4")


# --- sparse_data_v4 ------------------------------------------------------------------------


def _nats_log(i: int) -> str:
    port = 53122 + i
    return (
        "[1] 2026/10/27 06:20:01.004512 [INF] Starting nats-server\n"
        "[1] 2026/10/27 06:20:01.004601 [INF]   Version:  2.10.18\n"
        "[1] 2026/10/27 06:20:01.006112 [INF] Server is ready\n"
        f"[1] 2026/10/27 06:21:1{i}.551482 [ERR] 10.80.7.21:{port} - cid:11{8 + i} - "
        '"v1.36.0:go:statement-feed" - maximum payload exceeded: 1310720 vs 1048576\n'
        f"[1] 2026/10/27 06:21:1{i}.551530 [INF] 10.80.7.21:{port} - cid:11{8 + i} - "
        '"v1.36.0:go:statement-feed" - Client connection closed: Maximum Payload Violation\n'
    )


def sparse_data_v4() -> Case:
    b = LaunchBuilder(9111, "ift2 accounts regress #3020", first_result_id=9_111_000)
    b.add_failure("bare-close-account", cause=None, name="Закрытие счёта без остатка")
    b.add_failure("bare-arrest", cause=None, name="Наложение ареста на счёт", status="broken")
    for name in ["Сверка остатка после зачисления", "Сверка остатка после списания"]:
        b.add_failure("step-only-balance", cause=None, name=name,
                      step="Сверить остаток по счёту")
    b.add_failure("step-only-sign", cause=None, name="Подписание платёжного поручения",
                  step="Подписать платёжное поручение", status="broken")
    for i, name in enumerate(["Выписка в реальном времени", "Выписка по счёту юрлица онлайн"]):
        b.add_failure("log-only-nats", cause="statement-feed-oversized-messages",
                      category="приложение", name=name, log=_nats_log(i), log_name="nats.log",
                      evidence=["maximum payload exceeded: 1310720 vs 1048576"])
    b.add_result(name="Перевыпуск реквизитов счёта", status="failed", muted=True,
                 message="Expected: is <true>\n     but: was <false>")
    b.add_result(name="Смена тарифа РКО", status="broken", muted=True)
    b.add_result(name="Открытие валютного счёта", status="failed", hidden=True,
                 message="java.net.ConnectException: Connection refused")
    b.add_result(name="Открытие валютного счёта", status="passed")
    for name in ["Открытие текущего счёта", "Справка об остатке", "Реквизиты счёта в PDF"]:
        b.add_result(name=name, status="passed")
    b.add_result(name="Закрытие счёта с остатком", status="skipped")
    return b.build("sparse_data_v4")


# --- operation_logs_v4 ---------------------------------------------------------------------

_OPS = {  # тест → (rqUID, operUID)
    "t1": ("4f9c1d0a8e2b4c7f9a1b3c5d7e9f0a12", "a7c3e9f1b2d44e6a8c0f1e2d3c4b5a69"),
    "t2": ("5e0d2c1b9f3a4d8e8b2c4d6e8f0a1b23", "b8d4fa02c3e54f7b9d1a2e3f4d5c6b70"),
    "t3": ("6a1e3d2c0b4f4e9a9c3d5e7f9a1b2c34", "c9e50b13d4f64a8cae2b3f4a5e6d7c81"),
    "t4": ("7b2f4e3d1c5a4fab8d4e6f8a0b2c3d45", "d0f61c24e5a74b9dbf3c4a5b6f7e8d92"),
    "other": ("8c3a5f4e2d6b4abc9e5f7a9b1c3d4e56", "e1a72d35f6b84cae8a4d5b6c7a8f9ea3"),
    "t6": ("9d4b6a5f3e7c4bcd8f6a8b0c2d4e5f67", "f2b83e46a7c94dbf9b5e6c7d8b9a0fb4"),
    "t7": ("ae5c7b6a4f8d4cde9a7b9c1d3e5f6a78", "03c94f57b8da4ec0ac6f7d8e9cab1ac5"),
}
_OTHER_BRIDGE_OP = "14da5a68c9eb4fd1bd7a8e9fadbc2bd6"
_WALLET_OPS = {  # тест → (X-Request-ID / requestId, traceId)
    "t8": ("c1d2e3f4-a5b6-4c7d-8e9f-0a1b2c3d4e5f", "4bf92f3577b34da6a3ce929d0e0e4736"),
    "t9": ("d2e3f4a5-b6c7-4d8e-9fa0-1b2c3d4e5f60", "5ca03a4688c45eb7b4df030e1f1f5847"),
    "other": ("e3f4a5b6-c7d8-4e9f-a0b1-2c3d4e5f6071", "6db14b5799d56fc8c5e0141f2a2a6958"),
    "t10": ("f4a5b6c7-d8e9-4fa0-b1c2-3d4e5f607182", "7ec25c68aae670d9d6f1252a3b3b7a69"),
    "art": ("a5b6c7d8-e9fa-4ab1-c2d3-4e5f60718293", "8fd36d79bbf781eae7023630c4c48b7a"),
}


def _orch(time: str, thread: str, op: str | None, level: str, logger: str, text: str,
          *, oper_only: bool = False) -> str:
    if op is None:
        mdc = "[]"
    elif oper_only:
        mdc = f"[operUID={_OPS[op][1]}]"
    else:
        mdc = f"[rqUID={_OPS[op][0]} operUID={_OPS[op][1]}]"
    return f"2026-10-27 06:{time} [{thread}] {mdc} {level:<5} r.b.orch.{logger} - {text}\n"


_ORCHESTRATOR_LOG = "".join([
    _orch("39:58.002", "main", None, "INFO", "Application",
          "Started TransferOrchestrator in 12.4 seconds (process running for 13.1)"),
    _orch("40:00.010", "scheduling-1", None, "INFO", "rates.RateCacheRefresher",
          "refreshed 148 rates"),
    _orch("40:01.114", "exec-3", "t1", "INFO", "api.TransferEndpoint",
          "TransferRq accepted type=FX_TRANSFER amount=1000.00 USD"),
    _orch("40:01.120", "exec-5", "t3", "INFO", "api.TransferEndpoint",
          "TransferRq accepted type=OWN_ACCOUNTS amount=5000.00 RUB"),
    _orch("40:01.180", "exec-3", "t1", "INFO", "steps.FxStep", "requesting quote USD/RUB"),
    _orch("40:01.240", "exec-7", "t2", "INFO", "api.TransferEndpoint",
          "TransferRq accepted type=FX_TRANSFER amount=250.00 USD"),
    _orch("40:01.300", "exec-5", "t3", "INFO", "steps.AbsPostingStep",
          "posting sent to ESB.ABS.POSTING.RQ docNum=118840"),
    _orch("40:01.402", "exec-3", "t1", "ERROR", "steps.FxStep",
          "conversion rate is not fixed for pair USD/RUB: quote Q-5512 expired at 06:40:01.350"),
    _orch("40:01.403", "exec-3", "t1", "INFO", "api.TransferEndpoint", "TransferRs status=ERROR"),
    _orch("40:01.510", "exec-7", "t2", "ERROR", "steps.FxStep",
          "conversion rate is not fixed for pair USD/RUB: quote Q-5518 expired at 06:40:01.470"),
    _orch("40:01.511", "exec-7", "t2", "INFO", "api.TransferEndpoint", "TransferRs status=ERROR"),
    _orch("40:02.001", "exec-9", "t4", "INFO", "api.TransferEndpoint",
          "TransferRq accepted type=OWN_ACCOUNTS amount=300.00 RUB"),
    _orch("40:02.300", "exec-9", "t4", "INFO", "steps.AbsPostingStep",
          "posting confirmed docNum=118842"),
    _orch("40:02.310", "exec-9", "t4", "INFO", "api.TransferEndpoint",
          "TransferRs status=EXECUTED"),
    _orch("40:02.500", "exec-2", "other", "ERROR", "steps.LimitStep",
          "daily limit exceeded for client 7710442: 600000.00 > 500000.00"),
    _orch("40:03.000", "scheduling-2", None, "ERROR", "outbox.OutboxPurger",
          "purge failed: could not obtain lock on outbox_events"),
    _orch("40:03.100", "exec-4", "t6", "INFO", "api.TransferEndpoint",
          "TransferRq accepted type=TO_LEGAL_ENTITY amount=125000.00 RUB"),
    _orch("40:03.140", "exec-4", "t6", "INFO", "steps.CoreBridgeStep",
          "calling core-bridge POST /abs/postings", oper_only=True),
    _orch("40:03.210", "exec-6", "t7", "INFO", "api.TransferEndpoint",
          "TransferRq accepted type=TO_LEGAL_ENTITY amount=48000.00 RUB"),
    _orch("40:03.250", "exec-6", "t7", "INFO", "steps.CoreBridgeStep",
          "calling core-bridge POST /abs/postings", oper_only=True),
    _orch("40:03.620", "exec-4", "t6", "ERROR", "steps.CoreBridgeStep",
          "core-bridge fault ABS_REJECT, transfer REJECTED", oper_only=True),
    _orch("40:03.790", "exec-6", "t7", "ERROR", "steps.CoreBridgeStep",
          "core-bridge fault ABS_REJECT, transfer REJECTED", oper_only=True),
    _orch("40:10.000", "scheduling-1", None, "INFO", "rates.RateCacheRefresher",
          "refreshed 148 rates"),
    _orch("40:31.302", "exec-5", "t3", "ERROR", "steps.AbsPostingStep",
          "no reply from ABS within 30 s (queue ESB.ABS.POSTING.RS)"),
    _orch("40:31.303", "exec-5", "t3", "INFO", "api.TransferEndpoint", "TransferRs status=ERROR"),
])


def _bridge(time: str, level: str, logger: str, text: str, op: str | None = None) -> str:
    corr = f'"correlationId":"{op}",' if op else ""
    return (f'{{"time":"2026-10-27 06:{time}","level":"{level}",'
            f'"logger":"CoreBridge.{logger}",{corr}"message":"{text}"}}\n')


_CORE_BRIDGE_LOG = "".join([
    _bridge("39:50.0001", "INFO", "Program",
            "core-bridge started, ABS endpoint https://abs-soap.ift2.bank.local/ws"),
    _bridge("40:03.1610", "INFO", "Abs.PostingClient", "posting request sent docType=PP "
            "amount=125000.00", _OPS["t6"][1]),
    _bridge("40:03.2702", "INFO", "Abs.PostingClient", "posting request sent docType=PP "
            "amount=48000.00", _OPS["t7"][1]),
    _bridge("40:03.6050", "ERROR", "Abs.PostingClient", "ABS posting rejected: "
            "soap:Client.AccountClosed account 40702810938000012345 closed 2026-10-20",
            _OPS["t6"][1]),
    _bridge("40:03.7718", "ERROR", "Abs.PostingClient", "ABS posting rejected: "
            "soap:Client.AccountClosed account 40702810938000067890 closed 2026-10-20",
            _OPS["t7"][1]),
    _bridge("40:04.4120", "ERROR", "Abs.PostingClient", "ABS posting rejected: "
            "soap:Server.Busy try again later", _OTHER_BRIDGE_OP),
    _bridge("40:05.0000", "ERROR", "Abs.SessionPool",
            "keep-alive ping failed: The operation has timed out."),
])


_WALLET_LOG = "".join([
    "2026-10-27T06:50:00.001+0300 [INFO]  wallet: starting: version=3.14.2 "
    "tsp=https://tsp-gw.ift2.bank.local\n",
    "2026-10-27T06:50:05.000+0300 [ERROR] wallet.jobs: token cleanup failed: "
    'error="context deadline exceeded"\n',
    *[f"2026-10-27T06:50:{time}+0300 {'[' + level + ']':<7} wallet.{name}: {text}: "
      f"requestId={_WALLET_OPS[op][0]} traceId={_WALLET_OPS[op][1]}{tail}\n"
      for time, level, name, text, op, tail in [
          ("11.101", "INFO", "http", "request", "t8", " method=POST path=/v1/tokens"),
          ("11.420", "ERROR", "tokens", "provision failed", "t8",
           ' error="TSP responded 401: invalid client certificate"'),
          ("11.880", "INFO", "http", "request", "other", " method=POST path=/v1/tokens"),
          ("12.010", "ERROR", "tokens", "provision failed", "other",
           ' error="TSP responded 401: invalid client certificate"'),
          ("13.204", "INFO", "http", "request", "t9", " method=POST path=/v1/tokens"),
          ("13.530", "ERROR", "tokens", "provision failed", "t9",
           ' error="TSP responded 401: invalid client certificate"'),
          ("14.002", "INFO", "http", "request", "art", " method=GET path=/v1/cards/art"),
          ("14.090", "ERROR", "cards", "card art fetch failed", "art",
           ' error="unexpected status 404 from cdn-art.ift2.bank.local"'),
          ("15.310", "INFO", "http", "request", "t10", " method=POST path=/v1/devices"),
          ("15.377", "WARN", "devices", "binding rejected", "t10",
           " reason=DEVICE_LIMIT_REACHED bound=5 max=5"),
      ]],
])


def _transfer_xml(root: str, test: str, *, with_oper: bool = True, status: str | None = None,
                  amount: str = "1000.00 RUB") -> str:
    rq_uid, oper_uid = _OPS[test]
    oper = f"  <OperUID>{oper_uid}</OperUID>\n" if with_oper else ""
    value, currency = amount.split()
    body = (f"  <Status><StatusCode>{status}</StatusCode></Status>\n" if status else
            f"  <SPName>MBANK</SPName>\n  <Amount currency=\"{currency}\">{value}</Amount>\n")
    return (f'<?xml version="1.0" encoding="UTF-8"?>\n<{root} xmlns="urn:bank:esb:transfers:v3">\n'
            f"  <RqUID>{rq_uid}</RqUID>\n  <RqTm>2026-10-27T06:40:01.002+03:00</RqTm>\n"
            f"{oper}{body}</{root}>\n")


def operation_logs_v4() -> Case:
    b = LaunchBuilder(9112, "ift2 esb transfers + wallet regress #318", first_result_id=9_112_000)
    orch = ("transfer-orchestrator.log", _ORCHESTRATOR_LOG, "text/plain")
    bridge = ("core-bridge.json.log", _CORE_BRIDGE_LOG, "application/json")
    wallet = ("wallet-api.log", _WALLET_LOG, "text/plain")
    error_status = '\nExpected: "COMPLETED"\n     but: was "ERROR"'
    rejected = '\nExpected: "COMPLETED"\n     but: was "REJECTED"'
    step = "Выполнить перевод через ESB"

    def trace(message: str, line: int) -> str:
        return _cucumber_trace("java.lang.AssertionError: " + message,
                               "ru.bank.e2e.esb.TransferSteps", "statusIs", line,
                               "esb/transfers.feature", "перевод завершён", 30)

    for test, name, attachment in [
            ("t1", "Конвертация USD в RUB между своими счетами",
             ("rq-transfer.xml", _transfer_xml("TransferRq", "t1", amount="1000.00 USD"), "application/xml")),
            ("t2", "Конвертация USD в RUB на карту",
             ("rs-transfer.xml", _transfer_xml("TransferRs", "t2", status="ERROR"),
              "application/xml"))]:
        b.add_failure(
            "ops-fx-quote-expired", cause="fx-quote-expired-before-conversion",
            category="приложение", name=name, message=error_status, trace=trace(error_status, 61),
            step=step, logs=[attachment, orch],
            evidence=["conversion rate is not fixed for pair USD/RUB"])
    b.add_failure(
        "ops-abs-no-reply", cause="abs-posting-no-reply", category="окружение",
        name="Перевод между своими рублёвыми счетами", message=error_status,
        trace=trace(error_status, 61), step=step,
        logs=[("rq-transfer.xml", _transfer_xml("TransferRq", "t3", amount="5000.00 RUB"), "application/xml"), orch],
        evidence=["no reply from ABS within 30 s (queue ESB.ABS.POSTING.RS)"])
    case_message = (f"TransferRs operUID={_OPS['t4'][1]}\n"
                    'Expected: "Executed"\n     but: was "EXECUTED"')
    b.add_failure(
        "ops-test-status-case", cause="test-expects-camel-case-status", category="тест",
        name="Перевод между своими счетами: статус в ответе", message=case_message,
        trace=trace(case_message, 66), step="Проверить статус в TransferRs", logs=[orch],
        evidence=['Expected: "Executed"', 'but: was "EXECUTED"'])
    b.add_failure(
        "ops-no-operation-id", cause=None, name="Перевод по реквизитам в другой банк",
        message="ru.bank.e2e.esb.EsbTimeoutException: no TransferRs within 60 s",
        trace=java_trace("ru.bank.e2e.esb.EsbTimeoutException: no TransferRs within 60 s", [
            "ru.bank.e2e.esb.EsbClient.awaitResponse(EsbClient.java:112)",
            "ru.bank.e2e.esb.TransferSteps.sendTransfer(TransferSteps.java:44)",
        ]),
        step=step, logs=[orch])
    for test, name, attachment in [
            ("t6", "Перевод юрлицу по реквизитам",
             ("rq-transfer.xml", _transfer_xml("TransferRq", "t6", with_oper=False,
                                           amount="125000.00 RUB"),
              "application/xml")),
            ("t7", "Перевод юрлицу по шаблону",
             ("rs-transfer.xml", _transfer_xml("TransferRs", "t7", status="REJECTED"),
              "application/xml"))]:
        b.add_failure(
            "ops-abs-account-closed", cause="abs-test-accounts-closed", category="данные",
            name=name, message=rejected, trace=trace(rejected, 61), step=step,
            logs=[attachment, orch, bridge],
            evidence=["ABS posting rejected: soap:Client.AccountClosed"])
    provision = '\nExpected: "ACTIVE"\n     but: was "PROVISIONING_FAILED"'
    wallet_trace = java_trace("java.lang.AssertionError: " + provision, [
        "org.hamcrest.MatcherAssert.assertThat(MatcherAssert.java:20)",
        "ru.bank.e2e.wallet.TokenSteps.tokenIsActive(TokenSteps.java:52)",
    ])
    request = (
        "POST /v1/tokens HTTP/1.1\nHost: wallet-api.ift2.bank.local\n"
        f"traceparent: 00-{_WALLET_OPS['t8'][1]}-00f067aa0ba902b7-01\n"
        "Content-Type: application/json\n\n"
        '{"cardId":"CRD-55102","deviceId":"emulator-5556","wallet":"MIRPAY"}\n'
    )
    response = (
        "HTTP/1.1 502 Bad Gateway\n"
        f"X-Request-ID: {_WALLET_OPS['t9'][0]}\nContent-Type: application/json\n\n"
        f'{{"error":"PROVISIONING_FAILED","traceId":"{_WALLET_OPS["t9"][1]}"}}\n'
    )
    for name, attachment in [
            ("Токенизация карты в MIR Pay", ("provision-request.txt", request, "text/plain")),
            ("Токенизация карты на втором устройстве",
             ("provision-response.txt", response, "text/plain"))]:
        b.add_failure(
            "ops-tsp-cert", cause="wallet-tsp-client-cert-invalid", category="окружение",
            name=name, message=provision, trace=wallet_trace, step="Токенизировать карту",
            logs=[attachment, wallet],
            evidence=["TSP responded 401: invalid client certificate"])
    bound = '\nExpected: "BOUND"\n     but: was "REJECTED"'
    b.add_failure(
        "ops-device-limit", cause="test-user-device-limit", category="данные",
        name="Привязка нового устройства к кошельку", message=bound,
        trace=java_trace("java.lang.AssertionError: " + bound, [
            "ru.bank.e2e.wallet.DeviceSteps.deviceIsBound(DeviceSteps.java:38)",
        ]) + java_trace(
            "Caused by: ru.bank.e2e.wallet.WalletException: device binding rejected "
            f"(traceId={_WALLET_OPS['t10'][1]})",
            ["ru.bank.e2e.wallet.WalletApi.bindDevice(WalletApi.java:91)"]),
        step="Привязать устройство", logs=[wallet],
        evidence=["reason=DEVICE_LIMIT_REACHED bound=5 max=5"])
    return b.build("operation_logs_v4")


CASES: dict[str, Callable[[], Case]] = {
    "log_formats_v4": log_formats_v4,
    "log_traps_v4": log_traps_v4,
    "same_symptom_log_causes_v4": same_symptom_log_causes_v4,
    "hosts_and_locators_v4": hosts_and_locators_v4,
    "shared_assert_background_v4": shared_assert_background_v4,
    "one_problem_noisy_v4": one_problem_noisy_v4,
    "one_problem_resources_v4": one_problem_resources_v4,
    "signatures_and_kb_v4": signatures_and_kb_v4,
    "retries_v4": retries_v4,
    "retries_env_v4": retries_env_v4,
    "sparse_data_v4": sparse_data_v4,
    "operation_logs_v4": operation_logs_v4,
}
