import asyncio
import httpx
import websockets
import json

async def run_sim():
    print("Initializing...")
    async with httpx.AsyncClient() as client:
        res = await client.post("http://localhost:8000/api/start", json={
            "agents_config": {
                "ENGLAND": {"provider": "anthropic/claude-3-5-sonnet-20240620", "personality": "Neutral"},
                "FRANCE": {"provider": "anthropic/claude-3-5-sonnet-20240620", "personality": "Aggressive"},
                "GERMANY": {"provider": "anthropic/claude-3-5-sonnet-20240620", "personality": "Neutral"},
                "ITALY": {"provider": "anthropic/claude-3-5-sonnet-20240620", "personality": "Neutral"},
                "AUSTRIA": {"provider": "anthropic/claude-3-5-sonnet-20240620", "personality": "Neutral"},
                "RUSSIA": {"provider": "anthropic/claude-3-5-sonnet-20240620", "personality": "Neutral"},
                "TURKEY": {"provider": "anthropic/claude-3-5-sonnet-20240620", "personality": "Neutral"}
            }
        }, timeout=30.0)
        print("Start:", res.json())

    print("Connecting to websocket...")
    async with websockets.connect("ws://localhost:8000/ws/game") as ws:
        async def listen():
            while True:
                msg = await ws.recv()
                data = json.loads(msg)
                print(f"[{data['power']}] {data['content']}", end="", flush=True)

        t = asyncio.create_task(listen())

        async with httpx.AsyncClient() as client:
            print("\n\n--- NEGOTIATION PHASE ---")
            res = await client.post("http://localhost:8000/api/phase/negotiate", timeout=120.0)
            print("\nNegotiate Result:", res.json())
            
            print("\n\n--- ORDERS PHASE ---")
            res = await client.post("http://localhost:8000/api/phase/orders", timeout=120.0)
            print("\nOrders Result:", res.json())
            
            print("\n\n--- ADJUDICATE PHASE ---")
            res = await client.post("http://localhost:8000/api/phase/adjudicate", timeout=30.0)
            print("\nAdjudicate Result:", res.json())

        t.cancel()

asyncio.run(run_sim())
