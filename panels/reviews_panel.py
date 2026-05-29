import discord
from discord.ui import Modal, TextInput
from datetime import datetime, timezone

from utils.permissions import get_config
import database as db

class ReviewModal(discord.ui.Modal, title="Оставить отзыв"):
    rating = discord.ui.TextInput(
        label="Оценка (1-5)",
        placeholder="1-5",
        min_length=1,
        max_length=1,
        required=True
    )
    comment = discord.ui.TextInput(
        label="Комментарий",
        placeholder="Ваш отзыв о товаре/продавце",
        style=discord.TextStyle.paragraph,
        max_length=4000,
        required=True
    )

    def __init__(self, seller, product: str, lot_id: int):
        super().__init__()
        self.seller = seller
        self.product = product
        self.lot_id = lot_id

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        if self.rating.value not in '12345':
            await interaction.followup.send("❌ Оценка должна быть от 1 до 5", ephemeral=True)
            return
        rating = int(self.rating.value)
        stars = "⭐" * rating + "☆" * (5 - rating)

        config = get_config(interaction.guild_id)
        review_channel_id = config.get("review_channel") if config else None
        review_channel = interaction.guild.get_channel(review_channel_id) if review_channel_id else None
        if not review_channel:
            await interaction.followup.send("❌ Канал отзывов не найден.", ephemeral=True)
            return

        if self.seller:
            await db.add_review(interaction.user.id, self.seller.id, self.lot_id, rating, self.comment.value)

        embed = discord.Embed(
            title="📝 Отзыв о покупке",
            description=(
                f"**Товар:** {self.product}\n"
                f"**Оценка:** {stars} ({rating}/5)\n\n"
                f"**Отзыв:**\n{self.comment.value}"
            ),
            color=discord.Color.gold()
        )
        embed.set_author(
            name=interaction.user.display_name,
            icon_url=interaction.user.avatar.url if interaction.user.avatar else None
        )
        seller_name = self.seller.name if self.seller else "Неизвестен"
        embed.set_footer(text=f"Покупатель: {interaction.user.name} | Продавец: {seller_name}")
        embed.timestamp = datetime.now(timezone.utc)

        if self.seller:
            await update_seller_review_catalog(interaction.guild, review_channel, self.seller)
        else:
            await review_channel.send(embed=embed)

        await interaction.followup.send("✅ Спасибо за отзыв!", ephemeral=True)

        if self.seller:
            try:
                await self.seller.send(
                    f"📢 {interaction.user.mention} оставил отзыв о товаре **{self.product}**!\nОценка: {stars}"
                )
            except Exception:
                pass

async def update_seller_review_catalog(guild: discord.Guild, review_channel: discord.TextChannel, seller: discord.Member):
    """Обновляет или создаёт сообщение с отзывами продавца в канале отзывов"""
    reviews = await db.get_seller_reviews(seller.id, 20)
    avg_rating = await db.get_seller_rating(seller.id)

    embed = discord.Embed(
        title=f"⭐ Отзывы о {seller.display_name}",
        description=(
            f"**Средний рейтинг:** {'⭐' * round(avg_rating)}{'☆' * (5 - round(avg_rating))} ({avg_rating}/5)\n"
            f"**Всего отзывов:** {len(reviews)}"
        ),
        color=discord.Color.gold()
    )
    embed.set_thumbnail(url=seller.avatar.url if seller.avatar else None)

    for rev in reviews[:10]:
        buyer = guild.get_member(rev['user_id'])
        buyer_name = buyer.display_name if buyer else f"ID:{rev['user_id']}"
        stars = "⭐" * rev['rating'] + "☆" * (5 - rev['rating'])
        embed.add_field(
            name=f"{stars} от {buyer_name}",
            value=rev['comment'][:200],
            inline=False
        )

    existing = await db.get_seller_review_message(seller.id)
    if existing:
        try:
            msg = await review_channel.fetch_message(existing['message_id'])
            await msg.edit(embed=embed)
            return
        except (discord.NotFound, discord.HTTPException):
            pass

    msg = await review_channel.send(content=f"**📋 Продавец: {seller.mention}**", embed=embed)
    await db.set_seller_review_message(seller.id, msg.id, review_channel.id)
