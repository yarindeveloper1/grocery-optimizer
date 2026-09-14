import json
import urllib.request
import urllib.error

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/128 Safari/537.36"


def request(url, data=None, method=None):
    body = json.dumps(data).encode() if data is not None else None
    headers = {
        "User-Agent": UA,
        "Accept": "application/json,text/html;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Origin": "https://www.harristeeter.com",
        "Referer": "https://www.harristeeter.com/",
    }
    if body is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=body, headers=headers, method=method or ("POST" if body is not None else "GET"))
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            raw = r.read().decode("utf-8", "replace")
            print(f"STATUS {r.status} {url}")
            print(raw[:6000])
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        print(f"HTTP {e.code} {url}")
        print(raw[:3000])
    except Exception as e:
        print(f"ERROR {url}: {type(e).__name__}: {e}")


print("=== store options ===")
request("https://www.harristeeter.com/atlas/v1/modality/options", {"address": {"postalCode": "22207"}})
print("=== modality preferences ===")
request("https://www.harristeeter.com/atlas/v1/modality/preferences", {}, "POST")
print("=== product html ===")
request("https://www.harristeeter.com/p/harris-teeter-large-grade-a-white-eggs/0007203663220?fulfillment=PICKUP")
