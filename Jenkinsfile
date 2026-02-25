pipeline {
    agent any

    parameters {
        string(
            name: 'LAUNCH_ID',
            description: 'ID прогона (для ручного запуска). При вебхуке заполняется автоматически.',
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
        VENV_DIR   = "${WORKSPACE}/.venv"
        REPORT_FILE = 'alla-report.json'
    }

    options {
        timestamps()
        timeout(time: 30, unit: 'MINUTES')
        buildDiscarder(logRotator(numToKeepStr: '30'))
    }

    stages {
        stage('Prepare') {
            steps {
                script {
                    if (env.LAUNCH_NAME?.trim()) {
                        // Вебхук: alla сам разрешит имя → ID через API.
                        // Имя запуска передаётся через отдельную env-переменную —
                        // НЕ встраивается в строку ALLA_ARGS, чтобы избежать shell injection.
                        def projectId = env.LAUNCH_PROJECT_ID?.trim()
                        if (!projectId) {
                            error('Вебхук не содержит $.projectId.')
                        }
                        env.ALLA_LAUNCH_NAME = env.LAUNCH_NAME
                        env.ALLA_PROJECT_ID  = projectId
                        env.ALLA_LAUNCH_ID   = ''
                        echo "Вебхук: запуск '${env.LAUNCH_NAME}' в проекте #${projectId}"
                    } else {
                        // Ручной запуск: нужен числовой LAUNCH_ID
                        def launchId = params.LAUNCH_ID?.trim()
                        if (!launchId || !(launchId ==~ /^\d+$/)) {
                            error('Укажи LAUNCH_ID (ручной запуск) или настрой вебхук с launchName.')
                        }
                        env.ALLA_LAUNCH_NAME = ''
                        env.ALLA_PROJECT_ID  = ''
                        env.ALLA_LAUNCH_ID   = launchId
                        echo "Ручной запуск: ID #${launchId}"
                    }
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
                // Имя запуска передаётся через $ALLA_LAUNCH_NAME с двойными кавычками
                // на уровне shell — безопасно, Groovy не интерполирует \$VAR.
                sh """
                    if [ -n "\$ALLA_LAUNCH_NAME" ]; then
                        ${VENV_DIR}/bin/alla --launch-name "\$ALLA_LAUNCH_NAME" --project-id "\$ALLA_PROJECT_ID" \\
                            --output-format json \\
                            --log-level ${params.LOG_LEVEL} \\
                        > ${env.REPORT_FILE}
                    else
                        ${VENV_DIR}/bin/alla "\$ALLA_LAUNCH_ID" \\
                            --output-format json \\
                            --log-level ${params.LOG_LEVEL} \\
                        > ${env.REPORT_FILE}
                    fi
                """
            }
        }

        stage('Summary') {
            steps {
                script {
                    def report = readJSON file: env.REPORT_FILE
                    def triage = report.triage_report
                    def launchId = triage.launch_id ?: '?'

                    // failure_count — @property в Python, не сериализуется model_dump().
                    // Считаем вручную из failed_count + broken_count.
                    def failureCount = (triage.failed_count ?: 0) + (triage.broken_count ?: 0)

                    echo """
╔══════════════════════════════════════════════╗
  Прогон #${launchId}: ${triage.launch_name ?: '—'}
  Всего:    ${triage.total_results}
  Упало:    ${failureCount}  (failed: ${triage.failed_count}, broken: ${triage.broken_count})
  Кластеров: ${report.clustering_report?.cluster_count ?: '—'}
╚══════════════════════════════════════════════╝
                    """.stripIndent()

                    currentBuild.description =
                        "#${launchId} | упало: ${failureCount} | " +
                        "кластеров: ${report.clustering_report?.cluster_count ?: '?'}"
                }
            }
        }
    }

    post {
        always {
            archiveArtifacts(
                artifacts: env.REPORT_FILE,
                allowEmptyArchive: true
            )
        }
        success {
            echo "Анализ завершён. Результаты отправлены в TestOps."
        }
        failure {
            echo "Анализ завершился с ошибкой. Проверь логи выше."
        }
        cleanup {
            sh "rm -rf ${VENV_DIR}"
        }
    }
}
