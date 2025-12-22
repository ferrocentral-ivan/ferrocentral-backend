import os
import psycopg2
from psycopg2.extras import RealDictCursor

def get_connection():
    db_url = os.environ.get("DATABASE_URL")
    if not db_url:
        raise RuntimeError("Falta DATABASE_URL en variables de entorno")

    # Render Postgres normalmente requiere SSL
    return psycopg2.connect(db_url, cursor_factory=RealDictCursor, sslmode="require")


def _try(cur, sql, params=None):
    try:
        cur.execute(sql, params or ())
        return True
    except Exception:
        return False


def create_tables():
    conn = get_connection()
    cur = conn.cursor()

    # ===== EMPRESAS =====
    cur.execute("""
    CREATE TABLE IF NOT EXISTS empresas (
        id SERIAL PRIMARY KEY,
        nit TEXT NOT NULL UNIQUE,
        razon_social TEXT NOT NULL,
        contacto TEXT NOT NULL,
        telefono TEXT NOT NULL,
        correo TEXT NOT NULL UNIQUE,
        direccion TEXT NOT NULL,
        password TEXT NOT NULL,
        descuento DOUBLE PRECISION DEFAULT 0,
        reset_token TEXT,
        reset_token_expira TEXT,
        admin_id INTEGER
    );
    """)

    # ===== ADMINS =====
    cur.execute("""
    CREATE TABLE IF NOT EXISTS admins (
        id SERIAL PRIMARY KEY,
        username TEXT NOT NULL UNIQUE,
        password_hash TEXT NOT NULL,
        role TEXT NOT NULL CHECK(role IN ('SUPER_ADMIN','ADMIN')),
        active BOOLEAN NOT NULL DEFAULT TRUE,
        created_at TEXT NOT NULL
    );
    """)

    # ===== PEDIDOS =====
    cur.execute("""
    CREATE TABLE IF NOT EXISTS pedidos (
        id SERIAL PRIMARY KEY,
        empresa_id INTEGER NOT NULL,
        fecha TEXT NOT NULL,
        total_bs DOUBLE PRECISION NOT NULL,
        estado TEXT NOT NULL DEFAULT 'pendiente',
        lat DOUBLE PRECISION,
        lng DOUBLE PRECISION,
        maps_url TEXT,
        facturado_en TEXT,
        factura_nro TEXT
    );
    """)

    # ===== ITEMS =====
    cur.execute("""
    CREATE TABLE IF NOT EXISTS pedido_items (
        id SERIAL PRIMARY KEY,
        pedido_id INTEGER NOT NULL,
        producto_id TEXT NOT NULL,
        descripcion TEXT NOT NULL,
        cantidad INTEGER NOT NULL,
        precio_unit DOUBLE PRECISION NOT NULL
    );
    """)

    # ===== AUDIT LOG =====
    cur.execute("""
    CREATE TABLE IF NOT EXISTS audit_log (
        id SERIAL PRIMARY KEY,
        actor_role TEXT,
        actor_id INTEGER,
        action TEXT NOT NULL,
        entity TEXT,
        entity_id TEXT,
        payload_json TEXT,
        created_at TEXT NOT NULL
    );
    """)

    conn.commit()
    conn.close()
