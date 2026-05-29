import discord
from datetime import datetime

from utils.channel import fetch_channel_safe
from config.constants import OWNER_ID
import database as db

async def build_status_embed(guild: discord.Guild, user: discord.Member) -> discord.Embed:
    purchases = await db.get_user_purchases(user.id)
    user_reviews = await db.get_user_reviews(user.id)
    stats = await db.get_stats(user.id)
    ref_count = await db.get_referral_count(user.id)

    embed = discord.Embed(title=f"👤 Профиль участника: {user.display_name}", color=discord.Color.blue())
    embed.set_thumbnail(url=user.avatar.url if user.avatar else None)
    embed.add_field(
        name="🛒 Статистика покупок",
        value=f"Всего покупок: **{len(purchases)}**\nПродаж: **{stats['sales'] if stats else 0}**\nВыручка: **{stats['revenue'] if stats else 0} ₽**",
        inline=False
    )
    ref_link = f"https://discord.gg/ref_{user.id}"
    embed.add_field(name="🔗 Реферальная ссылка", value=f"`{ref_link}`\nПривёл: **{ref_count}** пользователей", inline=False)

    if purchases:
        purchase_text = ""
        for p in purchases[:5]:
            lot_item = db.lots_cache.get(p['lot_id'])
            lot_name = lot_item.name if lot_item else f"Товар #{p['lot_id']}"
            purchase_text += f"• {lot_name} — {p['price']} ({p['created_at'][:10]})\n"
        embed.add_field(name="📦 Последние покупки", value=purchase_text, inline=False)

    if user_reviews:
        reviews_text = "".join([f"{'⭐' * r['rating']} — {r['comment'][:60]}...\n" for r in user_reviews[:3]])
        embed.add_field(name="📝 Последние отзывы", value=reviews_text, inline=False)

    return embed

async def send_status_channel_panel(guild: discord.Guild, guild_config: dict, bot):
    channel_id = guild_config.get("status_channel")
    if not channel_id:
        return
    channel = await fetch_channel_safe(bot, channel_id)
    if not channel:
        return
    try:
        async for msg in channel.history(limit=30):
            if msg.author == bot.user:
                try:
                    await msg.delete()
                except Exception:
                    pass
    except Exception:
        pass
    owner_member = guild.get_member(OWNER_ID) or guild.owner
    if not owner_member:
        return
    embed = await build_status_embed(guild, owner_member)
    embed.title = "📊 Текущий статус системы"
    embed.add_field(
        name="🌐 Статус хостинга",
        value="🟢 Система запущена на Railway\n📅 " + datetime.now().strftime("%d.%m.%Y %H:%M:%S"),
        inline=False
    )
    try:
        await channel.send(embed=embed)
    except Exception as e:
        print(f"Ошибка отправки статуса: {e}")
