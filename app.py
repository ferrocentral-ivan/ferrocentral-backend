from flask import Flask, send_from_directory, request, jsonify, session, send_file
from flask_cors import CORS
import os, json, hashlib, secrets
from datetime import datetime, timedelta
from io import BytesIO
from PIL import Image, ImageOps
from backend.database import get_connection, create_tables
from werkzeug.security import generate_password_hash, check_password_hash
try:
    from actualizar_precios_openpyxl import actualizar_precios
except Exception as e:
    actualizar_precios = None
    print("WARN: actualizar_precios_openpyxl no disponible:", e)
from werkzeug.utils import secure_filename
import traceback
from psycopg2.extras import RealDictCursor
import psycopg2
from reportlab.pdfgen import canvas
from reportlab.lib.pagesizes import letter
from reportlab.lib import colors
from urllib.request import urlopen
from reportlab.lib.utils import ImageReader
import textwrap
from zoneinfo import ZoneInfo
from reportlab.pdfbase import pdfmetrics
import smtplib
import ssl
from email.message import EmailMessage







# =======================
# BACKUP PEDIDOS A JSON
# =======================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PEDIDOS_JSON_PATH = os.path.join(BASE_DIR, "pedidos.json")

def ensure_pedidos_json():
    if not os.path.exists(PEDIDOS_JSON_PATH):
        with open(PEDIDOS_JSON_PATH, "w", encoding="utf-8") as f:
            json.dump([], f, ensure_ascii=False, indent=2)

def read_pedidos_json():
    ensure_pedidos_json()
    with open(PEDIDOS_JSON_PATH, "r", encoding="utf-8") as f:
        return json.load(f)

def append_pedido_json(pedido_obj: dict):
    ensure_pedidos_json()
    data = read_pedidos_json()
    data.append(pedido_obj)
    with open(PEDIDOS_JSON_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)



BASE_URL = os.environ.get("FRONTEND_BASE_URL", "https://ferrocentral.com.bo")
  # en local puedes usar "http://127.0.0.1:5000"

app = Flask(__name__, static_folder='.', static_url_path='')

# ==== COOKIES / SESSION (PROD) ====
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY")  # Render ENV

#  Cookie compartida por subdominios (ferrocentral.com.bo y api.ferrocentral.com.bo)
app.config["SESSION_COOKIE_DOMAIN"] = "ferrocentral.com.bo"


#  IMPORTANTE: como mi front y mi api comparten el mismo dominio base,
# NO necesitas SameSite=None. Lax es más estable en Chrome/Edge.
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"

app.config["SESSION_COOKIE_SECURE"] = True
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(days=7)







@app.route("/health")
def health_check():
    return {"status": "healthy"}



CORS(app, origins=[
  "http://127.0.0.1:5000",
  "http://localhost:5000",
  "https://ferrocentral.com.bo",
  "https://www.ferrocentral.com.bo",
  
], supports_credentials=True)


ALLOWED_ORIGINS = {
    "http://127.0.0.1:5000",
    "http://localhost:5000",
    "https://ferrocentral.com.bo",
    "https://www.ferrocentral.com.bo",
}

@app.after_request
def add_cors_headers(resp):
    origin = request.headers.get("Origin")
    if origin in ALLOWED_ORIGINS:
        resp.headers["Access-Control-Allow-Origin"] = origin
        resp.headers["Vary"] = "Origin"
        resp.headers["Access-Control-Allow-Credentials"] = "true"
        resp.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization"
        resp.headers["Access-Control-Allow-Methods"] = "GET, POST, PUT, PATCH, DELETE, OPTIONS"
    return resp





def send_reset_email(to_email, link):
    """
    Envío por RESEND (HTTPS) como primera opción (ideal en Render).
    Si RESEND no está configurado, intenta SMTP.
    Si SMTP falla o falta config, hace fallback a print (no rompe nada).
    """
    subject = "Restablecer contraseña - Ferrocentral"
    body_text = f"""Hola,

Se solicitó restablecer la contraseña de tu cuenta en Ferrocentral.

Abre este enlace para crear una nueva contraseña:
{link}

Si tú no solicitaste este cambio, puedes ignorar este correo.

Ferrocentral
"""

    # =========================================================
    # 1) RESEND (HTTPS) - recomendado para Render
    # =========================================================
    resend_key = os.environ.get("RESEND_API_KEY", "").strip()
    email_from = os.environ.get("EMAIL_FROM", "contacto@ferrocentral.com.bo").strip()

    if resend_key:
        try:
            import requests

            payload = {
                "from": email_from,
                "to": [to_email],
                "subject": subject,
                "text": body_text
            }

            r = requests.post(
                "https://api.resend.com/emails",
                headers={
                    "Authorization": f"Bearer {resend_key}",
                    "Content-Type": "application/json"
                },
                json=payload,
                timeout=15
            )

            if 200 <= r.status_code < 300:
                print(f"INFO: RESEND ok -> enviado a {to_email}")
                return
            else:
                print(f"WARN: RESEND falló ({r.status_code}) -> {r.text}")

        except Exception as e:
            print(f"ERROR: RESEND exception -> {e}")

    # =========================================================
    # 2) SMTP (backup)
    # =========================================================
    smtp_host = os.environ.get("SMTP_HOST", "").strip()
    smtp_port = int(os.environ.get("SMTP_PORT", "587"))
    smtp_user = os.environ.get("SMTP_USER", "").strip()
    smtp_pass = os.environ.get("SMTP_PASS", "").strip()
    smtp_from = os.environ.get("SMTP_FROM", smtp_user).strip()
    smtp_tls = os.environ.get("SMTP_TLS", "1").strip()  # "1" o "0"

    if not smtp_host or not smtp_user or not smtp_pass:
        print("WARN: SMTP no configurado. Link de reset:")
        print(f"Enviar este enlace a {to_email}: {link}")
        return

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = smtp_from
    msg["To"] = to_email
    msg.set_content(body_text)

    try:
        if smtp_port == 465:
            context = ssl.create_default_context()
            with smtplib.SMTP_SSL(smtp_host, smtp_port, context=context, timeout=15) as server:
                server.login(smtp_user, smtp_pass)
                server.send_message(msg)
        else:
            with smtplib.SMTP(smtp_host, smtp_port, timeout=15) as server:
                server.ehlo()
                if smtp_tls == "1":
                    server.starttls(context=ssl.create_default_context())
                    server.ehlo()
                server.login(smtp_user, smtp_pass)
                server.send_message(msg)

        print(f"INFO: SMTP ok -> enviado a {to_email}")

    except Exception as e:
        print(f"ERROR: SMTP -> {e}")
        print("Link de reset (fallback):", link)



BO_TZ = ZoneInfo("America/La_Paz")
UTC_TZ = ZoneInfo("UTC")

def _normalize_iso_offset(s: str) -> str:
    """
    Normaliza offsets:
    - ...+00  -> ...+00:00
    - ...+0000 -> ...+00:00
    - ...Z -> ...+00:00
    """
    s = (s or "").strip()
    if s.endswith("Z"):
        return s[:-1] + "+00:00"

    # +00 o -04 (sin :00)
    import re
    m = re.search(r"([+-]\d{2})$", s)
    if m:
        return s + ":00"

    # +0000 o -0400 (sin :)
    m = re.search(r"([+-]\d{2})(\d{2})$", s)
    if m:
        return s[:-5] + m.group(1) + ":" + m.group(2)

    return s

def fmt_fecha_bo(fecha):
    """
    Convierte fecha (datetime o string) a hora Bolivia.
    - Si viene naive => asumimos UTC (Render)
    - Si viene con tz => convertimos a BO
    Devuelve string: YYYY-MM-DD HH:MM:SS
    """
    if fecha is None:
        return ""

    from datetime import datetime

    # string
    if isinstance(fecha, str):
        s = _normalize_iso_offset(fecha.replace("T", " "))
        # Intento 1: fromisoformat (maneja microsegundos y +00:00)
        try:
            dt = datetime.fromisoformat(s)
        except Exception:
            # Intento 2: formatos clásicos
            for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
                try:
                    dt = datetime.strptime(s, fmt)
                    break
                except Exception:
                    dt = None
            if dt is None:
                return str(fecha)
    else:
        dt = fecha

    # naive => UTC
    if getattr(dt, "tzinfo", None) is None:
        dt = dt.replace(tzinfo=UTC_TZ)

    dt_bo = dt.astimezone(BO_TZ)
    return dt_bo.strftime("%Y-%m-%d %H:%M:%S")





def _row_first_value(row, default=0):
    if not row:
        return default
    # tuple/list
    if isinstance(row, (tuple, list)):
        return row[0] if len(row) > 0 else default
    # dict-like
    if isinstance(row, dict):
        # intenta keys comunes
        for k in ("count", "total", "total_empresas", "total_pedidos"):
            if k in row and row[k] is not None:
                return row[k]
        # si no, agarra el primer value
        vals = list(row.values())
        return vals[0] if vals else default
    return default



def bootstrap_super_admin():
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) AS c FROM admins")
    c = cur.fetchone()["c"] or 0

    if c == 0:
        username = "contacto@ferrocentral.com.bo"
        password = "admin1994"  # luego lo cambiamos
        cur.execute("""
            INSERT INTO admins (username, password_hash, role, active, created_at)
            VALUES (%s, %s, 'SUPER_ADMIN', true, %s)
        """, (username, generate_password_hash(password), datetime.utcnow()))

        conn.commit()

    conn.close()



from functools import wraps

