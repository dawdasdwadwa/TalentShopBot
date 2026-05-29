import asyncio
import logging
from datetime import datetime, timezone, timedelta

import database as db

logger = logging.getLogger(__name__)

async def auto_cleanup_tickets(bot):
    """Автоматическое удаление старых тикетов"""
    await bot.wait_until_ready()
    while not bot.is_closed():
        await asyncio.sleep(3600)  # Каждый час
        try:
            seven_days_ago = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
            rows = await db.fetchall('SELECT * FROM tickets WHERE status = "closed" AND closed_at <= ?', (seven_days_ago,))
            for ticket in rows:
                guild = bot.get_guild(ticket['guild_id'])
                if not guild:
                    continue
                channel = guild.get_channel(ticket['channel_id'])
                if channel:
                    try:
                        await channel.delete(reason="Автоудаление тикета (7 дней)")
                    except Exception as e:
                        logger.error(f"Не удалось удалить канал {ticket['channel_id']}: {e}")
                if ticket.get('voice_channel_id'):
                    vc = guild.get_channel(ticket['voice_channel_id'])
                    if vc:
                        try:
                            await vc.delete()
                        except Exception:
                            pass
                await db.delete_ticket_record(ticket['channel_id'])
                logger.info(f"🗑️ Удалён тикет {ticket['channel_id']}")
        except Exception as e:
            logger.error(f"Ошибка очистки тикетов: {e}")
