import discord
from discord.ext import commands
from discord import app_commands
from typing import Optional

from ..utils.permissions import is_owner
from ..panels import ReviewModal
from .. import database as db
from ..database import get_seller_rating, get_seller_reviews

class ReviewsCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(name='review', description='Оставить отзыв о покупке')
    @app_commands.describe(seller="Продавец", product="Название товара", lot_id="ID товара")
    async def review(self, interaction: discord.Interaction, seller: discord.Member, product: str, lot_id: int):
        await interaction.response.send_modal(ReviewModal(seller, product, lot_id))

    @app_commands.command(name='reviews', description='Посмотреть отзывы о продавце')
    @app_commands.describe(seller="Продавец")
    async def reviews(self, interaction: discord.Interaction, seller: discord.Member):
        rating = await get_seller_rating(seller.id)
        reviews_list = await get_seller_reviews(seller.id, 10)
        embed = discord.Embed(
            title=f"⭐ Отзывы о {seller.display_name}",
            description=f"**Средний рейтинг:** {rating}/5.0",
            color=discord.Color.gold()
        )
        if reviews_list:
            text = ""
            for r in reviews_list[:10]:
                stars = "⭐" * r['rating'] + "☆" * (5 - r['rating'])
                text += f"{stars} — {r['comment'][:80]}\n"
            embed.add_field(name="Отзывы", value=text, inline=False)
        else:
            embed.add_field(name="Отзывы", value="Нет отзывов", inline=False)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name='seller_rating', description='Посмотреть рейтинг продавца')
    @app_commands.describe(seller="Продавец")
    async def seller_rating(self, interaction: discord.Interaction, seller: discord.Member):
        rating = await get_seller_rating(seller.id)
        reviews = await get_seller_reviews(seller.id, 5)
        embed = discord.Embed(
            title=f"⭐ Рейтинг {seller.display_name}",
            description=f"**Средняя оценка:** {rating}/5.0",
            color=discord.Color.gold()
        )
        if reviews:
            text = ""
            for r in reviews[:5]:
                stars = "⭐" * r['rating'] + "☆" * (5 - r['rating'])
                text += f"{stars} — {r['comment'][:50]}\n"
            embed.add_field(name="Последние отзывы", value=text, inline=False)
        await interaction.response.send_message(embed=embed, ephemeral=True)

async def setup(bot):
    await bot.add_cog(ReviewsCog(bot))