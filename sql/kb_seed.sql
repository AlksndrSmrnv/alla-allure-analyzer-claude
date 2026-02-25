-- alla Knowledge Base — начальное наполнение таблицы
-- Применить после kb_schema.sql:
--   psql -U <user> -d <dbname> -f sql/kb_seed.sql
--
-- Все INSERT идемпотентны: ON CONFLICT (id) DO NOTHING —
-- безопасно выполнять повторно без дублирования данных.

-- ============================================================
-- Глобальные записи (project_id IS NULL → видны всем проектам)
-- ============================================================

INSERT INTO alla.kb_entry
    (id, title, description, error_example, category, resolution_steps, project_id)
VALUES

-- ------------------------------------------------------------------
-- Из knowledge_base/entries.yaml
-- ------------------------------------------------------------------
(
    'connection_timeout',
    'Таймаут подключения к сервису',
    'Тесты не могут установить TCP-соединение с целевым сервисом. '
    'Сервис не запущен или недоступен по сети.',
    'java.net.SocketTimeoutException: Connect timed out
    at java.net.Socket.connect(Socket.java:601)
    at org.apache.http.conn.socket.PlainConnectionSocketFactory.connectSocket(PlainConnectionSocketFactory.java:75)
Caused by: Connection timed out connecting to service-endpoint:8080',
    'env',
    ARRAY[
        'Проверить доступность сервиса: curl -v <service-url>',
        'Проверить, что сервис запущен и прошёл health-check'
    ],
    NULL
),

(
    'null_pointer_exception',
    'NullPointerException в коде приложения',
    'Приложение выбрасывает NullPointerException — дефект в коде сервиса, '
    'объект не инициализирован.',
    'java.lang.NullPointerException: Cannot invoke method on null object reference
    at com.company.service.UserService.getUser(UserService.java:45)
    at com.company.controller.UserController.handleRequest(UserController.java:112)',
    'service',
    ARRAY[
        'Определить, какой объект оказался null (см. стек-трейс)',
        'Создать баг-репорт с точным стеком и шагами воспроизведения'
    ],
    NULL
),

-- ------------------------------------------------------------------
-- Дополнительные глобальные записи
-- ------------------------------------------------------------------
(
    'assertion_error',
    'Ошибка утверждения в тесте',
    'Тест упал из-за несоответствия ожидаемого и фактического значения — '
    'дефект в тестовом коде или изменение поведения приложения.',
    'java.lang.AssertionError: expected:<200> but was:<404>
    at org.junit.Assert.fail(Assert.java:89)
    at org.junit.Assert.assertEquals(Assert.java:135)
AssertionError: assert response.status_code == 200
  where response.status_code = 404',
    'test',
    ARRAY[
        'Проверить, изменилось ли поведение API (статус-код, тело ответа)',
        'Сравнить ожидаемое и фактическое значение в логе теста',
        'Обновить тест, если изменение поведения намеренное'
    ],
    NULL
),

(
    'db_connection_error',
    'Ошибка подключения к базе данных',
    'Тесты не могут подключиться к базе данных. '
    'База данных недоступна или некорректно настроена строка подключения.',
    'org.postgresql.util.PSQLException: Connection refused. Check that the hostname and port are correct and that the postmaster is accepting TCP/IP connections.
    at org.postgresql.core.v3.ConnectionFactoryImpl.openConnectionImpl(ConnectionFactoryImpl.java:262)
OperationalError: could not connect to server: Connection refused
    Is the server running on host "db-host" (10.0.0.1) and accepting
    TCP/IP connections on port 5432?',
    'env',
    ARRAY[
        'Проверить статус базы данных: pg_isready -h <host> -p <port>',
        'Проверить строку подключения в конфигурации тестового окружения',
        'Проверить сетевую доступность хоста БД из среды выполнения тестов'
    ],
    NULL
),

(
    'test_data_missing',
    'Отсутствуют тестовые данные',
    'Тест не нашёл необходимые данные в тестовой среде — '
    'не выполнена подготовка данных или данные были удалены.',
    'NoSuchElementException: No element with id=12345 found
    at com.company.repository.UserRepository.findById(UserRepository.java:78)
EntityNotFoundException: Unable to find entity with id: 99999
AssertionError: Expected test user "qa_user_001" to exist but got None',
    'data',
    ARRAY[
        'Проверить, что seed-данные были применены перед запуском тестов',
        'Запустить скрипт подготовки тестовых данных',
        'Убедиться, что тестовые данные не были удалены другими тестами'
    ],
    NULL
),

