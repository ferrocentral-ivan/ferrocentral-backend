from openpyxl import load_workbook
import os
import json

EXCEL_FILE = os.environ.get("EXCEL_FILE", "").strip()

if not EXCEL_FILE:
    # si no hay env, preferimos xlsx; si no existe, caemos a xlsm
    EXCEL_FILE = "proveedor.xlsx"

SHEET_PRECIOS = "NUEVA LISTA DE PRECIOS"

JSON_IN = "productos_precios.json"
JSON_OUT = "productos_precios.json"


def to_float(x):
    try:
        if x is None:
            return None
        return float(x)
    except:
        return None


def actualizar_precios(descuento_proveedor: float = 0.20):
    """
    Lee el Excel del proveedor y actualiza productos_precios.json.
    descuento_proveedor = 0.20 significa 20% de descuento (tu costo baja 20%).
    Retorna un dict con métricas.
    """
    base_dir = os.path.dirname(os.path.abspath(__file__))

    excel_path = os.path.join(base_dir, EXCEL_FILE)
    json_in_path = os.path.join(base_dir, JSON_IN)
    json_out_path = os.path.join(base_dir, JSON_OUT)

    import shutil
    shutil.copyfile(json_in_path, json_in_path + ".bak")


    if not os.path.exists(excel_path):
        # fallback si el admin subió proveedor.xlsm
        alt = os.path.join(base_dir, "proveedor.xlsm")
        if os.path.exists(alt):
            excel_path = alt
        else:
            return {"ok": False, "error": "No encuentro el Excel", "path": excel_path}

    if not os.path.exists(json_in_path):
        return {"ok": False, "error": "No encuentro productos_precios.json", "path": json_in_path}

    # 1) Leer Excel
    wb = load_workbook(excel_path, data_only=True, keep_vba=True)
    if SHEET_PRECIOS not in wb.sheetnames:
        return {"ok": False, "error": f"No existe la hoja '{SHEET_PRECIOS}'", "sheetnames": wb.sheetnames}

    ws = wb[SHEET_PRECIOS]

    # 2) Construir mapa: codigo -> P/U
    price_map = {}
    rows = 0

    for r in range(3, ws.max_row + 1):
        codigo_txt = ws.cell(row=r, column=1).value  # A: "PR-22090"
        cod_num = ws.cell(row=r, column=2).value     # B: 22090 (IGO)
        pu = ws.cell(row=r, column=8).value          # H: P/U

        pu_f = to_float(pu)
        if pu_f is None:
            continue

        key = None
        try:
            if cod_num is not None:
                key = str(int(cod_num))
        except:
            key = None

        if key is None and isinstance(codigo_txt, str):
            digits = "".join(ch for ch in codigo_txt if ch.isdigit())
            if digits:
                key = digits

        if key is None:
            continue

        price_map[key] = pu_f
        rows += 1

    # 3) Leer JSON actual
    with open(json_in_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    if not isinstance(data, list):
        return {"ok": False, "error": "productos_precios.json no es una lista"}

    # 4) Actualizar precios
    updated = 0
    missing = 0

    for item in data:
        code = str(item.get("code", "")).strip()
        if not code:
            missing += 1
            continue

        pu_base = price_map.get(code)
        if pu_base is None:
            missing += 1
            continue

        usd_price_unit = pu_base * (1 - float(descuento_proveedor))

        item["usd_price_unit"] = round(usd_price_unit, 4)
        item["proveedor_descuento"] = float(descuento_proveedor)
        updated += 1

    # 5) Guardar JSON
    with open(json_out_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    return {
        "ok": True,
        "excel_rows": rows,
        "updated": updated,
        "missing": missing,
        "json_path": json_out_path,
        "descuento_proveedor": float(descuento_proveedor),
    }


if __name__ == "__main__":
    # Para probar manual:
    res = actualizar_precios(0.20)
    print(res)
