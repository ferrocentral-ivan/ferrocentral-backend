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
        admin_id INTEGER,
        fecha TEXT NOT NULL,
        total DOUBLE PRECISION NOT NULL,
        estado TEXT NOT NULL DEFAULT 'pendiente',
        notas TEXT,
        direccion_entrega TEXT,
        telefono TEXT,
        lat DOUBLE PRECISION,
        lng DOUBLE PRECISION,
        maps_url TEXT,
        facturado_en TEXT,
        factura_nro TEXT
    );
    """)

        # ✅ NUEVO: columnas para envío por zona (si la tabla ya existía, esto las agrega)
    cur.execute("ALTER TABLE pedidos ADD COLUMN IF NOT EXISTS zona TEXT;")
    cur.execute("ALTER TABLE pedidos ADD COLUMN IF NOT EXISTS shipping_option TEXT;")
    cur.execute("ALTER TABLE pedidos ADD COLUMN IF NOT EXISTS shipping_fee DOUBLE PRECISION DEFAULT 0;")
    cur.execute("ALTER TABLE pedidos ADD COLUMN IF NOT EXISTS delivery_date_promised TEXT;")


        # ===== ITEMS =====
    cur.execute("""
    CREATE TABLE IF NOT EXISTS pedido_items (
        id SERIAL PRIMARY KEY,
        pedido_id INTEGER NOT NULL,
        producto_id TEXT NOT NULL,
        descripcion TEXT NOT NULL,
        cantidad INTEGER NOT NULL,
        precio_unit DOUBLE PRECISION NOT NULL,
        UNIQUE (pedido_id, producto_id)
    );
    """)


    # ===== PRODUCT OVERRIDES (tu app.py lo usa) =====
    cur.execute("""
    CREATE TABLE IF NOT EXISTS producto_overrides (
        code TEXT PRIMARY KEY,
        oculto BOOLEAN NOT NULL DEFAULT FALSE,
        imagen TEXT,
        destacado BOOLEAN NOT NULL DEFAULT FALSE,
        orden INTEGER NOT NULL DEFAULT 0,
        promo_label TEXT
    );
    """)

        # ===== CATALOGO (FUENTE DE VERDAD) =====
    cur.execute("""
    CREATE TABLE IF NOT EXISTS productos_catalogo (
        code TEXT PRIMARY KEY,
        data JSONB NOT NULL,
        updated_at TEXT NOT NULL
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

        # ===== FACTURA SIAT (PDF adjunto) =====
    cur.execute("""
    CREATE TABLE IF NOT EXISTS pedido_factura_siat (
        pedido_id INTEGER PRIMARY KEY,
        filename TEXT NOT NULL,
        pdf BYTEA NOT NULL,
        cuf TEXT,
        factura_nro TEXT,
        emitida_en TEXT,
        uploaded_at TEXT NOT NULL
    );
    """)

    cur.execute("""
    ALTER TABLE pedidos
    ADD COLUMN IF NOT EXISTS zona TEXT,
    ADD COLUMN IF NOT EXISTS shipping_option TEXT,
    ADD COLUMN IF NOT EXISTS shipping_fee DOUBLE PRECISION DEFAULT 0,
    ADD COLUMN IF NOT EXISTS delivery_date_promised TEXT,
    ADD COLUMN IF NOT EXISTS delivery_status TEXT DEFAULT 'NO_PUBLICADO',
    ADD COLUMN IF NOT EXISTS delivery_ticket_id INTEGER;
    """)


    cur.execute("""
    CREATE TABLE IF NOT EXISTS delivery_zonas (
        id SERIAL PRIMARY KEY,
        nombre TEXT UNIQUE NOT NULL,
        express_fee DOUBLE PRECISION NOT NULL DEFAULT 25,
        estandar_fee DOUBLE PRECISION NOT NULL DEFAULT 15,
        programada_fee DOUBLE PRECISION NOT NULL DEFAULT 10,
        consolidada_fee DOUBLE PRECISION NOT NULL DEFAULT 0,
        express_days INTEGER NOT NULL DEFAULT 1,
        estandar_days INTEGER NOT NULL DEFAULT 2,
        programada_days INTEGER NOT NULL DEFAULT 4,
        consolidada_days INTEGER NOT NULL DEFAULT 6,
        activo BOOLEAN NOT NULL DEFAULT TRUE
    );
    """)


    cur.execute("""
    CREATE TABLE IF NOT EXISTS delivery_tickets (
        id SERIAL PRIMARY KEY,
        pedido_id INTEGER UNIQUE NOT NULL,
        zona TEXT NOT NULL,
        fee DOUBLE PRECISION NOT NULL,
        fecha_prometida TEXT,
        estado TEXT NOT NULL DEFAULT 'ABIERTO',
        driver_nombre TEXT,
        driver_telefono TEXT,
        created_at TEXT NOT NULL
    );
    """)


    zonas = [
        "Centro",
        "Zona Norte",
        "Zona Sur",
        "Zona Oeste",
        "Zona Este",
        "Tiquipaya",
        "Quillacollo",
        "Colcapirhua",
        "Vinto",
        "Sacaba"
    ]

    for z in zonas:
        cur.execute(
            "INSERT INTO delivery_zonas (nombre) VALUES (%s) ON CONFLICT (nombre) DO NOTHING",
            (z,)
        )



    # Seed inicial (solo si no existen)
    cur.execute("""
    INSERT INTO envio_zonas (nombre, express_fee, express_days, estandar_fee, estandar_days, programada_fee, programada_days, consolidada_fee, consolidada_days, activo)
    VALUES
      ('Centro',      15, 1, 10, 2,  7, 3, 0, 4, TRUE),
      ('Norte',       18, 1, 12, 2,  8, 3, 0, 4, TRUE),
      ('Sur',         20, 1, 13, 2,  9, 3, 0, 4, TRUE),
      ('Tiquipaya',   22, 1, 15, 2, 10, 3, 0, 4, TRUE),
      ('Quillacollo', 25, 1, 18, 2, 12, 3, 0, 4, TRUE),
      ('Sacaba',      25, 1, 18, 2, 12, 3, 0, 4, TRUE)
    ON CONFLICT (nombre) DO NOTHING;
    """)

    # ===== PEDIDOS: columnas nuevas (si no existen) =====
    _try(cur, "ALTER TABLE pedidos ADD COLUMN IF NOT EXISTS zona TEXT;")
    _try(cur, "ALTER TABLE pedidos ADD COLUMN IF NOT EXISTS shipping_option TEXT;")
    _try(cur, "ALTER TABLE pedidos ADD COLUMN IF NOT EXISTS shipping_fee DOUBLE PRECISION DEFAULT 0;")
    _try(cur, "ALTER TABLE pedidos ADD COLUMN IF NOT EXISTS delivery_date_promised TEXT;")




    conn.commit()
    conn.close()