(
    'ssl_certificate_error',
    'Ошибка SSL-сертификата',
    'Тест не может установить HTTPS-соединение из-за проблем с SSL-сертификатом '
    '(самоподписанный, истёкший или недоверенный CA).',
    'javax.net.ssl.SSLHandshakeException: PKIX path building failed: unable to find valid certification path to requested target
    at sun.security.ssl.Alerts.getSSLException(Alerts.java:192)
requests.exceptions.SSLError: HTTPSConnectionPool: Max retries exceeded
Caused by: SSLCertVerificationError: certificate verify failed: self signed certificate',
    'env',
    ARRAY[
        'Проверить срок действия SSL-сертификата сервиса',
        'Для корпоративных сетей: добавить корневой CA в доверенные хранилища',
        'Временно: использовать ALLURE_SSL_VERIFY=false (только для диагностики)'
    ],
    NULL
),

(
    'out_of_memory_error',
    'Нехватка памяти (OutOfMemoryError)',
    'JVM или процесс тестов исчерпал доступную память. '
    'Возможные причины: утечка памяти, недостаточный heap, тяжёлые тестовые данные.',
    'java.lang.OutOfMemoryError: Java heap space
    at java.util.Arrays.copyOf(Arrays.java:3210)
    at com.company.service.DataProcessor.processAll(DataProcessor.java:145)
MemoryError: unable to allocate array
fatal error: runtime: out of memory',
    'env',
    ARRAY[
        'Увеличить heap JVM: -Xmx2g (или выше) в параметрах запуска тестов',
        'Проверить наличие утечек памяти в тестовом коде (не освобождаются ресурсы)',
        'Уменьшить параллелизм тестов, если память ограничена'
    ],
    NULL
)

ON CONFLICT (id) DO NOTHING;


-- ============================================================
-- Пример проектной записи (project_id = 42)
-- Адаптировать или удалить под свои нужды.
-- ============================================================

INSERT INTO alla.kb_entry
    (id, title, description, error_example, category, resolution_steps, project_id)
VALUES
(
    'payment_gateway_timeout_p42',
    'Таймаут платёжного шлюза (проект 42)',
    'Специфично для проекта 42: PaymentGateway не отвечает в пиковые часы нагрузки.',
    'com.company.payment.PaymentGatewayException: Gateway timed out after 30000ms
    at com.company.payment.PaymentClient.charge(PaymentClient.java:88)
    at com.company.payment.PaymentService.processPayment(PaymentService.java:55)',
    'env',
    ARRAY[
        'Проверить статус PaymentGateway в дашборде мониторинга',
        'Увеличить таймаут PaymentClient до 60 секунд (application.yml: payment.timeout=60000)',
        'Проверить метрику payment_gateway_latency_p99 в Grafana'
    ],
    42
)
ON CONFLICT (id) DO NOTHING;


-- ============================================================
-- Шаблоны для ручного добавления новых записей
-- ============================================================

-- Добавить глобальную запись (видна всем проектам):
--
-- INSERT INTO alla.kb_entry
--     (id, title, description, error_example, category, resolution_steps, project_id)
-- VALUES (
--     'my_unique_slug',            -- уникальный slug (строчные буквы, подчёркивания)
--     'Заголовок проблемы',        -- краткий заголовок для отчётов
--     'Подробное описание.',       -- полное описание причины
--     'Фрагмент ошибки из лога...', -- чем больше — тем лучше TF-IDF-сопоставление
--     'env',                       -- test | service | env | data
--     ARRAY['Шаг 1', 'Шаг 2'],    -- шаги по устранению
--     NULL                         -- NULL = глобальная
-- )
-- ON CONFLICT (id) DO NOTHING;


-- Добавить запись для конкретного проекта (project_id = N):
--
-- INSERT INTO alla.kb_entry
--     (id, title, description, error_example, category, resolution_steps, project_id)
-- VALUES (
--     'my_project_42_slug',
--     'Заголовок',
--     'Описание.',
--     'Фрагмент ошибки...',
--     'service',
--     ARRAY['Шаг 1'],
--     42                           -- ID проекта в Allure TestOps
-- )
-- ON CONFLICT (id) DO NOTHING;


-- Обновить шаги по устранению существующей записи:
--
-- UPDATE alla.kb_entry
-- SET    resolution_steps = ARRAY['Новый шаг 1', 'Новый шаг 2'],
--        updated_at        = now()
-- WHERE  id = 'my_unique_slug';


-- Обновить error_example (улучшить качество TF-IDF-сопоставления):
--
-- UPDATE alla.kb_entry
-- SET    error_example = 'Более полный фрагмент ошибки из лога...',
--        updated_at    = now()
-- WHERE  id = 'my_unique_slug';


-- Удалить запись:
--
-- DELETE FROM alla.kb_entry WHERE id = 'my_unique_slug';


-- Просмотреть все глобальные записи:
--
-- SELECT id, title, category, array_length(resolution_steps, 1) AS steps_count
-- FROM   alla.kb_entry
-- WHERE  project_id IS NULL
-- ORDER  BY id;


-- Просмотреть записи конкретного проекта:
--
-- SELECT id, title, category, project_id
-- FROM   alla.kb_entry
-- WHERE  project_id = 42
-- ORDER  BY id;
