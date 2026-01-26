import os
import json
from datetime import datetime
from typing import Optional, Dict, Any, Tuple
from openpyxl import load_workbook

from backend.database import get_connection

SHEET_PRECIOS = "NUEVA LISTA DE PRECIOS"
ALLOWED_EXCEL_EXTS = {".xlsx", ".xlsm", ".xls"}

TIPO_CAMBIO = float(os.getenv("TIPO_CAMBIO", "6.96"))

EXCEL_CANDIDATES = [
    "proveedor.xlsm",
    "proveedor.xlsx",
    "proveedor.xls",
]

def to_float(x):
    try:
        if x is None:
            return None
        return float(x)
    except:
        return None

def _parse_discount_cell(v) -> Optional[float]:
    if v is None:
        return None
    if isinstance(v, str):
        s = v.strip().replace(",", ".")
        if s.endswith("%"):
            s = s[:-1].strip()
        try:
            v = float(s)
        except:
            return None
    try:
        v = float(v)
    except:
        return None
    if v > 1:
        v = v / 100.0
    if v < 0 or v > 0.95:
        return None
    return v

def _find_excel_path(base_dir: str) -> Tuple[Optional[str], list]:
    checked = []

    env_name = os.environ.get("EXCEL_FILE")
    if env_name:
        p = os.path.join(base_dir, env_name)
        checked.append(p)
        if os.path.exists(p):
            return p, checked

    for name in EXCEL_CANDIDATES:
        p = os.path.join(base_dir, name)
        checked.append(p)
        if os.path.exists(p):
            return p, checked

    return None, checked

def _calc_margen(costo_bs: float) -> float:
    if costo_bs < 30:
        return 0.45
    elif costo_bs < 80:
        return 0.35
    elif costo_bs < 200:
        return 0.28
    else:
        return 0.20

def _calc_prices(usd_unit: float, descuento_proveedor: float) -> Dict[str, float]:
    # costo proveedor en Bs con descuento global (G6)
    costo_bs = usd_unit * TIPO_CAMBIO * (1.0 - float(descuento_proveedor))
    margen = _calc_margen(costo_bs)
    bs_web = round(costo_bs * (1.0 + margen), 2)

    # “bs_price_descuento25” como precio con -25% sobre web (solo si lo usas)
    bs_desc25 = round(bs_web * 0.75, 2)

    return {
        "bs_price_proveedor": round(costo_bs, 2),
        "margen": float(margen),
        "bs_price_web": float(bs_web),
        "bs_price_descuento25": float(bs_desc25),
    }

