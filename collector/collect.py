import asyncio
import json
import re
from datetime import datetime, timezone
from pathlib import Path

from playwright.async_api import async_playwright

ROOT = Path(__file__).resolve().parents[1]
CONFIG = json.loads((ROOT / "products.json").read_text(encoding="utf-8"))
OUT = ROOT / "docs" / "prices.json"

MONEY = re.compile(r"\$(\d{1,3}(?:,\d{3})*(?:\.\d{2})?)")
PER_LB = re.compile(r"\$\s*(\d{1,3}(?:,\d{3})*(?:\.\d{2})?)\s*/\s*lb", re.I)
CURRENT_PRICE = re.compile(r"Current price:\s*\$(\d{1,3}(?:,\d{3})*(?:\.\d{2})?)", re.I)
ORIGINAL_PRICE = re.compile(r"Original Price:\s*\$(\d{1,3}(?:,\d{3})*(?:\.\d{2})?)", re.I)
LIDL_PRICE = re.compile(r"\$\s*(\d{1,3}(?:,\d{3})*(?:\.\d{2})?)\s*\*", re.I)
SAFEWAY_YOUR_PRICE = re.compile(r"Your Price\s*\$\s*(\d{1,3}(?:,\d{3})*(?:\.\d{2})?)", re.I)
SAFEWAY_MEMBER_PRICE = re.compile(r"(?:Member|for U) Price\s*\$\s*(\d{1,3}(?:,\d{3})*(?:\.\d{2})?)", re.I)

REQUIRED_TERMS = {
    "Eggs": ["egg"],
    "Milk": ["milk"],
    "Bananas": ["banana"],
    "Chicken breast": ["chicken", "breast"],
    "Greek yogurt": ["greek", "yogurt"],
    "Salmon": ["salmon"],
    "Avocados": ["avocado"],
    "Olive oil": ["olive", "oil"],
}

PRICE_LIMITS = {
    "Eggs": (0.50, 20.00),
    "Milk": (1.00, 20.00),
    "Bananas": (0.10, 5.00),
    "Chicken breast": (0.50, 20.00),
    "Greek yogurt": (0.50, 25.00),
    "Salmon": (2.00, 50.00),
    "Avocados": (0.20, 20.00),
    "Olive oil": (2.00, 80.00),
}


def money(s):
    return float(s.replace(",", ""))


def package_from_config(cfg):
    if cfg.get("price_mode") == "per_lb":
        return 1.0, "lb"
    return float(cfg["fallback_qty"]), cfg["fallback_unit"]


def validate_price(product_name, price):
    lo, hi = PRICE_LIMITS.get(product_name, (0.05, 500.0))
    if not (lo <= price <= hi):
        raise RuntimeError(f"Suspicious price ${price:.2f} for {product_name}")


def required_terms(product_name):
    return REQUIRED_TERMS.get(product_name, [])


def identity_check(text, product_name, store):
    hay = text[:5000].lower()
    if not all(term in hay for term in required_terms(product_name)):
        raise RuntimeError(f"{store} product page failed identity check")


def parse_standard_price(text, mode="package"):
    if mode == "per_lb":
        m = PER_LB.search(text)
        if m:
            return money(m.group(1)), None
    m = CURRENT_PRICE.search(text)
    if m:
        original = ORIGINAL_PRICE.search(text)
        return money(m.group(1)), money(original.group(1)) if original else None
    vals = [money(x) for x in MONEY.findall(text[:5000])]
    vals = [x for x in vals if 0.05 <= x <= 500]
    return (vals[0], None) if vals else (None, None)


def parse_lidl_price(text, mode="package"):
    if mode == "per_lb":
        m = PER_LB.search(text[:5000])
        if m:
            return money(m.group(1)), None
    vals = [money(x) for x in LIDL_PRICE.findall(text[:5000])]
    if not vals:
        return None, None
    return vals[-1], None


def parse_safeway_price(text, mode="package"):
    head = text[:7000]
    if mode == "per_lb":
        m = PER_LB.search(head)
        if m:
            return money(m.group(1)), None
    member = SAFEWAY_MEMBER_PRICE.search(head)
    your = SAFEWAY_YOUR_PRICE.search(head)
    if your:
        return money(your.group(1)), money(member.group(1)) if member else None
    return None, None


