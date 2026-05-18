import asyncio, sys
from pathlib import Path
from playwright.async_api import async_playwright

ROOT = Path(__file__).resolve().parent.parent

async def main():
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        ctx = await browser.new_context(viewport={"width": 1440, "height": 900})
        page = await ctx.new_page()
        await page.goto("http://localhost:8765", wait_until="networkidle")
        await page.wait_for_timeout(500)
        await page.screenshot(path=str(ROOT/"artifacts"/"theme-dark.png"), full_page=True)
        await page.click("#theme-btn")
        await page.wait_for_timeout(800)
        await page.screenshot(path=str(ROOT/"artifacts"/"theme-parchment.png"), full_page=True)
        # Also snap parchment + an agent tab so we can see the cards
        await page.evaluate("switchTab('FRANCE')")
        await page.wait_for_timeout(400)
        await page.screenshot(path=str(ROOT/"artifacts"/"theme-parchment-france.png"), full_page=True)
        await browser.close()

asyncio.run(main())
