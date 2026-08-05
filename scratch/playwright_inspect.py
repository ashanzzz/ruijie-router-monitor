import asyncio
from playwright.async_api import async_playwright

async def run():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(ignore_https_errors=True)
        page = await context.new_page()

        async def handle_request(req):
            if 'api/cmd' in req.url:
                print('--- API CMD REQUEST ---')
                print('URL:', req.url)
                print('Headers:', req.headers)
                print('PostData:', req.post_data)

        async def handle_response(res):
            if 'api/cmd' in res.url:
                try:
                    text = await res.text()
                    print('--- API CMD RESPONSE ---')
                    print(text[:200])
                except Exception as e:
                    pass

        await context.add_init_script('''
            var originalSetRequestHeader = XMLHttpRequest.prototype.setRequestHeader;
            XMLHttpRequest.prototype.setRequestHeader = function(header, value) {
                if (header.toLowerCase() === 'content-accept') {
                    console.log("SETTING content-accept:", value);
                    console.log("Stack trace:", new Error().stack);
                }
                originalSetRequestHeader.apply(this, arguments);
            };
        ''')
        
        page.on('console', lambda msg: print(f"BROWSER CONSOLE: {msg.text}"))
        
        page.on('request', lambda r: asyncio.create_task(handle_request(r)))
        page.on('response', lambda r: asyncio.create_task(handle_response(r)))
        
        await page.goto('http://192.168.8.1/cgi-bin/luci/', wait_until='networkidle')
        await page.wait_for_selector('input[type="password"]')
        await page.fill('input[type="password"]', 'a123456789.')
        await page.keyboard.press('Enter')
        
        await page.wait_for_timeout(4000)
        
        response = await page.evaluate('''async () => {
            const v = document.querySelector('.app').__vue__;
            if (v && v.$api && v.$api.cmd) {
                return true;
            }
            return false;
        }''')
        
        print("Has $api.cmd:", response)
        
        # Test an API call using v.$api.cmd
        test_api_resp = await page.evaluate('''async () => {
            const v = document.querySelector('.app').__vue__;
            try {
                // v.$api.cmd(data, config) or similar
                // We'll just try to call it.
                // Usually it takes an object like { module: "user_list", ... }
                // Let's print out what v.$api.cmd is.
                return v.$api.cmd.toString();
            } catch(e) {
                return { error: e.toString() };
            }
        }''')
        
        print("Test API call:", test_api_resp)
        
        # Let's see if we can actually call it
        actual_res = await page.evaluate('''async () => {
            const v = document.querySelector('.app').__vue__;
            try {
                // v.$api.cmd(method, params, options)
                const res = await v.$api.cmd("devSta.get", {
                    module: 'user_list',
                    data: {devType: 'all', dataType: 'timely'}
                });
                return res;
            } catch(e) {
                return { error: e.toString() };
            }
        }''')
        
        import json
        print("Actual Res:")
        print(json.dumps(actual_res, indent=2, ensure_ascii=False))
        await browser.close()

asyncio.run(run())
