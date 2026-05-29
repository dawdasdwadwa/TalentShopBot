import asyncio
from datetime import datetime, timedelta

# Глобальные переменные для анти-спама
user_mention_count = {}
user_mention_last_reset = {}

async def cleanup_spam_cache(bot):
    """Очистка кэша анти-спама (раз в сутки)"""
    await bot.wait_until_ready()
    while not bot.is_closed():
        await asyncio.sleep(86400)  # Раз в сутки
        cutoff = datetime.now() - timedelta(hours=2)
        stale = [uid for uid, ts in user_mention_last_reset.items() if ts < cutoff]
        for uid in stale:
            user_mention_count.pop(uid, None)
            user_mention_last_reset.pop(uid, None)
        if stale:
            print(f"🧹 Очищено {len(stale)} записей анти-спам кэша")