def require_login(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not session.get("role"):
            return jsonify({"ok": False, "error": "No autenticado"}), 401
        return fn(*args, **kwargs)
    return wrapper

def require_role(*roles):
    def deco(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            r = session.get("role")
            if not r:
                return jsonify({"ok": False, "error": "No autenticado"}), 401
            if r not in roles:
                return jsonify({"ok": False, "error": "No autorizado"}), 403
            return fn(*args, **kwargs)
        return wrapper
    return deco

#  Verifica que un ADMIN solo acceda a pedidos propios
def forbid_if_not_owner(cur, pedido_id: int):
    role = session.get("role")
    if role != "ADMIN":
        return None  # SUPER_ADMIN pasa

    admin_id = session.get("admin_id")
    cur.execute("SELECT admin_id FROM pedidos WHERE id = %s", (pedido_id,))

    row = cur.fetchone()

    if not row or row["admin_id"] != admin_id:
        return jsonify({"ok": False, "error": "No autorizado"}), 403

    return None


def audit(action: str, entity: str, entity_id=None, payload=None):
    try:
        conn = get_connection()
        cur = conn.cursor()

        actor_role = session.get("role") or "ANON"
        actor_id = session.get("admin_id") or session.get("empresa_id")

        payload_json = None
        if payload is not None:
            payload_json = json.dumps(payload, ensure_ascii=False)

        cur.execute("""
            INSERT INTO audit_log (actor_role, actor_id, action, entity, entity_id, payload_json, created_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
        """, (actor_role, actor_id, action, entity, entity_id, payload_json, datetime.utcnow().isoformat()))

        conn.commit()
        conn.close()
    except Exception as e:
        # No rompas el sistema si falla el log
        print("AUDIT ERROR:", e)



#  Inicialización (NO tumbar el servidor si la DB falla)

def seed_catalogo_from_json_if_empty():
    """
    Seed seguro:
    - Si productos_catalogo está vacío, intenta cargar productos_precios.json del disco.
    - No reemplaza si ya hay datos en BD.
    - No rompe el arranque si falla.
    """
    try:
        conn = get_connection()
        cur = conn.cursor()

        cur.execute("SELECT COUNT(*) AS n FROM productos_catalogo")
        row = cur.fetchone()
        n = int((row or {}).get("n") or 0)

        if n > 0:
            conn.close()
            return  # ya hay catálogo en BD

        base_dir = os.path.dirname(os.path.abspath(__file__))
        json_path = os.path.join(base_dir, "productos_precios.json")
        if not os.path.exists(json_path):
            conn.close()
            return  # no hay JSON para seedear

        with open(json_path, "r", encoding="utf-8") as f:
            data = json.load(f)

        now = datetime.utcnow().isoformat()
        # Insert masivo simple (7000 rows OK)
        for p in data:
            code = str(p.get("code") or "").strip()
            if not code:
                continue
            cur.execute(
                """
                INSERT INTO productos_catalogo (code, data, updated_at)
                VALUES (%s, %s::jsonb, %s)
                ON CONFLICT (code) DO NOTHING
                """,
                (code, json.dumps(p, ensure_ascii=False), now)
            )

        conn.commit()
        conn.close()
        print(f"SEED OK: productos_catalogo poblado desde JSON: {len(data)} items")

    except Exception as e:
        print("SEED WARN: no se pudo seedear catalogo desde JSON:", e)

try:
    create_tables()
    bootstrap_super_admin()
    seed_catalogo_from_json_if_empty()

    # --- MIGRACIÓN SEGURA: agregar precio_final si no existe ---
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("ALTER TABLE pedido_items ADD COLUMN IF NOT EXISTS precio_final DOUBLE PRECISION")
    # --- MIGRACIÓN SEGURA: tabla para assets (QR bancario, etc.) ---
    cur.execute("""
    CREATE TABLE IF NOT EXISTS app_assets (
    key TEXT PRIMARY KEY,
    mime TEXT,
    data BYTEA,
    sha256 TEXT,
    updated_at TIMESTAMP DEFAULT NOW()
    )
    """)

    cur.execute("ALTER TABLE pedidos ADD COLUMN IF NOT EXISTS tipo TEXT DEFAULT 'pedido'")
    cur.execute("UPDATE pedidos SET tipo='pedido' WHERE tipo IS NULL")
    conn.commit()
    conn.close()

except Exception as e:
    # Importante: no crash del proceso (si no, Render reinicia en bucle)
    print("DB INIT ERROR: la app arrancó sin inicializar DB:", e)


# ---------------- RUTAS DE PÁGINAS ----------------

@app.route("/")
def api_root():
    return {"status": "ok", "service": "FerroCentral API"}



@app.route("/api/admins", methods=["POST"])
@require_role("SUPER_ADMIN")
def crear_admin():
    data = request.json or {}
    username = (data.get("username") or "").strip()
    password = (data.get("password") or "").strip()
    role = (data.get("role") or "ADMIN").strip()

    if not username or not password:
        return jsonify({"ok": False, "error": "Faltan datos"}), 400
    if role not in ("ADMIN", "SUPER_ADMIN"):
        return jsonify({"ok": False, "error": "Rol inválido"}), 400

    from werkzeug.security import generate_password_hash
    password_hash = generate_password_hash(password)

    conn = get_connection()
    cur = conn.cursor()
    try:
        cur.execute("""
             INSERT INTO admins (username, password_hash, role, active, created_at)
             VALUES (%s, %s, %s, true, %s)
        RETURNING id
        """, (username, password_hash, role, datetime.utcnow().isoformat()))
        new_id = cur.fetchone()["id"]

        conn.commit()
        audit("ADMIN_CREADO", "admin", new_id, {"username": username, "role": role})


    except Exception as e:
        conn.close()
        msg = str(e)
        if "duplicate key value" in msg:
             return jsonify({"ok": False, "error": "Ese correo ya existe como admin."}), 400
        return jsonify({"ok": False, "error": msg}), 400


    conn.close()
    return jsonify({"ok": True})

@app.route("/api/admins", methods=["GET"])
@require_role("SUPER_ADMIN")
def listar_admins():
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("""
        SELECT id, username, role, active, created_at
        FROM admins
        ORDER BY created_at DESC
    """)
    rows = cur.fetchall()
    conn.close()
    return jsonify({"ok": True, "admins": [dict(r) for r in rows]})

@app.route("/api/admins/<int:admin_id>/active", methods=["POST"])
@require_role("SUPER_ADMIN")
def activar_desactivar_admin(admin_id):
    data = request.json or {}
    active = True if data.get("active") else False

    conn = get_connection()
    cur = conn.cursor()
    cur.execute("UPDATE admins SET active = %s WHERE id = %s", (active, admin_id))

    conn.commit()
    audit("ADMIN_ACTIVE", "admin", admin_id, {"active": active})

    conn.close()

    return jsonify({"ok": True})

@app.route("/api/audit", methods=["GET"])
@require_role("SUPER_ADMIN")
def api_audit():
    limit = int(request.args.get("limit", 200))
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("""
        SELECT id, actor_role, actor_id, action, entity, entity_id, payload_json, created_at
        FROM audit_log
        ORDER BY id DESC
        LIMIT %s
    """, (limit,))
    rows = cur.fetchall()
    conn.close()
    return jsonify({"ok": True, "logs": [dict(r) for r in rows]})


@app.route('/inicio.html')
def inicio():
    return send_from_directory('.', 'inicio.html')


@app.route('/tienda')
def tienda():
    return send_from_directory('.', 'index.html')


@app.route('/login')
def login():
    return send_from_directory('.', 'login.html')

@app.route('/api/password_reset_request', methods=['POST'])
def api_password_reset_request():
    data = request.json or {}
    usuario = (data.get("usuario") or "").strip()

    if not usuario:
        return jsonify({"ok": False, "error": "Falta correo o NIT"}), 400

    conn = get_connection()
    cur = conn.cursor()

    # Buscar por correo o por NIT
    cur.execute("""
        SELECT id, correo
        FROM empresas
        WHERE correo = %s OR nit = %s
    """, (usuario, usuario))

    row = cur.fetchone()

    if row is None:
        # Por seguridad, respondemos ok igual, para no revelar si existe o no
        conn.close()
        return jsonify({"ok": True})

    token = secrets.token_urlsafe(32)
    expira = (datetime.utcnow() + timedelta(hours=2)).isoformat()

    cur.execute("""
    UPDATE empresas
    SET reset_token = %s, reset_token_expira = %s
    WHERE id = %s
""", (token, expira, row["id"]))


    conn.commit()
    conn.close()

    link = f"{BASE_URL}/reset_password.html?token={token}"
    send_reset_email(row["correo"], link)

    return jsonify({"ok": True})

@app.route('/api/password_reset', methods=['POST'])
def api_password_reset():
    data = request.json or {}
    token = (data.get("token") or "").strip()
    new_password = (data.get("password") or "").strip()

    if not token or not new_password:
        return jsonify({"ok": False, "error": "Faltan datos"}), 400

    if len(new_password) < 6:
        return jsonify({"ok": False, "error": "La contraseña es muy corta"}), 400

    conn = get_connection()
    cur = conn.cursor()

    cur.execute("""
        SELECT id, reset_token_expira
        FROM empresas
        WHERE reset_token = %s
    """, (token,))
    row = cur.fetchone()

    if row is None:
        conn.close()
        return jsonify({"ok": False, "error": "Enlace inválido"}), 400

    expira_str = row["reset_token_expira"]
    try:
        expira = datetime.fromisoformat(expira_str) if expira_str else None
    except Exception:
        expira = None

    if not expira or expira < datetime.utcnow():
        conn.close()
        return jsonify({"ok": False, "error": "Enlace vencido, solicita uno nuevo"}), 400

    # Actualizar contraseña
    new_hash = hashlib.sha256(new_password.encode()).hexdigest()
    cur.execute("""
        UPDATE empresas
        SET password = %s, reset_token = NULL, reset_token_expira = NULL
        WHERE id = %s
    """, (new_hash, row["id"]))

    conn.commit()
    conn.close()

    return jsonify({"ok": True})


@app.route('/api/pedido', methods=['POST'])
@require_role("EMPRESA")
def api_pedido():

    data = request.json or {}

    empresa_id = session.get("empresa_id")   # ✅ SOLO desde sesión, no del front
    total      = data.get("total")
    notas      = data.get("notas", "")
    items      = data.get("items", [])
    tipo = (data.get("tipo") or "pedido").strip().lower()
    if tipo not in ("pedido", "cotizacion"):
        tipo = "pedido"

    direccion_entrega = data.get("direccion_entrega", "")
    telefono          = data.get("telefono", "")
    lat               = data.get("lat", None)
    lng               = data.get("lng", None)

    maps_url = ""
    try:
        if lat is not None and lng is not None:
            maps_url = f"https://www.google.com/maps/search/?api=1&query={lat},{lng}"
    except Exception:
        maps_url = ""


    if not empresa_id or total is None or items is None or len(items) == 0:
        return jsonify({"ok": False, "error": "Datos de pedido incompletos"}), 400


    import datetime
    fecha = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    conn = get_connection()
    cur = conn.cursor()

    cur.execute("SELECT admin_id FROM empresas WHERE id = %s", (empresa_id,))

    row = cur.fetchone()
    admin_id = row["admin_id"] if row else None


    # Guardar el pedido
    cur.execute("""
    INSERT INTO pedidos (empresa_id, admin_id, fecha, total, estado, notas, tipo, direccion_entrega, telefono, lat, lng, maps_url)
VALUES (%s, %s, %s, %s, 'pendiente', %s, %s, %s, %s, %s, %s, %s)
RETURNING id
""", (empresa_id, admin_id, fecha, total, notas, tipo, direccion_entrega, telefono, lat, lng, maps_url))



    pedido_id = cur.fetchone()["id"]  # ID del nuevo pedido

    # Guardar items
    for item in items:
        cur.execute("""
            INSERT INTO pedido_items (pedido_id, producto_id, descripcion, cantidad, precio_unit)
            VALUES (%s, %s, %s, %s, %s)
        """, (
            pedido_id,
            item["id"],
            item["descripcion"],
            item["cantidad"],
            item["precio_unit"]
        ))

    conn.commit()
    audit("PEDIDO_CREADO", "pedido", pedido_id, {
        "empresa_id": empresa_id,
        "admin_id": admin_id,
        "total": total,
        "items_count": len(items)
    })
    conn.close()

    # ---- BACKUP JSON (pedidos.json) ----
    try:
        append_pedido_json({
            "id": int(pedido_id),
            "empresa_id": int(empresa_id),
            "admin_id": int(admin_id) if admin_id is not None else None,
            "fecha": fecha,
            "estado": "pendiente",
            "total": float(total),
            "items": items,
            "direccion_entrega": direccion_entrega,
            "telefono": telefono,
            "lat": lat,
            "lng": lng,
            "maps_url": maps_url,
            "tipo": tipo,

        })
    except Exception as e:
        print("WARN: no se pudo guardar pedidos.json:", e)

    return jsonify({"ok": True, "pedido_id": pedido_id, "maps_url": maps_url})



@app.route('/api/pedidos')
@require_role("SUPER_ADMIN", "ADMIN")
def api_pedidos():
    conn = get_connection()
    cur = conn.cursor()

    role = session.get("role")
    admin_id = session.get("admin_id")

    if role == "ADMIN":
        cur.execute("""
            SELECT p.id, p.fecha, p.total, p.estado, e.razon_social, COALESCE(p.tipo, 'pedido') AS tipo
            FROM pedidos p
            JOIN empresas e ON e.id = p.empresa_id
            WHERE p.estado NOT IN ('facturado', 'cancelado')
              AND p.admin_id = %s
            ORDER BY p.id DESC
        """, (admin_id,))
    else:
        cur.execute("""
            SELECT p.id, p.fecha, p.total, p.estado, e.razon_social, COALESCE(p.tipo, 'pedido') AS tipo
            FROM pedidos p
            JOIN empresas e ON e.id = p.empresa_id
            WHERE p.estado NOT IN ('facturado', 'cancelado')
            ORDER BY p.id DESC
        """)

    rows = cur.fetchall()
    conn.close()

    #  Ajustar fecha a hora Bolivia SOLO para mostrar en el panel
    for r in rows:
        try:
            r["fecha"] = fmt_fecha_bo(r.get("fecha"))
        except Exception:
            pass


    return jsonify({"ok": True, "pedidos": [dict(r) for r in rows]})

# =========================
# QR BANCARIO (IMAGEN REAL)
# =========================

@app.route('/api/public/qr-banco')
def api_public_qr_banco():
    """Devuelve la imagen del QR bancario actual (solo lectura, público)."""
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT mime, data FROM app_assets WHERE key='bank_qr' LIMIT 1")
    row = cur.fetchone()
    conn.close()

    # Soportar row como dict (RealDictCursor) o tuple/list
    if not row:
        return ("QR no configurado", 404)

    if isinstance(row, dict):
        mime = row.get("mime") or "image/png"
        data = row.get("data")
    else:
        mime = (row[0] or "image/png")
        data = row[1] if len(row) > 1 else None

    if not data:
        return ("QR no configurado", 404)

    # Nombre coherente con el tipo real
    filename = "qr-banco.webp" if (mime or "").lower() == "image/webp" else "qr-banco.png"

    resp = send_file(BytesIO(data), mimetype=mime, download_name=filename, conditional=False)
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    resp.headers["Pragma"] = "no-cache"
    return resp


@app.route('/api/admin/qr-banco', methods=['POST'])
@require_role("SUPER_ADMIN", "ADMIN")
def api_admin_qr_banco_upload():
    """Sube/actualiza el QR bancario (solo ADMIN/SUPER_ADMIN).
    Acepta captura/foto grande del celular, la recorta al centro y la comprime a WEBP.
    """
    f = request.files.get("file") or request.files.get("qr")
    if not f:
        return jsonify({"ok": False, "error": "Falta archivo (field 'file' o 'qr')."}), 400

    # Aceptamos formatos comunes del celular
    mime_in = (f.mimetype or "").lower()
    if mime_in not in ("image/png", "image/jpeg", "image/jpg", "image/webp"):
        return jsonify({"ok": False, "error": "Formato no válido. Usa PNG/JPG/WEBP."}), 400

    raw = f.read()
    if not raw or len(raw) < 200:
        return jsonify({"ok": False, "error": "Imagen vacía o inválida."}), 400

    # Límite de entrada (para no reventar memoria). Capturas del cel suelen estar < 10MB.
    if len(raw) > 12_000_000:
        return jsonify({"ok": False, "error": "Imagen muy grande (máx 12MB)."}), 400

    # --- Procesar: recorte centrado + resize + compresión WEBP ---
    try:
        im = Image.open(BytesIO(raw))
        im = ImageOps.exif_transpose(im)  # respeta rotación de fotos del celular
        im.load()

        w, h = im.size

        # Si es captura vertical/horizontal grande, recortamos al cuadrado del centro (donde suele estar el QR)
        if w == 0 or h == 0:
            raise ValueError("Imagen inválida (0x0).")

        side = min(w, h)
        left = (w - side) // 2
        top = (h - side) // 2
        im = im.crop((left, top, left + side, top + side))

        # Reducir tamaño final: suficiente para escaneo, rápido de cargar
        MAX_SIDE = 1024
        w2, h2 = im.size
        if max(w2, h2) > MAX_SIDE:
            scale = MAX_SIDE / float(max(w2, h2))
            im = im.resize((int(w2 * scale), int(h2 * scale)), Image.LANCZOS)

        # Convertir modo
        if im.mode not in ("RGB", "RGBA"):
            im = im.convert("RGB")

        # Guardar a WEBP con calidad adaptativa (buscamos <= ~900KB)
        out = BytesIO()
        q = 85
        while True:
            out.seek(0)
            out.truncate(0)
            im.save(out, format="WEBP", quality=q, method=6)
            data = out.getvalue()
            if len(data) <= 900_000 or q <= 60:
                break
            q -= 5

        mime = "image/webp"

    except Exception:
        # Si por alguna razón Pillow falla, guardamos el original (pero controlamos tamaño)
        if len(raw) > 2_000_000:
            return jsonify({"ok": False, "error": "No se pudo procesar la imagen. Intenta exportar a WEBP 80% (Photopea) y vuelve a subir."}), 400
        data = raw
        mime = mime_in or "image/png"

    sha = hashlib.sha256(data).hexdigest()
    now = datetime.utcnow()

    conn = get_connection()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO app_assets (key, mime, data, sha256, updated_at)
        VALUES ('bank_qr', %s, %s, %s, %s)
        ON CONFLICT (key)
        DO UPDATE SET mime=EXCLUDED.mime,
                      data=EXCLUDED.data,
                      sha256=EXCLUDED.sha256,
                      updated_at=EXCLUDED.updated_at
    """, (mime, psycopg2.Binary(data) if "psycopg2" in globals() else data, sha, now))

    conn.commit()
    conn.close()

    return jsonify({"ok": True, "sha256": sha, "updated_at": now.isoformat(), "mime": mime, "bytes": len(data)})



# =========================================================
# Teleprompter (aviso giratorio) - settings aislado
# =========================================================

def _ensure_site_settings_table():
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS site_settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
    """)
    conn.commit()
    conn.close()

def _get_setting(key: str, default: str = "") -> str:
    _ensure_site_settings_table()
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT value FROM site_settings WHERE key=%s", (key,))
    row = cur.fetchone()
    conn.close()
    if not row:
        return default
    # row puede venir como tuple o dict según cursor
    if isinstance(row, dict):
        return row.get("value", default)
    return row[0] if row[0] is not None else default

def _set_setting(key: str, value: str):
    _ensure_site_settings_table()
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO site_settings(key,value)
        VALUES(%s,%s)
        ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value
    """, (key, value))
    conn.commit()
    conn.close()

@app.route("/api/public/teleprompter", methods=["GET"])
def api_public_teleprompter():
    """
    Público: devuelve el aviso del teleprompter para inicio.html.
    items: lista de frases (separadas por |)
    """
    active = _get_setting("teleprompter_active", "1")
    raw = _get_setting("teleprompter_items", "")

    if not raw.strip():
        # Si no hay nada guardado, devolvemos ok=false para que el front use el fallback hardcodeado
        return jsonify({"ok": False, "error": "No configurado"}), 200

    items = [x.strip() for x in raw.split("|") if x.strip()]
    return jsonify({
        "ok": True,
        "active": (active != "0"),
        "items": items
    }), 200

@app.route("/api/admin/teleprompter", methods=["PUT"])
@require_role("SUPER_ADMIN")
def api_admin_teleprompter_update():
    """
    Admin (solo SUPER_ADMIN): guarda el aviso.
    Body JSON:
      { "active": true/false, "items": ["frase 1", "frase 2", ...] }
    """
    payload = request.get_json(silent=True) or {}
    active = payload.get("active", True)
    items = payload.get("items", [])

    if not isinstance(items, list):
        return jsonify({"ok": False, "error": "items debe ser una lista"}), 400

    # Sanitizar
    clean = []
    for it in items:
        s = str(it or "").strip()
        if s:
            clean.append(s)

    if not clean:
        return jsonify({"ok": False, "error": "Escribe al menos 1 frase"}), 400

    _set_setting("teleprompter_active", "1" if active else "0")
    _set_setting("teleprompter_items", " | ".join(clean))

    return jsonify({"ok": True}), 200



@app.route("/api/pedidos_json")
@require_role("SUPER_ADMIN", "ADMIN")
def api_pedidos_json():
    data = read_pedidos_json()

    role = session.get("role")
    admin_id = session.get("admin_id")

    if role == "ADMIN":
        data = [p for p in data if p.get("admin_id") == admin_id]

    return jsonify({"ok": True, "pedidos": data})




@app.route('/api/pedidos/<int:pedido_id>')
@require_role("SUPER_ADMIN", "ADMIN")

def api_pedido_detalle(pedido_id):
    conn = get_connection()
    cur = conn.cursor()

    blocked = forbid_if_not_owner(cur, pedido_id)
    if blocked:
        conn.close()
        return blocked


    # Cabecera del pedido + empresa
    cur.execute("""
        SELECT p.id,
               COALESCE(p.tipo, 'pedido') AS tipo,
               p.fecha,
               p.total,
               p.estado,
               p.notas,
                p.direccion_entrega,
                p.telefono,
                p.lat,
                p.lng,
                p.maps_url,
               e.razon_social,
               e.nit,
               e.contacto,
               COALESCE(e.descuento, 0) AS descuento
        FROM pedidos p
        JOIN empresas e ON e.id = p.empresa_id
        WHERE p.id = %s
    """, (pedido_id,))
    header = cur.fetchone()
    
    cur.execute("SELECT 1 FROM pedido_factura_siat WHERE pedido_id=%s", (pedido_id,))
    header_has_pdf = cur.fetchone() is not None


    if header is None:
        conn.close()
        return jsonify({
            "ok": False,
            "error": "Pedido no encontrado",
            "factura_siat": {"exists": header_has_pdf}
        }), 404
    
    
    #  Ajustar fecha a hora Bolivia también en el DETALLE
    try:
        header["fecha"] = fmt_fecha_bo(header.get("fecha"))
    except Exception:
        pass


    # Items del pedido
    cur.execute("""
        SELECT producto_id, descripcion, cantidad, precio_unit
        FROM pedido_items
        WHERE pedido_id = %s
    """, (pedido_id,))
    items = cur.fetchall()
    conn.close()

    return jsonify({
        "ok": True,
        "pedido": dict(header),
        "items": [dict(i) for i in items]
    })


@app.route("/api/pedidos/<int:pedido_id>/cotizacion", methods=["POST"])
@require_role("SUPER_ADMIN", "ADMIN")
def api_pedido_actualizar_cotizacion(pedido_id):
    """
    Guarda/actualiza la cotización (cantidades y precio_final) sin duplicar items.
    Acepta payloads antiguos y nuevos desde admin.html.
    """
    data = request.get_json(silent=True) or {}
    items = data.get("items") or []

    if not isinstance(items, list):
        return jsonify({"ok": False, "error": "items debe ser una lista"}), 400

    conn = get_connection()
    try:
        cur = conn.cursor(cursor_factory=RealDictCursor)

        blocked = forbid_if_not_owner(cur, pedido_id)
        if blocked:
            conn.close()
            return blocked

        cur.execute("SELECT id FROM pedidos WHERE id=%s", (pedido_id,))
        if not cur.fetchone():
            conn.close()
            return jsonify({"ok": False, "error": "Pedido no existe"}), 404

        total = 0.0

        for it in items:
            producto_id = str(it.get("producto_id", "")).strip()
            if not producto_id:
               continue

            # Descripción (por compatibilidad; si no viene, usamos el código)
            descripcion = str(
               it.get("descripcion") or it.get("description") or it.get("desc") or ""
            ).strip() or producto_id

            # cantidad segura
            try:
               cantidad = int(it.get("cantidad", 1) or 1)
            except Exception:
               cantidad = 1
            if cantidad < 1:
               cantidad = 1

            # Aceptar llaves alternativas (admin antiguo mandaba precio_unit)
            raw_pf = it.get("precio_final")
            if raw_pf is None:
                raw_pf = it.get("precio_unit")
            if raw_pf is None:
                raw_pf = it.get("precio")
            if raw_pf is None:
                raw_pf = it.get("precioUnit")

            try:
                precio_final = float(raw_pf or 0)
            except Exception:
                precio_final = 0.0

            if precio_final < 0:
                precio_final = 0.0

            #  Guardar precio_final SIN pisar el precio web (precio_unit)
            cur.execute("""
                UPDATE pedido_items
                SET cantidad=%s,
                    precio_final=%s
                WHERE pedido_id=%s AND producto_id=%s
            """, (cantidad, precio_final, pedido_id, producto_id))

            if cur.rowcount == 0:
                # si no existía el item, lo insertamos manteniendo precio_unit=0 (o puedes buscarlo luego)
                cur.execute("""
                    INSERT INTO pedido_items (pedido_id, producto_id, descripcion, cantidad, precio_unit, precio_final)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    ON CONFLICT (pedido_id, producto_id) DO UPDATE
                    SET cantidad=EXCLUDED.cantidad,
                        precio_final=EXCLUDED.precio_final
                """, (pedido_id, producto_id, descripcion, cantidad, 0.0, precio_final))


            total += cantidad * precio_final


        # Guardar total (opcional)
        cur.execute("UPDATE pedidos SET total=%s WHERE id=%s", (total, pedido_id))
        conn.commit()

        return jsonify({"ok": True, "total": total})

    except Exception as e:
        try:
            conn.rollback()
        except Exception:
            pass
        print("ERROR cotizacion:", e)
        return jsonify({"ok": False, "error": str(e)}), 500
    finally:
        try:
            conn.close()
        except Exception:
            pass

@app.route("/api/proforma/<int:pedido_id>")
@require_role("SUPER_ADMIN", "ADMIN")
def proforma_pdf(pedido_id):
    """
    Genera PDF de PROFORMA sin marcar el pedido como facturado.
    (Lo usa el botón "Guardar cambios de cotización" del panel admin.)
    """
    conn = get_connection()
    cur = conn.cursor(cursor_factory=RealDictCursor)

    blocked = forbid_if_not_owner(cur, pedido_id)
    if blocked:
        conn.close()
        return blocked

    # 1 Traer cabecera pedido + empresa
    cur.execute("""
        SELECT p.id, p.fecha, p.total, p.estado, p.notas,
               e.razon_social, e.nit, e.contacto, e.telefono, e.correo,
               COALESCE(e.descuento, 0) AS descuento
        FROM pedidos p
        JOIN empresas e ON p.empresa_id = e.id
        WHERE p.id = %s
    """, (pedido_id,))
    row = cur.fetchone()
    if not row:
        conn.close()
        return jsonify({"ok": False, "error": "Pedido no encontrado"}), 404

    p_fecha = row.get("fecha")
    p_notas = row.get("notas") or ""
    e_razon = row.get("razon_social") or ""
    e_nit = row.get("nit") or ""
    e_contacto = row.get("contacto") or ""
    e_tel = row.get("telefono") or ""
    e_correo = row.get("correo") or ""
    e_desc = float(row.get("descuento") or 0)

    # 2 Traer items (si existe precio_final úsalo)
    try:
        cur.execute("""
            SELECT descripcion, cantidad, precio_unit, precio_final
            FROM pedido_items
            WHERE pedido_id = %s
            ORDER BY producto_id ASC
        """, (pedido_id,))
        items_db = cur.fetchall()
        has_precio_final = True
    except Exception:
        conn.rollback()
        cur.execute("""
            SELECT descripcion, cantidad, precio_unit
            FROM pedido_items
            WHERE pedido_id = %s
            ORDER BY producto_id ASC
        """, (pedido_id,))
        items_db = cur.fetchall()
        has_precio_final = False

    conn.close()

    items = []
    for r in items_db:
        items.append({
            "descripcion": (r.get("descripcion") or ""),
            "cantidad": float(r.get("cantidad") or 0),
            "precio_unit": float(r.get("precio_unit") or 0),
            "precio_final": (None if (not has_precio_final or r.get("precio_final") is None)
                            else float(r.get("precio_final") or 0)),
        })

    # 3) Generar el PDF (mismo formato que facturar, pero sin cambiar estado)
    buffer = BytesIO()
    c = canvas.Canvas(buffer, pagesize=letter)
    width, height = letter

    # Encabezado rojo
    header_h = 45

    def _draw_proforma_header():
        c.setFillColor(colors.HexColor("#e53935"))
        c.rect(0, height - header_h, width, header_h, stroke=0, fill=1)

        texto_y_local = height - (header_h / 2) - 7

        c.setFillColor(colors.white)
        c.setFont("Helvetica-Bold", 20)
        c.drawCentredString(width / 2, texto_y_local, "FACTURA PROFORMA")

        c.setFont("Helvetica-Bold", 12)
        c.drawRightString(width - 50, texto_y_local, f"N° {pedido_id}")

        c.setFillColor(colors.black)



    # Franja roja superior (sin blanco arriba)
    _draw_proforma_header()



    # Logo (local o URL fallback) - SOLO VISUAL
    base_dir = os.path.dirname(os.path.abspath(__file__))
    logo_path = os.path.join(base_dir, "img", "logos", "logo_empresa.png")

    def _draw_logo(img_source):
        c.drawImage(
            img_source,
            25,                 # X: izquierda
            height - (header_h + 115),
            width=195,          # aqui se ajusta SOLO tamaño si quiero
            height=140,          # ajusta SOLO tamaño si quiero
            preserveAspectRatio=True,
            mask="auto",
        )

    try:
        if os.path.exists(logo_path):
            _draw_logo(logo_path)
        else:
            logo_url = "https://ferrocentral.com.bo/img/logos/logo_empresa.png"
            with urlopen(logo_url, timeout=10) as resp:
                data = resp.read()
            _draw_logo(ImageReader(BytesIO(data)))
    except Exception as e:
        print("⚠️ Logo proforma no cargado:", e)



    # Datos empresa
    y = height - (header_h + 35)
    c.setFillColor(colors.black)
    c.setFont("Helvetica-Bold", 12)
    c.drawCentredString(width / 2, y, "Distribuidora FerroCentral")
    y -= 15
    c.setFont("Helvetica", 10)
    c.drawCentredString(width / 2, y, "NIT: 454443545")
    y -= 12
    c.drawCentredString(width / 2, y, "Of: Calle David Avestegui #555 Queru Queru Central")
    y -= 12
    c.drawCentredString(width / 2, y, "Tel.Fijo: 4792110 - WhatsApp: 76920918")

    # Datos cliente
    y -= 35
    c.setFont("Helvetica-Bold", 11)
    c.drawString(60, y, "Datos del cliente")
    y -= 15
    c.setFont("Helvetica", 10)

    def _pdf_text(s):
        if s is None:
            return ""
        s = str(s)
        return s.encode("cp1252", errors="replace").decode("cp1252")

    c.drawString(60, y, f"Razón social: {_pdf_text(e_razon)}"); y -= 12
    c.drawString(60, y, f"NIT: {_pdf_text(e_nit)}"); y -= 12
    c.drawString(60, y, f"Contacto: {_pdf_text(e_contacto)}"); y -= 12
    c.drawString(60, y, f"Teléfono: {_pdf_text(e_tel)}"); y -= 12
    c.drawString(60, y, f"Correo: {_pdf_text(e_correo)}"); y -= 12
    c.drawString(60, y, f"Descuento aplicado: {e_desc:.2f}%"); y -= 12

    # --- Fecha/Hora y Vigencia (solo visual, en azul) ---
    try:
        # p_fecha viene de DB: puede ser datetime o string "YYYY-MM-DD HH:MM:SS"
        if isinstance(p_fecha, str):
            dt = datetime.strptime(p_fecha, "%Y-%m-%d %H:%M:%S")
            # asumimos que ese dt es UTC (Render) y convertimos a Bolivia
            dt = dt.replace(tzinfo=UTC_TZ).astimezone(BO_TZ)
        elif hasattr(p_fecha, "tzinfo"):
            # si es datetime naive, asumir UTC; si tiene tz, convertir
            dt = p_fecha
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=UTC_TZ)
            dt = dt.astimezone(BO_TZ)
        else:
            dt = datetime.now(BO_TZ)

        valido_hasta = dt + timedelta(days=5)

        # Texto en azul
        c.setFillColor(colors.HexColor("#8B0000"))
        c.drawString(60, y, f"Fecha y hora: {dt.strftime('%Y-%m-%d %H:%M')}"); y -= 12
        c.drawString(60, y, f"Válido hasta: {valido_hasta.strftime('%Y-%m-%d %H:%M')}"); y -= 12

        # Volver a negro para el resto del PDF
        c.setFillColor(colors.black)

    except Exception as e:
        # Si algo falla, no romper el PDF
        c.setFillColor(colors.black)
        print("WARN fecha/vigencia proforma:", e)



    # =========================
    # TABLA BONITA (GRID SUAVE)
    # =========================
    y -= 14

    # Margenes tabla
    x0 = 60
    xR = width - 60

    # Columnas: límites (x) para separar bien cada columna
    # Descripción | Cant | P. Base | P. c/desc | Subtotal
    col_desc  = x0
    col_cant  = 345
    col_pbase = 405
    col_pdesc = 470
    col_subt  = 520 # <- INICIO de la columna "Subtotal"
    # xR es el borde derecho final de la tabla


    # Anchos reales en puntos
    desc_w = col_cant - col_desc - 8  # padding
    cant_w = col_pbase - col_cant
    pbase_w = col_pdesc - col_pbase
    pdesc_w = col_subt - col_pdesc
    subt_w = xR - col_subt  # 0, solo referencia

    # Estilo líneas
    grid_color = colors.HexColor("#D9D9D9")   # gris claro elegante
    header_fill = colors.HexColor("#F3F3F3")  # fondo suave

    def wrap_by_width(text, font_name, font_size, max_width):
        """Wrap por ancho real (puntos) para que nunca se salga."""
        if not text:
            return [""]
        words = str(text).split()
        lines = []
        cur = ""
        for w in words:
            test = (cur + " " + w).strip()
            if pdfmetrics.stringWidth(test, font_name, font_size) <= max_width:
                cur = test
            else:
                if cur:
                    lines.append(cur)
                # palabra sola muy larga: cortarla por caracteres (fallback)
                if pdfmetrics.stringWidth(w, font_name, font_size) <= max_width:
                    cur = w
                else:
                    chunk = ""
                    for ch in w:
                        test2 = chunk + ch
                        if pdfmetrics.stringWidth(test2, font_name, font_size) <= max_width:
                            chunk = test2
                        else:
                            if chunk:
                                lines.append(chunk)
                            chunk = ch
                    cur = chunk
        if cur:
            lines.append(cur)
        return lines

    def draw_table_header(y_top):
        h = 18
        # fondo
        c.setFillColor(header_fill)
        c.rect(x0, y_top - h, xR - x0, h, stroke=0, fill=1)
        c.setFillColor(colors.black)

        # borde suave
        c.setStrokeColor(grid_color)
        c.setLineWidth(0.6)
        c.rect(x0, y_top - h, xR - x0, h, stroke=1, fill=0)

        # separadores verticales
        c.line(col_cant,  y_top - h, col_cant,  y_top)
        c.line(col_pbase, y_top - h, col_pbase, y_top)
        c.line(col_pdesc, y_top - h, col_pdesc, y_top)
        c.line(col_subt,  y_top - h, col_subt,  y_top)   # <- NUEVO (Subtotal)

        # textos
        c.setFont("Helvetica-Bold", 8)
        c.drawString(col_desc + 4, y_top - 13, "Descripción")
        c.drawCentredString((col_cant + col_pbase) / 2, y_top - 13, "Cant.")
        c.drawCentredString((col_pbase + col_pdesc) / 2, y_top - 13, "P. Base")
        c.drawCentredString((col_pdesc + col_subt) / 2, y_top - 13, "P. c/desc")
        c.drawCentredString((col_subt + xR) / 2, y_top - 13, "Subtotal")

        return y_top - h


    # dibujar header inicial
    y = draw_table_header(y) - 2

    c.setFont("Helvetica", 8)
    total_desc = 0.0
    total_base = 0.0

    line_h = 10  # altura por línea de texto
    pad_y = 4    # padding vertical interno

    for it in items:
        desc = _pdf_text(it["descripcion"])
        cant = it["cantidad"]
        p_base = it["precio_unit"]
        p_desc = it["precio_final"] if it.get("precio_final") is not None else (p_base * (1 - e_desc / 100.0))
        sub = cant * p_desc

        total_desc += sub
        total_base += cant * p_base

        # wrap descripción con ancho real
        font_name = "Helvetica"
        font_size = 8
        lines = wrap_by_width(desc, font_name, font_size, desc_w)
        n_lines = max(1, len(lines))

        # altura de fila dinámica
        row_h = (n_lines * line_h) + (pad_y * 2)

        # salto de página si no entra la fila completa
        if y - row_h < 90:
            c.showPage()
            _draw_proforma_header()

            y = height - (header_h + 45)
            y = draw_table_header(y) - 2
            c.setFont("Helvetica", 8)

        # dibujar rectángulo de la fila (grid suave)
        c.setStrokeColor(grid_color)
        c.setLineWidth(0.6)
        c.rect(x0, y - row_h, xR - x0, row_h, stroke=1, fill=0)

        # separadores verticales
        c.line(col_cant,  y - row_h, col_cant,  y)
        c.line(col_pbase, y - row_h, col_pbase, y)
        c.line(col_pdesc, y - row_h, col_pdesc, y)
        c.line(col_subt,  y - row_h, col_subt,  y)   


        # texto descripción (multi-línea)
        text_y = y - pad_y - 8  # primera línea
        c.setFont(font_name, font_size)
        for ln in lines:
            c.drawString(col_desc + 4, text_y, ln)
            text_y -= line_h

        # números (centrados/derecha) alineados al centro vertical de la fila
        mid_y = y - (row_h / 2) - 3

        c.setFont("Helvetica", 8)
        c.drawCentredString((col_cant + col_pbase) / 2, mid_y, f"{cant:g}")
        c.drawRightString(col_pdesc - 6, mid_y, f"{p_base:.2f}")   # P. Base (termina en col_pdesc)
        c.drawRightString(col_subt - 6, mid_y, f"{p_desc:.2f}")    # P. c/desc (termina en col_subt)
        c.drawRightString(xR - 6, mid_y, f"{sub:.2f}")             # Subtotal (termina en xR)

        # bajar y
        y -= row_h


        # =========================
    # TOTALES (RECUADRO BONITO)
    # =========================
    y -= 16

    box_h = 46
    # Evitar que se corte en el borde inferior
    if y - box_h < 90:
        c.showPage()
        _draw_proforma_header()
        y = height - (header_h + 45)
        y = draw_table_header(y) - 2
        c.setFont("Helvetica", 8)
        y -= 16

    # Alineado con la tabla (misma zona numérica)
    box_x = col_pbase
    box_w = xR - box_x

    red_bar = colors.HexColor("#e53935")   
    soft_bg = colors.HexColor("#fff5f5")   
    grid    = colors.HexColor("#d9d9d9")

    # Caja principal
    c.setStrokeColor(grid)
    c.setLineWidth(0.8)
    c.setFillColor(soft_bg)
    c.rect(box_x, y - box_h, box_w, box_h, stroke=1, fill=1)

    # Banda roja superior
    bar_h = 14
    c.setFillColor(red_bar)
    c.rect(box_x, y - bar_h, box_w, bar_h, stroke=0, fill=1)

    # Título banda
    c.setFillColor(colors.white)
    c.setFont("Helvetica-Bold", 9)
    c.drawString(box_x + 8, y - 11, "TOTALES")

    # 2 filas
    pad_x   = 8
    right_x = xR - 8
    line1_y = y - 26
    line2_y = y - 40

    c.setFillColor(colors.black)
    c.setFont("Helvetica", 9)
    c.drawString(box_x + pad_x, line1_y, "Total (sin descuento):")
    c.drawString(box_x + pad_x, line2_y, "Total (con descuento):")

    c.setFont("Helvetica-Bold", 9)
    c.drawRightString(right_x, line1_y, f"Bs {total_base:.2f}")
    c.drawRightString(right_x, line2_y, f"Bs {total_desc:.2f}")

    y -= (box_h + 10)


    c.save()
    buffer.seek(0)

    return send_file(
        buffer,
        as_attachment=False,
        download_name=f"proforma_{pedido_id}.pdf",
        mimetype="application/pdf",
    )


@app.route("/api/facturar/<int:pedido_id>")
@require_role("SUPER_ADMIN", "ADMIN")
def generar_factura_pdf(pedido_id):
    conn = get_connection()
    cur = conn.cursor()

    blocked = forbid_if_not_owner(cur, pedido_id)
    if blocked:
        conn.close()
        return blocked

    cur.execute("""
        SELECT p.id, p.fecha, p.total, p.estado, p.notas,
               e.razon_social, e.nit, e.contacto, e.telefono, e.correo,
               COALESCE(e.descuento, 0) AS descuento
        FROM pedidos p
        JOIN empresas e ON p.empresa_id = e.id
        WHERE p.id = %s
    """, (pedido_id,))
    row = cur.fetchone()

    if not row:
        conn.close()
        return jsonify({"ok": False, "error": "Pedido no encontrado"}), 404

    p_id     = row["id"]
    p_fecha  = row.get("fecha") or ""
    p_total  = float(row.get("total") or 0)
    p_estado = row.get("estado") or ""
    p_notas  = row.get("notas") or ""

    e_razon    = row.get("razon_social") or ""
    e_nit      = row.get("nit") or ""
    e_contacto = row.get("contacto") or ""
    e_tel      = row.get("telefono") or ""
    e_correo   = row.get("correo") or ""
    e_desc     = float(row.get("descuento") or 0)


    # Items (también vienen como dict)
    cur.execute("""
        SELECT descripcion, cantidad, precio_unit, NULL::double precision as precio_final
        FROM pedido_items
        WHERE pedido_id = %s
        ORDER BY producto_id ASC
    """, (pedido_id,))
    items_db = cur.fetchall()

    items = []
    for r in items_db:
        items.append({
            "descripcion": (r.get("descripcion") or ""),
            "cantidad": float(r.get("cantidad") or 0),
            "precio_unit": float(r.get("precio_unit") or 0),
            "precio_final": None if r.get("precio_final") is None else float(r.get("precio_final") or 0),
        })


    # Marcar como facturado
    cur.execute("UPDATE pedidos SET estado = 'facturado' WHERE id = %s", (pedido_id,))
    conn.commit()
    audit("PEDIDO_FACTURADO", "pedido", pedido_id, {"total": float(p_total or 0)})
    conn.close()

    # Generar PDF en memoria (más seguro que guardar archivo en Render)
    buffer = BytesIO()
    c = canvas.Canvas(buffer, pagesize=letter)
    width, height = letter

    def _pdf_text(s):
        if s is None:
            return ""
        s = str(s)
        return s.encode("cp1252", errors="replace").decode("cp1252")


    # Franja roja
    c.setFillColorRGB(0.88, 0.22, 0.22)
    c.rect(0, height - 80, width, 80, fill=1, stroke=0)

    c.setFillColor(colors.white)
    c.setFont("Helvetica-Bold", 20)
    c.drawCentredString(width / 2, height - 50, "FACTURA PROFORMA")

    c.setFont("Helvetica-Bold", 12)
    c.drawRightString(width - 40, height - 30, f"Nº {pedido_id}")

    # Logo (local o URL fallback)
    base_dir = os.path.dirname(os.path.abspath(__file__))
    logo_path = os.path.join(base_dir, "img", "logos", "logo_empresa.png")

    def _draw_logo_proforma(img_source):
        c.drawImage(
            img_source,
            50,                 # X fijo: zona izquierda libre
            height - 165,       # Y fijo: debajo de la franja roja
            width=110,          # ⬅️ SOLO crece el logo
            height=55,          # ⬅️ SOLO crece el logo
            preserveAspectRatio=True,
            mask="auto",
        )

    try:
        if os.path.exists(logo_path):
            _draw_logo_proforma(logo_path)
        else:
            logo_url = "https://ferrocentral.com.bo/img/logos/logo_empresa.png"
            with urlopen(logo_url, timeout=10) as resp:
                data = resp.read()
            _draw_logo_proforma(ImageReader(BytesIO(data)))
    except Exception as e:
        print("⚠️ Logo proforma no cargado:", e)


    # Datos empresa
    c.setFillColor(colors.black)
    c.setFont("Helvetica-Bold", 11)
    c.drawString(170, height - 110, "Distribuidora FerroCentral")
    c.setFont("Helvetica", 10)
    c.drawString(170, height - 125, "NIT: 454443545")
    c.drawString(170, height - 140, "Of: Calle David Avestegui #555 Queru Queru Central")
    c.drawString(170, height - 155, "Tel.Fijo: 4792110 - WhatsApp: 76920918")

    # Datos cliente
    y = height - 190
    c.setFont("Helvetica-Bold", 11)
    c.drawString(40, y, "Datos del cliente")
    y -= 15
    c.setFont("Helvetica", 10)
    c.drawString(40, y, _pdf_text(f"Razón social: {e_razon}"))
    y -= 12
    c.drawString(40, y, _pdf_text(f"NIT: {e_nit}"))
    y -= 12
    c.drawString(40, y, _pdf_text(f"Contacto: {e_contacto}"))
    y -= 12
    c.drawString(40, y, _pdf_text(f"Teléfono: {e_tel}"))
    y -= 12
    c.drawString(40, y, _pdf_text(f"Correo: {e_correo}"))
    y -= 12
    c.drawString(40, y, f"Descuento aplicado: {e_desc:.2f}%")

    y -= 10
    c.line(40, y, width - 40, y)
    y -= 20

    # Tabla
    c.setFont("Helvetica-Bold", 10)
    c.drawString(40,  y, "Descripción")
    c.drawString(360, y, "Cant.")
    c.drawString(410, y, "P. Base")
    c.drawString(470, y, "P. c/desc")
    c.drawString(540, y, "Subtotal")
    y -= 15
    c.line(40, y, width - 40, y)
    y -= 10

    c.setFont("Helvetica", 8)
    total_desc = 0.0
    total_base = 0.0


    for it in items:
        desc = _pdf_text(it["descripcion"])
        cant = it["cantidad"]
        p_base = it["precio_unit"]
        if it.get("precio_final") is not None:
            p_desc = float(it["precio_final"] or 0)
        else:
            p_desc = p_base * (1 - e_desc / 100.0)

        sub = cant * p_desc
        total_desc += sub
        total_base += cant * p_base


        c.drawString(40, y, desc[:70])
        c.drawRightString(390, y, f"{cant:g}")
        c.drawRightString(455, y, f"{p_base:.2f}")
        c.drawRightString(525, y, f"{p_desc:.2f}")
        c.drawRightString(width - 40, y, f"{sub:.2f}")

        y -= 12
        if y < 80:
            c.showPage()
            y = height - 80
            c.setFont("Helvetica", 9)

    y -= 10
    c.line(350, y, width - 40, y)
    y -= 15
    c.setFont("Helvetica-Bold", 11)
    c.drawRightString(width - 40, y, f"TOTAL (con descuento): Bs {total_desc:.2f}")

    c.showPage()
    c.save()
    buffer.seek(0)

    return send_file(
        buffer,
        as_attachment=False,
        download_name=f"factura_{pedido_id}.pdf",
        mimetype="application/pdf",
    )


@app.route("/api/reporte_facturados")
@require_role("SUPER_ADMIN", "ADMIN")
def reporte_facturados():
    try:
        conn = get_connection()
        cur = conn.cursor(cursor_factory=RealDictCursor)

        cur.execute("""
            SELECT
                p.id AS pedido_id,
                COALESCE(p.facturado_en, p.fecha) AS fecha,
                p.total AS total,
                e.razon_social AS empresa,
                e.nit AS nit
            FROM pedidos p
            LEFT JOIN empresas e ON e.id = p.empresa_id
            WHERE p.estado = 'facturado'
            ORDER BY COALESCE(p.facturado_en, p.fecha) DESC NULLS LAST
        """)
        rows = cur.fetchall()
        conn.close()

        buffer = BytesIO()
        c = canvas.Canvas(buffer, pagesize=letter)
        width, height = letter

        def _pdf_text(s):
            if s is None:
                return ""
            s = str(s)
            return s.encode("cp1252", errors="replace").decode("cp1252")

        def draw_header():
            # Franja roja
            c.setFillColorRGB(0.88, 0.22, 0.22)
            c.rect(0, height - 70, width, 70, fill=1, stroke=0)

            c.setFillColor(colors.white)
            c.setFont("Helvetica-Bold", 18)
            c.drawCentredString(width / 2, height - 42, "LIBRO DE VENTAS - FACTURAS SIAT")

            # Logo (mismo patrón que proforma)
            base_dir = os.path.dirname(os.path.abspath(__file__))
            logo_path = os.path.join(base_dir, "img", "logos", "logo_empresa.png")

            def _draw_logo(img_source):
                c.drawImage(
                    img_source,
                    40,
                    height - 62,
                    width=95,
                    height=48,
                    preserveAspectRatio=True,
                    mask="auto",
                )

            try:
                if os.path.exists(logo_path):
                    _draw_logo(logo_path)
                else:
                    logo_url = "https://ferrocentral.com.bo/img/logos/logo_empresa.png"
                    with urlopen(logo_url, timeout=10) as resp:
                        data = resp.read()
                    _draw_logo(ImageReader(BytesIO(data)))
            except Exception:
                pass

            c.setFillColor(colors.black)
            c.setFont("Helvetica", 9)
            c.drawRightString(width - 40, height - 85, f"Generado: {fmt_fecha_bo(datetime.now(BO_TZ))}")

        def draw_table_header(y):
            c.setFillColorRGB(0.95, 0.95, 0.95)
            c.roundRect(40, y - 18, width - 80, 22, 6, fill=1, stroke=0)

            c.setFillColor(colors.black)
            c.setFont("Helvetica-Bold", 10)
            c.drawString(55,  y - 12, "Fecha")
            c.drawString(165, y - 12, "Empresa")
            c.drawString(345, y - 12, "NIT")
            c.drawString(440, y - 12, "Pedido")
            c.drawRightString(width - 55, y - 12, "Total (Bs)")

            return y - 28

        draw_header()

        y = height - 120
        y = draw_table_header(y)

        c.setFont("Helvetica", 9)

        total_general = 0.0

        for r in rows:
            fecha_raw = r.get("fecha")
            fecha_txt = fmt_fecha_bo(fecha_raw)
            # mostrar sin segundos para que se vea más limpio
            if len(fecha_txt) >= 16:
                fecha_txt = fecha_txt[:16]

            empresa = _pdf_text(r.get("empresa") or "-")
            nit = _pdf_text(r.get("nit") or "-")
            pedido_id = r.get("pedido_id")
            total = float(r.get("total") or 0)
            total_general += total

            # fila con “tarjetita” suave
            c.setFillColorRGB(1, 1, 1)
            c.roundRect(40, y - 16, width - 80, 20, 6, fill=1, stroke=1)

            c.setFillColor(colors.black)
            c.drawString(55, y - 10, fecha_txt)
            c.drawString(165, y - 10, empresa[:26])
            c.drawString(345, y - 10, nit[:14])
            c.drawString(440, y - 10, str(pedido_id))
            c.drawRightString(width - 55, y - 10, f"{total:.2f}")

            y -= 26

            if y < 95:
                c.showPage()
                draw_header()
                y = height - 120
                y = draw_table_header(y)
                c.setFont("Helvetica", 9)

        # Total general
        y -= 10
        c.setFillColorRGB(0.95, 0.95, 0.95)
        c.roundRect(40, y - 20, width - 80, 26, 6, fill=1, stroke=0)
        c.setFillColor(colors.black)
        c.setFont("Helvetica-Bold", 11)
        c.drawRightString(width - 55, y - 10, f"TOTAL GENERAL: Bs {total_general:.2f}")

        c.showPage()
        c.save()
        buffer.seek(0)

        return send_file(
            buffer,
            as_attachment=False,
            download_name="libro_ventas_facturados.pdf",
            mimetype="application/pdf",
        )

    except Exception as e:
        print("❌ Error reporte_facturados:", e)
        return jsonify({"error": "No se pudo generar el libro de ventas"}), 500




@app.route("/api/facturas", methods=["GET"])
@require_role("SUPER_ADMIN", "ADMIN")
def api_listar_facturas():
    conn = get_connection()
    cur = conn.cursor()

    role = session.get("role")
    admin_id = session.get("admin_id")

    if role == "ADMIN":
        cur.execute("""
            SELECT
                p.id,
                p.fecha,
                p.total,
                p.factura_nro,
                e.razon_social,
                e.nit,
                s.filename,
                s.cuf,
                s.uploaded_at
            FROM pedidos p
            JOIN empresas e ON e.id = p.empresa_id
            LEFT JOIN pedido_factura_siat s ON s.pedido_id = p.id
            WHERE p.estado = 'facturado'
              AND p.admin_id = %s
            ORDER BY p.fecha DESC, p.id DESC
        """, (admin_id,))
    else:
        cur.execute("""
            SELECT
                p.id,
                p.fecha,
                p.total,
                p.factura_nro,
                e.razon_social,
                e.nit,
                s.filename,
                s.cuf,
                s.uploaded_at
            FROM pedidos p
            JOIN empresas e ON e.id = p.empresa_id
            LEFT JOIN pedido_factura_siat s ON s.pedido_id = p.id
            WHERE p.estado = 'facturado'
            ORDER BY p.fecha DESC, p.id DESC
        """)

    rows = cur.fetchall()
    conn.close()

    out = []
    for r in rows:
        rid = r["id"] if isinstance(r, dict) else r[0]
        fecha = r["fecha"] if isinstance(r, dict) else r[1]
        total = r["total"] if isinstance(r, dict) else r[2]
        factura_nro = r.get("factura_nro") if isinstance(r, dict) else r[3]
        razon = r.get("razon_social") if isinstance(r, dict) else r[4]
        nit = r.get("nit") if isinstance(r, dict) else r[5]
        filename = r.get("filename") if isinstance(r, dict) else r[6]
        cuf = r.get("cuf") if isinstance(r, dict) else r[7]
        uploaded_at = r.get("uploaded_at") if isinstance(r, dict) else r[8]

        out.append({
            "pedido_id": rid,
            "fecha": fmt_fecha_bo(fecha),                 # Bolivia
            "total": float(total or 0),
            "razon_social": razon or "",
            "nit": nit or "",
            "factura_nro": factura_nro,
            "cuf": cuf,
            "filename": filename,
            "has_pdf": bool(filename),
            "uploaded_at": fmt_fecha_bo(uploaded_at) if uploaded_at else ""
        })

    return jsonify({"ok": True, "facturas": out})



@app.route('/api/pedidos/<int:pedido_id>/estado', methods=['POST'])
@require_role("SUPER_ADMIN", "ADMIN")

def api_pedido_cambiar_estado(pedido_id):
    data = request.json or {}
    nuevo_estado = (data.get("estado") or "").strip()

    if not nuevo_estado:
        return jsonify({"ok": False, "error": "Estado vacío"}), 400

    conn = get_connection()
    cur = conn.cursor()

    blocked = forbid_if_not_owner(cur, pedido_id)
    if blocked:
        conn.close()
        return blocked

    cur.execute("UPDATE pedidos SET estado = %s WHERE id = %s", (nuevo_estado, pedido_id))
    if cur.rowcount == 0:
        conn.close()
        return jsonify({"ok": False, "error": "Pedido no encontrado"}), 404

    conn.commit()
    audit("PEDIDO_ESTADO", "pedido", pedido_id, {"estado": nuevo_estado})
    conn.close()

    return jsonify({"ok": True, "estado": nuevo_estado})

@app.route("/api/pedidos/<int:pedido_id>/factura_siat", methods=["POST"])
@require_role("SUPER_ADMIN", "ADMIN")
def subir_factura_siat(pedido_id):
    try:
        # Validación archivo
        if "file" not in request.files:
            return jsonify(ok=False, error="No se envió el archivo PDF"), 400

        f = request.files["file"]
        if not f or not f.filename:
            return jsonify(ok=False, error="Archivo vacío"), 400

        if not f.filename.lower().endswith(".pdf"):
            return jsonify(ok=False, error="El archivo debe ser PDF"), 400

        # Campos opcionales
        factura_nro = (request.form.get("factura_nro") or "").strip()
        cuf = (request.form.get("cuf") or "").strip()

        # Leer bytes (BYTEA)
        pdf_bytes = f.read()
        if not pdf_bytes:
            return jsonify(ok=False, error="PDF vacío"), 400

        # Nombre seguro
        filename = secure_filename(f.filename) or f"pedido_{pedido_id}.pdf"

        # Guardar en BD (coincide con database.py: filename, pdf, cuf, factura_nro, emitida_en, uploaded_at)
        now = datetime.utcnow().isoformat()

        conn = get_connection()
        cur = conn.cursor()

        # Seguridad: ADMIN solo sus pedidos
        blocked = forbid_if_not_owner(cur, pedido_id)
        if blocked:
            conn.close()
            return blocked

        cur.execute("""
            INSERT INTO pedido_factura_siat (pedido_id, filename, pdf, cuf, factura_nro, emitida_en, uploaded_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (pedido_id) DO UPDATE
            SET filename   = EXCLUDED.filename,
                pdf        = EXCLUDED.pdf,
                cuf        = EXCLUDED.cuf,
                factura_nro= EXCLUDED.factura_nro,
                emitida_en = EXCLUDED.emitida_en,
                uploaded_at= EXCLUDED.uploaded_at
        """, (pedido_id, filename, psycopg2.Binary(pdf_bytes), cuf or None, factura_nro or None, now, now))

        # Marcar pedido como facturado
        cur.execute("""
            UPDATE pedidos
            SET estado='facturado', facturado_en=%s, factura_nro=%s
            WHERE id=%s
        """, (now, factura_nro or None, pedido_id))

        conn.commit()
        conn.close()

        audit("FACTURA_SIAT_SUBIDA", "pedido", pedido_id, {"filename": filename, "cuf": cuf, "factura_nro": factura_nro})

        return jsonify(ok=True, filename=filename)

    except Exception as e:
        print("❌ ERROR FACTURA SIAT:", str(e))
        traceback.print_exc()
        return jsonify(ok=False, error="Error interno al guardar la factura"), 500

@app.route("/api/facturados")
@require_role("SUPER_ADMIN", "ADMIN")
def api_facturados():
    conn = get_connection()
    cur = conn.cursor()

    role = session.get("role")
    admin_id = session.get("admin_id")

    if role == "ADMIN":
        cur.execute("""
            SELECT
                p.id,
                p.fecha,
                p.total,
                e.razon_social,
                e.nit,
                CASE WHEN fs.pedido_id IS NULL THEN FALSE ELSE TRUE END AS factura_pdf,
                COALESCE(fs.factura_nro, '') AS factura_nro,
                COALESCE(fs.cuf, '') AS cuf
            FROM pedidos p
            JOIN empresas e ON e.id = p.empresa_id
            LEFT JOIN pedido_factura_siat fs ON fs.pedido_id = p.id
            WHERE p.estado = 'facturado'
              AND p.admin_id = %s
            ORDER BY p.id DESC
        """, (admin_id,))
    else:
        cur.execute("""
            SELECT
                p.id,
                p.fecha,
                p.total,
                e.razon_social,
                e.nit,
                CASE WHEN fs.pedido_id IS NULL THEN FALSE ELSE TRUE END AS factura_pdf,
                COALESCE(fs.factura_nro, '') AS factura_nro,
                COALESCE(fs.cuf, '') AS cuf
            FROM pedidos p
            JOIN empresas e ON e.id = p.empresa_id
            LEFT JOIN pedido_factura_siat fs ON fs.pedido_id = p.id
            WHERE p.estado = 'facturado'
            ORDER BY p.id DESC
        """)

    rows = cur.fetchall()
    conn.close()

    data = [dict(r) for r in rows]

    # (opcional) mostrar fecha en hora Bolivia como en /api/pedidos
    for r in data:
        try:
            r["fecha"] = fmt_fecha_bo(r.get("fecha"))
        except Exception:
            pass

    return jsonify({"ok": True, "facturados": data})



@app.route("/api/pedidos/<int:pedido_id>/factura_siat", methods=["GET"])
@require_role("SUPER_ADMIN", "ADMIN")
def api_descargar_factura_siat(pedido_id):
    conn = get_connection()
    cur = conn.cursor()

    blocked = forbid_if_not_owner(cur, pedido_id)
    if blocked:
        conn.close()
        return blocked

    cur.execute("""
        SELECT filename, pdf
        FROM pedido_factura_siat
        WHERE pedido_id = %s
    """, (pedido_id,))
    row = cur.fetchone()
    conn.close()

    if not row:
        return jsonify({"ok": False, "error": "No hay factura SIAT adjunta para este pedido"}), 404

    filename = row[0] if isinstance(row, (list, tuple)) else row.get("filename")
    pdf = row[1] if isinstance(row, (list, tuple)) else row.get("pdf")

    buffer = BytesIO(bytes(pdf))
    buffer.seek(0)

    return send_file(
        buffer,
        as_attachment=False,
        download_name=filename or f"factura_siat_{pedido_id}.pdf",
        mimetype="application/pdf",
    )


@app.route('/api/empresas')
@require_role("SUPER_ADMIN", "ADMIN")
def api_empresas():
    conn = get_connection()
    cur = conn.cursor()

    role = session.get("role")
    admin_id = session.get("admin_id")

    if role == "ADMIN":
        cur.execute("""
            SELECT e.id, e.nit, e.razon_social, e.contacto, e.telefono, e.correo, e.direccion,
                   COALESCE(e.descuento, 0) AS descuento,
                   COALESCE(v.total_vendido, 0) AS total_vendido
            FROM empresas e
            LEFT JOIN (
                SELECT empresa_id, SUM(total) AS total_vendido
                FROM pedidos
                WHERE estado = 'facturado' AND admin_id = %s
                GROUP BY empresa_id
            ) v ON v.empresa_id = e.id
            WHERE e.admin_id = %s
            ORDER BY razon_social ASC
        """, (admin_id, admin_id))
    else:
        cur.execute("""
            SELECT e.id, e.nit, e.razon_social, e.contacto, e.telefono, e.correo, e.direccion,
                COALESCE(e.descuento, 0) AS descuento,
                COALESCE(v.total_vendido, 0) AS total_vendido
            FROM empresas e
            LEFT JOIN (
                SELECT empresa_id, SUM(total) AS total_vendido
                FROM pedidos
                WHERE estado = 'facturado'
                GROUP BY empresa_id
            ) v ON v.empresa_id = e.id
            ORDER BY e.razon_social ASC
        """)


    rows = cur.fetchall()
    conn.close()

    empresas = [dict(r) for r in rows]
    return jsonify({"ok": True, "empresas": empresas})



@app.route('/api/empresas/<int:empresa_id>', methods=['GET', 'DELETE'])
@require_role("SUPER_ADMIN", "ADMIN")

def api_empresa_detalle_o_eliminar(empresa_id):
    conn = get_connection()
    cur = conn.cursor()

    if request.method == 'GET':
        # --- Detalle de empresa (como antes) ---
        cur.execute("""
            SELECT id, nit, razon_social, contacto, telefono, correo, direccion,
                   COALESCE(descuento, 0) AS descuento
            FROM empresas
            WHERE id = %s
        """, (empresa_id,))
        row = cur.fetchone()
        conn.close()

        if row is None:
            return jsonify({"ok": False, "error": "Empresa no encontrada"}), 404

        return jsonify({"ok": True, "empresa": dict(row)})

    # --- DELETE: eliminar empresa ---
    # Primero revisamos si tiene pedidos asociados
    cur.execute("SELECT COUNT(*) AS cnt FROM pedidos WHERE empresa_id = %s", (empresa_id,))
    row = cur.fetchone()
    if row and row["cnt"] > 0:
        conn.close()
        return jsonify({
            "ok": False,
            "error": "No se puede eliminar: la empresa tiene pedidos registrados."
        }), 400

    # Si no tiene pedidos, la eliminamos
    cur.execute("DELETE FROM empresas WHERE id = %s", (empresa_id,))
    if cur.rowcount == 0:
        conn.close()
        return jsonify({"ok": False, "error": "Empresa no encontrada"}), 404

    conn.commit()
    audit("EMPRESA_ELIMINADA", "empresa", empresa_id)

    conn.close()

    return jsonify({"ok": True})



@app.route('/api/empresas/<int:empresa_id>/descuento', methods=['POST'])
@require_role("SUPER_ADMIN", "ADMIN")

def api_actualizar_descuento(empresa_id):
    data = request.json or {}
    descuento = data.get("descuento")

    try:
        descuento = float(descuento)
    except:
        return jsonify({"ok": False, "error": "Descuento inválido"}), 400

    if descuento < 0 or descuento > 100:
        return jsonify({"ok": False, "error": "Debe estar entre 0 y 100"}), 400

    conn = get_connection()
    cur = conn.cursor()

    # --- Seguridad: ADMIN solo puede editar sus empresas ---
    role = session.get("role")
    admin_id = session.get("admin_id")

    cur.execute("SELECT admin_id FROM empresas WHERE id = %s", (empresa_id,))
    row = cur.fetchone()

    if not row:
        conn.close()
        return jsonify({"ok": False, "error": "Empresa no encontrada"}), 404

    empresa_admin_id = row["admin_id"]

    if role == "ADMIN" and empresa_admin_id != admin_id:
        conn.close()
        return jsonify({"ok": False, "error": "No autorizado"}), 403


    cur.execute("UPDATE empresas SET descuento = %s WHERE id = %s", (descuento, empresa_id))
    if cur.rowcount == 0:
        conn.close()
        return jsonify({"ok": False, "error": "Empresa no encontrada"}), 404

    conn.commit()
    audit("EMPRESA_DESCUENTO", "empresa", empresa_id, {"descuento": descuento})
    conn.close()

    return jsonify({"ok": True, "descuento": descuento})



@app.route('/registro_empresa')
def registro_empresa():
    return send_from_directory('.', 'registro_empresa.html')


@app.route('/admin')
def admin_panel():
    return send_from_directory('.', 'admin.html')

# --- ESTÁTICOS (CSS e imágenes del admin/tienda) ---
@app.route('/styles.css')
def serve_styles():
    return send_from_directory('.', 'styles.css')

@app.route('/img/<path:filename>')
def serve_img(filename):
    return send_from_directory('img', filename)



# ---------------- RUTAS API ----------------

from flask import make_response


@app.route("/api/productos_precios.json", methods=["GET"])
def api_productos_precios_json():
    base_dir = os.path.dirname(os.path.abspath(__file__))
    path = os.path.join(base_dir, "productos_precios.json")

    if not os.path.exists(path):
        return jsonify({"ok": False, "error": "No existe productos_precios.json"}), 404

    resp = make_response(send_file(path, mimetype="application/json"))
    # importante: evitar caché para que veas cambios rápido
    resp.headers["Cache-Control"] = "public, max-age=600"  # 10 minutos
    return resp



@app.route("/api/productos/<code>")
def api_producto_por_codigo(code):
    """
    Devuelve un producto buscando el código en el JSON disponible.
    Soporta claves: code, codigo, sku, id (y variantes)
    y normaliza valores tipo "17823.0" vs "17823".
    """
    import re

    def norm(v):
        s = str(v or "").strip()
        # si viene como "17823.0" -> "17823"
        if re.fullmatch(r"\d+(\.0+)?", s):
            try:
                return str(int(float(s)))
            except Exception:
                return s
        return s

    wanted = norm(code)

    # 1) Preferir productos_precios.json si existe (normalmente ahí está el catálogo real con precios)
    base_dir = os.path.dirname(os.path.abspath(__file__))
    candidates = ["productos_precios.json", "productos.json"]

    productos = None
    last_err = None

    for filename in candidates:
        try:
            path = os.path.join(base_dir, filename)
            if os.path.exists(path):
                with open(path, "r", encoding="utf-8") as f:
                    productos = json.load(f)
                break
        except Exception as e:
            last_err = e

    if productos is None:
        return jsonify({"ok": False, "error": f"No se pudo leer productos_precios.json ni productos.json ({last_err})"}), 500

    if not isinstance(productos, list):
        return jsonify({"ok": False, "error": "El archivo de productos no es una lista"}), 500

    # claves posibles
    keys = ["code", "codigo", "sku", "id", "CODIGO", "Código", "Codigo"]

    for p in productos:
        for k in keys:
            if k in p:
                if norm(p.get(k)) == wanted:
                    return jsonify({"ok": True, "producto": p})

    return jsonify({"ok": False, "error": "Producto no encontrado"}), 404


@app.route("/api/catalogo")
def api_catalogo():
    """
    Devuelve TODO el catálogo para la tienda.
    FUENTE DE VERDAD: PostgreSQL (tabla productos_catalogo).
    Fallback: productos_precios.json (solo para no romper).
    Optimización: ETag + Cache-Control para que no descargue 7000+ productos cada vez.
    """
    import json, os, hashlib
    from flask import request, Response, jsonify

    def _make_etag_response(data_list):
        # JSON estable (misma salida = mismo ETag)
        body = json.dumps(data_list, ensure_ascii=False, separators=(",", ":"))
        etag = hashlib.md5(body.encode("utf-8")).hexdigest()
        etag_hdr = f"\"{etag}\""  # formato estándar con comillas

        inm = (request.headers.get("If-None-Match") or "").strip()
        if inm == etag_hdr:
            # No cambió → 304 (cero descarga)
            resp = Response(status=304)
            resp.headers["ETag"] = etag_hdr
            resp.headers["Cache-Control"] = "public, max-age=600"  # 10 min
            return resp

        resp = Response(body, mimetype="application/json")
        resp.headers["ETag"] = etag_hdr
        resp.headers["Cache-Control"] = "public, max-age=600"  # 10 min
        return resp

    # 1) Intentar BD primero
    try:
        conn = get_connection()
        cur = conn.cursor()
        cur.execute("SELECT data FROM productos_catalogo")
        rows = cur.fetchall() or []
        conn.close()

        if rows:
            data = []
            for r in rows:
                item = r.get("data") if isinstance(r, dict) else r[0]
                if isinstance(item, dict):
                    data.append(item)
            return _make_etag_response(data)

    except Exception as e:
        print("CATALOGO DB ERROR:", e)

    # 2) Fallback (solo si BD vacía / falla)
    base_dir = os.path.dirname(os.path.abspath(__file__))
    path = os.path.join(base_dir, "productos_precios.json")

    if not os.path.exists(path):
        return jsonify({"ok": False, "error": "No hay catálogo disponible (BD vacía y falta productos_precios.json)"}), 404

    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)

    return _make_etag_response(data)




@app.get("/api/admin_stats")
@require_role("SUPER_ADMIN", "ADMIN")
def api_admin_stats():
    conn = get_connection()
    cur = conn.cursor()

    def one_value(default=0):
        row = cur.fetchone()
        if not row:
            return default
        if isinstance(row, dict):
            return next(iter(row.values()))
        return row[0]

    role = session.get("role")
    admin_id = session.get("admin_id")

    # =========================
    # SUPER ADMIN → ve TODO
    # =========================
    if role == "SUPER_ADMIN":
        # Total empresas (global)
        cur.execute("SELECT COUNT(*) AS empresas FROM empresas")
        empresas = one_value(0)

        # Pedidos hoy (global)
        cur.execute("""
            SELECT COUNT(*) AS pedidos_hoy
            FROM pedidos
            WHERE DATE(fecha) = CURRENT_DATE
        """)
        pedidos_hoy = one_value(0)

        # Pendientes (global)
        cur.execute("""
            SELECT COUNT(*) AS pendientes
            FROM pedidos
            WHERE estado = 'pendiente'
        """)
        pendientes = one_value(0)

        # Total vendido hoy (global)
        cur.execute("""
            SELECT COALESCE(SUM(total), 0) AS total_hoy
            FROM pedidos
            WHERE DATE(fecha) = CURRENT_DATE
        """)
        total_hoy = one_value(0)

    # =========================
    # ADMIN → SOLO LO SUYO
    # (usando pedidos.admin_id)
    # =========================
    else:
        # Total empresas del admin
        cur.execute("""
            SELECT COUNT(*) AS empresas
            FROM empresas
            WHERE admin_id = %s
        """, (admin_id,))
        empresas = one_value(0)

        # Pedidos hoy del admin
        cur.execute("""
            SELECT COUNT(*) AS pedidos_hoy
            FROM pedidos
            WHERE admin_id = %s
              AND DATE(fecha) = CURRENT_DATE
        """, (admin_id,))
        pedidos_hoy = one_value(0)

        # Pendientes del admin
        cur.execute("""
            SELECT COUNT(*) AS pendientes
            FROM pedidos
            WHERE admin_id = %s
              AND estado = 'pendiente'
        """, (admin_id,))
        pendientes = one_value(0)

        # Total vendido hoy del admin
        cur.execute("""
            SELECT COALESCE(SUM(total), 0) AS total_hoy
            FROM pedidos
            WHERE admin_id = %s
              AND DATE(fecha) = CURRENT_DATE
        """, (admin_id,))
        total_hoy = one_value(0)

    conn.close()

    return jsonify({
        "ok": True,
        "stats": {
            "empresas": int(empresas or 0),
            "pedidos_hoy": int(pedidos_hoy or 0),
            "pendientes": int(pendientes or 0),
            "total_hoy": float(total_hoy or 0),
        }
    })







@app.route("/api/ping")
def api_ping():
    return {"ok": True, "message": "Servidor Flask funcionando ✅"}


@app.get("/api/debug/session")
def debug_session():
    return jsonify({
        "ok": True,
        "role": session.get("role"),
        "admin_id": session.get("admin_id"),
        "empresa_id": session.get("empresa_id"),
        "cookie_header": request.headers.get("Cookie"),
    })



@app.get("/api/productos")
def api_productos():
    base_dir = os.path.dirname(os.path.abspath(__file__))

    
    path = os.path.join(base_dir, "productos_precios.json")

    if not os.path.exists(path):
        
        alt = os.path.join(base_dir, "productos_precios")
        if os.path.exists(alt):
            path = alt
        else:
            return jsonify({"ok": False, "error": f"No encuentro el archivo: {path}"}), 404

    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)

   
    items = data[:50] if isinstance(data, list) else data

    return jsonify({"ok": True, "total": len(data) if isinstance(data, list) else None, "items": items})



@app.route('/api/registro-empresa', methods=['POST'])
@require_role("SUPER_ADMIN", "ADMIN")
def api_registro_empresa():
    data = request.json or {}

    nit          = (data.get('nit') or '').strip()
    razon_social = (data.get('razon_social') or '').strip()
    contacto     = (data.get('contacto') or '').strip()
    telefono     = (data.get('telefono') or '').strip()
    correo       = (data.get('correo') or '').strip()
    direccion    = (data.get('direccion') or '').strip()
    password     = (data.get('password') or '').strip()

    if not all([nit, razon_social, contacto, telefono, correo, direccion, password]):
        return jsonify({"ok": False, "error": "Faltan datos"}), 400

    admin_id = session.get("admin_id")
    if not admin_id:
        return jsonify({"ok": False, "error": "Sesión de admin no válida. Vuelve a iniciar sesión."}), 401
    password_hash = hashlib.sha256(password.encode()).hexdigest()


    try:
        conn = get_connection()
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO empresas (nit, razon_social, contacto, telefono, correo, direccion, password, admin_id)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id
        """, (nit, razon_social, contacto, telefono, correo, direccion, password_hash, admin_id))

        empresa_id = cur.fetchone()["id"]
        conn.commit()
        audit("EMPRESA_CREADA", "empresa", empresa_id, {"nit": nit, "razon_social": razon_social})

        conn.close()
        return jsonify({"ok": True, "message": "Empresa registrada con éxito"})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 400



@app.post("/api/auth/login")
def auth_login():
    data = request.json or {}
    usuario = (data.get("usuario") or "").strip()
    password = (data.get("password") or "").strip()
    tipo = (data.get("tipo") or "empresa").strip() 

    if not usuario or not password:
        return jsonify({"ok": False, "error": "Faltan datos"}), 400

    # ---- LOGIN ADMIN ----
    if tipo == "admin":
        try:
            conn = get_connection()
        except Exception as e:
            return jsonify({"ok": False, "error": "Servidor sin conexión a la base de datos"}), 503

        cur = conn.cursor()
        cur.execute("SELECT * FROM admins WHERE username = %s AND active = true", (usuario,))
        row = cur.fetchone()
        conn.close()
        ...

        if not row or not check_password_hash(row["password_hash"], password):
            return jsonify({"ok": False, "error": "Credenciales inválidas"}), 401

        session.clear()
        session["role"] = row["role"]          # SUPER_ADMIN o ADMIN
        session["admin_id"] = row["id"]
        session["user"] = row["username"] 
        session.permanent = True

        audit("LOGIN", "admin", row["id"], {"role": row["role"]})


        return jsonify({"ok": True, "role": row["role"], "redirect": "/admin.html"})

    # ---- LOGIN EMPRESA ----
    password_hash = hashlib.sha256(password.encode()).hexdigest()

    try:
        conn = get_connection()
    except Exception:
        return jsonify({
            "ok": False,
            "error": "Servidor sin conexión a la base de datos"
        }), 503

    cur = conn.cursor()
    cur.execute("SELECT * FROM empresas WHERE correo = %s OR nit = %s", (usuario, usuario))
    row = cur.fetchone()
    conn.close()


    if not row:
        return jsonify({"ok": False, "error": "Empresa no encontrada"}), 404
    if row["password"] != password_hash:
        return jsonify({"ok": False, "error": "Contraseña incorrecta"}), 401

    session.clear()
    session["role"] = "EMPRESA"
    session["empresa_id"] = row["id"]
    session["user"] = row["correo"]
    session.permanent = True
    audit("LOGIN", "empresa", row["id"])


    return jsonify({
    "ok": True,
    "role": "EMPRESA",
    "redirect": "/index.html",
    "empresa": {
        "id": row["id"],
        "correo": row["correo"],
        "nit": row["nit"],
        "razon_social": row.get("razon_social") or row.get("nombre") or ""
    }
})


# -------------------------
# Compatibilidad (legacy)
# -------------------------
@app.post("/api/login")
def legacy_login():
    # Reusa el login nuevo (admin/empresa)
    return auth_login()

@app.post("/api/logout")
def legacy_logout():
    return auth_logout()

@app.get("/api/me")
def legacy_me():
    return auth_me()



@app.post("/api/auth/logout")
def auth_logout():
    session.clear()
    audit("LOGOUT", "sesion")
    return jsonify({"ok": True})





@app.get("/api/auth/me")
def auth_me():
    role = session.get("role")
    if not role:
        return jsonify({"ok": False}), 401

    resp = {
        "ok": True,
        "role": role,
        "admin_id": session.get("admin_id"),
        "empresa_id": session.get("empresa_id"),
        "user": session.get("user"),
    }

    # Si es empresa, devolvemos datos básicos
    if role == "EMPRESA" and session.get("empresa_id"):
        try:
            conn = get_connection()
            cur = conn.cursor()
            cur.execute("""
                SELECT id, correo, nit, razon_social,
                    COALESCE(descuento, 0) AS descuento
                FROM empresas
                WHERE id = %s
            """, (session["empresa_id"],))
            emp = cur.fetchone()
            conn.close()
            if emp:
                resp["empresa"] = {
                    "id": emp["id"],
                    "correo": emp["correo"],
                    "nit": emp["nit"],
                    "razon_social": emp.get("razon_social") or "",
                    "descuento": float(emp.get("descuento") or 0)
                }
        except Exception:
            pass

    return jsonify(resp)




ALLOWED_EXCEL_EXT = {".xlsx", ".xlsm", ".xls"}

def _ext_of(filename: str) -> str:
    return os.path.splitext(filename or "")[1].lower()

@app.route("/api/admin/precios/upload-excel", methods=["POST"])
@require_role("SUPER_ADMIN")
def api_upload_excel_precios():
    # 1) validar que venga archivo
    if "file" not in request.files:
        return jsonify({"ok": False, "error": "Falta archivo (campo 'file')"}), 400

    f = request.files["file"]
    if not f or not f.filename:
        return jsonify({"ok": False, "error": "Archivo vacío"}), 400

    orig_name = secure_filename(f.filename)
    ext = _ext_of(orig_name)

    # 2) validar extensión
    if ext not in ALLOWED_EXCEL_EXT:
        return jsonify({
            "ok": False,
            "error": f"Extensión no permitida ({ext}). Usa .xlsx o .xlsm"
        }), 400

    # 3) guardar en el mismo directorio del app.py (BASE_DIR)
    base_dir = os.path.dirname(os.path.abspath(__file__))
    # Guardar SIEMPRE con nombre fijo para evitar que se use un Excel viejo
    tmp_path = os.path.join(base_dir, "proveedor_upload_tmp.xlsm")
    final_path = os.path.join(base_dir, "proveedor.xlsm")

    try:
        f.save(tmp_path)
        os.replace(tmp_path, final_path)

        # Limpiar posibles archivos viejos que confunden al sistema
        old_xlsx = os.path.join(base_dir, "proveedor.xlsx")
        if os.path.exists(old_xlsx):
            try:
                os.remove(old_xlsx)
            except Exception:
                pass

    except Exception as e:
        return jsonify({"ok": False, "error": f"No se pudo guardar Excel: {e}"}), 500

    # Forzar que el script siempre use este Excel
    os.environ["EXCEL_FILE"] = "proveedor.xlsm"


    audit("EXCEL_SUBIDO", "sistema", None, {"filename": os.path.basename(final_path)})
    return jsonify({
        "ok": True,
        "saved_as": os.path.basename(final_path),
        "path": final_path
    })


import zipfile
from io import BytesIO

def _get_nuevos_detectados(conn):
    """
    Define 'nuevo' como: producto_overrides.promo_label = 'NUEVO'
    o imagen = 'img/nuevo.jpg' (tu placeholder).
    Ajusta si tu criterio es distinto.
    """
    cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute("""
        SELECT code
        FROM producto_overrides
        WHERE COALESCE(promo_label,'') = 'NUEVO'
           OR COALESCE(imagen,'') = 'img/nuevo.jpg'
    """)
    rows = cur.fetchall()
    return [str(r["code"]) for r in rows if r.get("code")]

@app.get("/api/admin/nuevos-pendientes")
@require_role("SUPER_ADMIN")
def api_admin_nuevos_pendientes():
    conn = get_connection()
    nuevos = _get_nuevos_detectados(conn)
    conn.close()
    return jsonify({"ok": True, "count": len(nuevos), "codes": nuevos})


@app.post("/api/admin/enriquecidos/importar")
@require_role("SUPER_ADMIN")
def api_admin_importar_enriquecidos():
    """
    Recibe JSON:
    {
      "items":[
        {"code":"104010","descripcion":"...","imagen":"truper_export/104010/images/1.webp"}
      ]
    }
    Actualiza:
    - productos_catalogo.descripcion (si existe tabla)
    - producto_overrides.imagen (si aún estaba placeholder)
    - opcional: quita promo_label 'NUEVO' cuando ya está completo
    """
    data = request.get_json(silent=True) or {}
    items = data.get("items") or []
    if not isinstance(items, list) or not items:
        return jsonify({"ok": False, "error": "items vacío"}), 400

    conn = get_connection()
    cur = conn.cursor()

    updated = 0
    for it in items:
        code = str(it.get("code","")).strip()
        desc = (it.get("descripcion") or "").strip()
        img  = (it.get("imagen") or "").strip()
        if not code:
            continue

        # 1) imagen override (solo si era placeholder o vacío)
        cur.execute("""
            UPDATE producto_overrides
               SET imagen = COALESCE(NULLIF(imagen,''), %s),
                   promo_label = CASE
                       WHEN COALESCE(promo_label,'') = 'NUEVO' THEN ''
                       ELSE promo_label
                   END
             WHERE code = %s
        """, (img or None, code))

        # 2) descripción en productos_catalogo si existe
        # (si tu tabla real se llama diferente, lo ajusto)
        if desc:
            try:
                cur.execute("""
                    UPDATE productos_catalogo
                       SET descripcion = %s
                     WHERE code = %s
                """, (desc, code))
            except Exception:
                # si no existe la tabla o columna, no rompe nada
                conn.rollback()
                conn = get_connection()
                cur = conn.cursor()

        updated += 1

    conn.commit()
    conn.close()

    return jsonify({"ok": True, "updated": updated})





@app.route("/api/product_overrides")
def api_product_overrides_all():
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("ALTER TABLE producto_overrides ADD COLUMN IF NOT EXISTS destacado BOOLEAN DEFAULT FALSE")
    cur.execute("ALTER TABLE producto_overrides ADD COLUMN IF NOT EXISTS orden INTEGER DEFAULT 0")
    cur.execute("ALTER TABLE producto_overrides ADD COLUMN IF NOT EXISTS promo_label TEXT")
    conn.commit()

    cur.execute("""
    SELECT
    code,
    oculto,
    imagen,
    COALESCE(destacado,false) AS destacado,
    COALESCE(orden,0) AS orden,
    COALESCE(promo_label,'') AS promo_label
    FROM producto_overrides
    """)
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return jsonify({"ok": True, "overrides": rows})

@app.route("/api/admin/actualizar-precios", methods=["POST"])
@require_role("SUPER_ADMIN")
def api_actualizar_precios():
    if actualizar_precios is None:
        return jsonify({
            "ok": False,
            "error": "Módulo actualizar_precios no disponible en el servidor"
        }), 500

    r = actualizar_precios()

    # Compatibilidad con el panel (evita "undefined")
    if isinstance(r, dict):
        # Nombres nuevos (del script)
        # actualizados, creados_nuevos, filas_excel_validas, descuento_proveedor, en_json_no_en_excel
        r.setdefault("updated", r.get("actualizados"))
        r.setdefault("missing", r.get("en_json_no_en_excel"))
        r.setdefault("rows", r.get("filas_excel_validas"))
        r.setdefault("discount", r.get("descuento_proveedor"))

        # Si el admin.html use estos nombres en español, también los dejamos
        r.setdefault("filas_excel", r.get("filas_excel_validas"))
        r.setdefault("descuento", r.get("descuento_proveedor"))
        r.setdefault("nuevos", r.get("nuevos") if r.get("nuevos") is not None else len(r.get("nuevos_codigos") or []))
        r.setdefault("nuevos_codigos", r.get("nuevos_codigos") or [x.get("code") for x in (r.get("nuevos_detectados") or []) if isinstance(x, dict)])

    return jsonify(r), (200 if r.get("ok") else 400)







@app.route("/api/product_overrides/<code>", methods=["GET", "POST"])
def api_product_override(code):
    code = str(code).strip()
    conn = get_connection()
    cur = conn.cursor()

    cur.execute("ALTER TABLE producto_overrides ADD COLUMN IF NOT EXISTS destacado BOOLEAN DEFAULT FALSE")
    cur.execute("ALTER TABLE producto_overrides ADD COLUMN IF NOT EXISTS orden INTEGER DEFAULT 0")
    conn.commit()


    if request.method == "GET":
        cur.execute(
            "SELECT code, oculto, imagen, COALESCE(destacado,false) AS destacado, COALESCE(orden,0) AS orden FROM producto_overrides WHERE code = %s",
            (code,)
        )
        row = cur.fetchone()
        conn.close()
        if not row:
            return jsonify({"ok": True, "override": None})
        return jsonify({"ok": True, "override": dict(row)})

    # POST: crear / actualizar override (SOLO SUPER_ADMIN)
    if (session.get("role") or "").upper() != "SUPER_ADMIN":
        conn.close()
        return jsonify({"ok": False, "error": "No autorizado"}), 403

    data = request.get_json() or {}
    oculto = True if data.get("oculto") else False
    imagen = (data.get("imagen") or "").strip() or None

    destacado = True if data.get("destacado") else False

    promo_label = (data.get("promo_label") or "").strip() or None


    try:
        orden = int(data.get("orden") or 0)
    except:
        orden = 0

    cur.execute(
        """
        INSERT INTO producto_overrides (code, oculto, imagen, destacado, orden, promo_label)
        VALUES (%s, %s, %s, %s, %s, %s)
        ON CONFLICT(code) DO UPDATE SET
            oculto = excluded.oculto,
            imagen = excluded.imagen,
            destacado = excluded.destacado,
            orden = excluded.orden,
            promo_label = excluded.promo_label
        """,
        (code, oculto, imagen, destacado, orden, promo_label),
    )

    conn.commit()
    conn.close()
    return jsonify({"ok": True})


# ---------------- MAIN ----------------

if __name__ == "__main__":
    import os
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
