#Thanks @dreamxbotz for helping in this journey 
import asyncio
import logging
import aiohttp
import traceback
from info import PING_INTERVAL, URL

logger = logging.getLogger(__name__)

async def ping_server():
    sleep_time = PING_INTERVAL
    while True:
        await asyncio.sleep(sleep_time)
        try:
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=10)
            ) as session:
                async with session.get(URL) as resp:
                    logger.info("Pinged server with response: {}".format(resp.status))
        except TimeoutError:
            logger.warning("Couldn't connect to the site URL..!")
        except Exception:
            traceback.print_exc()
