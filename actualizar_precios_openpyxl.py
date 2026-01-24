from openpyxl import load_workbook
import os
import json
from typing import Optional, Tuple, Dict, Any

SHEET_PRECIOS = "NUEVA LISTA DE PRECIOS"

JSON_IN = "productos_precios.json"
JSON_OUT = "productos_precios.json"

TIPO_CAMBIO = float(os.getenv("TIPO_CAMBIO", "6.96"))

# Intentaremos usar el Excel subido por el panel
EXCEL_CANDIDATES = [
    "proveedor.xlsm",
    "proveedor.xlsx",
    "proveedor.xls",
    # fallback viejo:
    "NOTA DE PEDIDO VER. 8-12-2025 OFICIAL.xlsm",
]

ALLOWED_EXCEL_EXTS = {".xlsx", ".xlsm", ".xls"}

def to_float(x):
    try:
        if x is None:
            return None
        return float(x)
    except:
        return None

def _parse_discount_cell(v) -> Optional[float]:
    """
    Convierte valores tipo:
    - 0.2  -> 0.2
    - 20   -> 0.2
    - "20%" -> 0.2
    """
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
    """
    Prioridad:
    1) Si app.py seteó EXCEL_FILE
    2) Candidatos conocidos
    """
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
    # Tu regla de tramos (mantengo tu idea)
    if costo_bs < 30:
        return 0.45
    elif costo_bs < 80:
        return 0.35
    elif costo_bs < 200:
        return 0.28
    else:
        return 0.20

