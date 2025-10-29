import os
import psycopg2
import psycopg2.extras
from dotenv import load_dotenv

# load .env for local
load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL")


def get_conn():
    return psycopg2.connect(
        DATABASE_URL,
        cursor_factory=psycopg2.extras.RealDictCursor
    )


def fetch_all(query, params=None):
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(query, params or [])
        return cur.fetchall()


def fetch_one(query, params=None):
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(query, params or [])
        return cur.fetchone()


def execute(query, params=None):
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(query, params or [])
        conn.commit()


def execute_returning(query, params=None):
    """
    Run a query that ends with 'RETURNING ...' and get that row back.
    e.g. INSERT ... RETURNING id;
    """
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(query, params or [])
        row = cur.fetchone()
        conn.commit()
        return row
