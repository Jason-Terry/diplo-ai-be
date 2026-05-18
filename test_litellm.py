import asyncio
from litellm import acompletion
import os
from dotenv import load_dotenv

load_dotenv()

async def test():
    try:
        response = await acompletion(
            model="anthropic/claude-3-haiku-20240307",
            messages=[{"role": "user", "content": "hello"}],
            max_tokens=10
        )
        print("Success haiku:", response.choices[0].message.content)
    except Exception as e:
        print("Error haiku:", e)

    try:
        response = await acompletion(
            model="anthropic/claude-3-5-sonnet-20240620",
            messages=[{"role": "user", "content": "hello"}],
            max_tokens=10
        )
        print("Success sonnet 20240620:", response.choices[0].message.content)
    except Exception as e:
        print("Error sonnet 20240620:", e)

asyncio.run(test())
