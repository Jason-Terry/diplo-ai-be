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
        await page.wait_for_timeout(400)
        await page.click("#layout-btn")
        await page.wait_for_timeout(400)
        await page.screenshot(path=str(ROOT / "artifacts" / "layout-dialog.png"), full_page=True)
        # Switch to FRANCE tab to show the panel filled
        await page.evaluate("switchTab('FRANCE')")
        await page.wait_for_timeout(400)
        await page.screenshot(path=str(ROOT / "artifacts" / "layout-dialog-france.png"), full_page=True)
        await browser.close()

asyncio.run(main())
