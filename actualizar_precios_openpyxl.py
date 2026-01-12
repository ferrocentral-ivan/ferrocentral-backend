from openpyxl import load_workbook
import os
import json
from typing import Optional, Tuple

SHEET_PRECIOS = "NUEVA LISTA DE PRECIOS"

JSON_IN = "productos_precios.json"
JSON_OUT = "productos_precios.json"

# Intentaremos usar el Excel subido por el panel (cualquier extensión común)
EXCEL_CANDIDATES = [
    "proveedor.xlsm",
    "proveedor.xlsx",
    "proveedor.xls",
    # fallback si aún usas el nombre viejo:
    "NOTA DE PEDIDO VER. 8-12-2025 OFICIAL.xlsm",
]

def to_float(x):
    try:
        if x is None:
            return None
        return float(x)
    except:
        return None

def _find_excel_path(base_dir: str) -> Tuple[Optional[str], list]:
    checked = []
    for name in EXCEL_CANDIDATES:
        p = os.path.join(base_dir, name)
        checked.append(p)
        if os.path.exists(p):
            return p, checked
    return None, checked

def _parse_discount_cell(v) -> Optional[float]:
    """
    Convierte valores tipo:
    - 0.2  -> 0.2
    - 20   -> 0.2 (si alguien puso 20 en vez de 20%)
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

    # si viene como 20 (en vez de 0.20)
    if v > 1:
        v = v / 100.0

    # clamp básico
    if v < 0 or v > 0.95:
        return None

    return v

def actualizar_precios(descuento_proveedor: Optional[float] = None):
    """
    - Lee el Excel del proveedor
    - Lee descuento desde G6 (si descuento_proveedor es None)
    - Actualiza productos_precios.json
    Retorna métricas.
    """
    base_dir = os.path.dirname(os.path.abspath(__file__))

    excel_path, checked = _find_excel_path(base_dir)
    json_in_path = os.path.join(base_dir, JSON_IN)
    json_out_path = os.path.join(base_dir, JSON_OUT)

    if not excel_path:
        return {"ok": False, "error": "No encuentro el Excel del proveedor", "checked": checked}

    if not os.path.exists(json_in_path):
        return {"ok": False, "error": "No encuentro productos_precios.json", "path": json_in_path}

    wb = load_workbook(excel_path, data_only=True, keep_vba=True)

    # 1) Descuento desde G6 en la hoja principal
    ws_header = wb["HOJA PEDIDO"] if "HOJA PEDIDO" in wb.sheetnames else wb.active
    descuento_excel = _parse_discount_cell(ws_header["G6"].value)

    if descuento_proveedor is None:
        descuento_proveedor = descuento_excel if descuento_excel is not None else 0.20
    else:
        # si te mandan un override, igual lo normalizamos
        descuento_proveedor = _parse_discount_cell(descuento_proveedor) or descuento_proveedor

    # 2) Hoja de precios
    if SHEET_PRECIOS not in wb.sheetnames:
        return {"ok": False, "error": f"No existe la hoja '{SHEET_PRECIOS}'", "sheets": wb.sheetnames}

    ws = wb[SHEET_PRECIOS]

    # 3) Mapa codigo -> precio Bs (columna H según tu layout: PRECIO BS-.)
    # Ajusta aquí si tu columna real cambia.
    price_map = {}
    rows = 0
    for r in ws.iter_rows(min_row=13, values_only=True):
        rows += 1
        codigo = r[1]   # B: CODIGO
        precio_bs = r[7]  # H: PRECIO BS-.
        if codigo is None:
            continue
        code = str(codigo).strip().replace(".0", "")
        pbs = to_float(precio_bs)
        if pbs is None:
            continue
        price_map[code] = pbs

    # 4) Cargar JSON base
    with open(json_in_path, "r", encoding="utf-8") as f:
        productos = json.load(f)

    updated = 0
    missing = 0

    # 5) Aplicar descuento y reglas
    # IMPORTANTE: aquí tú ya tenías tu lógica de margen por "producto pequeño vs grande".
    # Yo mantengo el esquema básico: costo = precio_bs*(1-desc) y luego tu margen según tramos.
    # Si tu app.py ya aplica el margen en otro sitio, lo ajustamos ahí.
    for p in productos:
        code = str(p.get("code") or p.get("codigo") or "").strip()
        if not code:
            continue

        if code not in price_map:
            missing += 1
            continue

        precio_lista_bs = price_map[code]
        costo_bs = precio_lista_bs * (1.0 - float(descuento_proveedor))

        # ===== TU REGLA DE MARGEN (EJEMPLO POR TRAMOS) =====
        # Ajusta a tu regla real si quieres:
        if costo_bs < 30:
            margen = 0.45
        elif costo_bs < 80:
            margen = 0.35
        elif costo_bs < 200:
            margen = 0.28
        else:
            margen = 0.20

        precio_web_bs = round(costo_bs * (1.0 + margen), 2)

        p["precio_lista_bs"] = round(precio_lista_bs, 2)
        p["costo_bs"] = round(costo_bs, 2)
        p["bs_price_web"] = precio_web_bs

        updated += 1

    with open(json_out_path, "w", encoding="utf-8") as f:
        json.dump(productos, f, ensure_ascii=False)

    return {
        "ok": True,
        "excel": os.path.basename(excel_path),
        "filas_excel": rows,
        "actualizados": updated,
        "missing": missing,
        "descuento": float(descuento_proveedor),
        "descuento_excel_g6": descuento_excel,
    }
