"""Snapshot ALL view + each per-power tab of the current game state."""

import asyncio
import sys
from pathlib import Path

from playwright.async_api import async_playwright

ROOT = Path(__file__).resolve().parent.parent
ARTIFACTS = ROOT / "artifacts"
TABS = ["ALL", "ENGLAND", "FRANCE", "GERMANY", "ITALY", "AUSTRIA", "RUSSIA", "TURKEY"]


async def main() -> int:
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        ctx = await browser.new_context(viewport={"width": 1440, "height": 900})
        page = await ctx.new_page()
        await page.goto("http://localhost:8765", wait_until="networkidle")
        await page.wait_for_timeout(500)

        for tab in TABS:
            await page.evaluate(f"switchTab('{tab}')")
            await page.wait_for_timeout(250)
            path = ARTIFACTS / f"tabs-{tab.lower()}.png"
            await page.screenshot(path=str(path), full_page=True)
            print(path)

        await browser.close()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
