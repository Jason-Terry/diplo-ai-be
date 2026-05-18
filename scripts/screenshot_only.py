"""Quick single-page screenshot."""
import asyncio
import sys
from pathlib import Path

from playwright.async_api import async_playwright

ROOT = Path(__file__).resolve().parent.parent
ARTIFACTS = ROOT / "artifacts"


async def main(name: str = "current") -> int:
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        ctx = await browser.new_context(viewport={"width": 1440, "height": 900})
        page = await ctx.new_page()
        await page.goto("http://localhost:8765", wait_until="networkidle")
        await page.wait_for_timeout(800)
        path = ARTIFACTS / f"{name}.png"
        await page.screenshot(path=str(path), full_page=True)
        print(path)
        await browser.close()
    return 0


if __name__ == "__main__":
    name = sys.argv[1] if len(sys.argv) > 1 else "current"
    sys.exit(asyncio.run(main(name)))
