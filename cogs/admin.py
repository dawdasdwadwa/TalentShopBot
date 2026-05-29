import discord
from discord.ext import commands
from discord import app_commands
from typing import Optional

from ..utils.permissions import is_owner, get_config
from ..panels.verify_panel import send_verify_panel
from ..panels.shop_panel import send_or_update_shop
from .. import database as db

class AdminCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    async def owner_only(self, interaction: discord.Interaction) -> bool:
        if not is_owner(interaction):
            await interaction.response.send_message("❌ Только для Owner", ephemeral=True)
            return False
        return True

    @app_commands.command(name='setup_verify', description='[OWNER] Пересоздать панель верификации')
    async def setup_verify(self, interaction: discord.Interaction):
        if not await self.owner_only(interaction):
            return
        await interaction.response.defer(ephemeral=True)
        config = get_config(interaction.guild_id)
        if not config:
            await interaction.followup.send("❌ Сервер не настроен", ephemeral=True)
            return
        await send_verify_panel(config, self.bot)
        await interaction.followup.send("✅ Панель верификации обновлена!", ephemeral=True)

    @app_commands.command(name='add_category', description='[OWNER] Добавить категорию')
    @app_commands.describe(name="Название", emoji="Эмодзи")
    async def add_category(self, interaction: discord.Interaction, name: str, emoji: str = "📁"):
        if not await self.owner_only(interaction):
            return
        await interaction.response.defer(ephemeral=True)
        cat_id = await db.add_category(name=name, emoji=emoji)
        await db.refresh_cache()
        await interaction.followup.send(f"✅ Категория `{emoji} {name}` добавлена (ID: {cat_id})", ephemeral=True)
        await send_or_update_shop(interaction.guild, self.bot)

    @app_commands.command(name='remove_category', description='[OWNER] Удалить категорию')
    @app_commands.describe(category_id="ID категории")
    async def remove_category(self, interaction: discord.Interaction, category_id: int):
        if not await self.owner_only(interaction):
            return
        category = await db.get_category(category_id)
        if not category:
            await interaction.response.send_message("❌ Категория не найдена", ephemeral=True)
            return
        await db.delete_category(category_id)
        await db.refresh_cache()
        await interaction.response.send_message(f"✅ Категория `{category.name}` удалена", ephemeral=True)
        await send_or_update_shop(interaction.guild, self.bot)

    @app_commands.command(name='list_categories', description='[OWNER] Показать категории')
    async def list_categories(self, interaction: discord.Interaction):
        if not await self.owner_only(interaction):
            return
        await db.refresh_cache()
        categories = db.categories_cache
        if not categories:
            await interaction.response.send_message("📭 Нет категорий", ephemeral=True)
            return
        embed = discord.Embed(title="📁 Список категорий", color=discord.Color.blue())
        for cat in categories.values():
            embed.add_field(
                name=f"{cat.emoji} {cat.name}",
                value=f"**ID:** `{cat.id}`\n**Товаров:** {len(cat.lots)}",
                inline=False
            )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name='edit_category', description='[OWNER] Редактировать категорию')
    @app_commands.describe(category_id="ID категории", new_name="Новое название", new_emoji="Новый эмодзи")
    async def edit_category(self, interaction: discord.Interaction, category_id: int, new_name: str = None, new_emoji: str = None):
        if not await self.owner_only(interaction):
            return
        category = await db.get_category(category_id)
        if not category:
            await interaction.response.send_message(f"❌ Категория `{category_id}` не найдена", ephemeral=True)
            return
        kwargs = {}
        if new_name:
            kwargs["name"] = new_name
        if new_emoji:
            kwargs["emoji"] = new_emoji
        if kwargs:
            await db.update_category(category_id, **kwargs)
            await db.refresh_cache()
        await send_or_update_shop(interaction.guild, self.bot)
        updated = await db.get_category(category_id)
        embed = discord.Embed(
            title="✅ Категория обновлена",
            description=f"**ID:** `{category_id}`\n**Название:** {updated.emoji} {updated.name}",
            color=discord.Color.green()
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name='add_seller_role', description='[OWNER] Выдать роль продавца')
    @app_commands.describe(member="Пользователь")
    async def add_seller_role(self, interaction: discord.Interaction, member: discord.Member):
        if not await self.owner_only(interaction):
            return
        config = get_config(interaction.guild_id)
        seller_role_id = config["roles"].get("seller") if config else None
        if not seller_role_id:
            await interaction.response.send_message("❌ Нет роли продавца", ephemeral=True)
            return
        seller_role = interaction.guild.get_role(seller_role_id)
        if seller_role in member.roles:
            await interaction.response.send_message(f"⚠️ У {member.mention} уже есть роль продавца", ephemeral=True)
            return
        await member.add_roles(seller_role)
        await interaction.response.send_message(f"✅ {member.mention} → роль продавца", ephemeral=True)

async def setup(bot):
    await bot.add_cog(AdminCog(bot))