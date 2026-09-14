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
    "Eggs": (0.50, 20.00), "Milk": (1.00, 20.00), "Bananas": (0.10, 5.00),
    "Chicken breast": (0.50, 20.00), "Greek yogurt": (0.50, 25.00),
    "Salmon": (2.00, 50.00), "Avocados": (0.20, 20.00), "Olive oil": (2.00, 80.00),
}


def money(s): return float(s.replace(",", ""))

def package_from_config(cfg):
    return (1.0, "lb") if cfg.get("price_mode") == "per_lb" else (float(cfg["fallback_qty"]), cfg["fallback_unit"])

def validate_price(name, price):
    lo, hi = PRICE_LIMITS.get(name, (0.05, 500.0))
    if not lo <= price <= hi: raise RuntimeError(f"Suspicious price ${price:.2f} for {name}")

def identity_check(text, name, store):
    hay = text[:7000].lower()
    if not all(x in hay for x in REQUIRED_TERMS.get(name, [])): raise RuntimeError(f"{store} product page failed identity check")

def parse_standard_price(text, mode="package"):
    if mode == "per_lb":
        m = PER_LB.search(text)
        if m: return money(m.group(1)), None
    m = CURRENT_PRICE.search(text)
    if m:
        o = ORIGINAL_PRICE.search(text)
        return money(m.group(1)), money(o.group(1)) if o else None
    vals = [money(x) for x in MONEY.findall(text[:5000]) if 0.05 <= money(x) <= 500]
    return (vals[0], None) if vals else (None, None)

def parse_lidl_price(text, mode="package"):
    if mode == "per_lb":
        m = PER_LB.search(text[:6000])
        if m: return money(m.group(1)), None
    vals = [money(x) for x in LIDL_PRICE.findall(text[:6000])]
    return (vals[-1], None) if vals else (None, None)

def parse_harris_price(text, mode="package"):
    head = text[:8000]
    if mode == "per_lb":
        m = PER_LB.search(head)
        if m: return money(m.group(1)), None
    vals = [money(x) for x in MONEY.findall(head) if 0.05 <= money(x) <= 500]
    if not vals: return None, None
    return vals[0], next((x for x in vals[1:5] if x > vals[0]), None)

def observed_package(text, unit):
    t = text[:4500].lower().replace("fl. oz.", "fl oz").replace("fl. oz", "fl oz")
    if unit == "fl_oz":
        m = re.search(r"(\d+(?:\.\d+)?)\s*(?:gallon|gal)\b", t)
        if m: return round(float(m.group(1))*128, 3), unit
        m = re.search(r"(\d+(?:\.\d+)?)\s*fl\s*oz\b", t)
        if m: return float(m.group(1)), unit
    if unit == "oz":
        m = re.search(r"(\d+(?:\.\d+)?)\s*(?:oz|ounce)\b", t)
        if m: return float(m.group(1)), unit
    if unit == "count":
        m = re.search(r"(?<![\d.])(\d+)\s*(?:ct|count|piece|pieces|each|ea)\b", t)
        if m: return float(m.group(1)), unit
        m = re.search(r"(?<![\d.])(\d+)\s*doz\b", t)
        if m: return float(m.group(1))*12, unit
    return None, None

def verify_package(text, cfg):
    eq, eu = package_from_config(cfg)
    if cfg.get("price_mode") == "per_lb": return eq, eu
    q, u = observed_package(text, eu)
    if q is None:
        if cfg.get("package_verified"): return eq, eu
        raise RuntimeError("Could not verify package size from product page")
    if u != eu or abs(q-eq) > max(0.05, eq*0.02): raise RuntimeError(f"Package mismatch: expected {eq:g} {eu}, page shows {q:g} {u}")
    return eq, eu


async def body_text(page):
    await page.wait_for_timeout(1200)
    return await page.locator("body").inner_text()

async def dismiss_common(page):
    for label in ["Accept All", "Accept", "I Agree", "Got it", "Got It", "Close", "No Thanks"]:
        try:
            b = page.get_by_role("button", name=re.compile(f"^{re.escape(label)}$", re.I))
            if await b.count(): await b.first.click(timeout=1200)
        except Exception: pass

async def safe_goto(page, url):
    last = None
    for wait in ["domcontentloaded", "commit"]:
        try:
            await page.goto(url, wait_until=wait, timeout=60000); return
        except Exception as e:
            last = e; await page.wait_for_timeout(1000)
    raise last

async def set_wegmans_store(page):
    await safe_goto(page, CONFIG["stores"]["Wegmans"]["store_page"]); await dismiss_common(page)
    try:
        b = page.get_by_role("button", name=re.compile("Shop This Store", re.I)).first
        if await b.count(): await b.click(timeout=3000); await page.wait_for_timeout(1000)
    except Exception: pass

async def collect_wegmans(context, product):
    cfg=product["Wegmans"]; page=await context.new_page()
    try:
        await set_wegmans_store(page); await safe_goto(page,cfg["url"]); await dismiss_common(page); text=await body_text(page)
        identity_check(text,product["product"],"Wegmans"); price,original=parse_standard_price(text,cfg.get("price_mode","package"))
        if price is None: raise RuntimeError("Wegmans did not expose a price after store selection")
        validate_price(product["product"],price); q,u=verify_package(text,cfg)
        return {"store":"Wegmans","product":product["product"],"package_qty":q,"package_unit":u,"price":price,"original_price":original,"promo":"Sale" if original and original>price else "","price_type":"ONLINE_LOCALIZED","source_url":page.url}
    finally: await page.close()

