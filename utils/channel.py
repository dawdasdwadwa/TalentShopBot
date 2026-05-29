import asyncio

async def fetch_channel_safe(bot, channel_id: int, retries: int = 5):
    """Безопасное получение канала с повторными попытками"""
    for attempt in range(retries):
        ch = bot.get_channel(channel_id)
        if ch:
            return ch
        try:
            ch = await bot.fetch_channel(channel_id)
            if ch:
                return ch
        except Exception:
            pass
        await asyncio.sleep(2)
    return None