def observed_package(text, expected_unit):
    t = text[:3500].lower().replace("fl. oz.", "fl oz").replace("fl. oz", "fl oz")
    if expected_unit == "fl_oz":
        m = re.search(r"(\d+(?:\.\d+)?)\s*(?:gallon|gal)\b", t)
        if m:
            return round(float(m.group(1)) * 128, 3), "fl_oz"
        m = re.search(r"(\d+(?:\.\d+)?)\s*fl\s*oz\b", t)
        if m:
            return float(m.group(1)), "fl_oz"
    elif expected_unit == "oz":
        m = re.search(r"(\d+(?:\.\d+)?)\s*(?:oz|ounce)\b", t)
        if m:
            return float(m.group(1)), "oz"
    elif expected_unit == "count":
        m = re.search(r"(?<![\d.])(\d+)\s*(?:ct|count|piece|pieces|each|ea)\b", t)
        if m:
            return float(m.group(1)), "count"
        m = re.search(r"(?<![\d.])(\d+)\s*doz\b", t)
        if m:
            return float(m.group(1)) * 12, "count"
    return None, None


def verify_package(text, cfg):
    expected_qty, expected_unit = package_from_config(cfg)
    if cfg.get("price_mode") == "per_lb":
        return expected_qty, expected_unit
    qty, unit = observed_package(text, expected_unit)
    if qty is None:
        if cfg.get("package_verified"):
            return expected_qty, expected_unit
        raise RuntimeError("Could not verify package size from product page")
    tolerance = max(0.05, expected_qty * 0.02)
    if unit != expected_unit or abs(qty - expected_qty) > tolerance:
        raise RuntimeError(
            f"Package mismatch: expected {expected_qty:g} {expected_unit}, page shows {qty:g} {unit}"
        )
    return expected_qty, expected_unit


def safeway_visible_diagnostics(text):
    out = []
    seen = set()
    for raw in text.splitlines():
        line = " ".join(raw.split())
        low = line.lower()
        if not line or not ("$" in line or "price" in low or "club" in low or "member" in low):
            continue
        line = line[:220]
        if line not in seen:
            seen.add(line)
            out.append(line)
        if len(out) >= 12:
            break
    return out


async def body_text(page):
    await page.wait_for_timeout(1200)
    return await page.locator("body").inner_text()


async def dismiss_common(page):
    for label in ["Accept All", "Accept", "I Agree", "Got it", "Got It", "Close", "No Thanks"]:
        try:
            loc = page.get_by_role("button", name=re.compile(f"^{re.escape(label)}$", re.I))
            if await loc.count():
                await loc.first.click(timeout=1200)
        except Exception:
            pass


async def set_wegmans_store(page):
    await page.goto(CONFIG["stores"]["Wegmans"]["store_page"], wait_until="domcontentloaded", timeout=60000)
    await dismiss_common(page)
    try:
        btn = page.get_by_role("button", name=re.compile("Shop This Store", re.I)).first
        if await btn.count():
            await btn.click(timeout=3000)
            await page.wait_for_timeout(1000)
    except Exception:
        pass


async def collect_wegmans(context, product):
    cfg = product["Wegmans"]
    page = await context.new_page()
    try:
        await set_wegmans_store(page)
        await page.goto(cfg["url"], wait_until="domcontentloaded", timeout=60000)
        await dismiss_common(page)
        text = await body_text(page)
        identity_check(text, product["product"], "Wegmans")
        price, original = parse_standard_price(text, cfg.get("price_mode", "package"))
        if price is None:
            raise RuntimeError("Wegmans did not expose a price after store selection")
        validate_price(product["product"], price)
        qty, unit = verify_package(text, cfg)
        return {
            "store": "Wegmans", "product": product["product"], "package_qty": qty,
            "package_unit": unit, "price": price, "original_price": original,
            "promo": ("Sale" if original and original > price else ""),
            "price_type": "ONLINE_LOCALIZED", "source_url": page.url
        }
    finally:
        await page.close()


async def set_lidl_store(page):
    store = CONFIG["stores"]["Lidl"]
    await page.goto(store["store_page"], wait_until="domcontentloaded", timeout=60000)
    await dismiss_common(page)
    selected = False
    try:
        loc = page.get_by_text(re.compile(r"^Set as favorite store$", re.I))
        if await loc.count():
            await loc.last.click(timeout=3000)
            await page.wait_for_timeout(1200)
            selected = True
    except Exception:
        pass
    return selected


async def collect_lidl(context, product):
    cfg = product["Lidl"]
    page = await context.new_page()
    try:
        await set_lidl_store(page)
        await page.goto(cfg["url"], wait_until="domcontentloaded", timeout=60000)
        await dismiss_common(page)
        await page.wait_for_timeout(1800)
        text = await body_text(page)
        identity_check(text + " " + cfg["url"] + " " + page.url, product["product"], "Lidl")
        price, original = parse_lidl_price(text, cfg.get("price_mode", "package"))
        if price is None:
            raise RuntimeError("Lidl did not expose a product price")
        validate_price(product["product"], price)
        qty, unit = verify_package(text, cfg)
        return {
            "store": "Lidl", "product": product["product"], "package_qty": qty,
            "package_unit": unit, "price": price, "original_price": original,
            "promo": "", "price_type": "ONLINE_STORE_PAGE", "source_url": page.url,
            "variant_note": cfg.get("note", "")
        }
    finally:
        await page.close()


