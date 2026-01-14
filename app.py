from flask import Flask, send_from_directory, request, jsonify, session, send_file
from flask_cors import CORS
import os, json, hashlib, secrets
from datetime import datetime, timedelta
from io import BytesIO
from backend.database import get_connection, create_tables
from werkzeug.security import generate_password_hash, check_password_hash
from actualizar_precios_openpyxl import actualizar_precios
from werkzeug.utils import secure_filename
import traceback




# =======================
# BACKUP PEDIDOS A JSON
# =======================
from psycopg2.extras import RealDictCursor
from reportlab.lib.utils import ImageReader
import urllib.request
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



BASE_URL = "https://ferrocentral.com.bo"  # en local puedes usar "http://127.0.0.1:5000"

app = Flask(__name__, static_folder='.', static_url_path='')

# ==== COOKIES / SESSION (PROD) ====
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY")  # Render ENV

# ✅ Cookie compartida por subdominios (ferrocentral.com.bo y api.ferrocentral.com.bo)
app.config["SESSION_COOKIE_DOMAIN"] = "ferrocentral.com.bo"


# ✅ IMPORTANTE: como tu front y tu api comparten el mismo dominio base,
# NO necesitas SameSite=None. Lax es más estable en Chrome/Edge.
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"

app.config["SESSION_COOKIE_SECURE"] = True
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(days=7)







@app.route("/health")
def health():
    return {"status": "healthy"}



CORS(app, origins=[
  "http://127.0.0.1:5000",
  "http://localhost:5000",
  "https://ferrocentral.com.bo",
  "https://www.ferrocentral.com.bo",
  
], supports_credentials=True)




def send_reset_email(to_email, link):
    """
    MVP: solo imprime el enlace en la consola.
    Más adelante aquí configuramos el envío real por correo.
    """
    print("******** RESET PASSWORD ********")
    print(f"Enviar este enlace a {to_email}: {link}")
    print("********************************")







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

# 🔒 Verifica que un ADMIN solo acceda a pedidos propios
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



# ✅ Inicialización correcta (FUERA de la función)
create_tables()
bootstrap_super_admin()



# ---------------- RUTAS DE PÁGINAS ----------------



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


@app.route('/')
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
    INSERT INTO pedidos (empresa_id, admin_id, fecha, total, estado, notas, direccion_entrega, telefono, lat, lng, maps_url)
