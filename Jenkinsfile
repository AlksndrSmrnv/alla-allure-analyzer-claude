pipeline {
    agent any

    parameters {
        string(
            name: 'LAUNCH_ID',
            description: 'ID прогона в Allure TestOps (заполняется автоматически вебхуком из TestOps)',
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
            // Токен НЕ задаётся здесь: GenericTrigger.token вычисляется при загрузке
            // pipeline и не поддерживает credentials()-биндинг из environment {}.
            // Задать токен в Jenkins UI:
            //   Job → Configure → Generic Webhook Trigger → Token
            // Endpoint для TestOps: POST /generic-webhook-trigger/invoke?token=<токен>

            // Вытащить ID запуска из JSON-тела вебхука TestOps
            // Путь зависит от формата вебхука TestOps — скорректировать при необходимости
            genericVariables: [
                [key: 'LAUNCH_ID', value: '$.id'],
                [key: 'LAUNCH_STATUS', value: '$.status']
            ],

            // Запускать сборку только когда прогон завершён
            regexpFilterText:  '$LAUNCH_STATUS',
            regexpFilterExpression: 'DONE|FAILED',

            causeString: 'Triggered by Allure TestOps webhook, launch #$LAUNCH_ID',
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
                    // При ручном запуске LAUNCH_ID приходит из params,
                    // при вебхуке — Generic Webhook Trigger кладёт его в env.
                    env.RESOLVED_LAUNCH_ID = params.LAUNCH_ID?.trim() ?: env.LAUNCH_ID?.trim()

                    if (!env.RESOLVED_LAUNCH_ID) {
                        error('LAUNCH_ID не задан. Укажи ID прогона вручную или запусти через вебхук.')
                    }
                    if (!(env.RESOLVED_LAUNCH_ID ==~ /^\d+$/)) {
                        error('LAUNCH_ID должен содержать только цифры.')
                    }

                    env.REPORT_FILE = "alla-report-${env.RESOLVED_LAUNCH_ID}.json"
                    echo "Запуск анализа прогона #${env.RESOLVED_LAUNCH_ID}"
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
