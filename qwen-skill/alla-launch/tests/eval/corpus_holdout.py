"""Отложенный корпус (holdout v2): только проверка, по нему ничего не подбирается.

Правило работы — README эталона («Корпус: dev и holdout»). Сценарии написаны без доступа к
коду кластеризации, сигнатуры и к dev-корпусу: тексты — по публичным форматам логов,
сообщений и стеков разных языков и фреймворков (Go testify/Gomega, Jest, WebdriverIO,
pytest, NUnit, xUnit, RSpec/Capybara, PHPUnit, Spock, Awaitility). Launch id — 71xx.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable
from typing import Any

from eval.corpus import Case, LaunchBuilder, java_trace, noise_lines

ESC = "\x1b"


def _json_line(fields: dict[str, Any], *, compact: bool = True) -> str:
    """Строка JSON-лога; порядок полей сохраняется."""
    separators = (",", ":") if compact else (", ", ": ")
    return json.dumps(fields, ensure_ascii=False, separators=separators) + "\n"


def _clock(seconds: int) -> str:
    """``HH:MM:SS`` от полуночи."""
    return f"{seconds // 3600:02d}:{seconds // 60 % 60:02d}:{seconds % 60:02d}"


def _testify(location: str, error: str) -> str:
    """Сообщение Go testify: ``Error Trace`` и многострочный ``Error``."""
    first, *rest = error.split("\n")
    lines = [f"\tError Trace:\t{location}", f"\tError:      \t{first}"]
    lines += [f"\t            \t{line}" for line in rest]
    return "\n".join(lines) + "\n"


def _jest_tobe(expected: str, received: str) -> str:
    """Сообщение Jest ``toBe``."""
    return ("expect(received).toBe(expected) // Object.is equality\n\n"
            f"Expected: {expected}\nReceived: {received}")


def _nunit(expected: str, actual: str) -> str:
    """Сообщение NUnit ``Assert.That(..., Is.EqualTo(...))``."""
    return f"  Expected: {expected}\n  But was:  {actual}\n"


def _rspec_eq(expected: str, got: str) -> str:
    """Сообщение RSpec ``eq``."""
    return f"\nexpected: {expected}\n     got: {got}\n\n(compared using ==)\n"


def _wdio_not_displayed(selector: str, timeout_ms: int) -> str:
    """Сообщение WebdriverIO ``waitForDisplayed``."""
    return f'element ("{selector}") still not displayed after {timeout_ms}ms'


def _ruby_log(pid: int, start: int, request: str, lines: Iterable[tuple[str, str]]) -> str:
    """Лог Ruby ``Logger`` (Rails) с тегом запроса."""
    out = []
    for offset, (level, text) in enumerate(lines):
        stamp = f"2026-10-04T{_clock(start + offset // 3)}.{120045 + offset * 6731:06d}"
        out.append(f"{level[0]}, [{stamp} #{pid}] {level:>5} -- : [{request}] {text}\n")
    return "".join(out)


# --- log_formats_more -------------------------------------------------------------------


def log_formats_more() -> Case:
    """Восемь форматов логов разных стеков; причина видна только в логе.

    На формат — своя проблема из двух тестов: zap JSON (Go), pino JSON (Node), Serilog
    (.NET), Ruby Logger (Rails), журнал PostgreSQL, klog (оператор Kubernetes), Logger
    Elixir, Monolog (Laravel). Сообщение теста — ассерт статуса или ожидания и причину не
    называет; у второго теста группы в логе другие идентификаторы и время.
    """
    builder = LaunchBuilder(7101, "regression 2026-10-04 #7101")

    # zap (Go), Go testify
    for index, (subscriber, case) in enumerate(((7701, "prepaid_to_postpaid"),
                                               (7702, "postpaid_to_prepaid"))):
        ts = 1759572000 + index * 40
        log = (
            f'{{"level":"info","ts":{ts}.101,"caller":"httpapi/server.go:64",'
            f'"msg":"request started","method":"POST",'
            f'"path":"/v1/subscribers/{subscriber}/plan"}}\n'
            f'{{"level":"info","ts":{ts}.118,"caller":"plan/switch.go:97",'
            f'"msg":"loading rating rules","plan":"UNLIM-30","region":77}}\n'
            f'{{"level":"error","ts":{ts}.233,"caller":"plan/switch.go:142",'
            f'"msg":"plan switch failed","subscriber":{subscriber},'
            f'"error":"rating rule RR-14 not found for plan UNLIM-30"}}\n'
            f'{{"level":"info","ts":{ts}.240,"caller":"httpapi/server.go:81",'
            f'"msg":"request finished","status":500,"duration":"139.2ms"}}\n'
        )
        builder.add_failure(
            "zap-rating-rule", cause="rating-rule-missing", category="данные",
            name=f"TestPlanSwitch/{case}",
            full_name=f"gitlab.acme.dev/telecom/e2e/plans.TestPlanSwitch/{case}",
            message=_testify(f"/builds/telecom/e2e/plans/switch_test.go:{57 + index * 31}",
                             "Not equal: \nexpected: 200\nactual  : 500"),
            step="Switch subscriber plan", log=log, log_name="tariff-engine.log",
            evidence=["rating rule RR-14 not found for plan UNLIM-30"])

    # pino (Node), Jest
    for index, flight in enumerate(("SU1402", "SU1403")):
        time = 1759572101000 + index * 52_317
        host = f"seatmap-svc-6b7c9d{index}f-q{index}x2p"
        error = {"type": "Error",
                 "message": "aircraft layout A321-NEO-v3 is missing in layouts cache",
                 "stack": "Error: aircraft layout A321-NEO-v3 is missing in layouts cache\n"
                          "    at LayoutStore.get (/srv/app/src/layouts.js:58:11)\n"
                          "    at SeatmapService.build (/srv/app/src/seatmap.js:24:32)"}
        log = (
            _json_line({"level": 30, "time": time, "pid": 18, "hostname": host,
                        "reqId": f"req-{index + 7}", "msg": "incoming request",
                        "req": {"method": "GET", "url": f"/v2/flights/{flight}/seatmap"}})
            + _json_line({"level": 50, "time": time + 14, "pid": 18, "hostname": host,
                          "reqId": f"req-{index + 7}", "err": error,
                          "msg": "seatmap build failed"})
            + _json_line({"level": 30, "time": time + 15, "pid": 18, "hostname": host,
                          "reqId": f"req-{index + 7}", "res": {"statusCode": 500},
                          "responseTime": 15.4, "msg": "request completed"})
        )
        message = _jest_tobe("200", "500")
        builder.add_failure(
            "pino-layout", cause="aircraft-layout-missing", category="данные",
            name=f"seatmap › returns seat map for flight {flight}",
            full_name=f"api-tests/seatmap.test.js#seatmap returns seat map for flight {flight}",
            message=message,
            trace=f"Error: {message}\n"
                  "    at Object.<anonymous> "
                  f"(/repo/api-tests/seatmap.test.js:{31 + index * 12}:28)",
            step="GET seat map", log=log, log_name="seatmap-svc.log",
            evidence=["aircraft layout A321-NEO-v3 is missing in layouts cache"])

    # Serilog (.NET), NUnit
    for index, (claim, policy) in enumerate((("CLM-55102", "POL-88-1043"),
                                             ("CLM-55117", "POL-88-1050"))):
        second = 36002 + index * 9
        log = (
            f"[{_clock(second)} INF] Request starting HTTP/1.1 POST http://claims-api/api/claims"
            f" - application/json {412 + index * 30}\n"
            f"[{_clock(second)} INF] Registering claim {claim} for policy {policy}\n"
            f"[{_clock(second)} ERR] Failed to register claim {claim}\n"
            "Npgsql.PostgresException (0x80004005): 23503: insert or update on table "
            '"claim_documents" violates foreign key constraint "fk_claim_documents_policy"\n'
            "   at Npgsql.Internal.NpgsqlConnector.ReadMessageLong(Boolean async, "
            "DataRowLoadingMode dataRowLoadingMode, Boolean readingNotifications)\n"
            "   at Insurance.Claims.ClaimRepository.AddDocumentsAsync(Claim claim) in "
            "/src/Insurance.Claims/ClaimRepository.cs:line 77\n"
            f"[{_clock(second + 1)} INF] Request finished HTTP/1.1 POST "
            "http://claims-api/api/claims - 500 0 - 84.1220ms\n"
        )
        method = ("RegisterClaimWithPhotos", "RegisterClaimWithWitness")[index]
        builder.add_failure(
            "serilog-claim-fk", cause="claim-documents-fk", category="приложение",
            name=method,
            full_name=f"Insurance.Tests.Claims.ClaimRegistrationTests.{method}",
            message=_nunit("201", "500"),
            trace=f"   at Insurance.Tests.Claims.ClaimRegistrationTests.{method}() in "
                  "/src/tests/Insurance.Tests/Claims/ClaimRegistrationTests.cs:line "
                  f"{48 + index * 21}\n",
            step="Register claim", log=log, log_name="claims-api.log",
            evidence=['violates foreign key constraint "fk_claim_documents_policy"'])

    # Ruby Logger (Rails), RSpec
    for index, (request, student) in enumerate((("8f2c1d0a", "S-1042"),
                                                ("b71e09c4", "S-1077"))):
        log = _ruby_log(41 + index, 36003 + index * 5, request, [
            ("INFO", 'Started POST "/api/v1/enrollments" for 172.22.4.15 at '
                     f"2026-10-04 {_clock(36003 + index * 5)} +0000"),
            ("INFO", "Processing by Api::V1::EnrollmentsController#create as JSON"),
            ("INFO", f'  Parameters: {{"student_id"=>"{student}", "cohort"=>"2026-B"}}'),
            ("ERROR", "Enrollments::CohortClosed: cohort 2026-B is closed for enrollment "
                      "since 2026-10-01"),
            ("INFO", "Completed 422 Unprocessable Entity in 61ms "
                     "(ActiveRecord: 12.4ms | Allocations: 5120)"),
        ])
        title = ("enrolls a student into an open cohort", "enrolls a transfer student")[index]
        builder.add_failure(
            "rails-cohort-closed", cause="cohort-closed", category="данные",
            name=f"Enrollments API POST /api/v1/enrollments {title}",
            full_name=f"spec/requests/enrollments_spec.rb[1:2:{index + 1}]",
            message=_rspec_eq("201", "422"),
            trace=f"./spec/requests/enrollments_spec.rb:{27 + index * 14}:in `block (3 levels) "
                  "in <top (required)>'\n",
            step="POST /api/v1/enrollments", log=log, log_name="enrollment.log",
            evidence=["cohort 2026-B is closed for enrollment"])

    # журнал PostgreSQL, pytest + httpx
    for index, (slot, pid) in enumerate(((4411, 2231), (4418, 2274))):
        url = f"http://appointments-api:8000/v1/slots/{slot}/book"
        error = (f"httpx.HTTPStatusError: Server error '500 Internal Server Error' for url '{url}'"
                 "\nFor more information check: "
                 "https://developer.mozilla.org/en-US/docs/Web/HTTP/Status/500")
        stamp = f"2026-10-04 10:00:{4 + index * 6:02d}"
        log = (
            f"{stamp}.512 UTC [{pid}] LOG:  connection authorized: user=clinic database=clinic\n"
            f"{stamp}.731 UTC [{pid}] ERROR:  deadlock detected\n"
            f"{stamp}.731 UTC [{pid}] DETAIL:  Process {pid} waits for ShareLock on transaction "
            f"{99812 + index}; blocked by process {pid + 9}.\n"
            f"\tProcess {pid + 9} waits for ShareLock on transaction {99810 + index}; "
            f"blocked by process {pid}.\n"
            f"{stamp}.731 UTC [{pid}] HINT:  See server log for query details.\n"
            f"{stamp}.731 UTC [{pid}] STATEMENT:  UPDATE doctor_slots SET state = 'BOOKED' "
            f"WHERE slot_id = {slot}\n"
        )
        test = ("test_book_free_slot", "test_book_slot_with_referral")[index]
        builder.add_failure(
            "postgres-deadlock", cause="doctor-slots-deadlock", category="приложение",
            name=test, full_name=f"tests.api.test_booking#{test}",
            message=error,
            trace=f"tests/api/test_booking.py:{44 + index * 19}: in {test}\n"
                  "    response.raise_for_status()\n"
                  ".venv/lib/python3.12/site-packages/httpx/_models.py:829: in raise_for_status\n"
                  "    raise HTTPStatusError(message, request=request, response=self)\n"
                  f"E   {error}\n",
            step="Book appointment slot", log=log, log_name="postgres.log",
            evidence=["ERROR: deadlock detected",
                      "UPDATE doctor_slots SET state = 'BOOKED'"])

    # klog (оператор Kubernetes), Ginkgo/Gomega
    for index, device in enumerate(("dev-7712", "dev-7790")):
        stamp = f"I1004 10:0{index}:05"
        log = (
            f'{stamp}.100231       1 reconciler.go:88] "Reconciling Device" '
            f'device="fleet/{device}"\n'
            f'{stamp}.214502       1 firmware.go:52] "Rolling out firmware" '
            f'device="fleet/{device}" version="4.2.1"\n'
            f'E1004 10:0{index}:05.341870       1 controller.go:329] "Reconciler error" '
            'err="admission webhook \\"vfirmware.iot.example.com\\" denied the request: '
            f'firmware 4.2.1 is not signed" device="fleet/{device}"\n'
            f'{stamp}.902114       1 reconciler.go:88] "Reconciling Device" '
            f'device="fleet/{device}"\n'
        )
        title = ("upgrades a tracker to 4.2.1", "upgrades a gateway to 4.2.1")[index]
        builder.add_failure(
            "klog-firmware-unsigned", cause="firmware-not-signed", category="окружение",
            name=f"[It] Device firmware rollout {title}",
            full_name=f"iot-e2e/devices Device firmware rollout {title}",
            message="Timed out after 120.001s.\nExpected\n    <string>: Pending\n"
                    "to equal\n    <string>: Online",
            trace=f"/builds/iot/e2e/devices/rollout_test.go:{73 + index * 22}\n",
            step="Wait for device to come online", log=log, log_name="device-operator.log",
            evidence=["firmware 4.2.1 is not signed"])

    # Logger (Elixir/Phoenix), Spock
    for index, room in enumerate((88, 131)):
        request = ("F5x2kQ9", "F5x3Lm1")[index]
        log = (
            f"10:00:{6 + index * 3:02d}.204 request_id={request} [info] "
            f"POST /api/rooms/{room}/messages\n"
            f"10:00:{6 + index * 3:02d}.219 request_id={request} [error] "
            "** (FunctionClauseError) no function clause matching in Chat.Moderation.check/2\n"
            "    (chat 1.14.0) lib/chat/moderation.ex:12: "
            f"Chat.Moderation.check(%{{room_id: {room}}}, :premoderated)\n"
            "    (chat 1.14.0) lib/chat_web/controllers/message_controller.ex:31: "
            "ChatWeb.MessageController.create/2\n"
            f"10:00:{6 + index * 3:02d}.221 request_id={request} [info] Sent 500 in 17ms\n"
        )
        feature = ("posts a message to a premoderated room",
                   "posts an attachment to a premoderated room")[index]
        builder.add_failure(
            "elixir-moderation", cause="moderation-clause", category="приложение",
            name=feature, full_name=f"chat.RoomMessagesSpec.{feature}",
            message="Condition not satisfied:\n\nresp.status == 201\n|    |      |\n"
                    "|    500    false\n",
            trace=java_trace(
                "org.spockframework.runtime.ConditionNotSatisfiedError: Condition not satisfied:",
                [f"chat.RoomMessagesSpec.{feature}(RoomMessagesSpec.groovy:{42 + index * 17})"]),
            step="Send message to room", log=log, log_name="chat.log",
            evidence=["no function clause matching in Chat.Moderation.check/2"])

    # Monolog (Laravel), PHPUnit
    for index, listing in enumerate((3301, 3315)):
        log = (
            f'[2026-10-04 10:00:{7 + index * 4:02d}] testing.INFO: Publishing listing {listing} '
            f'{{"user_id":{17 + index}}} []\n'
            f"[2026-10-04 10:00:{7 + index * 4:02d}] testing.ERROR: Imagick extension is not "
            f'loaded {{"userId":{17 + index},"exception":"[object] (RuntimeException(code: 0): '
            "Imagick extension is not loaded at "
            '/var/www/html/app/Services/Thumbnailer.php:27)"} []\n'
        )
        method = ("test_owner_can_publish_listing", "test_agent_can_publish_listing")[index]
        builder.add_failure(
            "monolog-imagick", cause="imagick-missing", category="окружение",
            name=method, full_name=f"Tests\\Feature\\ListingPublishTest::{method}",
            message="Expected response status code [200] but received 500.\n"
                    "Failed asserting that 500 is identical to 200.",
            trace=f"/var/www/html/tests/Feature/ListingPublishTest.php:{35 + index * 16}\n",
            step="Publish listing", log=log, log_name="laravel.log",
            evidence=["Imagick extension is not loaded"])

    return builder.build("log_formats_more")


# --- log_traps_more ---------------------------------------------------------------------


def _nest_line(level: str, color: str, context: str, text: str, second: int) -> str:
    """Строка встроенного логгера NestJS с ANSI-цветами."""
    return (f"{ESC}[{color}m[Nest] 31  - {ESC}[39m10/04/2026, 10:13:{second:02d} AM "
            f"{ESC}[{color}m{level:>7}{ESC}[39m {ESC}[33m[{context}]{ESC}[39m "
            f"{ESC}[{color}m{text}{ESC}[39m\n")


def _telemetry_log(start: int, device: str) -> str:
    """Лог Go slog JSON на ~450 КБ: 3600 обычных строк, ошибка ровно посередине."""
    def head(line: int) -> str:
        return f'{{"time":"2026-10-04T{_clock(start + line // 20)}.{line % 20 * 50:03d}Z",'

    def batch(line: int) -> str:
        return (f'"level":"INFO","msg":"telemetry batch accepted",'
                f'"device":"trk-{line % 64:04d}","points":{100 + line % 37},'
                f'"lag_ms":{line % 13}}}')

    middle = _json_line({
        "time": f"2026-10-04T{_clock(start + 90)}.010Z", "level": "ERROR",
        "msg": "telemetry ingest stopped", "device": device,
        "err": "clickhouse: code: 241, message: Memory limit (total) exceeded: would use "
               "3.51 GiB (attempt to allocate chunk of 4194304 bytes), maximum: 3.46 GiB"})
    return (noise_lines(head, 1800, batch) + middle
            + noise_lines(lambda line: head(line + 1800), 1800, batch))


def log_traps_more() -> Case:
    """Настоящая ошибка рядом со строками, похожими на ошибку, и неудобные логи.

    Слово error в URL, полях и тексте INFO; WARN с исключением, после которого повтор
    удался; обработанное исключение на DEBUG; ANSI-цвета; CRLF; лог в сотни КБ с ошибкой в
    середине; несколько вложений, ошибка не в первом. На приём — своя проблема из двух тестов.
    """
    builder = LaunchBuilder(7102, "nightly #7102")

    # error в URL, полях и тексте INFO (structlog, pytest)
    for index, course in enumerate(("MATH-201", "MATH-305")):
        stamp = f"2026-10-04T10:10:{index * 20:02d}"
        log = (
            f"{stamp}.120412Z [info     ] request started                "
            "method=GET path=/api/v1/error-codes\n"
            f"{stamp}.131877Z [info     ] request finished               "
            "path=/api/v1/error-codes status=200\n"
            f"{stamp}.402113Z [info     ] submissions checked            "
            f"course={course} errors=[] error_count=0\n"
            f"{stamp}.511020Z [info     ] no errors found in gradebook   course={course}\n"
            f"{stamp}.512301Z [info     ] config loaded                  "
            "on_error=skip error_report_url=https://grades.qa.example.test/errors\n"
            f"{stamp}.733901Z [error    ] grade publication failed       "
            f"course={course} reason='grading scale GS-5 has overlapping ranges'\n"
            f"{stamp}.734550Z [info     ] request finished               "
            "path=/api/v1/grades/publish status=500\n"
        )
        test = ("test_publish_final_grades", "test_publish_retake_grades")[index]
        message = f"Failed: grades for {course} were not published within 30 s"
        builder.add_failure(
            "info-error-words", cause="grading-scale-overlap", category="данные",
            name=test, full_name=f"tests.test_grades#{test}", message=message,
            trace=f"tests/test_grades.py:{58 + index * 23}: in {test}\n"
                  "    pytest.fail(f\"grades for {course} were not published within 30 s\")\n"
                  f"E   {message}\n",
            step="Publish grades", log=log, log_name="grades-service.log",
            evidence=["grading scale GS-5 has overlapping ranges"])

    # WARN с исключением и удавшийся повтор (ECS JSON, Awaitility)
    for index, trip in enumerate(("TRP-301", "TRP-322")):
        stamp = f"2026-10-04T10:11:{index * 15:02d}"
        retry_error = ("java.util.concurrent.TimeoutException: Did not observe any item or "
                       "terminal signal within 2000ms in 'flatMap'\n\tat reactor.core.publisher."
                       "FluxTimeout$TimeoutMainSubscriber.handleTimeout(FluxTimeout.java:296)")
        log = (
            _json_line({"@timestamp": f"{stamp}.101Z", "log.level": "INFO",
                        "message": f"Route {trip} requested Central->Airport",
                        "service.name": "timetable"})
            + _json_line({"@timestamp": f"{stamp}.104Z", "log.level": "WARN",
                          "message": "GTFS provider call failed, retrying (attempt 1/3)",
                          "error.type": "java.util.concurrent.TimeoutException",
                          "error.stack_trace": retry_error, "service.name": "timetable"})
            + _json_line({"@timestamp": f"{stamp}.391Z", "log.level": "INFO",
                          "message": "GTFS provider responded on attempt 2",
                          "service.name": "timetable"})
            + _json_line({"@timestamp": f"{stamp}.402Z", "log.level": "ERROR",
                          "message": f"Route {trip} rejected",
                          "error.type": "com.acme.transit.StaleFeedException",
                          "error.message": "GTFS feed 2026-10-02 is older than 24h",
                          "service.name": "timetable"})
        )
        method = ("plansRouteToAirport", "plansNightRoute")[index]
        exception = ("org.awaitility.core.ConditionTimeoutException: Condition with lambda "
                     "expression in com.acme.transit.TripPlannerIT that uses java.lang.String "
                     "was not fulfilled within 10 seconds.")
        builder.add_failure(
            "warn-retry-trap", cause="gtfs-feed-stale", category="окружение",
            name=method, full_name=f"com.acme.transit.TripPlannerIT.{method}",
            message=exception.split(": ", 1)[1],
            trace=java_trace(exception, [
                "org.awaitility.core.ConditionAwaiter.await(ConditionAwaiter.java:167)",
                "org.awaitility.core.ConditionFactory.until(ConditionFactory.java:985)",
                f"com.acme.transit.TripPlannerIT.{method}(TripPlannerIT.java:{64 + index * 18})",
            ]),
            step="Wait for planned route", log=log, log_name="timetable.json",
            evidence=["GTFS feed 2026-10-02 is older than 24h"])

    # обработанное исключение на DEBUG (ASP.NET Core console, xUnit)
    for index, booking in enumerate(("BK-9001", "BK-9017")):
        log = (
            "info: Microsoft.AspNetCore.Hosting.Diagnostics[1]\n"
            "      Request starting HTTP/1.1 POST http://rentals-api/api/bookings "
            "- application/json 233\n"
            "dbug: Rentals.Pricing.SurchargeCalculator[0]\n"
            "      Surcharge lookup fell back to default\n"
            "      System.Collections.Generic.KeyNotFoundException: The given key "
            f"'AIRPORT-{('SVO', 'LED')[index]}' was not present in the dictionary.\n"
            "         at System.Collections.Generic.Dictionary`2.get_Item(TKey key)\n"
            "         at Rentals.Pricing.SurchargeCalculator.Lookup(String zone) in "
            "/src/Rentals.Pricing/SurchargeCalculator.cs:line 41\n"
            "info: Rentals.Bookings.BookingService[0]\n"
            f"      Pricing booking {booking}\n"
            "fail: Microsoft.AspNetCore.Diagnostics.ExceptionHandlerMiddleware[1]\n"
            "      An unhandled exception has occurred while executing the request.\n"
            "      System.InvalidOperationException: Vehicle class 'SUV-L' has no active "
            "rate card for 2026-10-04\n"
            "         at Rentals.Pricing.RateCards.Get(String vehicleClass, DateOnly day) in "
            "/src/Rentals.Pricing/RateCards.cs:line 58\n"
            "info: Microsoft.AspNetCore.Hosting.Diagnostics[2]\n"
            "      Request finished HTTP/1.1 POST http://rentals-api/api/bookings "
            f"- 500 - application/problem+json {37 + index * 5}.2104ms\n"
        )
        method = ("CreatesBookingForSuv", "CreatesOneWayBookingForSuv")[index]
        builder.add_failure(
            "debug-handled-trap", cause="rate-card-missing", category="данные",
            name=method, full_name=f"Rentals.Tests.BookingApiTests.{method}",
            message="Assert.Equal() Failure\nExpected: 201\nActual:   500",
            trace=f"   at Rentals.Tests.BookingApiTests.{method}() in "
                  f"/src/tests/Rentals.Tests/BookingApiTests.cs:line {52 + index * 26}\n"
                  "--- End of stack trace from previous location ---\n",
            step="POST /api/bookings", log=log, log_name="rentals-api.log",
            evidence=["Vehicle class 'SUV-L' has no active rate card"])

    # ANSI-цвета (NestJS, Jest)
    for index, asset in enumerate(("sample-720p.mp4", "sample-1080p.mp4")):
        nest = _nest_line
        log = (
            nest("LOG", "32", "RoutesResolver", "ErrorsController {/api/errors}:", index)
            + nest("LOG", "32", "RouterExplorer", "Mapped {/api/errors/:id, GET} route", index)
            + nest("LOG", "32", "TranscodeService", f"Probing upload {asset}", index + 2)
            + nest("ERROR", "31", "TranscodeService",
                   "ffprobe exited with code 1: moov atom not found", index + 2)
            + nest("WARN", "33", "JobsService", "Job marked as FAILED, no retries left",
                   index + 2)
        )
        message = _jest_tobe('"READY"', '"FAILED"')
        builder.add_failure(
            "ansi-moov-atom", cause="fixture-video-corrupted", category="данные",
            name=f"transcoding › transcodes {asset}",
            full_name=f"tests/transcoding.e2e-spec.ts#transcoding transcodes {asset}",
            message=message,
            trace=f"Error: {message}\n    at Object.<anonymous> "
                  f"(/repo/tests/transcoding.e2e-spec.ts:{40 + index * 9}:35)",
            step="Wait for transcoding job", log=log, log_name="media-api.log",
            evidence=["moov atom not found"])

    # CRLF (Serilog file sink, NUnit)
    for index, envelope in enumerate(("ENV-3391", "ENV-3402")):
        lines = [
            f"2026-10-04 10:14:0{index}.123 +03:00 [INF] Envelope {envelope} sent to signing",
            f"2026-10-04 10:14:0{index}.380 +03:00 [ERR] Document signing failed for envelope "
            f"{envelope}",
            "System.Security.Cryptography.CryptographicException: The certificate chain was "
            "issued by an authority that is not trusted.",
            "   at ESign.Signing.CmsSigner.Sign(Byte[] content) in "
            "C:\\build\\src\\ESign.Signing\\CmsSigner.cs:line 88",
            f"2026-10-04 10:14:0{index}.402 +03:00 [INF] Envelope {envelope} state: Error",
        ]
        method = ("SignsEnvelopeWithQualifiedCertificate", "SignsEnvelopeByTwoParties")[index]
        builder.add_failure(
            "crlf-untrusted-chain", cause="signing-chain-untrusted", category="окружение",
            name=method, full_name=f"ESign.Tests.SigningTests.{method}",
            message=_nunit('"Signed"', '"Error"'),
            trace=f"   at ESign.Tests.SigningTests.{method}() in "
                  f"C:\\build\\tests\\ESign.Tests\\SigningTests.cs:line {33 + index * 15}\r\n",
            step="Sign envelope", log="\r\n".join(lines) + "\r\n", log_name="esign.log",
            evidence=["The certificate chain was issued by an authority that is not trusted."])

    # длинный лог, ошибка в середине (Go slog JSON, testify)
    for index, device in enumerate(("trk-0042", "trk-0107")):
        log = _telemetry_log(36900 + index * 600, device)
        test = ("TestTelemetry/track_is_stored", "TestTelemetry/geofence_alert")[index]
        builder.add_failure(
            "long-log-memory", cause="clickhouse-memory-limit", category="окружение",
            name=test, full_name=f"gitlab.acme.dev/fleet/e2e/telemetry.{test}",
            message=_testify(f"/builds/fleet/e2e/telemetry/track_test.go:{88 + index * 40}",
                             "Condition never satisfied"),
            step="Wait for telemetry points", log=log, log_name="telemetry-ingest.log",
            evidence=["Memory limit (total) exceeded"])

    # несколько вложений, ошибка не в первом (pytest, gunicorn)
    for index, result in enumerate(("LR-7781", "LR-7790")):
        request = (f"POST http://lab-gateway:8000/hl7/inbound\nContent-Type: application/hl7-v2"
                   f"\nX-Request-Id: {('5d1c', '9e04')[index]}-hl7\n\nMSH|^~\\&|LIS|QA|...\n")
        response = json.dumps({"result_id": result, "status": "PROCESSING", "errors": []})
        log = (
            f"[2026-10-04 10:16:0{index} +0000] [17] [INFO] Booting worker with pid: 17\n"
            f"[2026-10-04 10:16:0{index + 1} +0000] [17] [INFO] Accepted HL7 batch for {result}\n"
            f"[2026-10-04 10:16:0{index + 2} +0000] [17] [ERROR] HL7 message rejected: "
            "segment OBX-5 exceeds 64 KB limit\n"
            f"[2026-10-04 10:16:0{index + 2} +0000] [17] [INFO] Result {result} left in "
            "PROCESSING\n"
        )
        test = ("test_lab_result_with_pdf_report", "test_lab_result_with_scan")[index]
        message = f"Failed: lab result {result} still PROCESSING after 60 s"
        builder.add_failure(
            "third-attachment", cause="hl7-segment-limit", category="приложение",
            name=test, full_name=f"tests.lab.test_results#{test}",
            message=message,
            trace=f"tests/lab/test_results.py:{71 + index * 30}: in {test}\n"
                  f"E   {message}\n",
            step="Wait for lab result", log=request, log_name="request.http",
            logs=[("response.json", response, "application/json"),
                  ("lab-gateway.log", log, "text/plain")],
            evidence=["segment OBX-5 exceeds 64 KB limit"])

    return builder.build("log_traps_more")


# --- same_symptom_log_causes ------------------------------------------------------------


def same_symptom_log_causes() -> Case:
    """Одинаковые сообщение и шаг у разных проблем; причины различаются только логами.

    Шесть тестов pytest с одним и тем же ``HTTPStatusError`` 500 (три причины в логе сервиса
    на Rust) и шесть тестов Go с одним и тем же таймаутом клиента (три разные причины на
    сервере: блокировка в MySQL, медленный сосед, нехватка памяти и падение процесса).
    """
    builder = LaunchBuilder(7103, "release-candidate #7103")
    url = "http://policy-api:8080/v1/policies"
    status_error = (f"httpx.HTTPStatusError: Server error '500 Internal Server Error' for url "
                    f"'{url}'\nFor more information check: "
                    "https://developer.mozilla.org/en-US/docs/Web/HTTP/Status/500")
    policy_causes = [
        ("rust-rules-bundle", "uw-rules-bundle-missing", "окружение",
         "ERROR policy_api::issue: policy issue failed "
         "err=rules bundle uw-2026.10 is missing in bucket uw-rules",
         "rules bundle uw-2026.10 is missing in bucket uw-rules"),
        ("rust-overflow-panic", "premium-overflow", "приложение",
         "ERROR policy_api::panic: thread 'tokio-runtime-worker' panicked at "
         "src/pricing.rs:88:21:\nattempt to multiply with overflow",
         "attempt to multiply with overflow"),
        ("rust-sequence-max", "policy-number-sequence-exhausted", "окружение",
         "ERROR policy_api::numbering: policy issue failed err=error returned from "
         "database: nextval: reached maximum value of sequence \"policy_number_seq\" (999999)",
         'reached maximum value of sequence "policy_number_seq"'),
    ]
    tests = ["test_issue_policy_for_young_driver", "test_issue_policy_with_franchise",
             "test_issue_policy_for_country_house", "test_issue_policy_with_installments",
             "test_issue_policy_for_two_drivers", "test_issue_policy_for_electric_car"]
    for index, test in enumerate(tests):
        group, cause, category, error, evidence = policy_causes[index % 3]
        stamp = f"2026-10-04T10:20:{index * 7:02d}"
        log = (
            f"{stamp}.104512Z  INFO policy_api::http: request started method=POST "
            "uri=/v1/policies\n"
            f"{stamp}.121907Z  INFO policy_api::issue: issuing policy product=\"osago\"\n"
            f"{stamp}.188230Z {error}\n"
            f"{stamp}.188911Z  INFO policy_api::http: request finished status=500 "
            f"latency={84 + index}ms\n"
        )
        builder.add_failure(
            group, cause=cause, category=category,
            name=test, full_name=f"tests.policies.test_issue#{test}",
            message=status_error,
            trace="tests/policies/conftest.py:52: in issue_policy\n"
                  "    response.raise_for_status()\n"
                  f"E   {status_error}\n",
            step="Issue policy", log=log, log_name="policy-api.log",
            evidence=[evidence])

    timeout = _testify(
        "/builds/travel/e2e/holds/helpers_test.go:39",
        "Received unexpected error:\nPost \"http://booking-api:8080/v1/holds\": context "
        "deadline exceeded (Client.Timeout exceeded while awaiting headers)")
    hold_causes = [
        ("hold-lock-wait", "holds-lock-wait", "приложение",
         {"level": "ERROR", "msg": "hold insert failed",
          "err": "Error 1205 (HY000): Lock wait timeout exceeded; try restarting transaction"},
         "Lock wait timeout exceeded; try restarting transaction"),
        ("hold-channel-manager", "channel-manager-unreachable", "окружение",
         {"level": "ERROR", "msg": "availability check failed",
          "err": "Post \"http://channel-manager:9000/v2/availability\": dial tcp "
                 "172.24.3.17:9000: i/o timeout"},
         "dial tcp 172.24.3.17:9000: i/o timeout"),
        ("hold-oom", "booking-api-oom", "приложение", None,
         "fatal error: runtime: out of memory"),
    ]
    hold_tests = ["TestCreateHold/standard_room", "TestCreateHold/family_suite",
                  "TestCreateHold/two_rooms", "TestCreateHold/long_stay",
                  "TestCreateHold/late_checkin", "TestCreateHold/loyalty_rate"]
    for index, test in enumerate(hold_tests):
        group, cause, category, record, evidence = hold_causes[index % 3]
        stamp = f"2026-10-04T10:25:{index * 9:02d}"
        log = _json_line({"time": f"{stamp}.010Z", "level": "INFO", "msg": "hold requested",
                          "hotel": 501 + index, "nights": 2 + index % 3})
        if record is not None:
            log += _json_line({"time": f"{stamp}.950Z", **record, "hotel": 501 + index})
            log += _json_line({"time": f"{stamp}.951Z", "level": "INFO",
                               "msg": "request finished", "status": 500,
                               "duration": f"{50 + index}.1s"})
        else:
            log += ("fatal error: runtime: out of memory\n\nruntime stack:\n"
                    "runtime.throw({0x9f3c2a?, 0x0?})\n"
                    "\t/usr/local/go/src/runtime/panic.go:1023 +0x5c fp=0x7ffd5c1e8f30\n"
                    "runtime.sysMapOS(0xc04c000000, 0x4000000)\n"
                    "\t/usr/local/go/src/runtime/mem_linux.go:167 +0x11b\n")
        builder.add_failure(
            group, cause=cause, category=category,
            name=test, full_name=f"gitlab.acme.dev/travel/e2e/holds.{test}",
            message=timeout, step="Create hold", log=log, log_name="booking-api.log",
            evidence=[evidence])

    return builder.build("same_symptom_log_causes")


# --- hosts_and_locators -----------------------------------------------------------------


def hosts_and_locators() -> Case:
    """Один шаг и одна обёртка в стеке; проблемы различаются хостом или локатором.

    Python requests и Go net/http — недоступны два разных сервиса; WebdriverIO и Capybara —
    не найдены два разных элемента. Внутри стека всё остальное в сообщении и стеке совпадает
    (кроме адреса объекта у urllib3 и строки вызова в тесте).
    """
    builder = LaunchBuilder(7104, "smoke #7104")

    # Python requests через обёртку qa_kit/http.py
    hosts = [("tariffs-api", "tariffs-api-down"), ("numbering-api", "numbering-api-down")]
    for index in range(4):
        host, cause = hosts[index % 2]
        error = (
            f"requests.exceptions.ConnectionError: HTTPConnectionPool(host='{host}', port=8000):"
            " Max retries exceeded with url: /v1/config (Caused by NewConnectionError("
            f"'<urllib3.connection.HTTPConnection object at 0x7f3a2c1d{0x5E50 + index * 0x130:x}>: "
            "Failed to establish a new connection: [Errno 111] Connection refused'))")
        test = ("test_plan_list", "test_number_reserve", "test_plan_change",
                "test_number_release")[index]
        builder.add_failure(
            f"py-{host}", cause=cause, category="окружение",
            name=test, full_name=f"tests.smoke.test_api#{test}",
            message=error,
            trace=f"tests/smoke/test_api.py:{21 + index * 9}: in {test}\n"
                  "    config = api.get(\"/v1/config\")\n"
                  "qa_kit/http.py:48: in get\n    return self._send(\"GET\", path)\n"
                  "qa_kit/http.py:72: in _send\n"
                  "    response = self._session.request(method, self._url(path), timeout=10)\n"
                  f"E   {error}\n",
            step="Load service config", evidence=[f"host='{host}', port=8000"])

    # Go net/http через apitest.Client
    services = [("sim-provisioning", "172.25.1.14", "sim-provisioning-down"),
                ("porting-gw", "172.25.1.19", "porting-gw-down")]
    for index in range(4):
        host, address, cause = services[index % 2]
        test = ("TestSim/activate", "TestPorting/port_in", "TestSim/suspend",
                "TestPorting/port_out")[index]
        builder.add_failure(
            f"go-{host}", cause=cause, category="окружение",
            name=test, full_name=f"gitlab.acme.dev/telecom/e2e/smoke.{test}",
            message=_testify(
                f"/builds/telecom/e2e/apitest/client.go:88\n"
                f"/builds/telecom/e2e/smoke/smoke_test.go:{30 + index * 11}",
                f"Received unexpected error:\nGet \"http://{host}:7000/v1/config\": "
                f"dial tcp {address}:7000: connect: connection refused"),
            step="Load service config", evidence=[f"dial tcp {address}:7000"])

    # WebdriverIO через BasePage.tap
    selectors = [('[data-qa="plan-card"]', "plan-card-missing"),
                 ('[data-qa="roaming-toggle"]', "roaming-toggle-missing")]
    for index in range(4):
        selector, cause = selectors[index % 2]
        message = _wdio_not_displayed(selector, 10000)
        title = ("opens plan details", "enables roaming", "compares plans",
                 "disables roaming")[index]
        builder.add_failure(
            f"wdio-{cause}", cause=cause, category="тест",
            name=f"Plan settings {title}",
            full_name=f"tests/specs/plans.e2e.js#Plan settings {title}",
            message=message,
            trace=f"Error: {message}\n"
                  "    at BasePage.tap (/repo/tests/pageobjects/base.page.js:22:19)\n"
                  "    at Context.<anonymous> "
                  f"(/repo/tests/specs/plans.e2e.js:{18 + index * 13}:9)",
            step="Open plan settings", evidence=[selector])

    # Capybara через Pages::Base#press
    locators = [('Unable to find button "Book appointment" that is not disabled',
                 "book-button-missing", 'button "Book appointment"'),
                ('Unable to find css "#slot-picker .slot--free"', "free-slot-missing",
                 'css "#slot-picker .slot--free"')]
    for index in range(4):
        locator, cause, evidence = locators[index % 2]
        title = ("books a therapist", "books a dentist", "books a follow-up visit",
                 "books an online consultation")[index]
        builder.add_failure(
            f"capybara-{cause}", cause=cause, category="тест",
            name=f"Appointment booking {title}",
            full_name=f"spec/features/appointments_spec.rb[1:{index + 1}]",
            message=f"Capybara::ElementNotFound: {locator}",
            trace="./spec/support/pages/base_page.rb:14:in `press'\n"
                  f"./spec/features/appointments_spec.rb:{12 + index * 8}:in "
                  "`block (2 levels) in <top (required)>'\n",
            step="Choose a time slot", evidence=[evidence])

    return builder.build("hosts_and_locators")


# --- shared_assert_background -----------------------------------------------------------


_OTEL_BACKGROUND = (
    "fail: OpenTelemetry.Exporter.OtlpTraceExporter[0]\n"
    "      Exporter failed send data to collector to http://otel-collector:4317/ endpoint. "
    "Data will not be sent.\n"
    '      Grpc.Core.RpcException: Status(StatusCode="Unavailable", '
    'Detail="Error connecting to subchannel.")\n'
)
_FLAGS_BACKGROUND = (
    "fail: Unleash.Communication.UnleashApiClient[0]\n"
    "      Failed to fetch toggles from http://unleash:4242/api/client/features\n"
    "      System.Net.Http.HttpRequestException: Connection refused (unleash:4242)\n"
)


def shared_assert_background() -> Case:
    """Общий ассерт NUnit у разных проблем; причины — в логах среди фоновых ошибок.

    Три проблемы сервиса рецептов по три теста с одинаковым ``Expected: 200 But was: 500``;
    в логах — ошибки посторонних сервисов (экспорт телеметрии, флаги), у части логов разных
    проблем фон одинаков. Четвёртая проблема — одна ошибка, фон у каждого теста свой.
    """
    builder = LaunchBuilder(7105, "regression #7105")
    causes = {
        "formulary": ("formulary-missing-drug", "данные",
                      "Clinic.Prescriptions.FormularyException: Drug code N02BE01 is not in "
                      "the active formulary",
                      "Drug code N02BE01 is not in the active formulary"),
        "db-read": ("prescriptions-db-timeout", "окружение",
                    "Npgsql.NpgsqlException (0x80004005): Exception while reading from stream\n"
                    "       ---> System.TimeoutException: Timeout during reading attempt",
                    "Npgsql.NpgsqlException (0x80004005): Exception while reading from stream"),
        "concurrency": ("prescription-concurrency", "приложение",
                        "Microsoft.EntityFrameworkCore.DbUpdateConcurrencyException: The "
                        "database operation was expected to affect 1 row(s), but actually "
                        "affected 0 row(s); data may have been modified or deleted since "
                        "entities were loaded.",
                        "DbUpdateConcurrencyException: The database operation was expected to "
                        "affect 1 row(s), but actually affected 0 row(s)"),
        "dosage-format": ("dosage-format", "приложение",
                          "System.FormatException: The input string 'twice-a-day' was not in a "
                          "correct format.",
                          "The input string 'twice-a-day' was not in a correct format."),
    }
    otel, flags = _OTEL_BACKGROUND, _FLAGS_BACKGROUND
    plan = [
        ("formulary", "CreatesPrescriptionForParacetamol", otel),
        ("formulary", "CreatesPrescriptionForChild", otel),
        ("formulary", "RenewsPrescription", flags),
        ("db-read", "ListsActivePrescriptions", otel),
        ("db-read", "ListsPrescriptionHistory", otel),
        ("db-read", "FiltersPrescriptionsByDoctor", otel + flags),
        ("concurrency", "CancelsPrescription", flags),
        ("concurrency", "ExtendsPrescription", flags),
        ("concurrency", "ChangesPharmacy", otel),
        ("dosage-format", "ImportsPrescriptionFromHis", ""),
        ("dosage-format", "ImportsPrescriptionWithSchedule", flags + otel),
        ("dosage-format", "ImportsPrescriptionForCourse", flags),
    ]
    for index, (key, method, background) in enumerate(plan):
        cause, category, error, evidence = causes[key]
        error_lines = "\n".join(f"      {line.lstrip()}" for line in error.split("\n"))
        log = (
            "info: Microsoft.AspNetCore.Hosting.Diagnostics[1]\n"
            f"      Request starting HTTP/1.1 POST http://prescriptions-api/api/v2/"
            f"prescriptions/{7300 + index} - application/json {180 + index * 7}\n"
            + background
            + "fail: Microsoft.AspNetCore.Diagnostics.ExceptionHandlerMiddleware[1]\n"
            "      An unhandled exception has occurred while executing the request.\n"
            f"{error_lines}\n"
            "info: Microsoft.AspNetCore.Hosting.Diagnostics[2]\n"
            "      Request finished HTTP/1.1 POST http://prescriptions-api/api/v2/"
            f"prescriptions/{7300 + index} - 500 - application/problem+json "
            f"{41 + index * 3}.5530ms\n"
        )
        builder.add_failure(
            key, cause=cause, category=category,
            name=method, full_name=f"Clinic.Tests.Api.PrescriptionsTests.{method}",
            message=_nunit("200", "500"),
            trace=f"   at Clinic.Tests.Api.PrescriptionsTests.{method}() in "
                  f"/src/tests/Clinic.Tests/Api/PrescriptionsTests.cs:line {40 + index * 12}\n",
            step="Send prescription request", log=log, log_name="prescriptions-api.log",
            evidence=[evidence])

    return builder.build("shared_assert_background")


# --- one_problem_noisy ------------------------------------------------------------------


def _boarding_log(index: int, *, line: int, noise: str) -> str:
    """Лог pino сервиса посадочных талонов; меняются под, pid, id, время и строка стека."""
    pod = f"boarding-{('7f9c6d', '5b2e8a', '7f9c6d', '9d1f3c')[index % 4]}-{index:02d}k{index}x"
    pid = 17 + index * 3
    base = 1759573200000 + index * 61_733
    request = f"{index * 7919 + 101:x}-{index:04d}"
    common = {"pid": pid, "hostname": pod}
    noises = {
        "metrics": {"level": 50, "time": base + 3, **common, "msg": "metrics push failed",
                    "err": {"type": "Error", "message": "connect ETIMEDOUT 172.29.0.40:9091"}},
        "slow": {"level": 40, "time": base + 5, **common, "msg": "slow query",
                 "durationMs": 1840 + index, "table": "passengers"},
        "session": {"level": 50, "time": base + 2, **common, "msg": "session store ping failed",
                    "err": {"type": "Error", "message": "Connection is closed."}},
        "none": {},
    }
    error = ("Channel closed by server: 406 (PRECONDITION-FAILED) with message "
             "\"PRECONDITION_FAILED - inequivalent arg 'x-message-ttl' for queue "
             "'boarding-pass.issue' in vhost '/': received '60000' but current is '30000'\"")
    out = _json_line({"level": 30, "time": base, **common, "reqId": request,
                      "msg": "issue boarding pass", "passenger": f"P{4100 + index * 13}"})
    if noises[noise]:
        out += _json_line(noises[noise])
    out += _json_line({
        "level": 50, "time": base + 9 + index, **common, "reqId": request,
        "err": {"type": "Error", "message": error,
                "stack": f"Error: {error}\n    at BoardingPassPublisher.publish "
                         f"(/srv/app/dist/publisher.js:{line}:23)"},
        "msg": "failed to publish boarding pass job"})
    return out


def one_problem_noisy() -> Case:
    """Одна проблема (несовместимые аргументы очереди RabbitMQ) за тремя симптомами.

    Логи участников различаются подом, pid, id запроса, временем, строкой стека и
    посторонними ошибками; симптомы — ассерт Jest, исключение axios и ожидание
    WebdriverIO — разные группы с общей причиной. Один тест ассерта без лога.
    """
    builder = LaunchBuilder(7106, "nightly #7106")
    cause = "boarding-queue-args-mismatch"
    evidence = "inequivalent arg 'x-message-ttl' for queue 'boarding-pass.issue'"
    noises = ["metrics", "slow", "none", "session", "metrics", "slow", "session", "none",
              "metrics", "slow", "none"]

    assert_message = _jest_tobe('"ISSUED"', '"PENDING"')
    for index, flight in enumerate(("SU100", "SU202", "SU305", "SU410", "SU512")):
        has_log = index != 4
        builder.add_failure(
            "assert-pending", cause=cause, category="окружение",
            name=f"boarding pass › is issued after check-in on {flight}",
            full_name=f"tests/api/boarding.test.ts#boarding pass is issued after check-in "
                      f"on {flight}",
            message=assert_message,
            trace=f"Error: {assert_message}\n    at Object.<anonymous> "
                  f"(/repo/tests/api/boarding.test.ts:{27 + index * 15}:41)",
            step="Check boarding pass status",
            log=_boarding_log(index, line=41 + index % 2 * 3, noise=noises[index])
            if has_log else None,
            log_name="boarding.log",
            evidence=[evidence] if has_log else [])

    for index, kind in enumerate(("mobile", "kiosk", "web")):
        message = "AxiosError: Request failed with status code 503"
        builder.add_failure(
            "axios-503", cause=cause, category="окружение",
            name=f"boarding pass API › issues {kind} pass",
            full_name=f"tests/api/boarding-issue.test.ts#boarding pass API issues {kind} pass",
            message=message,
            trace=f"{message}\n    at settle (/repo/node_modules/axios/lib/core/settle.js:19:12)"
                  "\n    at IncomingMessage.handleStreamEnd "
                  "(/repo/node_modules/axios/lib/adapters/http.js:599:11)\n"
                  f"    at Object.<anonymous> (/repo/tests/api/boarding-issue.test.ts:"
                  f"{33 + index * 11}:22)",
            step="POST /v1/boarding-passes",
            log=_boarding_log(5 + index, line=44, noise=noises[5 + index]),
            log_name="boarding.log", evidence=[evidence])

    for index, flight in enumerate(("SU611", "SU733", "SU845")):
        message = _wdio_not_displayed('[data-qa="boarding-pass-qr"]', 15000)
        builder.add_failure(
            "wdio-qr", cause=cause, category="окружение",
            name=f"Online check-in shows boarding pass for {flight}",
            full_name=f"tests/specs/checkin.e2e.js#Online check-in shows boarding pass for "
                      f"{flight}",
            message=message,
            trace=f"Error: {message}\n    at async CheckinPage.openBoardingPass "
                  "(/repo/tests/pageobjects/checkin.page.js:48:9)",
            step="Open boarding pass",
            log=_boarding_log(8 + index, line=41 + index * 3, noise=noises[8 + index]),
            log_name="boarding.log", evidence=[evidence])

    return builder.build("one_problem_noisy")


# --- one_problem_resources --------------------------------------------------------------


def one_problem_resources() -> Case:
    """Одна проблема — разные ресурсы в сообщении.

    Реплики одного сервиса (имя пода в ошибке gRPC), разные студенты и курсы при одной
    блокировке таблицы, сбой DNS стенда по разным хостам в форматах Go, Node и .NET (на
    формат — группа, причина общая), разные элементы страницы, не загрузившейся из-за
    отсутствующего чанка JS.
    """
    builder = LaunchBuilder(7107, "regression #7107")

    for index, pod in enumerate(("rating-engine-6c9d8f7b5-x4k2m", "rating-engine-6c9d8f7b5-p7r9t",
                                 "rating-engine-6c9d8f7b5-z2w5q")):
        test = ("TestRating/local_call", "TestRating/sms_bundle", "TestRating/intl_call")[index]
        builder.add_failure(
            "replicas-cold-cache", cause="rating-cache-empty", category="приложение",
            name=test, full_name=f"gitlab.acme.dev/telecom/e2e/rating.{test}",
            message=_testify(f"/builds/telecom/e2e/rating/rating_test.go:{45 + index * 17}",
                             "Received unexpected error:\nrpc error: code = Internal desc = "
                             f"tariff cache is empty on {pod}"),
            step="Rate call record", evidence=["tariff cache is empty"])

    for index, (student, course) in enumerate((("S-2041", "PHYS-110"), ("S-2077", "CHEM-120"),
                                               ("S-2102", "PHYS-110"), ("S-2163", "BIO-130"))):
        log = (
            _json_line({"student": student, "course": course, "event": "allocating seat",
                        "level": "info", "timestamp": f"2026-10-04T10:40:{index * 4:02d}.120Z"},
                       compact=False)
            + _json_line({"student": student, "course": course,
                          "event": "seat allocation failed", "level": "error",
                          "timestamp": f"2026-10-04T10:40:{index * 4 + 3:02d}.122Z",
                          "error": "lock timeout: relation seat_quota is locked by migration "
                                   "0042_seat_quota"}, compact=False)
        )
        message = f"Failed: student {student} stayed WAITLISTED in course {course} after 30 s"
        test = f"test_enroll_into_course[{student}-{course}]"
        builder.add_failure(
            "data-waitlisted", cause="seat-quota-locked", category="окружение",
            name=test, full_name=f"tests.test_enrollment#{test}", message=message,
            trace=f"tests/test_enrollment.py:66: in test_enroll_into_course\nE   {message}\n",
            step="Wait for enrollment", log=log, log_name="enrollment-worker.log",
            evidence=["is locked by migration 0042_seat_quota"])

    dns = "qa-cluster-dns-down"
    for index, host in enumerate(("esign-api", "timesheet-api")):
        fqdn = f"{host}.qa.svc.cluster.local"
        test = ("TestContracts/send_for_signature", "TestTimesheets/submit_week")[index]
        builder.add_failure(
            "dns-go", cause=dns, category="окружение",
            name=test, full_name=f"gitlab.acme.dev/hr/e2e/flows.{test}",
            message=_testify(f"/builds/hr/e2e/flows/flows_test.go:{51 + index * 24}",
                             f"Received unexpected error:\nGet \"https://{fqdn}/v1/health\": "
                             f"dial tcp: lookup {fqdn} on 10.96.0.10:53: server misbehaving"),
            step="Call HR service",
            evidence=["on 10.96.0.10:53: server misbehaving"])
    for index, host in enumerate(("hr-docs", "vacation-api")):
        fqdn = f"{host}.qa.svc.cluster.local"
        message = f"Error: getaddrinfo EAI_AGAIN {fqdn}"
        title = ("uploads employment contract", "requests vacation")[index]
        builder.add_failure(
            "dns-node", cause=dns, category="окружение",
            name=f"HR portal › {title}",
            full_name=f"tests/hr-portal.test.js#HR portal {title}",
            message=message,
            trace=f"{message}\n    at GetAddrInfoReqWrap.onlookup [as oncomplete] "
                  "(node:dns:109:26)",
            step="Call HR service", evidence=["getaddrinfo EAI_AGAIN"])
    for index, host in enumerate(("payroll-calendar", "staff-directory")):
        fqdn = f"{host}.qa.svc.cluster.local"
        method = ("ReturnsWorkingDays", "FindsEmployeeByTabNumber")[index]
        exception = (f"System.Net.Http.HttpRequestException: Resource temporarily unavailable "
                     f"({fqdn}:443)")
        builder.add_failure(
            "dns-dotnet", cause=dns, category="окружение",
            name=method, full_name=f"Hr.Tests.Directory.LookupTests.{method}",
            message=exception,
            trace=f"{exception}\n ---> System.Net.Sockets.SocketException (11): Resource "
                  "temporarily unavailable\n   at System.Net.Http.HttpConnectionPool."
                  "ConnectToTcpHostAsync(String host, Int32 port, HttpRequestMessage "
                  "initialRequest, Boolean async, CancellationToken cancellationToken)\n",
            step="Call HR service", evidence=["Resource temporarily unavailable"])

    page_errors = json.dumps([{
        "type": "pageerror",
        "message": "ChunkLoadError: Loading chunk 712 failed.\n(missing: "
                   "https://rentals-web.qa.example.test/static/js/712.9c1e4b.chunk.js)"}])
    for index, element in enumerate(("vehicle-list", "pickup-date", "extras-panel",
                                     "rental-summary")):
        message = _wdio_not_displayed(f'[data-qa="{element}"]', 10000)
        title = ("lists vehicles", "picks a date", "adds child seat", "shows summary")[index]
        builder.add_failure(
            "page-not-loaded", cause="rentals-web-chunk-missing", category="окружение",
            name=f"Rental wizard {title}",
            full_name=f"tests/specs/rental-wizard.e2e.js#Rental wizard {title}",
            message=message,
            trace=f"Error: {message}\n    at async RentalWizard.open "
                  f"(/repo/tests/pageobjects/rental.page.js:{30 + index * 6}:9)",
            step="Open rental wizard", log=page_errors, log_name="page-errors.json",
            evidence=["ChunkLoadError: Loading chunk 712 failed."])

    return builder.build("one_problem_resources")


# --- signatures_and_kb ------------------------------------------------------------------


def signatures_and_kb() -> Case:
    """Близкие тексты разных проблем, общий и частичный признак причины в логе.

    Три проблемы с одним ассертом RSpec и похожими ошибками ``Tickets::IssueJob failed``;
    одна проблема с разными логами участников; причина с тремя симптомами (ассерт, таймаут
    клиента, элемент не найден) и общей строкой в логе; причина, у UI-симптома которой лога
    нет; посторонняя проблема с тем же ассертом, что у первого симптома общей причины.
    """
    builder = LaunchBuilder(7108, "regression #7108")
    issued = _rspec_eq('"issued"', '"pending"')
    close = [
        ("issue-hold-expired", "seat-hold-expired", "приложение",
         "Tickets::IssueJob failed: seat hold SH-4471 was released after 900 s"),
        ("issue-venue-map", "venue-map-version", "данные",
         "Tickets::IssueJob failed: venue map VM-12 version mismatch (expected 7, got 6)"),
        ("issue-pdf-renderer", "pdf-renderer-missing", "окружение",
         "Tickets::IssueJob failed: PDF renderer unavailable: wkhtmltopdf exited with "
         "status 127"),
    ]
    titles = ["issues a ticket for a concert", "issues a ticket for a play",
              "issues a ticket for a match", "issues a family ticket",
              "issues a VIP ticket", "issues a student ticket"]
    for index, title in enumerate(titles):
        group, cause, category, error = close[index % 3]
        request = f"{0x3a00 + index * 77:x}c1"
        log = _ruby_log(52, 39600 + index * 11, request, [
            ("INFO", 'Started POST "/api/v1/tickets" for 172.28.5.9'),
            ("INFO", "Processing by Api::V1::TicketsController#create as JSON"),
            ("INFO", "Completed 202 Accepted in 34ms (ActiveRecord: 7.9ms | Allocations: 3011)"),
            ("INFO", f"Performing Tickets::IssueJob (Job ID: {request}-job)"),
            ("ERROR", error),
        ])
        builder.add_failure(
            group, cause=cause, category=category,
            name=f"Tickets {title}", full_name=f"spec/requests/tickets_spec.rb[1:{index + 1}]",
            message=issued,
            trace=f"./spec/requests/tickets_spec.rb:{22 + index * 9}:in `block (2 levels) in "
                  "<top (required)>'\n",
            step="Wait for ticket issue", log=log, log_name="events-api.log",
            evidence=[error.split(": ", 1)[1]])

    refund_error = 'Refunds::FeeTable: fee table FT-2026 has no rule for channel "kiosk"'
    refund_logs = [
        [("INFO", 'Started POST "/api/v1/refunds" for 172.28.5.9'),
         ("ERROR", refund_error),
         ("INFO", "Completed 422 Unprocessable Entity in 18ms")],
        [("INFO", 'Started POST "/api/v1/refunds" for 172.28.5.14'),
         ("INFO", "Processing by Api::V1::RefundsController#create as JSON"),
         ("WARN", "Cache miss for fee_tables/FT-2026, loading from database"),
         ("INFO", "  FeeRule Load (2.1ms)  SELECT \"fee_rules\".* FROM \"fee_rules\" "
                  "WHERE \"fee_rules\".\"table_id\" = 2026"),
         ("ERROR", refund_error),
         ("INFO", "Completed 422 Unprocessable Entity in 41ms (ActiveRecord: 2.1ms)")],
        [("INFO", "Processing by Api::V1::RefundsController#create as JSON"),
         ("INFO", '  Parameters: {"ticket_id"=>"TK-88120", "channel"=>"kiosk"}'),
         ("ERROR", refund_error),
         ("ERROR", "Sentry: event dropped, rate limit exceeded"),
         ("INFO", "Completed 422 Unprocessable Entity in 26ms")],
    ]
    for index, lines in enumerate(refund_logs):
        title = ("refunds a kiosk ticket", "refunds a kiosk ticket with fee",
                 "refunds a kiosk group ticket")[index]
        builder.add_failure(
            "refund-fee-rule", cause="refund-fee-rule-missing", category="данные",
            name=f"Refunds {title}", full_name=f"spec/requests/refunds_spec.rb[1:{index + 1}]",
            message=_rspec_eq("200", "422"),
            trace=f"./spec/requests/refunds_spec.rb:{17 + index * 12}:in `block (2 levels) in "
                  "<top (required)>'\n",
            step="POST /api/v1/refunds",
            log=_ruby_log(53 + index, 39900 + index * 13, f"rf{index}{index * 31:03d}", lines),
            log_name="events-api.log",
            evidence=['fee table FT-2026 has no rule for channel "kiosk"'])

    replica = ('ActiveRecord::ConnectionNotEstablished (connection to server at '
               '"events-db-replica" (172.28.0.12), port 5432 failed: FATAL:  the database '
               'system is in recovery mode)')
    replica_evidence = "the database system is in recovery mode"
    symptoms = [
        ("replica-api-503", _rspec_eq("200", "503"), "GET /api/v1/events",
         ["lists upcoming events", "filters events by city"]),
        ("replica-read-timeout",
         "Net::ReadTimeout: Net::ReadTimeout with #<TCPSocket:(closed)>",
         "GET /api/v1/events/:id/seats", ["loads seat availability", "loads sector prices"]),
        ("replica-page-seats",
         'Capybara::ElementNotFound: Unable to find css ".event-seat--available"',
         "Open seat selection", ["selects a seat", "selects two adjacent seats"]),
    ]
    for group_index, (group, message, step, group_titles) in enumerate(symptoms):
        for index, title in enumerate(group_titles):
            number = group_index * 2 + index
            log = _ruby_log(60 + number, 40200 + number * 17, f"rp{number:02d}e", [
                ("INFO", f'Started GET "/api/v1/events?page={number + 1}" for 172.28.5.9'),
                ("ERROR", replica),
                ])
            builder.add_failure(
                group, cause="events-replica-recovery", category="окружение",
                name=f"Events {title}",
                full_name=f"spec/events/events_spec.rb[{group_index + 1}:{index + 1}]",
                message=message,
                trace=f"./spec/events/events_spec.rb:{15 + number * 10}:in `block (2 levels) "
                      "in <top (required)>'\n",
                step=step, log=log, log_name="events-api.log", evidence=[replica_evidence])

    for index, title in enumerate(("creates an event with a poster",
                                   "replaces an event poster")):
        log = _ruby_log(70 + index, 40500 + index * 7, f"pst{index}", [
            ("INFO", 'Started POST "/api/v1/events" for 172.28.5.9'),
            ("ERROR", "Aws::S3::Errors::NoSuchBucket (The specified bucket does not exist)"),
            ("INFO", "Completed 500 Internal Server Error in 112ms"),
        ])
        builder.add_failure(
            "poster-api-500", cause="posters-bucket-missing", category="окружение",
            name=f"Events admin {title}",
            full_name=f"spec/requests/events_admin_spec.rb[1:{index + 1}]",
            message=_rspec_eq("201", "500"),
            trace=f"./spec/requests/events_admin_spec.rb:{31 + index * 14}:in `block (2 levels) "
                  "in <top (required)>'\n",
            step="POST /api/v1/events", log=log, log_name="events-api.log",
            evidence=["The specified bucket does not exist"])
    for index, title in enumerate(("shows the poster on the event page",
                                   "shows the poster in the event list")):
        builder.add_failure(
            "poster-page-missing", cause="posters-bucket-missing", category="окружение",
            name=f"Event page {title}",
            full_name=f"spec/features/event_page_spec.rb[1:{index + 1}]",
            message='expected to find css "img.event-poster" but there were no matches',
            trace=f"./spec/features/event_page_spec.rb:{19 + index * 8}:in `block (2 levels) in "
                  "<top (required)>'\n",
            step="Open event page",
            evidence=['expected to find css "img.event-poster"'])

    for index, title in enumerate(("lists past events", "lists events of a venue")):
        log = _ruby_log(80 + index, 40800 + index * 5, f"ro{index}", [
            ("INFO", 'Started GET "/api/v1/events" for 172.28.5.9'),
            ("ERROR", "ReadOnlyModeError: writes are disabled by flag events.read_only "
                      "(audit log write rejected)"),
        ])
        builder.add_failure(
            "read-only-flag", cause="events-read-only-flag", category="окружение",
            name=f"Archive {title}", full_name=f"spec/requests/archive_spec.rb[1:{index + 1}]",
            message=_rspec_eq("200", "503"),
            trace=f"./spec/requests/archive_spec.rb:{11 + index * 9}:in `block (2 levels) in "
                  "<top (required)>'\n",
            step="GET /api/v1/events", log=log, log_name="events-api.log",
            evidence=["writes are disabled by flag events.read_only"])

    return builder.build("signatures_and_kb")


# --- retries_more -----------------------------------------------------------------------


def _fields(*, history_id: str | None = None, history_key: str | None = None,
            case_id: int | None = None, params: dict[str, str] | None = None,
            stand: str | None = None, start: int = 0) -> dict[str, Any]:
    """Поля связи TestOps для результата (только заданные) и время начала."""
    fields: dict[str, Any] = {"start": 1759575600000 + start * 1000}
    if history_id is not None:
        fields["historyId"] = history_id
    if history_key is not None:
        fields["historyKey"] = history_key
    if case_id is not None:
        fields["testCaseId"] = case_id
    if params is not None:
        fields["parameters"] = [{"name": key, "value": value} for key, value in params.items()]
    if stand is not None:
        fields["environment"] = [{"name": "stand", "value": stand}]
    return fields


def retries_more() -> Case:
    """Повторы pytest-rerunfailures: hidden-попытки и связь по полям TestOps.

    Прошёл после повтора; все попытки с той же ошибкой (разный номер заявки); попытка с
    другой ошибкой; параметризованный тест (попытки не смешиваются между параметрами); смена
    стенда между попытками (не повтор); ошибка попытки только в деталях результата; семь
    попыток; попытка без полей связи (не связывается); связь по ``historyKey`` без
    ``historyId`` и по ``testCaseId`` + параметрам + окружению.
    """
    builder = LaunchBuilder(7109, "telecom regression #7109")
    module = "tests.sim.test_lifecycle"

    def attempt(name: str, message: str | None, fields: dict[str, Any],
                status: str = "failed") -> int:
        return builder.add_result(name=name, status=status, full_name=f"{module}#{name}",
                                  message=message, hidden=True, extra=fields)

    # прошёл после повтора
    name = "test_activate_esim"
    attempt(name, "Failed: eSIM bundle download did not finish within 60 s",
            _fields(history_id="9a1f03", case_id=9101, stand="qa-1", start=0))
    passed = builder.add_result(name=name, status="passed", full_name=f"{module}#{name}",
                                extra=_fields(history_id="9a1f03", case_id=9101, stand="qa-1",
                                              start=70))
    builder.expect_passed_after_retry(passed)

    # все попытки с той же ошибкой, номер заявки меняется
    name = "test_port_in_number"
    attempts = [attempt(name, f"Failed: porting request PR-{5531 + n} rejected by donor "
                              "operator: code 4012 (subscriber data mismatch)",
                        _fields(history_id="9a1f04", case_id=9102, stand="qa-1",
                                start=100 + n * 40)) for n in range(2)]
    final = builder.add_failure(
        "porting-4012", cause="porting-subscriber-mismatch", category="данные",
        name=name, full_name=f"{module}#{name}",
        message="Failed: porting request PR-5533 rejected by donor operator: code 4012 "
                "(subscriber data mismatch)",
        step="Submit porting request",
        extra=_fields(history_id="9a1f04", case_id=9102, stand="qa-1", start=180),
        evidence=["code 4012 (subscriber data mismatch)"])
    builder.expect_attempts(final, [(attempts[0], True), (attempts[1], True)])

    # попытка с другой ошибкой
    name = "test_sim_provisioning"
    reset = attempt(name, "requests.exceptions.ChunkedEncodingError: (\"Connection broken: "
                          "ConnectionResetError(104, 'Connection reset by peer')\", "
                          "ConnectionResetError(104, 'Connection reset by peer'))",
                    _fields(history_id="9a1f05", case_id=9103, stand="qa-1", start=200),
                    status="broken")
    final = builder.add_failure(
        "sim-stuck-provisioning", cause=None, name=name, full_name=f"{module}#{name}",
        message="Failed: SIM 89701010000000004417 stayed in state PROVISIONING after 120 s",
        step="Wait for SIM activation",
        extra=_fields(history_id="9a1f05", case_id=9103, stand="qa-1", start=260))
    builder.expect_attempts(final, [(reset, False)])

    # параметризованный тест: у каждого параметра свои попытки
    for zone, history in (("EU", "9a1f06"), ("ASIA", "9a1f07")):
        name = f"test_roaming_rate[{zone}]"
        message = f"Failed: roaming rate for zone {zone} is 0.00, expected > 0"
        params = {"zone": zone}
        earlier = attempt(name, message, _fields(history_id=history, case_id=9104,
                                                 params=params, stand="qa-1", start=300))
        final = builder.add_failure(
            "roaming-zero-rate", cause="roaming-rates-not-loaded", category="данные",
            name=name, full_name=f"{module}#{name}", message=message, step="Get roaming rate",
            extra=_fields(history_id=history, case_id=9104, params=params, stand="qa-1",
                          start=330),
            evidence=["is 0.00, expected > 0"])
        builder.expect_attempts(final, [(earlier, True)])

    # смена стенда между попытками: это не повтор
    name = "test_data_bundle_counter"
    message = "Failed: data counter for bundle B-10GB shows 0 MB used after a 50 MB session"
    attempt(name, message, _fields(history_id="9a1f08", case_id=9105, stand="qa-2", start=400))
    final = builder.add_failure(
        "bundle-counter-zero", cause=None, name=name, full_name=f"{module}#{name}",
        message=message, step="Check data counter",
        extra=_fields(history_id="9a1f09", case_id=9105, stand="qa-1", start=460))
    builder.expect_attempts(final, [])

    # ошибка попытки — только в деталях результата
    name = "test_apn_settings_push"
    message = "Failed: APN settings 'internet.qa' were not pushed to device emulator-5554"
    silent = attempt(name, None, _fields(history_id="9a1f0a", case_id=9106, stand="qa-1",
                                         start=500))
    builder.add_detail(silent, message=message)
    final = builder.add_failure(
        "apn-not-pushed", cause=None, name=name, full_name=f"{module}#{name}",
        message=message, step="Push APN settings",
        extra=_fields(history_id="9a1f0a", case_id=9106, stand="qa-1", start=560))
    builder.expect_attempts(final, [(silent, True)])

    # семь попыток: первая с другой ошибкой
    name = "test_voicemail_greeting_upload"
    storage = ("httpx.HTTPStatusError: Server error '507 Insufficient Storage' for url "
               "'http://voicemail-api:8080/v1/greetings'")
    def voicemail(start: int) -> dict[str, Any]:
        return _fields(history_id="9a1f0b", case_id=9107, stand="qa-1", start=start)

    many = [attempt(name, "httpx.ConnectError: [Errno 111] Connection refused",
                    voicemail(600), status="broken")]
    many += [attempt(name, storage, voicemail(620 + n * 20)) for n in range(6)]
    final = builder.add_failure(
        "voicemail-507", cause="voicemail-storage-full", category="окружение",
        name=name, full_name=f"{module}#{name}", message=storage, step="Upload greeting",
        extra=voicemail(760),
        evidence=["507 Insufficient Storage"])
    builder.expect_attempts(final, [(many[0], False), *((item, True) for item in many[1:])])

    # попытка без полей связи: связать нечем
    name = "test_ussd_balance_request"
    message = "Failed: USSD *100# returned empty response"
    builder.add_result(name=name, status="failed", full_name=f"{module}#{name}",
                       message=message, hidden=True, extra={"start": 1759575600000 + 800_000})
    final = builder.add_failure(
        "ussd-empty", cause=None, name=name, full_name=f"{module}#{name}", message=message,
        step="Send USSD request",
        extra=_fields(history_id="9a1f0c", case_id=9108, stand="qa-1", start=840))
    builder.expect_attempts(final, [])

    # без historyId: связь по historyKey
    name = "test_call_forwarding_busy"
    message = "Failed: call to 79990001122 was not forwarded to voicemail on busy"
    keyed = attempt(name, message, _fields(history_key="cf-busy:qa-1", case_id=9109,
                                           stand="qa-1", start=900))
    final = builder.add_failure(
        "forwarding-busy", cause=None, name=name, full_name=f"{module}#{name}",
        message=message, step="Call busy subscriber",
        extra=_fields(history_key="cf-busy:qa-1", case_id=9109, stand="qa-1", start=950))
    builder.expect_attempts(final, [(keyed, True)])

    # без historyId и historyKey: testCaseId + параметры + окружение
    name = "test_number_block[lost]"
    message = "Failed: number 79990003344 is still active after block with reason 'lost'"
    params = {"reason": "lost"}
    by_case = attempt(name, message, _fields(case_id=9110, params=params, stand="qa-1",
                                             start=1000))
    builder.add_result(name="test_number_block[stolen]", status="passed",
                       full_name=f"{module}#test_number_block[stolen]",
                       extra=_fields(case_id=9110, params={"reason": "stolen"}, stand="qa-1",
                                     start=1020))
    final = builder.add_failure(
        "block-lost", cause=None, name=name, full_name=f"{module}#{name}", message=message,
        step="Block number", extra=_fields(case_id=9110, params=params, stand="qa-1",
                                           start=1060))
    builder.expect_attempts(final, [(by_case, True)])

    return builder.build("retries_more")


# --- sparse_data ------------------------------------------------------------------------


def sparse_data() -> Case:
    """Падения почти без данных рядом с muted, hidden и passed.

    Два падения без сообщения, трейса и лога; два — только с шагом (разные шаги); два — только
    с логом с одной ошибкой. Muted и hidden падения в группы не входят, passed — рядом.
    """
    builder = LaunchBuilder(7110, "lease smoke #7110")
    builder.add_failure("empty-archive", cause=None, name="test_archive_old_leases",
                        full_name="tests.leases.test_archive#test_archive_old_leases")
    builder.add_failure("empty-return", cause=None, name="Lease return › closes the lease",
                        full_name="tests/lease-return.test.js#Lease return closes the lease",
                        status="broken")
    builder.add_failure("step-sign", cause=None, name="test_sign_lease_with_sms_code",
                        full_name="tests.leases.test_signing#test_sign_lease_with_sms_code",
                        step="Sign lease with SMS code")
    builder.add_failure("step-passport", cause=None, name="test_upload_passport_scan",
                        full_name="tests.leases.test_documents#test_upload_passport_scan",
                        step="Upload passport scan", status="broken")
    for index, test in enumerate(("test_lease_pdf_download", "test_lease_history_download")):
        log = (
            f"[2026-10-04 11:00:0{index} +0000] [9] [INFO] Booting worker with pid: "
            f"{23 + index}\n"
            f"[2026-10-04 11:00:0{index + 2} +0000] [9] [CRITICAL] WORKER TIMEOUT "
            f"(pid:{23 + index})\n"
            f"[2026-10-04 11:00:0{index + 3} +0000] [9] [ERROR] Worker (pid:{23 + index}) was "
            "sent SIGKILL! Perhaps out of memory?\n"
        )
        builder.add_failure(
            "log-only-sigkill", cause="lease-api-worker-oom", category="окружение",
            name=test, full_name=f"tests.leases.test_documents#{test}",
            log=log, log_name="lease-api.log",
            evidence=["was sent SIGKILL! Perhaps out of memory?"])
    builder.add_result(name="test_lease_reminder_schedule", status="failed", muted=True,
                       full_name="tests.leases.test_reminders#test_lease_reminder_schedule",
                       message="Failed: reminder for lease L-77 scheduled on 2026-10-05, "
                               "expected 2026-10-04")
    builder.add_result(name="test_lease_deposit_refund", status="broken", hidden=True,
                       full_name="tests.leases.test_deposit#test_lease_deposit_refund",
                       message="Failed: deposit for lease L-81 was not returned")
    for test in ("test_lease_create", "test_lease_extend", "test_lease_terminate"):
        builder.add_result(name=test, status="passed",
                           full_name=f"tests.leases.test_lifecycle#{test}")
    return builder.build("sparse_data")


CASES: dict[str, Callable[[], Case]] = {
    "log_formats_more": log_formats_more,
    "log_traps_more": log_traps_more,
    "same_symptom_log_causes": same_symptom_log_causes,
    "hosts_and_locators": hosts_and_locators,
    "shared_assert_background": shared_assert_background,
    "one_problem_noisy": one_problem_noisy,
    "one_problem_resources": one_problem_resources,
    "signatures_and_kb": signatures_and_kb,
    "retries_more": retries_more,
    "sparse_data": sparse_data,
}
