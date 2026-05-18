"""Drive the UI through a single phase advance: click action button twice
(orders → resolve). Used to validate the build phase and SC-change card."""

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

        # Take a snapshot of the starting state
        await page.screenshot(path=str(ARTIFACTS / "finish-01-before.png"), full_page=True)

        # Click the action button (Submit Builds / Submit Orders / Run Negotiations …)
        btn = page.locator("#action-btn")
        first_label = (await btn.text_content() or "").strip()
        print(f"→ first click: {first_label}")
        await btn.click()
        # Wait for it to flip to Resolve or back to Negotiate
        await page.wait_for_function(
            "/(Resolve|Negotiations|Submit)/i.test(document.getElementById('action-btn').textContent) && !document.getElementById('action-btn').disabled",
            timeout=180_000,
        )
        await page.screenshot(path=str(ARTIFACTS / "finish-02-mid.png"), full_page=True)

        second_label = (await btn.text_content() or "").strip()
        print(f"→ second click: {second_label}")
        await btn.click()
        await page.wait_for_function(
            "/(Resolve|Negotiations|Submit)/i.test(document.getElementById('action-btn').textContent) && !document.getElementById('action-btn').disabled",
            timeout=120_000,
        )
        await page.wait_for_timeout(1000)
        await page.screenshot(path=str(ARTIFACTS / "finish-03-resolved.png"), full_page=True)

        await browser.close()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
