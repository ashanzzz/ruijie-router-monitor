import asyncio
from playwright.async_api import async_playwright
import json
import sys

async def get_ruijie_auth(host, password):
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(ignore_https_errors=True)
        page = await context.new_page()

        auth_data = {}

        async def handle_response(response):
            if "api/auth" in response.url and response.request.method == "POST":
                try:
                    text = await response.text()
                    data = json.loads(text)
                    if data.get("code") == 0 and data.get("data"):
                        auth_data['token'] = data['data']['token']
                        auth_data['sid'] = data['data']['sid']
                        auth_data['sn'] = data['data']['sn']
                except:
                    pass

        page.on("response", handle_response)
        
        try:
            await page.goto(f"{host}/cgi-bin/luci/", wait_until="networkidle", timeout=10000)
            await page.wait_for_selector('input[type="password"]', timeout=5000)
            await page.fill('input[type="password"]', password)
            await page.keyboard.press("Enter")
            
            # Wait for auth response
            for _ in range(20):
                if 'token' in auth_data:
                    break
                await asyncio.sleep(0.5)
                
        except Exception as e:
            pass
        finally:
            await browser.close()
            
        if 'token' in auth_data:
            print(json.dumps(auth_data))
        else:
            print(json.dumps({"error": "Failed"}))

if __name__ == "__main__":
    if len(sys.argv) > 2:
        asyncio.run(get_ruijie_auth(sys.argv[1], sys.argv[2]))
