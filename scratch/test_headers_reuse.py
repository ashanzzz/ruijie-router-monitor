import asyncio
from playwright.async_api import async_playwright

async def run():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(ignore_https_errors=True)
        page = await context.new_page()

        captured_headers = None
        captured_url = None

        async def handle_request(req):
            nonlocal captured_headers, captured_url
            if 'api/cmd' in req.url and captured_headers is None:
                captured_headers = req.headers
                captured_url = req.url

        page.on('request', lambda r: asyncio.create_task(handle_request(r)))
        
        await page.goto('http://192.168.8.1/cgi-bin/luci/', wait_until='networkidle')
        await page.fill('input[type="password"]', 'a123456789.')
        await page.keyboard.press('Enter')
        
        # Wait for headers to be captured
        for _ in range(10):
            if captured_headers:
                break
            await asyncio.sleep(1)
            
        if not captured_headers:
            print("Failed to capture headers")
            return
            
        print("Captured headers:", captured_headers)
        
        r1 = await context.request.post(
            captured_url, 
            headers=captured_headers, 
            data={"method": "devSta.get", "params": {"module": "local_topology"}}
        )
        d1 = await r1.text()
        print("local_topology response:", d1[:300])

        r2 = await context.request.post(
            captured_url, 
            headers=captured_headers, 
            data={"method": "devSta.get", "params": {"module": "user_list"}}
        )
        d2 = await r2.text()
        print("user_list response:", str(d2)[:300])
        
        await browser.close()

asyncio.run(run())
