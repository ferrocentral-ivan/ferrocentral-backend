from openpyxl import load_workbook
import os
import json

EXCEL_FILE = "proveedor.xlsm"   # nombre fijo del Excel subido
SHEET_PEDIDO = "HOJA PEDIDO"
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

def detectar_descuento_proveedor(ws):
    """
    Intenta detectar el descuento del proveedor desde el Excel.
    Devuelve 0.20 / 0.25 etc. o None si no se pudo detectar.
    """
    # Estrategia simple y robusta:
    # Busca en las primeras filas alguna celda que sea 0.2 / 0.25 o 20% / 25%
    for row in ws.iter_rows(min_row=1, max_row=20, max_col=10, values_only=True):
        for v in row:
            if v is None:
                continue
            # Caso porcentaje como texto: "20%" o "25 %"
            if isinstance(v, str):
                s = v.strip().replace(" ", "")
                if s.endswith("%"):
                    try:
                        pct = float(s[:-1]) / 100.0
                        if 0 < pct < 0.9:
                            return pct
                    except:
                        pass
            # Caso decimal: 0.2 / 0.25
            if isinstance(v, (int, float)):
                if 0 < float(v) < 0.9:
                    # si es 20 o 25 en vez de 0.20 (a veces pasa)
                    if float(v) > 1:
                        pct = float(v) / 100.0
                        if 0 < pct < 0.9:
                            return pct
                    return float(v)
    return None


def actualizar_precios(descuento_proveedor: float = 0.20):
    """
    Lee el Excel del proveedor y actualiza productos_precios.json.
    descuento_proveedor = 0.20 significa 20% de descuento (tu costo baja 20%).
    Retorna un dict con métricas.
    """
    base_dir = os.path.dirname(os.path.abspath(__file__))

    json_in_path = os.path.join(base_dir, JSON_IN)
    json_out_path = os.path.join(base_dir, JSON_OUT)


    excel_path_xlsm = os.path.join(base_dir, "proveedor.xlsm")
    excel_path_xlsx = os.path.join(base_dir, "proveedor.xlsx")

    excel_path = excel_path_xlsm if os.path.exists(excel_path_xlsm) else excel_path_xlsx
    if not os.path.exists(excel_path):
        return {"ok": False, "error": "No encuentro el Excel subido (proveedor.xlsm/proveedor.xlsx)", "path": excel_path}


    if not os.path.exists(json_in_path):
        return {"ok": False, "error": "No encuentro productos_precios.json", "path": json_in_path}
    
    import shutil
    shutil.copyfile(json_in_path, json_in_path + ".bak")

    # 1) Leer Excel
    wb = load_workbook(excel_path, data_only=True, keep_vba=True)
    if SHEET_PRECIOS not in wb.sheetnames:
        return {"ok": False, "error": f"No existe la hoja '{SHEET_PRECIOS}'", "sheetnames": wb.sheetnames}
    
    
    # 1.1) Leer descuento desde HOJA PEDIDO!G6 (prioridad)
    if SHEET_PEDIDO in wb.sheetnames:
        ws_pedido = wb[SHEET_PEDIDO]
        v = ws_pedido["G6"].value
        try:
            if isinstance(v, str) and "%" in v:
                v = float(v.replace("%", "").strip()) / 100.0
            else:
                v = float(v)
            if 0 < v < 0.9:
                descuento_proveedor = v
        except:
            pass


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
