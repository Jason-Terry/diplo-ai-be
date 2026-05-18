"""Headless browser harness for autonomous UX checks.

Usage:
    uv run python scripts/browser_check.py            # baseline screenshots
    uv run python scripts/browser_check.py --flow     # walk through a full game flow

Assumes the backend is running on http://localhost:8000.
"""

import argparse
import asyncio
import sys
from pathlib import Path

from playwright.async_api import Page, async_playwright

ROOT = Path(__file__).resolve().parent.parent
ARTIFACTS = ROOT / "artifacts"
ARTIFACTS.mkdir(exist_ok=True)
BASE_URL = "http://localhost:8765"


async def shoot(page: Page, name: str) -> None:
    path = ARTIFACTS / f"{name}.png"
    await page.screenshot(path=str(path), full_page=True)
    print(f"  → {path.relative_to(ROOT)}")


async def collect_console(page: Page) -> list[str]:
    logs: list[str] = []
    page.on("console", lambda msg: logs.append(f"[{msg.type}] {msg.text}"))
    page.on("pageerror", lambda err: logs.append(f"[pageerror] {err}"))
    return logs


async def baseline() -> int:
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        ctx = await browser.new_context(viewport={"width": 1440, "height": 900})
        page = await ctx.new_page()
        logs = await collect_console(page)

        print("→ loading /")
        await page.goto(BASE_URL, wait_until="networkidle")
        await shoot(page, "01-initial")

        print("→ open setup modal")
        await page.click("#action-btn")
        await page.wait_for_selector("#setup-modal:not(.hidden)", timeout=3000)
        await shoot(page, "02-setup-modal")

        await browser.close()

        if logs:
            print("\nConsole log:")
            for line in logs:
                print(" ", line)
        return 0


async def full_flow() -> int:
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        ctx = await browser.new_context(viewport={"width": 1440, "height": 900})
        page = await ctx.new_page()
        logs = await collect_console(page)

        print("→ loading /")
        await page.goto(BASE_URL, wait_until="networkidle")
        await shoot(page, "flow-01-initial")

        print("→ initialize game")
        await page.click("#action-btn")
        await page.wait_for_selector("#setup-modal:not(.hidden)", timeout=3000)
        # Force every power to a fast/cheap Anthropic model
        for sel in await page.query_selector_all("select[id^='provider-']"):
            await sel.select_option("anthropic/claude-haiku-4-5-20251001")
        # Vary the policy across powers so eval logs see a mix
        policy_cycle = ["HONEST_ALLIER", "CALCULATED_BETRAYER", "WILDCARD",
                        "DEFENSIVE_REALIST", "SOLO_OPPORTUNIST", "PARANOID_HEDGER",
                        "WILDCARD"]
        for i, sel in enumerate(await page.query_selector_all("select[id^='policy-']")):
            await sel.select_option(policy_cycle[i % len(policy_cycle)])
        await page.click(".modal-content .primary-btn")
        await page.wait_for_function(
            "document.getElementById('setup-modal').classList.contains('hidden')",
            timeout=10000,
        )
        await page.wait_for_function(
            "document.getElementById('action-btn').textContent.includes('Negotiations')",
            timeout=15000,
        )
        await shoot(page, "flow-02-after-init")

        print("→ run negotiations (this calls the LLM — may take a minute)")
        await page.click("#action-btn")
        await page.wait_for_function(
            "document.getElementById('action-btn').textContent.includes('Orders')",
            timeout=180_000,
        )
        await shoot(page, "flow-03-after-negotiate")

        print("→ run orders")
        await page.click("#action-btn")
        await page.wait_for_function(
            "document.getElementById('action-btn').textContent.includes('Resolve')",
            timeout=240_000,
        )
        await shoot(page, "flow-04-after-orders")

        print("→ resolve turn")
        await page.click("#action-btn")
        await page.wait_for_function(
            "document.getElementById('action-btn').textContent.match(/(Negotiations|Submit)/)",
            timeout=60_000,
        )
        await shoot(page, "flow-05-after-adjudicate")
        # Snapshot of header to confirm phase advanced
        await page.wait_for_timeout(500)
        await shoot(page, "flow-06-fall-spring-1901")

        await browser.close()

        if logs:
            print("\nConsole log:")
            for line in logs:
                print(" ", line)
        return 0


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--flow", action="store_true", help="walk through a full game phase cycle")
    args = p.parse_args()
    if args.flow:
        return asyncio.run(full_flow())
    return asyncio.run(baseline())


if __name__ == "__main__":
    sys.exit(main())
