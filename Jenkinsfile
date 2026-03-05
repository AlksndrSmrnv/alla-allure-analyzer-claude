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
        // URL alla-сервера в Kubernetes (без trailing slash)
        ALLA_ENDPOINT = credentials('alla-k8s-endpoint')
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
                        // Вебхук: резолвим имя запуска в числовой ID через alla-сервер.
                        def projectId = env.LAUNCH_PROJECT_ID?.trim()
                        if (!projectId || !(projectId ==~ /^\d+$/)) {
                            error("Вебхук: $.projectId должен быть числовым, получено: '${projectId}'")
                        }

                        echo "Вебхук: запуск '${env.LAUNCH_NAME}' в проекте #${projectId}"

                        // Имя и projectId передаются через env-переменные оболочки
                        // (не через Groovy-интерполяцию), чтобы избежать shell injection:
                        // спецсимволы в имени запуска не попадают в тело скрипта.
                        env.ALLA_LAUNCH_NAME_VAR = env.LAUNCH_NAME
                        env.ALLA_PROJECT_ID_VAR  = projectId
                        def resolveUrl = "${env.ALLA_ENDPOINT}/api/v1/launch/resolve"
                        def responseJson = withCredentials([
                            file(credentialsId: 'alla-client-cert', variable: 'CLIENT_CERT'),
                            file(credentialsId: 'alla-client-key',  variable: 'CLIENT_KEY'),
                        ]) {
                            sh(
                                script: """
                                    curl -sfk --max-time 30 \\
                                        --cert "\$CLIENT_CERT" \\
                                        --key  "\$CLIENT_KEY"  \\
                                        -G "${resolveUrl}" \\
                                        --data-urlencode "name=\$ALLA_LAUNCH_NAME_VAR" \\
                                        --data-urlencode "project_id=\$ALLA_PROJECT_ID_VAR"
                                """,
                                returnStdout: true
                            )
                        }.trim()

                        def parsed = readJSON text: responseJson
                        def resolvedId = parsed.launch_id as String
                        if (!(resolvedId ==~ /^\d+$/)) {
                            error("Неожиданный launch_id от сервера: '${resolvedId}'")
                        }
                        env.ALLA_LAUNCH_ID = resolvedId
                        echo "Резолв: имя '${env.LAUNCH_NAME}' → ID #${env.ALLA_LAUNCH_ID}"
                    } else {
                        // Ручной запуск: нужен числовой LAUNCH_ID
                        def launchId = params.LAUNCH_ID?.trim()
                        if (!launchId || !(launchId ==~ /^\d+$/)) {
                            error('Укажи LAUNCH_ID (ручной запуск) или настрой вебхук с launchName.')
                        }
                        env.ALLA_LAUNCH_ID = launchId
                        echo "Ручной запуск: ID #${launchId}"
                    }
                }
            }
        }

        stage('Analyze Launch') {
            steps {
                script {
                    def launchId = env.ALLA_LAUNCH_ID
                    def analyzeUrl = "${env.ALLA_ENDPOINT}/api/v1/analyze/${launchId}/html"

                    withCredentials([
                        file(credentialsId: 'alla-client-cert', variable: 'CLIENT_CERT'),
                        file(credentialsId: 'alla-client-key',  variable: 'CLIENT_KEY'),
                    ]) {
                        sh """
                            curl -sfk --max-time 1800 -X POST "${analyzeUrl}" \\
                                --cert "\$CLIENT_CERT" \\
                                --key  "\$CLIENT_KEY"  \\
                                -o /dev/null -D /tmp/alla-headers.txt
                        """
                    }

                    def reportUrl = sh(
                        script: "grep -i '^X-Report-URL:' /tmp/alla-headers.txt | sed 's/^[^:]*: *//' | tr -d '\\r\\n' || true",
                        returnStdout: true
                    ).trim()

                    if (reportUrl) {
                        env.ALLA_REPORT_URL = reportUrl
                        echo "Анализ завершён. Отчёт: ${reportUrl}"
                    } else {
                        echo "Анализ завершён. URL отчёта не получен (проверьте ALLURE_SERVER_EXTERNAL_URL + ALLURE_REPORTS_DIR)."
                    }
                }
            }
        }

        stage('Summary') {
            steps {
                script {
                    def launchId = env.ALLA_LAUNCH_ID
                    def launchName = env.LAUNCH_NAME?.trim() ?: ''
                    def reportUrl = env.ALLA_REPORT_URL ?: ''

                    currentBuild.description = launchName
                        ? "#${launchId} | ${launchName}"
                        : "#${launchId}"

                    if (reportUrl) {
                        echo "Анализ завершён. Прогон #${launchId}. Отчёт: ${reportUrl}"
                    } else {
                        echo "Анализ завершён. Прогон #${launchId}."
                    }
                }
            }
        }
    }

    post {
        success {
            echo "Анализ завершён. Результаты отправлены в TestOps."
        }
        failure {
            echo "Анализ завершился с ошибкой. Проверь логи выше."
        }
    }
}
