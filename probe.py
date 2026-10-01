"""One-off test of lighter Amazon pages that might replace one request per card.

Saves what Amazon returns to the debug folder so the monitor can be tuned to it:
  debug/cart_probe.html - "add to cart" confirmation for ALL cards in one request
                          (just a preview page; nothing is bought or added)
  debug/aod_probe.html  - the small "all offers" panel for the first card
"""
import time

import monitor

products = monitor.load_products(monitor.PRODUCTS_FILE)
session = monitor.new_session()
monitor.ensure_location(session)

cart_query = "&".join(f"ASIN.{i}={p.asin}&Quantity.{i}=1" for i, p in enumerate(products, 1))
probes = {
    "cart_probe.html": f"https://www.amazon.in/gp/aws/cart/add.html?{cart_query}",
    "aod_probe.html": (f"https://www.amazon.in/gp/product/ajax/ref=dp_aod_ALL_mbc?asin={products[0].asin}"
                       "&m=&smid=&sourcecustomerorglistid=&sourcecustomerorglistitemid=&sr=&pc=dp"
                       "&experienceId=aodAjaxMain"),
}
monitor.DEBUG_DIR.mkdir(exist_ok=True)
for name, url in probes.items():
    time.sleep(3)
    try:
        resp = session.get(url, timeout=15)
    except Exception as e:
        print(f"{name}: request failed: {e}")
        continue
    (monitor.DEBUG_DIR / name).write_text(resp.text, encoding="utf-8")
    blocked = resp.status_code == 503 or monitor.parse_status(resp.text) is monitor.Status.BLOCKED
    print(f"{name}: HTTP {resp.status_code}, {len(resp.text) // 1024} KB"
          + (" - BLOCKED by Amazon, try again later" if blocked else " - saved"))

print("\nDone. Upload the files from the debug folder to GitHub (Add file -> Upload files).")
input("Press Enter to close...")