async def set_lidl_store(page):
    await safe_goto(page,CONFIG["stores"]["Lidl"]["store_page"]); await dismiss_common(page)
    try:
        x=page.get_by_text(re.compile(r"^Set as favorite store$",re.I))
        if await x.count(): await x.last.click(timeout=3000); await page.wait_for_timeout(1200)
    except Exception: pass

async def collect_lidl(context, product):
    cfg=product["Lidl"]; page=await context.new_page()
    try:
        await set_lidl_store(page); await safe_goto(page,cfg["url"]); await dismiss_common(page); await page.wait_for_timeout(1800); text=await body_text(page)
        identity_check(text+" "+cfg["url"]+" "+page.url,product["product"],"Lidl"); price,original=parse_lidl_price(text,cfg.get("price_mode","package"))
        if price is None: raise RuntimeError("Lidl did not expose a product price")
        validate_price(product["product"],price); q,u=verify_package(text,cfg)
        return {"store":"Lidl","product":product["product"],"package_qty":q,"package_unit":u,"price":price,"original_price":original,"promo":"","price_type":"ONLINE_STORE_PAGE","source_url":page.url,"variant_note":cfg.get("note","")}
    finally: await page.close()

async def set_harris_store(page):
    store=CONFIG["stores"]["Harris Teeter"]
    await safe_goto(page,"https://www.harristeeter.com/stores/search"); await dismiss_common(page)
    search=page.get_by_placeholder(re.compile(r"45201|Cincinnati|City|ZIP",re.I)).first
    if not await search.count(): raise RuntimeError("Harris Teeter store search input was not available")
    await search.fill(store["zip_code"]); await search.press("Enter"); await page.wait_for_timeout(2500); text=await body_text(page)
    if "lee harrison" not in text.lower(): raise RuntimeError("Lee Harrison not found in Harris Teeter store search")
    clicked=False
    try:
        name=page.get_by_text(re.compile(r"^Lee Harrison$",re.I)).first
        card=name.locator("xpath=ancestor::*[self::div or self::li or self::article][.//*[contains(normalize-space(.), 'Shop Pickup')]][1]")
        if await card.count():
            shop=card.get_by_text(re.compile(r"^Shop Pickup$",re.I)).first
            if await shop.count(): await shop.click(timeout=4000); clicked=True
    except Exception: pass
    if not clicked:
        shops=page.get_by_text(re.compile(r"^Shop Pickup$",re.I))
        if await shops.count()==1: await shops.first.click(timeout=4000); clicked=True
    if not clicked: raise RuntimeError("Could not select Lee Harrison pickup from store results")
    await page.wait_for_timeout(2500)

async def harris_location_evidence(context,text):
    hay=text[:12000].lower()
    if "lee harrison" in hay or "2425 n harrison" in hay or "00023" in hay: return True
    for c in await context.cookies("https://www.harristeeter.com"):
        v=str(c.get("value","")).lower()
        if "00023" in v or "lee%20harrison" in v or "lee harrison" in v: return True
    return False

async def collect_harris(context, product):
    cfg=product["Harris Teeter"]; page=await context.new_page()
    try:
        await set_harris_store(page); await safe_goto(page,cfg["url"]); await dismiss_common(page); await page.wait_for_timeout(1800); text=await body_text(page)
        identity_check(text+" "+cfg["url"]+" "+page.url,product["product"],"Harris Teeter")
        if not await harris_location_evidence(context,text): raise RuntimeError("Could not verify Lee Harrison store localization")
        price,original=parse_harris_price(text,cfg.get("price_mode","package"))
        if price is None: raise RuntimeError("Harris Teeter did not expose a product price")
        validate_price(product["product"],price); q,u=verify_package(text,cfg)
        return {"store":"Harris Teeter","product":product["product"],"package_qty":q,"package_unit":u,"price":price,"original_price":original,"promo":"Sale" if original and original>price else "","price_type":"ONLINE_LOCALIZED","source_url":page.url,"variant_note":cfg.get("note","")}
    finally: await page.close()

async def main():
    offers=[]; stores=list(CONFIG["stores"]); failures={s:[] for s in stores}; collectors={"Wegmans":collect_wegmans,"Lidl":collect_lidl,"Harris Teeter":collect_harris}
    async with async_playwright() as p:
        browser=await p.chromium.launch(headless=True,args=["--disable-http2","--disable-quic"])
        context=await browser.new_context(locale="en-US",timezone_id="America/New_York",user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/128 Safari/537.36")
        for store in stores:
            fn=collectors.get(store)
            if not fn: failures[store].append("No collector implemented"); continue
            for product in CONFIG["products"]:
                if store not in product: failures[store].append(f"{product['product']}: no product mapping"); continue
                try:
                    o=await fn(context,product); o["captured_at"]=datetime.now(timezone.utc).isoformat(); offers.append(o); print(f"OK {store}: {product['product']} -> ${o['price']}")
                except Exception as e:
                    failures[store].append(f"{product['product']}: {e}"); print(f"FAIL {store}: {product['product']}: {e}")
        await context.close(); await browser.close()
    status={}
    for store in stores:
        count=sum(1 for o in offers if o["store"]==store); requested=sum(1 for p in CONFIG["products"] if store in p)
        status[store]={"ok":count>0,"products_collected":count,"products_requested":requested,"message":"OK" if requested and count==requested else f"{count}/{requested} products collected","errors":failures[store][:8]}
    OUT.write_text(json.dumps({"updated_at":datetime.now(timezone.utc).isoformat(),"zip_code":CONFIG["zip_code"],"offers":offers,"status":status},indent=2),encoding="utf-8")
    print(f"Wrote {OUT} with {len(offers)} offers")

if __name__=="__main__": asyncio.run(main())
