"""Postgres connection helpers for the ops API."""

import os
from contextlib import contextmanager
from pathlib import Path

import psycopg
from psycopg.rows import dict_row


def _read_pg_password() -> str:
    """Read the osb_writer password from /root/.osb_pg_pass on the VPS,
    or fall back to the OPS_PG_PASSWORD env var (handy for local dev)."""
    pass_file = Path("/root/.osb_pg_pass")
    if pass_file.exists():
        return pass_file.read_text().strip()
    pw = os.environ.get("OPS_PG_PASSWORD")
    if not pw:
        raise RuntimeError(
            "Postgres password not available — /root/.osb_pg_pass missing "
            "and OPS_PG_PASSWORD env var not set."
        )
    return pw


def get_dsn() -> str:
    user = os.environ.get("OPS_PG_USER", "osb_writer")
    host = os.environ.get("OPS_PG_HOST", "127.0.0.1")
    port = os.environ.get("OPS_PG_PORT", "5432")
    db = os.environ.get("OPS_PG_DB", "osb_data")
    return f"postgres://{user}:{_read_pg_password()}@{host}:{port}/{db}"


@contextmanager
def conn_cursor():
    """Yields (conn, cursor) with dict rows. Auto-commits on clean exit."""
    with psycopg.connect(get_dsn(), row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            yield conn, cur
        conn.commit()


def query_all(sql: str, params: tuple = ()) -> list[dict]:
    with conn_cursor() as (_, cur):
        cur.execute(sql, params)
        return cur.fetchall()


def query_one(sql: str, params: tuple = ()) -> dict | None:
    with conn_cursor() as (_, cur):
        cur.execute(sql, params)
        return cur.fetchone()