def actualizar_precios(descuento_proveedor: Optional[float] = None):
    """
    - Lee el Excel del proveedor
    - Lee descuento desde G6 (si descuento_proveedor es None)
    - Actualiza productos_precios.json
    - CREA productos nuevos si aparecen en Excel y no existían en JSON
    """
    base_dir = os.path.dirname(os.path.abspath(__file__))

    excel_path, checked = _find_excel_path(base_dir)
    json_in_path = os.path.join(base_dir, JSON_IN)
    json_out_path = os.path.join(base_dir, JSON_OUT)

    if not excel_path:
        return {"ok": False, "error": "No encuentro el Excel del proveedor", "checked": checked}

    if not os.path.exists(json_in_path):
        return {"ok": False, "error": "No encuentro productos_precios.json", "path": json_in_path}

    ext = os.path.splitext(excel_path)[1].lower()
    if ext not in ALLOWED_EXCEL_EXTS:
        return {"ok": False, "error": f"Extensión Excel no soportada: {ext}"}

    wb = load_workbook(excel_path, data_only=True, keep_vba=(ext == ".xlsm"))

    # 1) Descuento desde G6 en HOJA PEDIDO
    ws_header = wb["HOJA PEDIDO"] if "HOJA PEDIDO" in wb.sheetnames else wb.active
    descuento_excel = _parse_discount_cell(ws_header["G6"].value)

    if descuento_proveedor is None:
        descuento_proveedor = descuento_excel if descuento_excel is not None else 0.20
    else:
        descuento_proveedor = _parse_discount_cell(descuento_proveedor) or float(descuento_proveedor)

    # 2) Hoja de precios
    if SHEET_PRECIOS not in wb.sheetnames:
        return {"ok": False, "error": f"No existe la hoja '{SHEET_PRECIOS}'", "sheets": wb.sheetnames}

    ws = wb[SHEET_PRECIOS]

    # 3) Leer Excel completo (desde fila 3)
    excel_by_code: Dict[str, Dict[str, Any]] = {}
    filas_excel = 0

    for r in ws.iter_rows(min_row=3, values_only=True):
        # Layout (según tu Excel):
        # A productCode, B code, C co, F location, G description, H usd_unit, I package, J brand, L warehouse
        productCode = r[0]
        codigo = r[1]
        co = r[2]
        location = r[5]
        description = r[6]
        usd_unit = r[7]
        package = r[8]
        brand = r[9]
        warehouse = r[11]

        if codigo is None:
            continue

        code = str(codigo).strip().replace(".0", "")
        usd = to_float(usd_unit)
        if not code or usd is None:
            continue

        filas_excel += 1

        excel_by_code[code] = {
            "code": code,
            "productCode": str(productCode).strip() if productCode else None,
            "co": str(co).strip() if co else None,
            "location": str(location).strip() if location is not None else None,
            "warehouse": str(warehouse).strip() if warehouse else None,
            "description": str(description).strip() if description else "",
            "usd_price_unit": usd,
            "package": str(package).strip() if package else None,
            "brand": str(brand).strip().lower() if brand else None,
        }

    # 4) Cargar JSON base y mapear por code
    with open(json_in_path, "r", encoding="utf-8") as f:
        productos = json.load(f)

    by_code: Dict[str, Dict[str, Any]] = {}
    for p in productos:
        c = str(p.get("code") or "").strip()
        if c:
            by_code[c] = p

    updated = 0
    created = 0
    missing_in_excel = 0

    # 5) Actualizar o crear
    for code, info in excel_by_code.items():
        precio_usd = info["usd_price_unit"]
        precio_lista_bs = precio_usd * TIPO_CAMBIO
        costo_bs = precio_lista_bs * (1.0 - float(descuento_proveedor))
        margen = _calc_margen(costo_bs)
        precio_web_bs = round(costo_bs * (1.0 + margen), 2)

        if code in by_code:
            p = by_code[code]

            # Actualiza SOLO lo necesario para precios (no rompemos promo, etc.)
            p["usd_price_unit"] = precio_usd
            # si tienes docena/web históricos, no los destruyo; si no existe, seteo unit
            if p.get("usd_price_docena") is None:
                p["usd_price_docena"] = precio_usd
            if p.get("usd_price_web") is None:
                p["usd_price_web"] = precio_usd

            p["bs_price_descuento25"] = round(costo_bs, 2)
            p["bs_price_web"] = precio_web_bs
            p["proveedor_descuento"] = float(descuento_proveedor)

            # Completar metadata SOLO si está vacío (para no pisar cambios manuales)
            if not (p.get("description") or "").strip():
                p["description"] = info["description"]
            if not (p.get("brand") or "").strip() and info.get("brand"):
                p["brand"] = info["brand"]
            if not (p.get("productCode") or "").strip() and info.get("productCode"):
                p["productCode"] = info["productCode"]
            if not (p.get("co") or "").strip() and info.get("co"):
                p["co"] = info["co"]
            if not (p.get("location") or "").strip() and info.get("location"):
                p["location"] = info["location"]
            if not (p.get("warehouse") or "").strip() and info.get("warehouse"):
                p["warehouse"] = info["warehouse"]
            if not (p.get("mode_of_sale") or "").strip():
                p["mode_of_sale"] = ""

            # sale_label: si ya existe lo mantengo; si no, lo creo simple
            if not (p.get("sale_label") or "").strip():
                # paquete suele ser "1 PZA", "2 PZAS", etc.
                p["sale_label"] = f"CAJA: 1 unidades"  # por defecto seguro

            if p.get("box_qty") is None:
                p["box_qty"] = 1
            if p.get("estrella_score") is None:
                p["estrella_score"] = 0

            updated += 1

        else:
            # Crear producto nuevo con el MISMO schema que tu catálogo
            nuevo = {
                "code": code,
                "productCode": info.get("productCode") or f"{(info.get('co') or '').upper()}{code}",
                "co": (info.get("co") or "").upper() if info.get("co") else "",
                "brand": info.get("brand") or "",
                "description": info.get("description") or "",
                "location": info.get("location") or "",
                "warehouse": info.get("warehouse") or "",
                "usd_price_unit": precio_usd,
                "usd_price_docena": precio_usd,
                "usd_price_web": precio_usd,
                "bs_price_descuento25": round(costo_bs, 2),
                "bs_price_web": precio_web_bs,
                "has_promo": False,
                "promo_percent": None,
                "promo_price_bs": None,
                "mode_of_sale": "",
                "box_qty": 1,
                "sale_label": "CAJA: 1 unidades",
                "estrella_score": 0,
                "proveedor_descuento": float(descuento_proveedor),
            }

            productos.append(nuevo)
            by_code[code] = nuevo
            created += 1

    # 6) Contar los que están en JSON pero no vinieron en el Excel (info útil)
    for p in productos:
        c = str(p.get("code") or "").strip()
        if c and c not in excel_by_code:
            missing_in_excel += 1

    # 7) Guardar (json bonito)
    with open(json_out_path, "w", encoding="utf-8") as f:
        json.dump(productos, f, ensure_ascii=False, indent=2)

    return {
        "ok": True,
        "excel": os.path.basename(excel_path),
        "filas_excel_validas": filas_excel,
        "actualizados": updated,
        "creados_nuevos": created,
        "en_json_no_en_excel": missing_in_excel,
        "descuento_proveedor": float(descuento_proveedor),
    }
