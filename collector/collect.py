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

REQUIRED_TERMS = {
    "Eggs": ["egg"], "Milk": ["milk"], "Bananas": ["banana"],
    "Chicken breast": ["chicken", "breast"], "Greek yogurt": ["greek", "yogurt"],
    "Salmon": ["salmon"], "Avocados": ["avocado"], "Olive oil": ["olive", "oil"],
}
PRICE_LIMITS = {
    "Eggs": (0.50, 20.00), "Milk": (1.00, 20.00), "Bananas": (0.10, 10.00),
    "Chicken breast": (0.50, 20.00), "Greek yogurt": (0.50, 25.00),
    "Salmon": (2.00, 50.00), "Avocados": (0.20, 20.00), "Olive oil": (2.00, 80.00),
}


def money(s):
    return float(s.replace(",", ""))


def package_from_config(cfg):
    if cfg.get("price_mode") == "per_lb":
        return 1.0, "lb"
    return float(cfg["fallback_qty"]), cfg["fallback_unit"]


def validate_price(name, price):
    lo, hi = PRICE_LIMITS.get(name, (0.05, 500.0))
    if not lo <= price <= hi:
        raise RuntimeError(f"Suspicious price ${price:.2f} for {name}")


def identity_check(text, name, store):
    hay = text[:10000].lower()
    if not all(term in hay for term in REQUIRED_TERMS.get(name, [])):
        raise RuntimeError(f"{store} product failed identity check")


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
        m = PER_LB.search(text[:6000])
        if m:
            return money(m.group(1)), None
    vals = [money(x) for x in LIDL_PRICE.findall(text[:6000])]
    return (vals[-1], None) if vals else (None, None)


def parse_target_price(text, mode="package"):
    head = text[:8000]
    if mode == "per_lb":
        m = re.search(
            r"\$\s*\d{1,3}(?:,\d{3})*(?:\.\d{2})?\s*max price\s*\(\$\s*(\d{1,3}(?:,\d{3})*(?:\.\d{2})?)\s*/\s*lb\)",
            head, re.I,
        )
        if m:
            return money(m.group(1)), None
        m = PER_LB.search(head)
        if m:
            return money(m.group(1)), None
    for line in head.splitlines():
        s = line.strip()
        if not s.startswith("$"):
            continue
        m = re.match(r"\$\s*(\d{1,3}(?:,\d{3})*(?:\.\d{2})?)", s)
        if not m:
            continue
        current = money(m.group(1))
        was = re.search(r"\bwas\s*\$\s*(\d{1,3}(?:,\d{3})*(?:\.\d{2})?)", s, re.I)
        original = money(was.group(1)) if was else None
        return current, original
    return None, None


def observed_package(text, unit):
    t = text[:7000].lower().replace("fl. oz.", "fl oz").replace("fl. oz", "fl oz")
    if unit == "fl_oz":
        m = re.search(r"(\d+(?:\.\d+)?)\s*(?:gallon|gal)\b", t)
        if m:
            return round(float(m.group(1)) * 128, 3), unit
        m = re.search(r"(\d+(?:\.\d+)?)\s*fl\s*oz\b", t)
        if m:
            return float(m.group(1)), unit
    elif unit == "oz":
        m = re.search(r"(\d+(?:\.\d+)?)\s*(?:oz|ounce)\b", t)
        if m:
            return float(m.group(1)), unit
    elif unit == "lb":
        m = re.search(r"(\d+(?:\.\d+)?)\s*(?:lb|lbs|pound|pounds)\b", t)
        if m:
            return float(m.group(1)), unit
    elif unit == "count":
        m = re.search(r"(?<![\d.])(\d+)\s*(?:ct|count|piece|pieces|each|ea)\b", t)
        if m:
            return float(m.group(1)), unit
        m = re.search(r"(?<![\d.])(\d+)\s*doz\b", t)
        if m:
            return float(m.group(1)) * 12, unit
    return None, None


def verify_package(text, cfg):
    expected_qty, expected_unit = package_from_config(cfg)
    if cfg.get("price_mode") == "per_lb":
        return expected_qty, expected_unit
    qty, unit = observed_package(text, expected_unit)
    if qty is None:
        if cfg.get("package_verified"):
            return expected_qty, expected_unit
        raise RuntimeError("Could not verify package size from product evidence")
    if unit != expected_unit or abs(qty - expected_qty) > max(0.05, expected_qty * 0.02):
        raise RuntimeError(
            f"Package mismatch: expected {expected_qty:g} {expected_unit}, evidence shows {qty:g} {unit}"
        )
    return expected_qty, expected_unit


async def body_text(page):
    await page.wait_for_timeout(1200)
    return await page.locator("body").inner_text()


async def dismiss_common(page):
    for label in ["Accept All", "Accept", "I Agree", "Got it", "Got It", "Close", "No Thanks"]:
        try:
            btn = page.get_by_role("button", name=re.compile(f"^{re.escape(label)}$", re.I))
            if await btn.count():
                await btn.first.click(timeout=1200)
        except Exception:
            pass


async def safe_goto(page, url):
    await page.goto(url, wait_until="domcontentloaded", timeout=60000)


