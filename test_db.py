import asyncio
import time
from database import get_all_trades

async def test():
    t = time.time()
    res = await get_all_trades(50)
    print("Trades:", len(res))
    print("Time taken:", time.time() - t)

asyncio.run(test())
