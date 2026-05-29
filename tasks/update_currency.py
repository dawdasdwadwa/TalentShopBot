import asyncio
import aiohttp
import logging
import database as db

logger = logging.getLogger(__name__)

async def fetch_currency_rates():
    """Получает актуальные курсы валют"""
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                'https://api.exchangerate-api.com/v4/latest/RUB',
                timeout=aiohttp.ClientTimeout(total=10)
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    rates = data.get("rates", {})
                    result = {"UAH": rates.get("UAH", 0), "USD": rates.get("USD", 0), "EUR": rates.get("EUR", 0)}
                    await db.update_currency_rates(result)
                    return result
    except Exception as e:
        logger.error(f"Ошибка получения курса валют: {e}")
    return {}

async def auto_update_currency(bot):
    """Автоматическое обновление курсов валют каждые 6 часов"""
    await bot.wait_until_ready()
    while not bot.is_closed():
        try:
            await fetch_currency_rates()
            logger.info("✅ Курсы валют обновлены")
        except Exception as e:
            logger.error(f"Ошибка обновления курсов: {e}")
        await asyncio.sleep(21600)  # 6 часов