async def set_safeway_store(page):
    store = CONFIG["stores"]["Safeway"]
    url = (
        "https://www.safeway.com/?preference=PICKUP"
        f"&storeId={store['store_id']}&zipcode={store['zip_code']}"
    )
    await page.goto(url, wait_until="domcontentloaded", timeout=60000)
    await dismiss_common(page)
    await page.wait_for_timeout(1200)


async def collect_safeway(context, product):
    cfg = product["Safeway"]
    store = CONFIG["stores"]["Safeway"]
    page = await context.new_page()
    try:
        await set_safeway_store(page)
        sep = "&" if "?" in cfg["url"] else "?"
        href = cfg["url"] + sep + f"preference=PICKUP&storeId={store['store_id']}&zipcode={store['zip_code']}"
        await page.goto(href, wait_until="domcontentloaded", timeout=60000)
        await dismiss_common(page)
        await page.wait_for_timeout(1800)
        text = await body_text(page)
        identity_check(text, product["product"], "Safeway")
        price, original = parse_safeway_price(text, cfg.get("price_mode", "package"))
        if price is None:
            print(f"DIAG Safeway {product['product']} URL: {page.url}")
            for line in safeway_visible_diagnostics(text):
                print(f"DIAG Safeway {product['product']}: {line}")
            raise RuntimeError("Safeway did not expose a localized product price")
        validate_price(product["product"], price)
        qty, unit = verify_package(text, cfg)
        return {
            "store": "Safeway", "product": product["product"], "package_qty": qty,
            "package_unit": unit, "price": price, "original_price": original,
            "promo": ("Member price available" if original and original != price else ""),
            "price_type": "ONLINE_LOCALIZED", "source_url": page.url
        }
    finally:
        await page.close()


async def collect_aldi(context, product):
    cfg = product["ALDI"]
    url = cfg["url"] + ("&" if "?" in cfg["url"] else "?") + f"service=delivery&zipcode={CONFIG['zip_code']}"
    page = await context.new_page()
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=60000)
        await dismiss_common(page)
        text = await body_text(page)
        price, original = parse_standard_price(text, cfg.get("price_mode", "package"))
        if price is None:
            raise RuntimeError("No ALDI price found")
        validate_price(product["product"], price)
        qty, unit = package_from_config(cfg)
        return {
            "store": "ALDI", "product": product["product"], "package_qty": qty,
            "package_unit": unit, "price": price, "original_price": original,
            "promo": ("Price drop" if original and original > price else ""),
            "price_type": "ONLINE_LOCALIZED", "source_url": page.url,
            "variant_note": cfg.get("note", "")
        }
    finally:
        await page.close()


async def collect_giant(context, product):
    raise RuntimeError("Giant retired from active store set")


async def main():
    offers = []
    store_names = list(CONFIG["stores"].keys())
    failures = {s: [] for s in store_names}
    collectors = {
        "Wegmans": collect_wegmans,
        "Lidl": collect_lidl,
        "Safeway": collect_safeway,
        "ALDI": collect_aldi,
        "Giant": collect_giant,
    }

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            locale="en-US",
            timezone_id="America/New_York",
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/128 Safari/537.36"
        )
        for store in store_names:
            fn = collectors.get(store)
            if fn is None:
                failures[store].append("No collector implemented")
                continue
            for product in CONFIG["products"]:
                if store not in product:
                    failures[store].append(f"{product['product']}: no product mapping")
                    continue
                try:
                    offer = await fn(context, product)
                    offer["captured_at"] = datetime.now(timezone.utc).isoformat()
                    offers.append(offer)
                    print(f"OK {store}: {product['product']} -> ${offer['price']}")
                except Exception as e:
                    failures[store].append(f"{product['product']}: {e}")
                    print(f"FAIL {store}: {product['product']}: {e}")
        await context.close()
        await browser.close()

    status = {}
    for store in store_names:
        count = sum(1 for o in offers if o["store"] == store)
        requested = sum(1 for p in CONFIG["products"] if store in p)
        status[store] = {
            "ok": count > 0,
            "products_collected": count,
            "products_requested": requested,
            "message": ("OK" if requested and count == requested else f"{count}/{requested} products collected"),
            "errors": failures[store][:8]
        }

    payload = {
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "zip_code": CONFIG["zip_code"],
        "offers": offers,
        "status": status
    }
    OUT.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(f"Wrote {OUT} with {len(offers)} offers")


if __name__ == "__main__":
    asyncio.run(main())