async def set_wegmans_store(page):
    await safe_goto(page, CONFIG["stores"]["Wegmans"]["store_page"])
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
        await safe_goto(page, cfg["url"])
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
            "promo": "Sale" if original and original > price else "",
            "price_type": "ONLINE_LOCALIZED", "source_url": page.url,
        }
    finally:
        await page.close()


async def set_lidl_store(page):
    await safe_goto(page, CONFIG["stores"]["Lidl"]["store_page"])
    await dismiss_common(page)
    try:
        loc = page.get_by_text(re.compile(r"^Set as favorite store$", re.I))
        if await loc.count():
            await loc.last.click(timeout=3000)
            await page.wait_for_timeout(1200)
    except Exception:
        pass


async def collect_lidl(context, product):
    cfg = product["Lidl"]
    page = await context.new_page()
    try:
        await set_lidl_store(page)
        await safe_goto(page, cfg["url"])
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
            "variant_note": cfg.get("note", ""),
        }
    finally:
        await page.close()


async def set_target_store(page):
    store = CONFIG["stores"]["Target"]
    context = page.context
    await safe_goto(page, "https://www.target.com/")
    await context.add_cookies([
        {"name": "fiatsCookie", "value": f"DSI_{store['store_number']}|DSN_DC%20Tenleytown|DSZ_20016", "domain": ".target.com", "path": "/"},
        {"name": "GuestLocation", "value": "20016|38.949|-77.080|DC|US", "domain": ".target.com", "path": "/"},
        {"name": "UserLocation", "value": "20016|38.949|-77.080|DC|US", "domain": ".target.com", "path": "/"},
        {"name": "adScriptData", "value": "DC", "domain": ".target.com", "path": "/"},
    ])
    await safe_goto(page, store["store_page"])
    await dismiss_common(page)
    await page.wait_for_timeout(1200)
    cookies = await context.cookies("https://www.target.com")
    fiat = next((str(c.get("value", "")) for c in cookies if c.get("name") == "fiatsCookie"), "")
    if f"DSI_{store['store_number']}" not in fiat:
        raise RuntimeError(f"Target overwrote Tenleytown store selection: {fiat or 'no fiatsCookie'}")


async def target_location_evidence(context, text):
    hay = text[:14000].lower()
    if "cheyenne" in hay or "pickup at cheyenne" in hay:
        return False
    if "dc tenleytown" in hay or "4500 wisconsin" in hay:
        return True
    cookies = await context.cookies("https://www.target.com")
    fiat = next((str(c.get("value", "")) for c in cookies if c.get("name") == "fiatsCookie"), "")
    guest = next((str(c.get("value", "")) for c in cookies if c.get("name") == "GuestLocation"), "")
    return "DSI_3351" in fiat and guest.startswith("20016|")


async def collect_target(context, product):
    cfg = product["Target"]
    page = await context.new_page()
    try:
        await set_target_store(page)
        await safe_goto(page, cfg["url"])
        await dismiss_common(page)
        await page.wait_for_timeout(1800)
        text = await body_text(page)
        identity_check(text + " " + cfg.get("query", ""), product["product"], "Target")
        localized = await target_location_evidence(context, text)
        if not localized:
            cookie_diag = [(c.get("name"), str(c.get("value", ""))[:120]) for c in await context.cookies("https://www.target.com") if c.get("name") in {"fiatsCookie", "GuestLocation", "UserLocation"}]
            header = " | ".join(x.strip() for x in text.splitlines()[:45] if x.strip())
            print(f"DIAG Target {product['product']} header={header[:1400]}")
            print(f"DIAG Target location cookies={cookie_diag}")
            raise RuntimeError("Could not verify Target DC Tenleytown localization")
        price, original = parse_target_price(text, cfg.get("price_mode", "package"))
        if price is None:
            raise RuntimeError("Target did not expose a product price")
        validate_price(product["product"], price)
        qty, unit = verify_package(text, cfg)
        return {
            "store": "Target", "product": product["product"], "package_qty": qty,
            "package_unit": unit, "price": price, "original_price": original,
            "promo": "Sale" if original and original > price else "",
            "price_type": "ONLINE_LOCALIZED", "source_url": page.url,
            "variant_note": cfg.get("note", ""),
        }
    finally:
        await page.close()


async def main():
    offers = []
    stores = list(CONFIG["stores"])
    failures = {store: [] for store in stores}
    collectors = {
        "Wegmans": collect_wegmans,
        "Lidl": collect_lidl,
        "Target": collect_target,
    }
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            locale="en-US", timezone_id="America/New_York",
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/128 Safari/537.36",
        )
        for store in stores:
            fn = collectors.get(store)
            if not fn:
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
    for store in stores:
        count = sum(1 for offer in offers if offer["store"] == store)
        requested = sum(1 for product in CONFIG["products"] if store in product)
        status[store] = {
            "ok": count > 0,
            "products_collected": count,
            "products_requested": requested,
            "message": "OK" if requested and count == requested else f"{count}/{requested} products collected",
            "errors": failures[store][:8],
        }
    payload = {
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "zip_code": CONFIG["zip_code"],
        "offers": offers,
        "status": status,
    }
    OUT.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(f"Wrote {OUT} with {len(offers)} offers")


if __name__ == "__main__":
    asyncio.run(main())
