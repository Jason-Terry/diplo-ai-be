import asyncio, sys
from playwright.async_api import async_playwright

async def main():
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        ctx = await browser.new_context(viewport={"width": 1440, "height": 900})
        page = await ctx.new_page()
        await page.goto("http://localhost:8765", wait_until="networkidle")
        # Force parchment
        await page.evaluate("applyTheme('parchment')")
        # Trigger a re-render
        await page.evaluate("const s=document.getElementById('map-svg'); if(s)s.remove(); renderMapSVG();")
        await page.wait_for_timeout(800)
        info = await page.evaluate("""() => {
            const seas = ['nth','bal','bot','eng','ion','adr','aeg','tys','bla'];
            const out = {};
            seas.forEach(id => {
                const n = document.querySelector(`[id="${id}"]`);
                out[id] = n ? {
                    tag: n.tagName,
                    style: n.getAttribute('style'),
                    fill: getComputedStyle(n).fill,
                    opacity: getComputedStyle(n).fillOpacity,
                } : 'NOT FOUND';
            });
            return out;
        }""")
        for k, v in info.items():
            print(f'{k}: {v}')
        await page.screenshot(path='artifacts/debug-sea.png', full_page=True)
        await browser.close()

asyncio.run(main())
