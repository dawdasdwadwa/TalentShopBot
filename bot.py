import discord
from discord.ext import commands
import os
import sys
import asyncio
import logging
import io

ROOT_DIR = os.path.dirname(os.path.abspath(__file__))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

# Настройка UTF-8 для консоли
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8')

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

import database as db
from config.constants import BACKUP_CHANNEL_ID
from tasks.startup import _startup_background, _safe_task
from tasks.cleanup_tickets import auto_cleanup_tickets
from tasks.update_currency import auto_update_currency
from tasks.cleanup_spam import cleanup_spam_cache
from ai.views import StartAIButton

# Интенты
intents = discord.Intents.default()
intents.members = True
intents.message_content = True
bot = commands.Bot(command_prefix='!', intents=intents)

_startup_done = False

# ================= ЗАГРУЗКА КОГОВ =================
async def load_cogs():
    await bot.load_extension('cogs.admin')
    await bot.load_extension('cogs.shop')
    await bot.load_extension('cogs.tickets')
    await bot.load_extension('cogs.reviews')
    await bot.load_extension('cogs.profile')
    await bot.load_extension('cogs.moderation')
    await bot.load_extension('cogs.backup')
    logger.info("✅ Все коги загружены")

# ================= СОБЫТИЯ =================
@bot.event
async def on_ready():
    global _startup_done
    if _startup_done:
        logger.info(f"♻️ Reconnect: {bot.user}")
        return
    _startup_done = True

    logger.info(f"🔥 Бот запущен: {bot.user}")

    # Инициализация БД
    await db.init_db()
    await db.refresh_cache()
    logger.info("✅ База данных готова")

    # Загрузка когов
    await load_cogs()

    # Регистрация Persistent Views
    bot.add_view(StartAIButton())
    logger.info("✅ Persistent View зарегистрированы")

    # Синхронизация команд
    await bot.tree.sync()
    logger.info("✅ Слеш-команды синхронизированы")

    # Фоновые задачи
    asyncio.create_task(_safe_task(auto_cleanup_tickets(bot), "auto_cleanup_tickets"))
    asyncio.create_task(_safe_task(auto_update_currency(bot), "auto_update_currency"))
    asyncio.create_task(_safe_task(cleanup_spam_cache(bot), "cleanup_spam_cache"))

    # Запуск панелей
    asyncio.create_task(_safe_task(_startup_background(bot), "_startup_background"))

    logger.info(f"✅ Бот готов: {bot.user}")

@bot.event
async def on_member_join(member: discord.Member):
    from utils.permissions import get_config
    config = get_config(member.guild.id)
    if not config:
        return
    unverified_role_id = config["roles"].get("unverified")
    if unverified_role_id:
        unverified_role = member.guild.get_role(unverified_role_id)
        if unverified_role:
            await member.add_roles(unverified_role)
    welcome_channel_id = config.get("welcome_channel")
    if welcome_channel_id:
        welcome_channel = member.guild.get_channel(welcome_channel_id)
        if welcome_channel:
            embed = discord.Embed(
                title=f"👋 Добро пожаловать, {member.name}!",
                description=f"📌 Пройдите верификацию в <#{config['verify_channel']}>",
                color=discord.Color.green()
            )
            await welcome_channel.send(content=member.mention, embed=embed)

@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        await bot.process_commands(message)
        return
    
    from utils.permissions import is_admin_member
    if is_admin_member(message.author):
        await bot.process_commands(message)
        return
    
    # Проверка на спам-ссылки
    from config.constants import BANNED_PATTERNS
    import re
    for pattern in BANNED_PATTERNS:
        if re.search(pattern, message.content.lower()):
            try:
                await message.delete()
            except:
                pass
            await bot.process_commands(message)
            return
    
    await bot.process_commands(message)

# ================= ЗАПУСК =================
if __name__ == "__main__":
    token = os.getenv('DISCORD_TOKEN')
    if not token:
        raise RuntimeError("❌ DISCORD_TOKEN не задан!")
    bot.run(token)
