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

        REPORT_HTML = 'alla-report.html'
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
                        if (!projectId) {
                            error('Вебхук не содержит $.projectId.')
                        }

                        echo "Вебхук: запуск '${env.LAUNCH_NAME}' в проекте #${projectId}"

                        // Имя передаётся через файл, чтобы избежать shell injection
                        // при подстановке в URL (curl --data-urlencode читает из stdin).
                        def resolveUrl = "${env.ALLA_ENDPOINT}/api/v1/launch/resolve"
                        def responseJson = sh(
                            script: """
                                curl -sf --max-time 30 \\
                                    -G "${resolveUrl}" \\
                                    --data-urlencode "name=${env.LAUNCH_NAME}" \\
                                    --data-urlencode "project_id=${projectId}"
                            """,
                            returnStdout: true
                        ).trim()

                        def parsed = readJSON text: responseJson
                        env.ALLA_LAUNCH_ID = parsed.launch_id as String
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

                    sh """
                        curl -sf --max-time 1800 -X POST "${analyzeUrl}" -o "${env.REPORT_HTML}"
                    """

                    echo "HTML-отчёт сохранён: ${env.REPORT_HTML}"
                }
            }
        }

        stage('Summary') {
            steps {
                script {
                    def launchId = env.ALLA_LAUNCH_ID
                    def launchName = env.LAUNCH_NAME?.trim() ?: ''

                    currentBuild.description = launchName
                        ? "#${launchId} | ${launchName}"
                        : "#${launchId}"

                    echo "Анализ завершён. Прогон #${launchId}. HTML-отчёт: ${env.BUILD_URL}artifact/${env.REPORT_HTML}"
                }
            }
        }
    }

    post {
        always {
            archiveArtifacts(
                artifacts: "${env.REPORT_HTML}",
                allowEmptyArchive: true
            )
            publishHTML([
                allowMissing: true,
                alwaysLinkToLastBuild: false,
                keepAll: true,
                reportDir: '.',
                reportFiles: env.REPORT_HTML,
                reportName: 'alla — AI Анализ тестов',
                reportTitles: ''
            ])
        }
        success {
            echo "Анализ завершён. Результаты отправлены в TestOps."
        }
        failure {
            echo "Анализ завершился с ошибкой. Проверь логи выше."
        }
    }
}
