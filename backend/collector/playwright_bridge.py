import asyncio
from playwright.async_api import async_playwright
import json
import sys
import time
import time
import os
from datetime import datetime, timezone
import uuid

# Force stdout to flush
sys.stdout.reconfigure(line_buffering=True)

async def daemon(host, password, output_file):
    bridge_process_id = uuid.uuid4().hex
    sequence = 0

    print(f"Starting Playwright bridge daemon for {host}...", flush=True)
    while True:
        try:
            print("Launching Playwright...", flush=True)
            async with async_playwright() as p:
                browser_session_id = uuid.uuid4().hex
                browser = await p.chromium.launch(headless=True)
                context = await browser.new_context(ignore_https_errors=True)
                page = await context.new_page()
                
                data_cache = {"topology": None, "user_list": None}

                async def handle_response(response):
                    nonlocal sequence
                    if "api/cmd" in response.url and response.request.method == "POST":
                        try:
                            post_data = response.request.post_data
                            if post_data and "local_topology" in post_data:
                                text = await response.text()
                                d = json.loads(text)
                                if d.get("code") == 0:
                                    data_cache["topology"] = d.get("data")
                                    print("Captured local_topology", flush=True)
                            elif post_data and "user_list" in post_data:
                                text = await response.text()
                                d = json.loads(text)
                                if d.get("code") == 0:
                                    data_cache["user_list"] = d.get("data")
                                    sequence += 1
                                    
                                    snapshot = {
                                        "schema_version": 1,
                                        "bridge_process_id": bridge_process_id,
                                        "browser_session_id": browser_session_id,
                                        "sequence": sequence,
                                        "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                                        "topology": data_cache.get("topology"),
                                        "user_list": data_cache.get("user_list")
                                    }
                                    
                                    # Atomic write
                                    temp_file = f"{output_file}.tmp"
                                    with open(temp_file, "w", encoding="utf-8") as f:
                                        json.dump(snapshot, f)
                                        f.flush()
                                        os.fsync(f.fileno())
                                    os.replace(temp_file, output_file)
                                    print(f"Wrote ruijie_data.json (seq: {sequence})", flush=True)
                        except Exception as e:
                            pass

                page.on("response", handle_response)

                print("Logging in...", flush=True)
                await page.goto(f"{host}/cgi-bin/luci/", wait_until="networkidle", timeout=10000)
                await page.wait_for_selector('input[type="password"]', timeout=5000)
                await page.fill('input[type="password"]', password)
                await page.keyboard.press("Enter")
                
                print("Wait for initial login loads...", flush=True)
                await asyncio.sleep(5)
                
                # Fetch loop
                for _ in range(60): # Run for 10 minutes per browser session
                    try:
                        print("Triggering UI updates...", flush=True)
                        
                        # Click on '终端' (Clients) to trigger user_list
                        menus = await page.query_selector_all('text="终端"')
                        for m in menus:
                            try:
                                await m.click()
                                break
                            except:
                                pass
                        
                        await asyncio.sleep(4)
                        
                        # Click on '整网' or '首页' (Home) to trigger local_topology
                        menus = await page.query_selector_all('text="整网"')
                        if not menus:
                            menus = await page.query_selector_all('text="首页"')
                        for m in menus:
                            try:
                                await m.click()
                                break
                            except:
                                pass
                                
                        await asyncio.sleep(4)
                        
                        await asyncio.sleep(5)
                    except Exception as e:
                        print("Loop error:", e, flush=True)
                        break
                        
                await browser.close()
        except Exception as e:
            print("Daemon error:", e, flush=True)
            await asyncio.sleep(5)

if __name__ == "__main__":
    if len(sys.argv) > 2:
        host = sys.argv[1]
        output_file = sys.argv[2]
        password = os.environ.get("RUIJIE_PASS", "")
        asyncio.run(daemon(host, password, output_file))
