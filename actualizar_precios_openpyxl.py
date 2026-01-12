from openpyxl import load_workbook
import os
import json

# Hoja con los ítems/precios
SHEET_PRECIOS = "NUEVA LISTA DE PRECIOS"
# Hoja donde está el descuento del proveedor
SHEET_PEDIDO = "HOJA PEDIDO"
DISCOUNT_CELL = "G6"

# Columnas (según tu Excel)
# C = código, H = precio unitario USD, I = precio en Bs
CODE_COL = 3
USD_COL = 8
BS_COL = 9

# Tipo de cambio de respaldo (solo si faltara Bs)
DEFAULT_TC = 6.96

JSON_IN = "productos_precios.json"
JSON_OUT = "productos_precios.json"

def _to_float(x):
    try:
        if x is None:
            return None
        if isinstance(x, str):
            x = x.strip().replace(",", "")
            if x.endswith("%"):
                x = x[:-1]
        return float(x)
    except Exception:
        return None

def _norm_descuento(val):
    """
    Acepta: 0.2, 20, "20%", "0.20"
    Devuelve: 0.20
    """
    f = _to_float(val)
    if f is None:
        return 0.0
    if f > 1.0:
        f = f / 100.0
    if f < 0:
        f = 0.0
    if f > 0.90:
        f = 0.90
    return round(f, 4)

def margen_por_costo_bs(costo_bs: float) -> float:
    """
    Regla simple (ajústala si quieres):
    - más margen en productos baratos
    - menos margen en productos caros
    """
    if costo_bs <= 50:
        return 0.35
    if costo_bs <= 150:
        return 0.30
    if costo_bs <= 400:
        return 0.25
    if costo_bs <= 800:
        return 0.20
    return 0.15

def actualizar_precios():
    base_dir = os.path.dirname(os.path.abspath(__file__))

    # El upload del backend guarda "proveedor.<ext>" y setea EXCEL_FILE en env
    excel_name = os.environ.get("EXCEL_FILE", "proveedor.xlsm").strip()
    excel_path = os.path.join(base_dir, excel_name)

    # fallback por si EXCEL_FILE no está o no existe
    if not os.path.exists(excel_path):
        for alt in ("proveedor.xlsm", "proveedor.xlsx", "proveedor.xls"):
            alt_path = os.path.join(base_dir, alt)
            if os.path.exists(alt_path):
                excel_path = alt_path
                excel_name = alt
                break

    json_in_path = os.path.join(base_dir, JSON_IN)
    json_out_path = os.path.join(base_dir, JSON_OUT)

    if not os.path.exists(excel_path):
        return {"ok": False, "error": f"No encuentro el Excel subido en backend. Busqué: {excel_name}"}

    if not os.path.exists(json_in_path):
        return {"ok": False, "error": "No encuentro productos_precios.json en el backend"}

    # Cargar JSON actual
    with open(json_in_path, "r", encoding="utf-8") as f:
        productos = json.load(f)

    # Leer Excel
    wb = load_workbook(excel_path, data_only=True, keep_vba=True)

    if SHEET_PRECIOS not in wb.sheetnames:
        return {"ok": False, "error": f"No existe la hoja '{SHEET_PRECIOS}'"}

    ws = wb[SHEET_PRECIOS]

    descuento = 0.0
    if SHEET_PEDIDO in wb.sheetnames:
        ws_pedido = wb[SHEET_PEDIDO]
        descuento = _norm_descuento(ws_pedido[DISCOUNT_CELL].value)

    # Construir mapa código -> (usd, bs)
    price_map = {}
    filas = 0

    # Tu data empieza cerca de la fila 13 (por cómo se ve el formato).
    # Si tu Excel cambia, ajustamos este start.
    START_ROW = 13

    for r in range(START_ROW, ws.max_row + 1):
        code = ws.cell(row=r, column=CODE_COL).value
        if code is None:
            continue

        code = str(code).strip()
        usd = _to_float(ws.cell(row=r, column=USD_COL).value)
        bs = _to_float(ws.cell(row=r, column=BS_COL).value)

        # si no hay usd ni bs, saltar
        if usd is None and bs is None:
            continue

        filas += 1
        price_map[code] = {"usd": usd, "bs": bs}

    # Aplicar a productos
    actualizados = 0
    missing = 0

    for p in productos:
        code = str(p.get("code") or "").strip()
        if not code:
            continue

        row = price_map.get(code)
        if not row:
            missing += 1
            continue

        usd = row["usd"]
        bs = row["bs"]

        # Costo real con descuento (en USD) si hay usd
        usd_desc = None
        if usd is not None:
            usd_desc = usd * (1.0 - descuento)

        # Costo base en Bs:
        # Preferimos Bs del Excel (porque tu tienda trabaja en Bs).
        # Si no viene Bs, calculamos por TC.
        if bs is None:
            if usd_desc is None:
                continue
            costo_bs = usd_desc * DEFAULT_TC
        else:
            # Importante:
            # En muchos Excels, la columna Bs YA refleja el descuento si el archivo
            # viene calculado por el proveedor. Si no, igual está bien porque además
            # guardamos "descuento" para trazabilidad.
            costo_bs = bs

        # Tu precio web final con tu margen escalonado
        margen = margen_por_costo_bs(costo_bs)
        bs_web = round(costo_bs * (1.0 + margen), 2)

        # Guardar en el JSON que consume tu tienda
        if usd_desc is not None:
            p["usd_price_unit"] = round(usd_desc, 4)
            p["precio_unitario_usd"] = round(usd_desc, 4)

        p["bs_cost"] = round(costo_bs, 2)
        p["bs_price_web"] = bs_web
        p["margen"] = round(margen, 4)

        actualizados += 1

    # Guardar
    with open(json_out_path, "w", encoding="utf-8") as f:
        json.dump(productos, f, ensure_ascii=False)

    return {
        "ok": True,
        "updated": actualizados,
        "excel_rows": filas,
        "missing": missing,
        "descuento": descuento,
        "excel_file": excel_name,
    }
