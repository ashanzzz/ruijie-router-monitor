import asyncio
from playwright.async_api import async_playwright
import json
import sys
import time
import os
import os

# Force stdout to flush
sys.stdout.reconfigure(line_buffering=True)

import os

# Force stdout to flush
sys.stdout.reconfigure(line_buffering=True)

async def daemon(host, password, output_file):
    print(f"Starting Playwright bridge daemon for {host}...", flush=True)
    while True:
        try:
            print("Launching Playwright...", flush=True)
            async with async_playwright() as p:
                browser = await p.chromium.launch(headless=True)
                context = await browser.new_context(ignore_https_errors=True)
                page = await context.new_page()
                
                data_cache = {"topology": None, "user_list": None}

                async def handle_response(response):
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
                                    print("Captured user_list", flush=True)
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
                        
                        if data_cache.get("topology") or data_cache.get("user_list"):
                            with open(output_file, "w", encoding="utf-8") as f:
                                json.dump(data_cache, f)
                            print("Wrote ruijie_data.json", flush=True)
                                
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
