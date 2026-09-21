#!/usr/bin/env python3
"""Refresh India-market companies + SUV/MPV models from CarWale public brand pages.

Merges into data/cars.json (keeps curated prices/features for known ids).
Also writes data/companies.json and data/catalog.js.

  python3 scripts/refresh_catalog.py
"""
from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
UA = (
    "Mozilla/5.0 (compatible; suv-compare-catalog/1.0; "
    "+https://github.com/theyogendraDanwar/suv-compare)"
)
SUV_BODY_IDS = {6, 9}  # 6=SUV/crossover, 9=MPV
CURRENT_STATUS = 2
DENY_SLUG_PARTS = ("old-generation", "expert-reviews", "news", "compare", "best-", "facelift")
SLEEP_S = 0.4


def read_json(path: Path, fallback):
    try:
        return json.loads(path.read_text())
    except Exception:
        return fallback


def write_json(path: Path, obj) -> None:
    path.write_text(json.dumps(obj, indent=2, ensure_ascii=False) + "\n")


def fetch_text(url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "text/html"})
    with urllib.request.urlopen(req, timeout=45) as res:
        return res.read().decode("utf-8", "replace")


def extract_json_array(html: str, key: str):
    needle = f'"{key}":['
    idx = html.find(needle)
    if idx < 0:
        return None
    start = html.find("[", idx)
    i = start
    depth = 0
    in_str = False
    esc = False
    while i < len(html):
        ch = html[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
        else:
            if ch == '"':
                in_str = True
            elif ch == "[":
                depth += 1
            elif ch == "]":
                depth -= 1
                if depth == 0:
                    return json.loads(html[start : i + 1])
        i += 1
    return None


def slugify_id(model_mask: str) -> str:
    base = re.sub(r"[^a-z0-9]+", "-", (model_mask or "").lower()).strip("-")
    return base or f"model-{int(time.time())}"


def parse_price(formatted: str | None):
    if not formatted:
        return None
    s = formatted.replace(",", "")
    m = re.search(r"([\d.]+)\s*Lakh", s, re.I)
    if m:
        return int(round(float(m.group(1)) * 100_000))
    m = re.search(r"([\d.]+)\s*Crore", s, re.I)
    if m:
        return int(round(float(m.group(1)) * 10_000_000))
    digits = re.sub(r"\D", "", s)
    return int(digits) if digits else None


def brand_display(name: str) -> str:
    if name == "Maruti Suzuki":
        return "Maruti"
    if name in ("Mercedes Benz", "Mercedes-Benz"):
        return "Mercedes-Benz"
    return name


def stub_car(model: dict, brand: str) -> dict:
    mask = model.get("modelMaskingName") or slugify_id(model.get("modelName", ""))
    price = parse_price((model.get("priceOverview") or {}).get("formattedPrice")) or 1_500_000
    if model.get("isElectricVehicle"):
        fuel = "Electric"
        mileage = "— km real"
    else:
        fuel = {2: "Diesel", 3: "Hybrid"}.get(model.get("fuelTypeId"), "Petrol")
        mileage = "— kmpl"
    return {
        "id": slugify_id(mask),
        "brand": brand,
        "short": model.get("modelName") or mask,
        "name": f"{brand} {model.get('modelName') or mask}",
        "variant": model.get("versionName") or model.get("trimName") or "Mid–high trim · confirm dealer",
        "price": price,
        "fuel": fuel,
        "mileage": mileage,
        "note": "Auto-imported from CarWale — confirm Jaipur on-road & features.",
        "ventilated": False,
        "wirelessCarplay": True,
        "rearBlinds": False,
        "powerSeat": False,
        "source": "carwale",
        "sourceModelId": model.get("modelId"),
        "bodyStyleId": model.get("bodyStyleId"),
    }


def discover_companies(seed: list[dict]) -> list[dict]:
    by_slug = {c["slug"]: dict(c) for c in seed if c.get("slug")}
    try:
        html = fetch_text("https://www.carwale.com/new-cars/")
        found = set(re.findall(r'href="/([a-z0-9-]+-cars)/', html))
        deny = re.compile(
            r"^(best|compare|new|used|electric|diesel|petrol|hybrid|cng|"
            r"5-seater|6-seater|7-seater|8-seater)-cars$"
        )
        for slug in found:
            if deny.match(slug) or slug in by_slug:
                continue
            raw = slug.replace("-cars", "")
            name = " ".join(w.capitalize() for w in raw.split("-"))
            name = {
                "Maruti Suzuki": "Maruti",
                "Mercedes Benz": "Mercedes-Benz",
                "Force Motors": "Force",
            }.get(name, name)
            by_slug[slug] = {"name": name, "slug": slug, "source": "carwale"}
    except Exception as err:
        print(f"company discovery soft-fail: {err}")
    return sorted(by_slug.values(), key=lambda c: c["name"])


def fetch_brand_models(slug: str) -> list[dict]:
    html = fetch_text(f"https://www.carwale.com/{slug}/")
    models = extract_json_array(html, "models") or []
    out = []
    for m in models:
        if m.get("status") != CURRENT_STATUS:
            continue
        if m.get("bodyStyleId") not in SUV_BODY_IDS:
            continue
        mask = str(m.get("modelMaskingName") or "")
        if any(p in mask for p in DENY_SLUG_PARTS):
            continue
        out.append(m)
    return out


def merge_cars(existing: list[dict], incoming: list[dict]):
    by_id = {c["id"]: dict(c) for c in existing}
    by_key = {f"{c['brand']}|{c['short']}".lower(): c["id"] for c in existing}
    added = updated = 0
    for model in incoming:
        brand = brand_display(model.get("makeName") or model.get("subMakeName") or "")
        if not brand:
            continue
        short = model.get("modelName") or ""
        key = f"{brand}|{short}".lower()
        stub = stub_car(model, brand)
        existing_id = by_key.get(key) or (stub["id"] if stub["id"] in by_id else None)
        if existing_id and existing_id in by_id:
            cur = by_id[existing_id]
            if (not cur.get("price")) or cur.get("source") == "carwale":
                if stub.get("price"):
                    cur["price"] = stub["price"]
            if cur.get("source") == "carwale":
                cur["fuel"] = stub.get("fuel") or cur.get("fuel")
                cur["variant"] = stub.get("variant") or cur.get("variant")
            cur["sourceModelId"] = stub.get("sourceModelId")
            cur["bodyStyleId"] = stub.get("bodyStyleId")
            by_id[existing_id] = cur
            updated += 1
        else:
            by_id[stub["id"]] = stub
            by_key[key] = stub["id"]
            added += 1
    cars = sorted(by_id.values(), key=lambda c: (c.get("brand", ""), c.get("name", "")))
    return cars, added, updated


def write_catalog_js(cars, companies, updated_at: str) -> None:
    payload = {
        "updatedAt": updated_at,
        "companies": [{"name": c["name"], "slug": c.get("slug")} for c in companies],
        "cars": cars,
    }
    text = (
        "/* auto-generated by scripts/refresh_catalog.py — do not edit */\n"
        f"window.__SUV_CATALOG__ = {json.dumps(payload, ensure_ascii=False)};\n"
    )
    (DATA / "catalog.js").write_text(text)


def main() -> None:
    DATA.mkdir(parents=True, exist_ok=True)
    cars_doc = read_json(DATA / "cars.json", {"cars": []})
    companies_doc = read_json(DATA / "companies.json", {"companies": []})

    companies = discover_companies(companies_doc.get("companies") or [])
    print(f"Companies: {len(companies)}")

    remote: list[dict] = []
    for company in companies:
        slug = company.get("slug")
        if not slug:
            continue
        try:
            models = fetch_brand_models(slug)
            print(f"  {company['name']}: {len(models)} SUV/MPV")
            remote.extend(models)
            time.sleep(SLEEP_S)
        except Exception as err:
            print(f"  skip {slug}: {err}")

    cars, added, updated = merge_cars(cars_doc.get("cars") or [], remote)
    updated_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    write_json(
        DATA / "cars.json",
        {"updatedAt": updated_at, "source": "carwale+curated", "cars": cars},
    )
    write_json(
        DATA / "companies.json",
        {"updatedAt": updated_at, "source": "carwale+curated", "companies": companies},
    )
    write_catalog_js(cars, companies, updated_at)
    print(f"Done. cars={len(cars)} (+{added} new, ~{updated} touched).")


if __name__ == "__main__":
    main()
