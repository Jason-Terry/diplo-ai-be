"""Trigger a negotiation and screenshot during the live LLM activity to
capture the per-agent 'thinking' indicators."""

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
        await page.wait_for_timeout(500)

        # Kick off negotiations
        await page.click("#action-btn")
        # Snap a few times during the run
        for i in range(6):
            await page.wait_for_timeout(4000)
            path = ARTIFACTS / f"active-{i:02d}.png"
            await page.screenshot(path=str(path), full_page=True)
            print(path)
        await browser.close()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
