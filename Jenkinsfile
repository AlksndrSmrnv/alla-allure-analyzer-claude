pipeline {
    agent any

    parameters {
        string(
            name: 'LAUNCH_ID',
            description: 'ID прогона (для ручного запуска). При вебхуке заполняется автоматически.',
            trim: true
        )
        string(
            name: 'PROJECT_ID',
            description: 'ID проекта в Allure TestOps (нужен при резолве ID по имени через вебхук).',
            trim: true
        )
        choice(
            name: 'LOG_LEVEL',
            choices: ['INFO', 'DEBUG', 'WARNING', 'ERROR'],
            description: 'Уровень логирования'
        )
    }

    triggers {
        GenericTrigger(
            // Токен задаётся здесь намеренно: Jenkins перечитывает triggers {} при каждом
            // запуске и перезаписывает конфигурацию триггера из UI — токен из UI затирается.
            // Токен вебхука не является критическим секретом (это идентификатор в URL,
            // не пароль от системы), поэтому хранить его в Jenkinsfile — приемлемо.
            // Endpoint для TestOps: POST /generic-webhook-trigger/invoke?token=alla-webhook-token
            token: 'alla-webhook-token',

            // Вытащить поля из JSON-тела вебхука TestOps.
            // Путь зависит от формата вебхука — скорректировать при необходимости.
            // Полное тело вебхука выводится в лог сборки (printPostContent: true).
            genericVariables: [
                [key: 'LAUNCH_NAME',        value: '$.launchName'],
                [key: 'LAUNCH_PROJECT_ID',  value: '$.projectId'],
                [key: 'LAUNCH_PROJECT_NAME',value: '$.projectName'],
                [key: 'WEBHOOK_PAYLOAD',    value: '$']    // весь JSON как строка
            ],

            causeString: 'Triggered by Allure TestOps webhook: $LAUNCH_NAME',
            printContributedVariables: true,
            printPostContent: true
        )
    }

    environment {
        // Allure TestOps
        ALLURE_ENDPOINT          = credentials('allure-endpoint')
        ALLURE_TOKEN             = credentials('allure-token')

        // Langflow — на верхнем уровне, доступны всем stages
        ALLURE_LANGFLOW_BASE_URL = credentials('langflow-base-url')
        ALLURE_LANGFLOW_FLOW_ID  = credentials('langflow-flow-id')
        ALLURE_LANGFLOW_API_KEY  = credentials('langflow-api-key')

        // Путь к venv внутри workspace
        // REPORT_FILE вычисляется в Validate после разрешения RESOLVED_LAUNCH_ID
        VENV_DIR = "${WORKSPACE}/.venv"
    }

    options {
        timestamps()
        timeout(time: 30, unit: 'MINUTES')
        buildDiscarder(logRotator(numToKeepStr: '30'))
    }

    stages {
        stage('Validate') {
            steps {
                script {
                    // Лог входящих данных: источник и полное тело вебхука
                    def source = env.LAUNCH_NAME ? 'вебхук TestOps' : 'ручной запуск'
                    echo """
=== Источник запуска: ${source} ===
  params.LAUNCH_ID:       ${params.LAUNCH_ID        ?: '(пусто)'}
  params.PROJECT_ID:      ${params.PROJECT_ID       ?: '(пусто)'}
  env.LAUNCH_NAME:        ${env.LAUNCH_NAME         ?: '(пусто)'}
  env.LAUNCH_PROJECT_ID:  ${env.LAUNCH_PROJECT_ID   ?: '(пусто)'}
  env.LAUNCH_PROJECT_NAME:${env.LAUNCH_PROJECT_NAME ?: '(пусто)'}
--- Полное тело вебхука (WEBHOOK_PAYLOAD) ---
${env.WEBHOOK_PAYLOAD ?: '(пусто — ручной запуск или ошибка парсинга)'}
=============================================
                    """.stripIndent()

                    // При ручном запуске берём LAUNCH_ID из params, только если он числовой.
                    // Jenkins при вебхук-запуске подставляет последнее сохранённое значение
                    // параметра — оно может быть нечисловым или пустым. В таком случае
                    // RESOLVED_LAUNCH_ID остаётся пустым и ID разрешается по имени в следующем stage.
                    def paramId = params.LAUNCH_ID?.trim()
                    env.RESOLVED_LAUNCH_ID = (paramId ==~ /^\d+$/) ? paramId : ''

                    if (env.RESOLVED_LAUNCH_ID) {
                        if (!(env.RESOLVED_LAUNCH_ID ==~ /^\d+$/)) {
                            error('LAUNCH_ID должен содержать только цифры.')
                        }
                        env.REPORT_FILE = "alla-report-${env.RESOLVED_LAUNCH_ID}.json"
                        echo "LAUNCH_ID задан вручную: #${env.RESOLVED_LAUNCH_ID}"
                    } else if (env.LAUNCH_NAME?.trim()) {
                        echo "LAUNCH_ID не получен напрямую — будет разрешён по имени '${env.LAUNCH_NAME}'."
                    } else {
                        error('Не задан ни LAUNCH_ID (ручной запуск), ни launchName (вебхук).')
                    }
                }
            }
        }

        stage('Resolve Launch ID') {
            // Запускается только когда ID не задан явно (вебхук без $.id)
            when {
                expression { !env.RESOLVED_LAUNCH_ID?.trim() }
            }
            steps {
                script {
                    def projectId = env.LAUNCH_PROJECT_ID?.trim() ?: params.PROJECT_ID?.trim()
                    if (!projectId) {
                        error('PROJECT_ID не задан. Укажи его в параметрах сборки или убедись, что вебхук содержит поле projectId.')
                    }

                    echo "Ищу запуск '${env.LAUNCH_NAME}' в проекте #${projectId} через Allure TestOps API..."

                    // Запрашиваем последние запуски проекта, отсортированные по дате создания.
                    // API не поддерживает фильтр по имени — ищем совпадение на стороне Jenkins.
                    // %2C вместо запятой в sort — требование API (encoded comma).
                    // -w выводит HTTP-статус в конец ответа для диагностики; -f убран намеренно,
                    // чтобы тело ответа с ошибкой было видно в логах.
                    def response = sh(
                        script: """
                            curl -s \
                                -w "\\nHTTP_CODE:%{http_code}" \
                                -H "Authorization: Bearer \${ALLURE_TOKEN}" \
                                "\${ALLURE_ENDPOINT}/api/launch?projectId=${projectId}&page=0&size=10&sort=created_date%2CDESC"
                        """,
                        returnStdout: true
                    ).trim()

                    // Разобрать тело ответа и HTTP-статус
                    def parts    = response.split('\nHTTP_CODE:')
                    def body     = parts[0].trim()
                    def httpCode = parts.length > 1 ? parts[1].trim() : 'unknown'

                    echo "HTTP статус: ${httpCode}"
                    echo "Ответ API:\n${body}"

                    if (httpCode != '200') {
                        error("API /api/launch вернул статус ${httpCode}. Тело ответа выше в логе.")
                    }

                    def json = readJSON text: body
                    if (!json.content || json.content.size() == 0) {
                        error("В проекте #${projectId} не найдено ни одного запуска.")
                    }

                    // Ищем запуск с именем, совпадающим с тем, что пришло в вебхуке
                    def launch = json.content.find { it.name == env.LAUNCH_NAME }
                    if (!launch) {
                        def found = json.content.collect { it.name }.join(', ')
                        error("Запуск '${env.LAUNCH_NAME}' не найден. Последние запуски в проекте: ${found}")
                    }
                    env.RESOLVED_LAUNCH_ID = launch.id.toString()
                    env.REPORT_FILE = "alla-report-${env.RESOLVED_LAUNCH_ID}.json"
                    echo "Найден запуск: ID=${env.RESOLVED_LAUNCH_ID}, name='${launch.name}'"
                }
            }
        }

        stage('Setup Python') {
            steps {
                sh """
                    python3 -m venv ${VENV_DIR}
                    ${VENV_DIR}/bin/pip install --quiet --upgrade pip
                """
            }
        }

        stage('Install alla') {
            steps {
                sh """
                    ${VENV_DIR}/bin/pip install --quiet -e .
                """
            }
        }

        stage('Analyze Launch') {
            environment {
                // Базовые настройки
                ALLURE_LOG_LEVEL            = "${params.LOG_LEVEL}"
                ALLURE_SSL_VERIFY           = 'true'

                // Кластеризация
                ALLURE_CLUSTERING_ENABLED   = 'true'
                ALLURE_CLUSTERING_THRESHOLD = '0.60'

                // База знаний — включена, результаты пишутся в TestOps
                ALLURE_KB_ENABLED           = 'true'
                ALLURE_KB_PUSH_ENABLED      = 'true'

                // LLM — включён всегда
                ALLURE_LLM_ENABLED          = 'true'
                ALLURE_LLM_PUSH_ENABLED     = 'true'
            }
            steps {
                sh """
                    ${VENV_DIR}/bin/alla ${env.RESOLVED_LAUNCH_ID} \
                        --output-format json \
                        --log-level ${params.LOG_LEVEL} \
                    > ${env.REPORT_FILE}
                """
            }
        }

        stage('Summary') {
            steps {
                script {
                    def report = readJSON file: env.REPORT_FILE
                    def triage = report.triage_report

                    // failure_count — @property в Python, не сериализуется model_dump().
                    // Считаем вручную из failed_count + broken_count.
                    def failureCount = (triage.failed_count ?: 0) + (triage.broken_count ?: 0)

                    echo """
╔══════════════════════════════════════════════╗
  Прогон #${env.RESOLVED_LAUNCH_ID}: ${triage.launch_name ?: '—'}
  Всего:    ${triage.total_results}
  Упало:    ${failureCount}  (failed: ${triage.failed_count}, broken: ${triage.broken_count})
  Кластеров: ${report.clustering_report?.cluster_count ?: '—'}
╚══════════════════════════════════════════════╝
                    """.stripIndent()

                    // Установить описание сборки для быстрого просмотра в UI
                    currentBuild.description =
                        "#${env.RESOLVED_LAUNCH_ID} | упало: ${failureCount} | " +
                        "кластеров: ${report.clustering_report?.cluster_count ?: '?'}"
                }
            }
        }
    }

    post {
        always {
            archiveArtifacts(
                artifacts: "alla-report-${env.RESOLVED_LAUNCH_ID}.json",
                allowEmptyArchive: true
            )
        }
        success {
            echo "Анализ прогона #${env.RESOLVED_LAUNCH_ID} завершён. Результаты отправлены в TestOps."
        }
        failure {
            echo "Анализ завершился с ошибкой. Проверь логи выше."
        }
        cleanup {
            sh "rm -rf ${VENV_DIR}"
        }
    }
}
