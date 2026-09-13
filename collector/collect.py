import asyncio
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

from playwright.async_api import async_playwright

ROOT = Path(__file__).resolve().parents[1]
CONFIG = json.loads((ROOT / "products.json").read_text(encoding="utf-8"))
OUT = ROOT / "docs" / "prices.json"

MONEY = re.compile(r"\$(\d{1,3}(?:,\d{3})*(?:\.\d{2})?)")
PER_LB = re.compile(r"\$(\d{1,3}(?:,\d{3})*(?:\.\d{2})?)\s*/\s*lb", re.I)
CURRENT_PRICE = re.compile(r"Current price:\s*\$(\d{1,3}(?:,\d{3})*(?:\.\d{2})?)", re.I)
ORIGINAL_PRICE = re.compile(r"Original Price:\s*\$(\d{1,3}(?:,\d{3})*(?:\.\d{2})?)", re.I)


def money(s):
    return float(s.replace(",", ""))


def infer_package(text, fallback_qty, fallback_unit):
    t = text.lower().replace("fl. oz", "fl oz")
    patterns = [
        (r"(\d+(?:\.\d+)?)\s*fl\s*oz\b", "fl_oz", 1),
        (r"(\d+(?:\.\d+)?)\s*oz\b", "oz", 1),
        (r"(\d+(?:\.\d+)?)\s*lb\b", "lb", 1),
        (r"(\d+(?:\.\d+)?)\s*(?:ct|count)\b", "count", 1),
        (r"(\d+(?:\.\d+)?)\s*(?:gal|gallon)\b", "fl_oz", 128),
    ]
    for pat, unit, factor in patterns:
        m = re.search(pat, t, re.I)
        if m:
            return round(float(m.group(1)) * factor, 3), unit
    return float(fallback_qty), fallback_unit


def parse_price(text, mode="package"):
    if mode == "per_lb":
        m = PER_LB.search(text)
        if m:
            return money(m.group(1)), None
    m = CURRENT_PRICE.search(text)
    if m:
        original = ORIGINAL_PRICE.search(text)
        return money(m.group(1)), money(original.group(1)) if original else None
    vals = [money(x) for x in MONEY.findall(text)]
    vals = [x for x in vals if 0.05 <= x <= 500]
    if not vals:
        return None, None
    return vals[0], None


async def body_text(page):
    await page.wait_for_timeout(1000)
    return await page.locator("body").inner_text()


async def dismiss_common(page):
    for label in ["Accept All", "Accept", "I Agree", "Got it", "Close"]:
        try:
            loc = page.get_by_role("button", name=re.compile(f"^{re.escape(label)}$", re.I))
            if await loc.count():
                await loc.first.click(timeout=1200)
        except Exception:
            pass


async def choose_link(page, query, href_fragment):
    links = page.locator(f'a[href*="{href_fragment}"]')
    n = min(await links.count(), 40)
    if not n:
        return None
    tokens = [x.lower() for x in re.findall(r"[A-Za-z0-9]+", query) if len(x) > 2]
    best = None
    best_score = -1
    for i in range(n):
        link = links.nth(i)
        try:
            txt = (await link.inner_text()).strip()
            href = await link.get_attribute("href")
        except Exception:
            continue
        score = sum(tok in txt.lower() for tok in tokens)
        if score > best_score and href:
            best, best_score = href, score
    return best


async def collect_aldi(context, product):
    cfg = product["ALDI"]
    url = cfg["url"] + ("&" if "?" in cfg["url"] else "?") + f"service=delivery&zipcode={CONFIG['zip_code']}"
    page = await context.new_page()
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=60000)
        await dismiss_common(page)
        text = await body_text(page)
        price, original = parse_price(text, cfg.get("price_mode", "package"))
        if price is None:
            raise RuntimeError("No ALDI price found")
        qty, unit = infer_package(text, cfg["fallback_qty"], cfg["fallback_unit"])
        if cfg.get("price_mode") == "per_lb":
            qty, unit = 1.0, "lb"
        return {
            "store": "ALDI",
            "product": product["product"],
            "package_qty": qty,
            "package_unit": unit,
            "price": price,
            "original_price": original,
            "promo": ("Price drop" if original and original > price else ""),
            "price_type": "ONLINE_LOCALIZED",
            "source_url": page.url,
            "variant_note": cfg.get("note", "")
        }
    finally:
        await page.close()


async def set_giant_store(page):
    await page.goto(CONFIG["stores"]["Giant"]["store_page"], wait_until="domcontentloaded", timeout=60000)
    await dismiss_common(page)
    await page.goto("https://giantfood.com/groceries/", wait_until="domcontentloaded", timeout=60000)
    await dismiss_common(page)
    text = await body_text(page)
    if "3336 Wisconsin" in text:
        return
    for label in ["Select Store", "Change Store", "Choose Store"]:
        try:
            b = page.get_by_text(re.compile(label, re.I)).first
            if await b.count():
                await b.click(timeout=2000)
                break
        except Exception:
            pass
    for placeholder in ["ZIP", "Zip", "zip code", "Enter ZIP"]:
        try:
            inp = page.get_by_placeholder(re.compile(placeholder, re.I)).first
            if await inp.count():
                await inp.fill(CONFIG["zip_code"])
                await inp.press("Enter")
                await page.wait_for_timeout(1500)
                break
        except Exception:
            pass
    try:
        choice = page.get_by_text(re.compile(r"3336 Wisconsin", re.I)).first
        if await choice.count():
            await choice.click(timeout=2500)
            await page.wait_for_timeout(1200)
    except Exception:
        pass


