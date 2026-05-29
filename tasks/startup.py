import asyncio
import logging
import discord

from config.settings import CONFIG
from ..utils.channel import fetch_channel_safe
from ..panels.verify_panel import send_verify_panel
from ..panels.ticket_panel import send_ticket_panel
from ..panels.shop_panel import send_or_update_shop
from ..panels.status_panel import send_status_channel_panel
from ..utils.permissions import get_config, is_admin_member
from ..config.constants import BACKUP_CHANNEL_ID, AI_CONVEYOR_CHANNEL_ID
from .. import database as db

logger = logging.getLogger(__name__)

async def assign_unverified_roles(bot):
    """Выдаёт роль Unverified новым участникам"""
    for guild in bot.guilds:
        config = get_config(guild.id)
        if not config:
            continue
        unverified_role_id = config["roles"].get("unverified")
        if not unverified_role_id:
            continue
        unverified_role = guild.get_role(unverified_role_id)
        if not unverified_role:
            continue
        customer_role = guild.get_role(config["roles"].get("customer")) if config.get("roles", {}).get("customer") else None
        buyer_role = guild.get_role(config["roles"].get("buyer")) if config.get("roles", {}).get("buyer") else None
        for member in guild.members:
            if member.bot or is_admin_member(member):
                continue
            has_role = (customer_role and customer_role in member.roles) or (buyer_role and buyer_role in member.roles)
            if not has_role and unverified_role not in member.roles:
                try:
                    await member.add_roles(unverified_role)
                except Exception:
                    pass

async def setup_panels(bot):
    """Отправляет все панели (верификация, тикеты, магазин, статус)"""
    logger.info("⏳ setup_panels(): НАЧАЛО")
    try:
        await db.refresh_cache()
        logger.info(f"✅ setup_panels(): кэш обновлён (категорий: {len(db.categories_cache)})")
    except Exception as e:
        logger.error(f"❌ setup_panels(): ошибка обновления кэша: {e}")
        return
    for guild_id, g_config in CONFIG.items():
        guild = bot.get_guild(guild_id)
        if not guild:
            logger.warning(f"⚠️ Гильдия {guild_id} не найдена")
            continue
        logger.info(f"⏳ Настройка панелей для {g_config['name']}...")
        try:
            await send_verify_panel(g_config, bot)
            logger.info(f"  ✅ Верификация — канал {g_config.get('verify_channel')}")
        except Exception as e:
            logger.error(f"  ❌ Ошибка верификации: {e}")
        try:
            await send_ticket_panel(g_config, bot)
            logger.info(f"  ✅ Тикеты — канал {g_config.get('ticket_channel')}")
        except Exception as e:
            logger.error(f"  ❌ Ошибка тикетов: {e}")
        if g_config.get("shop_channel"):
            try:
                await send_or_update_shop(guild, bot)
                logger.info(f"  ✅ Магазин — канал {g_config.get('shop_channel')}")
            except Exception as e:
                logger.error(f"  ❌ Ошибка магазина: {e}")
        if g_config.get("status_channel"):
            try:
                await send_status_channel_panel(guild, g_config, bot)
                logger.info(f"  ✅ Статус — канал {g_config.get('status_channel')}")
            except Exception as e:
                logger.error(f"  ❌ Ошибка статуса: {e}")
    try:
        await assign_unverified_roles(bot)
        logger.info("✅ Роли unverified назначены")
    except Exception as e:
        logger.error(f"❌ Ошибка назначения ролей: {e}")
    logger.info("✅ setup_panels(): ЗАВЕРШЕНО")

async def setup_ai_panel(bot):
    """Отправляет панель ИИ-конвейера"""
    from ..ai.views import StartAIButton
    channel = await fetch_channel_safe(bot, AI_CONVEYOR_CHANNEL_ID)
    if not channel:
        logger.warning(f"⚠️ Канал ИИ-конвейера {AI_CONVEYOR_CHANNEL_ID} не найден")
        return
    try:
        async for msg in channel.history(limit=30):
            if msg.author == bot.user:
                try:
                    await msg.delete()
                    await asyncio.sleep(0.5)
                except Exception:
                    pass
    except Exception as e:
        logger.warning(f"Не удалось очистить канал ИИ-конвейера: {e}")
    embed = discord.Embed(
        title="🤖 ИИ-Конвейер",
        description=(
            "**Нажми на кнопку ниже, выбери режим и задай вопрос.**\n\n"
            "📋 **Режимы:** Кодинг, Советы, Оформление, Аналитика, Контент, Маркетинг, Бот-фичи\n"
            "🧠 **Фишки:** История диалога, учёт структуры сервера, 4 API ключа в ротации\n"
            "🏠 **Новое:** Можешь включить учёт структуры этого сервера — ИИ учтёт твои роли и каналы!"
        ),
        color=discord.Color.blurple()
    )
    view = discord.ui.View()
    view.add_item(StartAIButton())
    try:
        await channel.send(embed=embed, view=view)
        logger.info("✅ Панель ИИ-конвейера отправлена")
    except Exception as e:
        logger.error(f"❌ Ошибка отправки панели ИИ-конвейера: {e}")

async def _startup_background(bot):
    """Фоновая задача запуска всех панелей"""
    logger.info("🔥 _startup_background() начал работу...")
    try:
        await db.restore_from_backup_channel(BACKUP_CHANNEL_ID, bot)
        logger.info("✅ Восстановление из бэкапа завершено")
    except Exception as e:
        logger.error(f"❌ Ошибка восстановления бэкапа: {e}")
    try:
        await setup_panels(bot)
        logger.info("✅ Панели серверов настроены")
    except Exception as e:
        logger.error(f"❌ Ошибка настройки панелей: {e}")
    try:
        await setup_ai_panel(bot)
        logger.info("✅ Панель ИИ-конвейера настроена")
    except Exception as e:
        logger.error(f"❌ Ошибка настройки ИИ-панели: {e}")
    logger.info("✅ _startup_background() завершён!")

async def _safe_task(coro, name: str):
    """Безопасный запуск задачи с логированием ошибок"""
    try:
        await coro
    except Exception:
        logger.exception(f"❌ Необработанное исключение в задаче '{name}'")
