"""PostgreSQL-реализация хранилища правил объединения кластеров."""

import logging

import psycopg

from alla.exceptions import KnowledgeBaseError
from alla.knowledge.merge_rules_models import MergeRule, MergeRulePair

logger = logging.getLogger(__name__)

_DDL = """
CREATE SCHEMA IF NOT EXISTS alla;

CREATE TABLE IF NOT EXISTS alla.merge_rules (
    rule_id          BIGSERIAL    PRIMARY KEY,
    project_id       INTEGER      NOT NULL,
    signature_hash_a TEXT         NOT NULL,
    signature_hash_b TEXT         NOT NULL,
    audit_text_a     TEXT         NOT NULL DEFAULT '',
    audit_text_b     TEXT         NOT NULL DEFAULT '',
    launch_id        INTEGER,
    created_at       TIMESTAMPTZ  NOT NULL DEFAULT now(),
    CONSTRAINT chk_merge_rules_ordered CHECK (signature_hash_a < signature_hash_b)
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_merge_rules_pair
    ON alla.merge_rules (project_id, signature_hash_a, signature_hash_b);

CREATE INDEX IF NOT EXISTS idx_merge_rules_project
    ON alla.merge_rules (project_id);
"""


def _order_pair(
    hash_a: str,
    hash_b: str,
    audit_a: str,
    audit_b: str,
) -> tuple[str, str, str, str]:
    """Вернуть пару в лексикографическом порядке."""
    if hash_a <= hash_b:
        return hash_a, hash_b, audit_a, audit_b
    return hash_b, hash_a, audit_b, audit_a


class PostgresMergeRulesStore:
    """Хранилище правил объединения кластеров в PostgreSQL."""

    def __init__(self, dsn: str) -> None:
        self._dsn = dsn
        self._table_ensured = False

    def _ensure_table(self) -> None:
        if self._table_ensured:
            return
        try:
            with psycopg.connect(self._dsn) as conn:
                with conn.cursor() as cur:
                    cur.execute(_DDL)
                conn.commit()
            self._table_ensured = True
        except Exception as exc:
            raise KnowledgeBaseError(
                f"Ошибка создания таблицы alla.merge_rules: {exc}"
            ) from exc

    def save_rules(
        self,
        project_id: int,
        pairs: list[MergeRulePair],
        launch_id: int | None = None,
    ) -> tuple[list[MergeRule], int, int]:
        """UPSERT правила объединения. Возвращает правила и счётчики create/update."""
        self._ensure_table()

        query = """
            INSERT INTO alla.merge_rules (
                project_id, signature_hash_a, signature_hash_b,
                audit_text_a, audit_text_b, launch_id
            )
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT (project_id, signature_hash_a, signature_hash_b)
            DO UPDATE SET
                audit_text_a = EXCLUDED.audit_text_a,
                audit_text_b = EXCLUDED.audit_text_b,
                launch_id = EXCLUDED.launch_id,
                created_at = now()
            RETURNING rule_id, project_id, signature_hash_a, signature_hash_b,
                      audit_text_a, audit_text_b, launch_id, created_at,
                      (xmax = 0) AS is_insert
        """
        results: list[MergeRule] = []
        created = 0
        updated = 0

        try:
            with psycopg.connect(self._dsn) as conn:
                with conn.cursor() as cur:
                    for pair in pairs:
                        if pair.signature_hash_a == pair.signature_hash_b:
                            continue
                        ha, hb, ta, tb = _order_pair(
                            pair.signature_hash_a,
                            pair.signature_hash_b,
                            pair.audit_text_a,
                            pair.audit_text_b,
                        )
                        cur.execute(
                            query,
                            (project_id, ha, hb, ta, tb, launch_id),
                        )
                        row = cur.fetchone()
                        if row:
                            is_insert = row[8]
                            if is_insert:
                                created += 1
                            else:
                                updated += 1
                            results.append(
                                MergeRule(
                                    rule_id=row[0],
                                    project_id=row[1],
                                    signature_hash_a=row[2],
                                    signature_hash_b=row[3],
                                    audit_text_a=row[4],
                                    audit_text_b=row[5],
                                    launch_id=row[6],
                                    created_at=row[7],
                                )
                            )
                conn.commit()
        except Exception as exc:
            raise KnowledgeBaseError(
                f"Ошибка сохранения правил объединения: {exc}"
            ) from exc

        logger.info(
            "Merge rules: сохранено %d (создано: %d, обновлено: %d)",
            len(results),
            created,
            updated,
        )
        return results, created, updated

    def load_rules(self, project_id: int) -> list[MergeRule]:
        """Загрузить все правила объединения для проекта."""
        self._ensure_table()

        query = """
            SELECT rule_id, project_id, signature_hash_a, signature_hash_b,
                   audit_text_a, audit_text_b, launch_id, created_at
            FROM alla.merge_rules
            WHERE project_id = %s
            ORDER BY created_at
        """
        try:
            with psycopg.connect(self._dsn) as conn:
                with conn.cursor() as cur:
                    cur.execute(query, (project_id,))
                    return [
                        MergeRule(
                            rule_id=row[0],
                            project_id=row[1],
                            signature_hash_a=row[2],
                            signature_hash_b=row[3],
                            audit_text_a=row[4],
                            audit_text_b=row[5],
                            launch_id=row[6],
                            created_at=row[7],
                        )
                        for row in cur.fetchall()
                    ]
        except Exception as exc:
            raise KnowledgeBaseError(
                f"Ошибка загрузки merge rules: {exc}"
            ) from exc

    def delete_rule(self, rule_id: int) -> bool:
        """Удалить правило по rule_id."""
        self._ensure_table()

        query = "DELETE FROM alla.merge_rules WHERE rule_id = %s"
        try:
            with psycopg.connect(self._dsn) as conn:
                with conn.cursor() as cur:
                    cur.execute(query, (rule_id,))
                    deleted = cur.rowcount > 0
                    conn.commit()
                    return deleted
        except Exception as exc:
            raise KnowledgeBaseError(
                f"Ошибка удаления правила объединения: {exc}"
            ) from exc