async def collect_giant(context, product):
    cfg = product["Giant"]
    page = await context.new_page()
    try:
        await set_giant_store(page)
        url = "https://giantfood.com/product-search/" + quote(cfg["query"])
        await page.goto(url, wait_until="domcontentloaded", timeout=60000)
        await dismiss_common(page)
        await page.wait_for_timeout(1800)
        href = await choose_link(page, cfg["query"], "/groceries/product/")
        if not href:
            raise RuntimeError("No Giant product link found")
        if href.startswith("/"):
            href = "https://giantfood.com" + href
        await page.goto(href, wait_until="domcontentloaded", timeout=60000)
        await page.wait_for_timeout(1600)
        text = await body_text(page)
        if "See Best Price" in text and len(MONEY.findall(text)) <= 1:
            raise RuntimeError("Giant did not expose a localized price to this session")
        price, original = parse_price(text, cfg.get("price_mode", "package"))
        if price is None:
            raise RuntimeError("No Giant price found")
        qty, unit = infer_package(text[:2500], cfg["fallback_qty"], cfg["fallback_unit"])
        if cfg.get("price_mode") == "per_lb":
            qty, unit = 1.0, "lb"
        return {
            "store": "Giant",
            "product": product["product"],
            "package_qty": qty,
            "package_unit": unit,
            "price": price,
            "original_price": original,
            "promo": ("Sale" if original and original > price else ""),
            "price_type": "ONLINE_LOCALIZED",
            "source_url": page.url
        }
    finally:
        await page.close()


async def set_wegmans_store(page):
    await page.goto(CONFIG["stores"]["Wegmans"]["store_page"], wait_until="domcontentloaded", timeout=60000)
    await dismiss_common(page)
    try:
        btn = page.get_by_role("button", name=re.compile("Shop This Store", re.I)).first
        if await btn.count():
            await btn.click(timeout=3000)
            await page.wait_for_timeout(1200)
    except Exception:
        pass


async def wegmans_search(page, query):
    for placeholder in ["Search", "Search..."]:
        try:
            inp = page.get_by_placeholder(re.compile(f"^{re.escape(placeholder)}$", re.I)).first
            if await inp.count():
                await inp.fill(query)
                await inp.press("Enter")
                await page.wait_for_timeout(2200)
                return
        except Exception:
            pass
    inputs = page.locator('input[type="search"], input[placeholder*="Search" i]')
    if await inputs.count():
        inp = inputs.first
        await inp.fill(query)
        await inp.press("Enter")
        await page.wait_for_timeout(2200)
        return
    raise RuntimeError("Wegmans search box not found")


async def collect_wegmans(context, product):
    cfg = product["Wegmans"]
    page = await context.new_page()
    try:
        await set_wegmans_store(page)
        await wegmans_search(page, cfg["query"])
        href = await choose_link(page, cfg["query"], "/shop/product/")
        if not href:
            raise RuntimeError("No Wegmans product link found")
        if href.startswith("/"):
            href = "https://www.wegmans.com" + href
        await page.goto(href, wait_until="domcontentloaded", timeout=60000)
        await page.wait_for_timeout(1800)
        text = await body_text(page)
        price, original = parse_price(text, cfg.get("price_mode", "package"))
        if price is None:
            raise RuntimeError("Wegmans did not expose a price after store selection")
        qty, unit = infer_package(text[:2200], cfg["fallback_qty"], cfg["fallback_unit"])
        if cfg.get("price_mode") == "per_lb":
            qty, unit = 1.0, "lb"
        return {
            "store": "Wegmans",
            "product": product["product"],
            "package_qty": qty,
            "package_unit": unit,
            "price": price,
            "original_price": original,
            "promo": ("Sale" if original and original > price else ""),
            "price_type": "ONLINE_LOCALIZED",
            "source_url": page.url
        }
    finally:
        await page.close()


async def main():
    offers = []
    failures = {"Wegmans": [], "Giant": [], "ALDI": []}

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            locale="en-US",
            timezone_id="America/New_York",
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/128 Safari/537.36"
        )
        collectors = {"ALDI": collect_aldi, "Giant": collect_giant, "Wegmans": collect_wegmans}
        for store, fn in collectors.items():
            for product in CONFIG["products"]:
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
    for store in ["Wegmans", "Giant", "ALDI"]:
        count = sum(1 for o in offers if o["store"] == store)
        status[store] = {
            "ok": count > 0,
            "products_collected": count,
            "products_requested": len(CONFIG["products"]),
            "message": ("OK" if count == len(CONFIG["products"]) else f"{count}/{len(CONFIG['products'])} products collected"),
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
