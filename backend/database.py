import sqlite3
import os

DB_NAME = "trupper.db"

def get_connection():
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    return conn

def _try(cur, sql, params=()):
    try:
        cur.execute(sql, params)
        return True
    except Exception:
        return False

def create_tables():
    conn = get_connection()
    cur = conn.cursor()

    # ===== EMPRESAS =====
    cur.execute("""
    CREATE TABLE IF NOT EXISTS empresas (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        nit TEXT NOT NULL,
        razon_social TEXT NOT NULL,
        contacto TEXT NOT NULL,
        telefono TEXT NOT NULL,
        correo TEXT NOT NULL UNIQUE,
        direccion TEXT NOT NULL,
        password TEXT NOT NULL
    );
    """)

    # Migraciones simples (si la columna no existe, no pasa nada)
    _try(cur, "ALTER TABLE empresas ADD COLUMN descuento REAL DEFAULT 0;")
    _try(cur, "ALTER TABLE empresas ADD COLUMN reset_token TEXT;")
    _try(cur, "ALTER TABLE empresas ADD COLUMN reset_token_expira TEXT;")
    _try(cur, "ALTER TABLE empresas ADD COLUMN admin_id INTEGER;")  # quien la registró/asignó

    # ===== ADMINS (SUPER_ADMIN / ADMIN) =====
    cur.execute("""
    CREATE TABLE IF NOT EXISTS admins (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT NOT NULL UNIQUE,
        password_hash TEXT NOT NULL,
        role TEXT NOT NULL CHECK(role IN ('SUPER_ADMIN','ADMIN')),
        active INTEGER NOT NULL DEFAULT 1,
        created_at TEXT NOT NULL
    );
    """)

    # ===== PEDIDOS =====
    cur.execute("""
    CREATE TABLE IF NOT EXISTS pedidos (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        empresa_id INTEGER NOT NULL,
        fecha TEXT NOT NULL,
        total REAL NOT NULL,
        estado TEXT NOT NULL DEFAULT 'pendiente',
        notas TEXT,
        admin_id INTEGER,
        facturado_en TEXT,
        factura_nro TEXT,
        FOREIGN KEY (empresa_id) REFERENCES empresas(id)
    );
    """)

    # Migraciones por si ya existía la tabla pedidos
    _try(cur, "ALTER TABLE pedidos ADD COLUMN admin_id INTEGER;")
    _try(cur, "ALTER TABLE pedidos ADD COLUMN facturado_en TEXT;")
    _try(cur, "ALTER TABLE pedidos ADD COLUMN factura_nro TEXT;")

    # ===== ITEMS =====
    cur.execute("""
    CREATE TABLE IF NOT EXISTS pedido_items (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        pedido_id INTEGER NOT NULL,
        producto_id TEXT NOT NULL,
        descripcion TEXT NOT NULL,
        cantidad INTEGER NOT NULL,
        precio_unit REAL NOT NULL,
        FOREIGN KEY (pedido_id) REFERENCES pedidos(id)
    );
    """)

    # ===== OVERRIDES (ocultar/imagen) =====
    cur.execute("""
    CREATE TABLE IF NOT EXISTS producto_overrides (
        code   TEXT PRIMARY KEY,
        oculto INTEGER NOT NULL DEFAULT 0,
        imagen TEXT
    );
    """)

    # ===== AUDITORÍA (recomendado) =====
    cur.execute("""
    CREATE TABLE IF NOT EXISTS audit_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        actor_role TEXT NOT NULL,
        actor_id INTEGER,
        action TEXT NOT NULL,
        entity TEXT NOT NULL,
        entity_id INTEGER,
        payload_json TEXT,
        created_at TEXT NOT NULL
    );
    """)

    conn.commit()
    conn.close()

# Crear la BD automáticamente al importar
if not os.path.exists(DB_NAME):
    create_tables()