def actualizar_precios(descuento_proveedor: Optional[float] = None):
    base_dir = os.path.dirname(os.path.abspath(__file__))

    excel_path, checked = _find_excel_path(base_dir)
    if not excel_path:
        return {"ok": False, "error": "No encuentro el Excel del proveedor", "checked": checked}

    ext = os.path.splitext(excel_path)[1].lower()
    if ext not in ALLOWED_EXCEL_EXTS:
        return {"ok": False, "error": f"Extensión Excel no soportada: {ext}"}

    wb = load_workbook(excel_path, data_only=True, read_only=True, keep_vba=False)

    ws_header = wb["HOJA PEDIDO"] if "HOJA PEDIDO" in wb.sheetnames else wb.active
    descuento_excel = _parse_discount_cell(ws_header["G6"].value)

    if descuento_proveedor is None:
        descuento_proveedor = descuento_excel if descuento_excel is not None else 0.20
    else:
        descuento_proveedor = _parse_discount_cell(descuento_proveedor) or float(descuento_proveedor)

    if SHEET_PRECIOS not in wb.sheetnames:
        return {"ok": False, "error": f"No existe la hoja '{SHEET_PRECIOS}'", "sheets": wb.sheetnames}

    ws = wb[SHEET_PRECIOS]

    excel_by_code: Dict[str, Dict[str, Any]] = {}
    filas_excel = 0

    for r in ws.iter_rows(min_row=3, values_only=True):
        codigo = r[1]
        descripcion = r[6]
        usd_unit = r[7]
        marca = r[9]

        if codigo is None:
            continue

        code_raw = str(codigo).strip()
        if code_raw.endswith(".0"):
            code_raw = code_raw[:-2]
        code = code_raw.strip()

        if not code or (not code.isdigit()):
            continue

        usd_u = to_float(usd_unit)
        if usd_u is None:
            continue

        filas_excel += 1
        excel_by_code[code] = {
            "code": code,
            "description": str(descripcion).strip() if descripcion else "",
            "brand": str(marca).strip().lower() if marca else "",
            "usd_price_unit": usd_u,
        }

    conn = get_connection()
    cur = conn.cursor()

    cur.execute("SELECT code, data FROM productos_catalogo")
    rows = cur.fetchall() or []
    db_by_code = {}
    for rr in rows:
        c = str(rr.get("code") or "").strip()
        if c:
            db_by_code[c] = rr.get("data") or {}

    db_codes = set(db_by_code.keys())
    excel_codes = set(excel_by_code.keys())

    missing = sorted(list(db_codes - excel_codes))

    actualizados = 0
    nuevos_codigos = []
    nuevos_detalle = []

    now = datetime.utcnow().isoformat()

    for code, ex in excel_by_code.items():
        prices = _calc_prices(ex["usd_price_unit"], float(descuento_proveedor))

        if code in db_by_code:
            p = dict(db_by_code[code])
            p["code"] = code
            p["description"] = ex.get("description", p.get("description", ""))
            p["brand"] = ex.get("brand", p.get("brand", "")).strip().lower()
            p["usd_price_unit"] = ex["usd_price_unit"]
            p["proveedor_descuento"] = float(descuento_proveedor)

            # precios calculados
            p.update(prices)

            cur.execute(
                """
                INSERT INTO productos_catalogo (code, data, updated_at)
                VALUES (%s, %s::jsonb, %s)
                ON CONFLICT (code)
                DO UPDATE SET data = EXCLUDED.data, updated_at = EXCLUDED.updated_at
                """,
                (code, json.dumps(p, ensure_ascii=False), now)
            )
            actualizados += 1
            continue

        # === NUEVO DETECTADO ===
        nuevos_codigos.append(code)
        nuevos_detalle.append({
            "code": code,
            "description": ex.get("description", ""),
            "brand": ex.get("brand", ""),
            "usd_price_unit": ex.get("usd_price_unit"),
        })

        # Insertar en catálogo (mínimo seguro) + marcar es_nuevo
        pnew = {
            "code": code,
            "description": ex.get("description", ""),
            "brand": ex.get("brand", ""),
            "usd_price_unit": ex.get("usd_price_unit"),
            "proveedor_descuento": float(descuento_proveedor),
            "es_nuevo": True,
        }
        pnew.update(prices)

        cur.execute(
            """
            INSERT INTO productos_catalogo (code, data, updated_at)
            VALUES (%s, %s::jsonb, %s)
            ON CONFLICT (code)
            DO NOTHING
            """,
            (code, json.dumps(pnew, ensure_ascii=False), now)
        )

        # Crear override para que salga en OFERTAS como NUEVO + placeholder
        # (Si ya existe override, NO lo pisamos)
        cur.execute(
            """
            INSERT INTO producto_overrides (code, oculto, imagen, destacado, orden, promo_label)
            VALUES (%s, FALSE, %s, FALSE, 0, %s)
            ON CONFLICT (code) DO NOTHING
            """,
            (code, "img/nuevo.jpg", "NUEVO")
        )

    conn.commit()
    conn.close()

    return {
        "ok": True,
        "actualizados": actualizados,
        "filas_excel_validas": filas_excel,
        "descuento_proveedor": float(descuento_proveedor),
        "en_json_no_en_excel": missing,
        "nuevos": len(nuevos_codigos),
        "nuevos_codigos": nuevos_codigos,
        "nuevos_detectados": nuevos_detalle
    }
