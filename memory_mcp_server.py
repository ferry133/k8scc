#!/usr/bin/env python3
"""MCP Memory Server — DB-backed long-term memory for Claude Code.

Connects to the shared PostgreSQL `knowledge` table.
CLAUDE_USER_ID env var determines the agent_id (defaults to 'claude_code').
Start: python3 memory_mcp_server.py  (stdio transport, spawned by Claude Code)
Requires: DATABASE_URL env var
"""
import os
import psycopg2
from mcp.server.fastmcp import FastMCP

app = FastMCP("memory")
AGENT_ID = os.environ.get("CLAUDE_USER_ID", "claude_code")
DB_URL = os.environ["DATABASE_URL"]


def _conn():
    return psycopg2.connect(DB_URL)


@app.tool()
def remember(fact: str, confidence: float = 0.8) -> str:
    """儲存一條知識到長期記憶。重複的事實會自動加權平均信心度。"""
    with _conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO knowledge (agent_id, fact, confidence, source_count)
            VALUES (%s, %s, %s, 1)
            ON CONFLICT (agent_id, fact) DO UPDATE SET
                confidence   = (knowledge.confidence * knowledge.source_count + %s)
                               / (knowledge.source_count + 1),
                source_count = knowledge.source_count + 1,
                updated_at   = now()
            """,
            (AGENT_ID, fact, confidence, confidence),
        )
    return f"已記憶：{fact}"


@app.tool()
def recall(topic: str) -> str:
    """從長期記憶搜尋相關知識（關鍵字模糊比對，依信心度排序）。"""
    with _conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT fact, confidence FROM knowledge
            WHERE agent_id = %s
              AND fact ILIKE %s AND confidence > 0.4
            ORDER BY confidence DESC LIMIT 10
            """,
            (AGENT_ID, f"%{topic}%"),
        )
        rows = cur.fetchall()
    if not rows:
        return f"找不到關於「{topic}」的記憶"
    return "\n".join(f"・{f}（{c:.0%}）" for f, c in rows)


@app.tool()
def forget(fact: str) -> str:
    """刪除符合關鍵字的記憶（ILIKE 模糊比對）。"""
    with _conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
            DELETE FROM knowledge
            WHERE agent_id = %s AND fact ILIKE %s
            """,
            (AGENT_ID, f"%{fact}%"),
        )
        deleted = cur.rowcount
    return f"已刪除 {deleted} 條記憶"


if __name__ == "__main__":
    app.run()