VALUES (%s, %s, %s, %s, 'pendiente', %s, %s, %s, %s, %s, %s)
RETURNING id
""", (empresa_id, admin_id, fecha, total, notas, direccion_entrega, telefono, lat, lng, maps_url))



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
            SELECT p.id, p.fecha, p.total, p.estado, e.razon_social
            FROM pedidos p
            JOIN empresas e ON e.id = p.empresa_id
            WHERE p.estado NOT IN ('facturado', 'cancelado')
              AND p.admin_id = %s
            ORDER BY p.id DESC
        """, (admin_id,))
    else:
        cur.execute("""
            SELECT p.id, p.fecha, p.total, p.estado, e.razon_social
            FROM pedidos p
            JOIN empresas e ON e.id = p.empresa_id
            WHERE p.estado NOT IN ('facturado', 'cancelado')
            ORDER BY p.id DESC
        """)

    rows = cur.fetchall()
    conn.close()

    return jsonify({"ok": True, "pedidos": [dict(r) for r in rows]})


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

    if header is None:
        conn.close()
        return jsonify({"ok": False, "error": "Pedido no encontrado"}), 404

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
    Guarda la cotización (cantidades y precio final) de forma robusta:
    - No depende de ON CONFLICT (por si falta el UNIQUE en Render)
    - Detecta columnas reales de pedido_items y pedidos
    - Siempre hace rollback ante cualquier error
    """

    data = request.get_json(silent=True) or {}
    items = data.get("items") or []

    if not isinstance(items, list):
        return jsonify({"ok": False, "error": "items debe ser una lista"}), 400

    conn = get_connection()
    cur = conn.cursor()

    try:
        # Seguridad: ADMIN solo puede tocar pedidos propios
        blocked = forbid_if_not_owner(cur, pedido_id)
        if blocked:
            conn.close()
            return blocked

        # 1) Detectar columnas reales en pedido_items
        cur.execute("""
            SELECT column_name
            FROM information_schema.columns
            WHERE table_schema='public' AND table_name='pedido_items'
        """)
        rows_cols = cur.fetchall()

        cols = set()
        for r in rows_cols:
            if isinstance(r, dict):
                cols.add(r.get("column_name"))
            else:
                cols.add(r[0])

        # columnas base
        has_desc = "descripcion" in cols
        has_cant = "cantidad" in cols
        has_precio_unit = "precio_unit" in cols
        has_precio_final = "precio_final" in cols

        # Elegir columna de precio donde guardar el precio cotizado
        price_col = "precio_final" if has_precio_final else "precio_unit"

        if not has_cant:
            raise Exception("La tabla pedido_items no tiene columna 'cantidad' (revisa migración).")
        if price_col == "precio_unit" and not has_precio_unit:
            raise Exception("La tabla pedido_items no tiene 'precio_unit' (revisa migración).")

        # 2) Guardar items con UPDATE primero; si no existe, INSERT
        for it in items:
            # producto_id (acepta producto_id o id por compatibilidad)
            raw_pid = it.get("producto_id", it.get("id"))
            try:
                producto_id = int(raw_pid)
            except Exception:
                continue

            # cantidad
            raw_cant = it.get("cantidad", 1)
            try:
                if isinstance(raw_cant, str):
                    raw_cant = raw_cant.replace(",", ".").strip()
                cantidad = int(float(raw_cant or 1))
            except Exception:
                cantidad = 1
            if cantidad < 1:
                cantidad = 1

            # precio final (en tu admin normalmente viene como precio_unit = precio final)
            raw_price = it.get("precio_unit", it.get("precio_final", 0))
            try:
                if isinstance(raw_price, str):
                    raw_price = raw_price.replace(",", ".").strip()
                precio_final = float(raw_price or 0)
            except Exception:
                precio_final = 0.0
            if precio_final < 0:
                precio_final = 0.0

            # descripcion (si existe columna)
            descripcion = (it.get("descripcion") or "").strip()
            if not descripcion and has_desc:
                try:
                    cur.execute("SELECT descripcion FROM productos WHERE id=%s", (producto_id,))
                    rr = cur.fetchone()
                    if rr:
                        if isinstance(rr, dict):
                            descripcion = (rr.get("descripcion") or "").strip()
                        else:
                            descripcion = (rr[0] or "").strip()
                except Exception:
                    pass
            if not descripcion:
                descripcion = f"COD: {producto_id}"

            # UPDATE
            if has_desc:
                cur.execute(
                    f"""
                    UPDATE pedido_items
                    SET cantidad=%s, {price_col}=%s, descripcion=%s
                    WHERE pedido_id=%s AND producto_id=%s
                    """,
                    (cantidad, precio_final, descripcion, pedido_id, producto_id)
                )
            else:
                cur.execute(
                    f"""
                    UPDATE pedido_items
                    SET cantidad=%s, {price_col}=%s
                    WHERE pedido_id=%s AND producto_id=%s
                    """,
                    (cantidad, precio_final, pedido_id, producto_id)
                )

            # Si no existía, INSERT
            if cur.rowcount == 0:
                if has_desc:
                    cur.execute(
                        f"""
                        INSERT INTO pedido_items (pedido_id, producto_id, descripcion, cantidad, {price_col})
                        VALUES (%s, %s, %s, %s, %s)
                        """,
                        (pedido_id, producto_id, descripcion, cantidad, precio_final)
                    )
                else:
                    cur.execute(
                        f"""
                        INSERT INTO pedido_items (pedido_id, producto_id, cantidad, {price_col})
                        VALUES (%s, %s, %s, %s)
                        """,
                        (pedido_id, producto_id, cantidad, precio_final)
                    )

        # 3) Recalcular total desde pedido_items
        cur.execute(
            f"""
            SELECT COALESCE(SUM(cantidad * {price_col}), 0) AS total
            FROM pedido_items
            WHERE pedido_id=%s
            """,
            (pedido_id,)
        )
        rt = cur.fetchone()
        if isinstance(rt, dict):
            total = float(rt.get("total") or 0)
        else:
            total = float(rt[0] or 0)

        # 4) Actualizar pedidos.total y (si existe) pedidos.total_final
        cur.execute("UPDATE pedidos SET total=%s WHERE id=%s", (total, pedido_id))

        cur.execute("""
            SELECT 1
            FROM information_schema.columns
            WHERE table_schema='public' AND table_name='pedidos' AND column_name='total_final'
        """)
        if cur.fetchone():
            cur.execute("UPDATE pedidos SET total_final=%s WHERE id=%s", (total, pedido_id))

        conn.commit()
        audit("COTIZACION_GUARDADA", "pedido", pedido_id, {"total": total, "items_count": len(items)})

        return jsonify({"ok": True, "total": total, "price_col": price_col})

    except Exception as e:
        try:
            conn.rollback()
        except Exception:
            pass
        return jsonify({"ok": False, "error": str(e)}), 500

    finally:
        try:
            cur.close()
        except Exception:
            pass
        try:
            conn.close()
        except Exception:
            pass






def _draw_logo(c, width, height):
    """Dibuja el logo en el PDF. Prioriza un archivo local en el backend;
    si no existe, intenta descargarlo desde el frontend (Hostinger).
    """
    try:
        base_dir = os.path.dirname(os.path.abspath(__file__))
        local_logo = os.path.join(base_dir, "img", "logos", "logo_empresa.png")

        # coordenadas (ajusta si quieres)
        x = -30
        y = height - 70 - 120 + 10
        w = 280
        h = 120

        if os.path.exists(local_logo):
            c.drawImage(local_logo, x, y, width=w, height=h, preserveAspectRatio=True, mask="auto")
            return

        # Fallback: intentar desde Hostinger (URL pública)
        url = "https://ferrocentral.com.bo/img/logos/logo_empresa.png"
        with urllib.request.urlopen(url, timeout=5) as resp:
            img_bytes = resp.read()
        img = ImageReader(BytesIO(img_bytes))
        c.drawImage(img, x, y, width=w, height=h, preserveAspectRatio=True, mask="auto")
    except Exception:
        # si falla, simplemente no mostrar logo (sin romper el PDF)
        return


from reportlab.pdfgen import canvas
from reportlab.lib.pagesizes import letter
from reportlab.lib import colors


@app.route("/api/proforma/<int:pedido_id>")
@require_role("SUPER_ADMIN", "ADMIN")
def generar_proforma_pdf(pedido_id):
    """
    PDF de cotización/proforma (NO marca como facturado).
    Nota: en tu flujo actual, luego de guardar cotización, `pedido_items.precio_unit`
    representa el precio FINAL cotizado. Por eso aquí NO aplicamos descuento nuevamente.
    """
    conn = get_connection()
    cur = conn.cursor(cursor_factory=RealDictCursor)

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
        return "Pedido no encontrado", 404

    e_razon = row.get("razon_social") or ""
    e_nit   = row.get("nit") or ""
    e_cont  = row.get("contacto") or ""
    e_tel   = row.get("telefono") or ""
    e_mail  = row.get("correo") or ""
    e_desc  = float(row.get("descuento") or 0)

    cur.execute("""
        SELECT producto_id, descripcion, cantidad, precio_unit
        FROM pedido_items
        WHERE pedido_id = %s
        ORDER BY producto_id ASC
    """, (pedido_id,))
    items_db = cur.fetchall()
    conn.close()

    items = []
    for r in items_db:
        pid = r.get("producto_id")
        desc = (r.get("descripcion") or "").strip()
        if not desc:
            desc = f"COD: {pid}" if pid is not None else "Producto"
        items.append({
            "producto_id": pid,
            "descripcion": desc,
            "cantidad": float(r.get("cantidad") or 0),
            "precio_unit": float(r.get("precio_unit") or 0),  # precio FINAL cotizado
        })

    buffer = BytesIO()
    c = canvas.Canvas(buffer, pagesize=letter)
    width, height = letter

    # Encabezado
    c.setFillColor(colors.HexColor("#e53935"))
    c.rect(0, height - 70, width, 70, stroke=0, fill=1)
    c.setFillColor(colors.white)
    c.setFont("Helvetica-Bold", 18)
    c.drawCentredString(width / 2, height - 45, "COTIZACIÓN / PROFORMA")
    c.setFont("Helvetica-Bold", 10)
    c.drawRightString(width - 30, height - 35, f"N° {pedido_id}")

    _draw_logo(c, width, height)

    # Datos empresa
    c.setFillColor(colors.black)
    c.setFont("Helvetica-Bold", 10)
    c.drawCentredString(width / 2, height - 105, "Distribuidora FerroCentral")
    c.setFont("Helvetica", 9)
    c.drawCentredString(width / 2, height - 120, "NIT: 454443545")
    c.drawCentredString(width / 2, height - 133, "Of: Calle David Avestegui #555 Queru Queru Central")
    c.drawCentredString(width / 2, height - 146, "Tel.Fijo: 4792110 - WhatsApp: 76920918")

    # Datos cliente
    y = height - 190
    c.setFont("Helvetica-Bold", 10)
    c.drawString(40, y, "Datos del cliente")
    c.setFont("Helvetica", 9)
    y -= 14
    c.drawString(40, y, f"Razón social: {e_razon}")
    y -= 12
    c.drawString(40, y, f"NIT: {e_nit}")
    y -= 12
    c.drawString(40, y, f"Contacto: {e_cont}")
    y -= 12
    c.drawString(40, y, f"Teléfono: {e_tel}")
    y -= 12
    c.drawString(40, y, f"Correo: {e_mail}")
    y -= 12
    c.drawString(40, y, f"Descuento empresa (referencial): {e_desc:.2f}%")

    # Tabla
    y -= 18
    c.setLineWidth(0.5)
    c.line(40, y, width - 40, y)
    y -= 14

    c.setFont("Helvetica-Bold", 9)
    c.drawString(40, y, "Descripción")
    c.drawRightString(width - 190, y, "Cant.")
    c.drawRightString(width - 110, y, "Precio U.")
    c.drawRightString(width - 40, y, "Subtotal")
    y -= 10
    c.line(40, y, width - 40, y)
    y -= 14

    total = 0.0
    c.setFont("Helvetica", 9)
    for it in items:
        cant = float(it["cantidad"] or 0)
        pu   = float(it["precio_unit"] or 0)
        sub  = cant * pu
        total += sub

        c.drawString(40, y, str(it["descripcion"])[:70])
        c.drawRightString(width - 190, y, f"{cant:.0f}")
        c.drawRightString(width - 110, y, f"{pu:.2f}")
        c.drawRightString(width - 40, y, f"{sub:.2f}")
        y -= 14
        if y < 80:
            c.showPage()
            y = height - 80

    y -= 6
    c.setLineWidth(0.8)
    c.line(width - 260, y, width - 40, y)
    y -= 18
    c.setFont("Helvetica-Bold", 10)
    c.drawRightString(width - 40, y, f"TOTAL: Bs {total:.2f}")

    c.showPage()
    c.save()
    buffer.seek(0)

    filename = f"proforma_pedido_{pedido_id}.pdf"
    return send_file(buffer, mimetype="application/pdf", as_attachment=False, download_name=filename)


@app.route("/api/facturar/<int:pedido_id>")
@require_role("SUPER_ADMIN", "ADMIN")
def generar_factura_pdf(pedido_id):
    """
    Genera PDF de factura proforma y marca el pedido como FACTURADO.
    Importante: si quieres que el pedido NO desaparezca de 'pendientes', no lo marques aquí.
    """
    conn = get_connection()
    cur = conn.cursor(cursor_factory=RealDictCursor)

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

    e_razon    = row.get("razon_social") or ""
    e_nit      = row.get("nit") or ""
    e_contacto = row.get("contacto") or ""
    e_tel      = row.get("telefono") or ""
    e_correo   = row.get("correo") or ""
    e_desc     = float(row.get("descuento") or 0)

    cur.execute("""
        SELECT producto_id, descripcion, cantidad, precio_unit
        FROM pedido_items
        WHERE pedido_id = %s
        ORDER BY producto_id ASC
    """, (pedido_id,))
    items_db = cur.fetchall()

    items = []
    for r in items_db:
        pid = r.get("producto_id")
        desc = (r.get("descripcion") or "").strip()
        if not desc:
            desc = f"COD: {pid}" if pid is not None else "Producto"
        items.append({
            "producto_id": pid,
            "descripcion": desc,
            "cantidad": float(r.get("cantidad") or 0),
            "precio_unit": float(r.get("precio_unit") or 0),  # precio FINAL cotizado
        })

    # Marcar como facturado
    cur.execute("UPDATE pedidos SET estado = 'facturado' WHERE id = %s", (pedido_id,))
    conn.commit()
    audit("PEDIDO_FACTURADO", "pedido", pedido_id, {"total": float(row.get("total") or 0)})
    conn.close()

    buffer = BytesIO()
    c = canvas.Canvas(buffer, pagesize=letter)
    width, height = letter

    # Franja roja
    c.setFillColor(colors.HexColor("#e53935"))
    c.rect(0, height - 70, width, 70, stroke=0, fill=1)

    c.setFillColor(colors.white)
    c.setFont("Helvetica-Bold", 18)
    c.drawCentredString(width / 2, height - 45, "FACTURA PROFORMA")
    c.setFont("Helvetica-Bold", 10)
    c.drawRightString(width - 30, height - 35, f"N° {pedido_id}")

    _draw_logo(c, width, height)

    # Datos empresa
    c.setFillColor(colors.black)
    c.setFont("Helvetica-Bold", 10)
    c.drawCentredString(width / 2, height - 105, "Distribuidora FerroCentral")
    c.setFont("Helvetica", 9)
    c.drawCentredString(width / 2, height - 120, "NIT: 454443545")
    c.drawCentredString(width / 2, height - 133, "Of: Calle David Avestegui #555 Queru Queru Central")
    c.drawCentredString(width / 2, height - 146, "Tel.Fijo: 4792110 - WhatsApp: 76920918")

    # Datos cliente
    y = height - 190
    c.setFont("Helvetica-Bold", 10)
    c.drawString(40, y, "Datos del cliente")
    c.setFont("Helvetica", 9)
    y -= 14
    c.drawString(40, y, f"Razón social: {e_razon}")
    y -= 12
    c.drawString(40, y, f"NIT: {e_nit}")
    y -= 12
    c.drawString(40, y, f"Contacto: {e_contacto}")
    y -= 12
    c.drawString(40, y, f"Teléfono: {e_tel}")
    y -= 12
    c.drawString(40, y, f"Correo: {e_correo}")
    y -= 12
    c.drawString(40, y, f"Descuento empresa (referencial): {e_desc:.2f}%")

    # Tabla
    y -= 18
    c.setLineWidth(0.5)
    c.line(40, y, width - 40, y)
    y -= 14

    c.setFont("Helvetica-Bold", 9)
    c.drawString(40, y, "Descripción")
    c.drawRightString(width - 190, y, "Cant.")
    c.drawRightString(width - 110, y, "Precio U.")
    c.drawRightString(width - 40, y, "Subtotal")
    y -= 10
    c.line(40, y, width - 40, y)
    y -= 14

    total = 0.0
    c.setFont("Helvetica", 9)
    for it in items:
        cant = float(it["cantidad"] or 0)
        pu   = float(it["precio_unit"] or 0)
        sub  = cant * pu
        total += sub

        c.drawString(40, y, str(it["descripcion"])[:70])
        c.drawRightString(width - 190, y, f"{cant:.0f}")
        c.drawRightString(width - 110, y, f"{pu:.2f}")
        c.drawRightString(width - 40, y, f"{sub:.2f}")
        y -= 14
        if y < 80:
            c.showPage()
            y = height - 80

    y -= 6
    c.setLineWidth(0.8)
    c.line(width - 260, y, width - 40, y)
    y -= 18
    c.setFont("Helvetica-Bold", 10)
    c.drawRightString(width - 40, y, f"TOTAL: Bs {total:.2f}")

    c.showPage()
    c.save()
    buffer.seek(0)

    return send_file(buffer, as_attachment=False, download_name=f"factura_{pedido_id}.pdf", mimetype="application/pdf")


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

@app.route('/api/empresas')
@require_role("SUPER_ADMIN", "ADMIN")
def api_empresas():
    conn = get_connection()
    cur = conn.cursor()

    role = session.get("role")
    admin_id = session.get("admin_id")

    if role == "ADMIN":
        cur.execute("""
            SELECT id, nit, razon_social, contacto, telefono, correo, direccion,
                   COALESCE(descuento, 0) AS descuento
            FROM empresas
            WHERE admin_id = %s
            ORDER BY razon_social ASC
        """, (admin_id,))
    else:
        cur.execute("""
            SELECT id, nit, razon_social, contacto, telefono, correo, direccion,
                   COALESCE(descuento, 0) AS descuento
            FROM empresas
            ORDER BY razon_social ASC
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
    resp.headers["Cache-Control"] = "no-store, max-age=0"
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
    Devuelve TODO el catálogo que usa la tienda (UN SOLO ORIGEN):
    productos_precios.json
    """
    import json
    import os
    from flask import jsonify, make_response

    base_dir = os.path.dirname(os.path.abspath(__file__))
    path = os.path.join(base_dir, "productos_precios.json")

    if not os.path.exists(path):
        return jsonify({"ok": False, "error": "No existe productos_precios.json"}), 404

    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)

    resp = make_response(jsonify(data))
    # Evitar que el navegador se quede con precios viejos
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    resp.headers["Pragma"] = "no-cache"
    return resp


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

    # Total empresas
    cur.execute("SELECT COUNT(*) AS empresas FROM empresas")
    empresas = one_value(0)

    # Pedidos hoy
    cur.execute("""
        SELECT COUNT(*) AS pedidos_hoy
        FROM pedidos
        WHERE DATE(fecha) = CURRENT_DATE
    """)
    pedidos_hoy = one_value(0)

    # Pendientes
    cur.execute("""
        SELECT COUNT(*) AS pendientes
        FROM pedidos
        WHERE estado = 'pendiente'
    """)
    pendientes = one_value(0)

    # Total vendido hoy
    cur.execute("""
        SELECT COALESCE(SUM(total), 0) AS total_hoy
        FROM pedidos
        WHERE DATE(fecha) = CURRENT_DATE
    """)
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

    # si tu archivo se llama productos_precios.json, déjalo así:
    path = os.path.join(base_dir, "productos_precios.json")

    if not os.path.exists(path):
        # fallback por si tu archivo se llama distinto
        alt = os.path.join(base_dir, "productos_precios")
        if os.path.exists(alt):
            path = alt
        else:
            return jsonify({"ok": False, "error": f"No encuentro el archivo: {path}"}), 404

    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)

    # Para no reventar el navegador, devolvemos solo 50 primero
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
    tipo = (data.get("tipo") or "empresa").strip()  # empresa | admin

    if not usuario or not password:
        return jsonify({"ok": False, "error": "Faltan datos"}), 400

    # ---- LOGIN ADMIN ----
    if tipo == "admin":
        conn = get_connection()
        cur = conn.cursor()
        cur.execute("SELECT * FROM admins WHERE username = %s AND active = true", (usuario,))

        row = cur.fetchone()
        

        conn.close()

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

    conn = get_connection()
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
    "redirect": "/inicio.html",
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
            cur.execute("SELECT id, correo, nit, razon_social FROM empresas WHERE id = %s", (session["empresa_id"],))
            emp = cur.fetchone()
            conn.close()
            if emp:
                resp["empresa"] = {
                    "id": emp["id"],
                    "correo": emp["correo"],
                    "nit": emp["nit"],
                    "razon_social": emp.get("razon_social") or ""
                }
        except Exception:
            pass

    return jsonify(resp)




ALLOWED_EXCEL_EXT = {".xlsx", ".xlsm"}

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
    tmp_path = os.path.join(base_dir, f"proveedor_upload_tmp{ext}")
    final_path = os.path.join(base_dir, f"proveedor{ext}")

    try:
        # Guardado "atómico": primero tmp, luego replace
        f.save(tmp_path)
        os.replace(tmp_path, final_path)
    except Exception as e:
        return jsonify({"ok": False, "error": f"No se pudo guardar Excel: {e}"}), 500

    # 4) setear variable de entorno en runtime (para que actualizar_precios lo use)
    # Nota: esto funciona para el proceso actual; en deploys/restarts usarás el default proveedor.xlsx
    os.environ["EXCEL_FILE"] = os.path.basename(final_path)

    audit("EXCEL_SUBIDO", "sistema", None, {"filename": os.path.basename(final_path)})
    return jsonify({
        "ok": True,
        "saved_as": os.path.basename(final_path),
        "path": final_path
    })




@app.route("/api/product_overrides")
@require_role("SUPER_ADMIN", "ADMIN")
def api_product_overrides_all():

    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT code, oculto, imagen FROM producto_overrides")
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return jsonify({"ok": True, "overrides": rows})

@app.route("/api/admin/actualizar-precios", methods=["POST"])
@require_role("SUPER_ADMIN")
def api_actualizar_precios():
    # Ya NO pedimos descuento por JSON.
    # El script lo lee del Excel (HOJA PEDIDO!G6)
    r = actualizar_precios()   # <- sin descuento
    return jsonify(r), (200 if r.get("ok") else 400)




@app.route("/api/product_overrides/<code>", methods=["GET", "POST"])
@require_role("SUPER_ADMIN", "ADMIN")
def api_product_override(code):
    code = str(code).strip()
    conn = get_connection()
    cur = conn.cursor()

    if request.method == "GET":
        cur.execute(
            "SELECT code, oculto, imagen FROM producto_overrides WHERE code = %s",
            (code,)
        )
        row = cur.fetchone()
        conn.close()
        if not row:
            return jsonify({"ok": True, "override": None})
        return jsonify({"ok": True, "override": dict(row)})

    # POST: crear / actualizar override
    data = request.get_json() or {}
    oculto = True if data.get("oculto") else False
    imagen = (data.get("imagen") or "").strip() or None

    cur.execute(
        """
        INSERT INTO producto_overrides (code, oculto, imagen)
        VALUES (%s, %s, %s)
        ON CONFLICT(code) DO UPDATE SET
            oculto = excluded.oculto,
            imagen = excluded.imagen
        """,
        (code, oculto, imagen),
    )
    conn.commit()
    conn.close()
    return jsonify({"ok": True})


# ---------------- MAIN ----------------

if __name__ == "__main__":
    import os
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)