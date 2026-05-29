import discord
from discord.ext import commands
from discord import app_commands
from typing import Optional

from utils.permissions import is_owner, get_config
from panels.shop_panel import send_or_update_shop, ShopView, ShopSearchModal
import database as db
from database import has_user_bought, update_stock, get_stock, add_purchase

class ShopCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    async def owner_only(self, interaction: discord.Interaction) -> bool:
        if not is_owner(interaction):
            await interaction.response.send_message("❌ Только для Owner", ephemeral=True)
            return False
        return True

    @app_commands.command(name='add_lot', description='[OWNER] Добавить товар')
    @app_commands.describe(
        category_id="ID категории", name="Название", price="Цена в рублях",
        stock="Количество", short_description="Короткое описание",
        full_description="Детальное описание", seller="Продавец", role_id="ID роли"
    )
    async def add_lot(
        self, interaction: discord.Interaction,
        category_id: int, name: str, price: str, stock: int,
        short_description: str, full_description: str,
        seller: discord.Member, role_id: str = None
    ):
        if not await self.owner_only(interaction):
            return
        category = await db.get_category(category_id)
        if not category:
            await interaction.response.send_message(f"❌ Категория `{category_id}` не найдена", ephemeral=True)
            return
        if len(short_description) > 100:
            await interaction.response.send_message("❌ Короткое описание — не более 100 символов", ephemeral=True)
            return
        if len(full_description) > 2000:
            await interaction.response.send_message("❌ Детальное описание — не более 2000 символов", ephemeral=True)
            return
        config = get_config(interaction.guild_id)
        seller_role_id = config["roles"].get("seller") if config else None
        if seller_role_id:
            seller_role = interaction.guild.get_role(seller_role_id)
            if seller_role and seller_role not in seller.roles:
                await interaction.response.send_message(f"⚠️ У {seller.mention} нет роли продавца", ephemeral=True)
                return
        role_id_int = int(role_id) if role_id and role_id.isdigit() else None

        lot_id = await db.add_lot(
            name=name, price=price, stock=stock,
            short_description=short_description, full_description=full_description,
            seller_id=seller.id, category_id=category_id, role_id=role_id_int
        )
        await db.refresh_cache()
        await interaction.response.send_message(f"✅ Товар `{name}` добавлен (ID: {lot_id})", ephemeral=True)
        await send_or_update_shop(interaction.guild, self.bot)

    @app_commands.command(name='remove_lot', description='[OWNER] Удалить товар')
    @app_commands.describe(lot_id="ID товара")
    async def remove_lot(self, interaction: discord.Interaction, lot_id: int):
        if not await self.owner_only(interaction):
            return
        lot = await db.get_lot(lot_id)
        if not lot:
            await interaction.response.send_message("❌ Товар не найден", ephemeral=True)
            return
        await db.delete_lot(lot_id)
        await db.refresh_cache()
        await interaction.response.send_message(f"✅ Товар `{lot.name}` удалён", ephemeral=True)
        await send_or_update_shop(interaction.guild, self.bot)

    @app_commands.command(name='edit_lot', description='[OWNER] Редактировать товар')
    @app_commands.describe(
        lot_id="ID товара", new_name="Новое название", new_price="Новая цена",
        new_stock="Новое количество", new_short_description="Новое короткое описание",
        new_full_description="Новое детальное описание",
        new_category_id="ID новой категории", new_seller="Новый продавец", new_role_id="ID роли"
    )
    async def edit_lot(
        self, interaction: discord.Interaction, lot_id: int,
        new_name: str = None, new_price: str = None, new_stock: int = None,
        new_short_description: str = None, new_full_description: str = None,
        new_category_id: int = None, new_seller: discord.Member = None, new_role_id: str = None
    ):
        if not await self.owner_only(interaction):
            return
        lot = await db.get_lot(lot_id)
        if not lot:
            await interaction.response.send_message(f"❌ Товар `{lot_id}` не найден", ephemeral=True)
            return
        changes = []
        kwargs = {}
        if new_category_id:
            new_cat = await db.get_category(new_category_id)
            if not new_cat:
                await interaction.response.send_message(f"❌ Категория `{new_category_id}` не найдена", ephemeral=True)
                return
            kwargs["category_id"] = new_category_id
            changes.append(f"категория: → {new_category_id}")
        if new_seller:
            kwargs["seller_id"] = new_seller.id
            changes.append(f"продавец: → {new_seller.mention}")
        if new_name:
            kwargs["name"] = new_name
            changes.append(f"название: {new_name}")
        if new_price:
            kwargs["price"] = new_price
            changes.append(f"цена: {new_price}")
        if new_stock is not None:
            kwargs["stock"] = new_stock
            changes.append(f"сток: {new_stock}")
        if new_short_description:
            kwargs["short_description"] = new_short_description
            changes.append("короткое описание обновлено")
        if new_full_description:
            kwargs["full_description"] = new_full_description
            changes.append("детальное описание обновлено")
        if new_role_id:
            kwargs["role_id"] = int(new_role_id) if new_role_id.isdigit() else None
            changes.append("роль обновлена")
        if not changes:
            await interaction.response.send_message("❌ Ни одно поле не изменено", ephemeral=True)
            return
        await db.update_lot(lot_id, **kwargs)
        await db.refresh_cache()
        await send_or_update_shop(interaction.guild, self.bot)
        embed = discord.Embed(
            title="✅ Товар отредактирован",
            description=f"**ID:** `{lot_id}`\n**Изменения:**\n• " + "\n• ".join(changes),
            color=discord.Color.green()
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name='list_lots', description='[OWNER] Обновить магазин')
    async def list_lots(self, interaction: discord.Interaction):
        if not await self.owner_only(interaction):
            return
        await interaction.response.defer(ephemeral=True)
        await send_or_update_shop(interaction.guild, self.bot)
        await interaction.followup.send("✅ Магазин обновлён", ephemeral=True)

async def setup(bot):
    await bot.add_cog(ShopCog(bot))
