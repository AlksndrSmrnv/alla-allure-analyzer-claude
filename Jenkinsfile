pipeline {
    agent any

    parameters {
        string(
            name: 'LAUNCH_ID',
            description: 'ID прогона в Allure TestOps',
            trim: true
        )
        choice(
            name: 'LOG_LEVEL',
            choices: ['INFO', 'DEBUG', 'WARNING', 'ERROR'],
            description: 'Уровень логирования'
        )
    }

    environment {
        // Обязательные: добавить в Jenkins → Manage Credentials как Secret text
        ALLURE_ENDPOINT = credentials('allure-endpoint')
        ALLURE_TOKEN    = credentials('allure-token')

        // Путь к venv внутри workspace
        VENV_DIR        = "${WORKSPACE}/.venv"
        REPORT_FILE     = "alla-report-${params.LAUNCH_ID}.json"
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
                    if (!params.LAUNCH_ID?.trim()) {
                        error('LAUNCH_ID не задан. Укажи ID прогона и запусти снова.')
                    }
                    if (!(params.LAUNCH_ID ==~ /^\d+$/)) {
                        error('LAUNCH_ID должен содержать только цифры.')
                    }
                    echo "Запуск анализа прогона #${params.LAUNCH_ID}"
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
                withCredentials([
                    string(credentialsId: 'langflow-base-url', variable: 'ALLURE_LANGFLOW_BASE_URL'),
                    string(credentialsId: 'langflow-flow-id',  variable: 'ALLURE_LANGFLOW_FLOW_ID'),
                    string(credentialsId: 'langflow-api-key',  variable: 'ALLURE_LANGFLOW_API_KEY')
                ]) {
                    sh """
                        ${VENV_DIR}/bin/alla ${params.LAUNCH_ID} \
                            --output-format json \
                            --log-level ${params.LOG_LEVEL} \
                        > ${REPORT_FILE}
                    """
                }
            }
        }

        stage('Summary') {
            steps {
                script {
                    def report = readJSON file: REPORT_FILE
                    def triage = report.triage_report

                    // failure_count — @property в Python, не сериализуется model_dump().
                    // Считаем вручную из failed_count + broken_count.
                    def failureCount = (triage.failed_count ?: 0) + (triage.broken_count ?: 0)

                    echo """
╔══════════════════════════════════════════════╗
  Прогон #${params.LAUNCH_ID}: ${triage.launch_name ?: '—'}
  Всего:    ${triage.total_results}
  Упало:    ${failureCount}  (failed: ${triage.failed_count}, broken: ${triage.broken_count})
  Кластеров: ${report.clustering_report?.cluster_count ?: '—'}
╚══════════════════════════════════════════════╝
                    """.stripIndent()

                    // Установить описание сборки для быстрого просмотра в UI
                    currentBuild.description =
                        "#${params.LAUNCH_ID} | упало: ${failureCount} | " +
                        "кластеров: ${report.clustering_report?.cluster_count ?: '?'}"
                }
            }
        }
    }

    post {
        always {
            archiveArtifacts(
                artifacts: "alla-report-${params.LAUNCH_ID}.json",
                allowEmptyArchive: true
            )
        }
        success {
            echo "Анализ прогона #${params.LAUNCH_ID} завершён. Результаты отправлены в TestOps."
        }
        failure {
            echo "Анализ завершился с ошибкой. Проверь логи выше."
        }
        cleanup {
            sh "rm -rf ${VENV_DIR}"
        }
    }
}
