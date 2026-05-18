"""Snapshot the unit-history modal by clicking a random unit."""

import asyncio
import sys
from pathlib import Path

from playwright.async_api import async_playwright

ROOT = Path(__file__).resolve().parent.parent
ARTIFACTS = ROOT / "artifacts"


async def main() -> int:
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        ctx = await browser.new_context(viewport={"width": 1440, "height": 900})
        page = await ctx.new_page()
        await page.goto("http://localhost:8765", wait_until="networkidle")
        await page.wait_for_timeout(600)

        # Click the first unit marker
        marker = await page.query_selector(".unit-marker[data-unit-id]")
        if not marker:
            print("no unit markers found")
            await browser.close()
            return 1
        await marker.click()
        await page.wait_for_timeout(400)
        await page.screenshot(path=str(ARTIFACTS / "unit-history-modal.png"), full_page=True)
        print(ARTIFACTS / "unit-history-modal.png")
        await browser.close()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
