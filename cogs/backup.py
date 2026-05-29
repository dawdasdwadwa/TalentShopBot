import discord
from discord.ext import commands
from discord import app_commands

from utils.permissions import is_owner
from config.constants import BACKUP_CHANNEL_ID
import database as db
from panels.shop_panel import send_or_update_shop

class BackupCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    async def owner_only(self, interaction: discord.Interaction) -> bool:
        if not is_owner(interaction):
            await interaction.response.send_message("❌ Только для Owner", ephemeral=True)
            return False
        return True

    @app_commands.command(name='backup', description='[OWNER] Экспортировать данные в JSON')
    async def backup(self, interaction: discord.Interaction):
        if not await self.owner_only(interaction):
            return
        await interaction.response.defer(ephemeral=True)
        await db.refresh_cache()
        categories = db.categories_cache
        lots = db.lots_cache
        export = {
            "categories": {str(cat.id): {"name": cat.name, "emoji": cat.emoji, "lots": cat.lots} for cat in categories.values()},
            "lots": {str(lot.lot_id): {"name": lot.name, "price": lot.price, "stock": lot.stock, "seller_id": lot.seller_id, "category_id": lot.category_id, "role_id": lot.role_id} for lot in lots.values()},
            "exported_at": datetime.now().isoformat()
        }
        channel = self.bot.get_channel(BACKUP_CHANNEL_ID)
        if not channel:
            await interaction.followup.send("❌ Канал для бэкапов не найден", ephemeral=True)
            return
        import io
        import json
        from datetime import datetime
        json_bytes = json.dumps(export, ensure_ascii=False, indent=2).encode('utf-8')
        filename = f"shop_backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
        await channel.send(file=discord.File(io.BytesIO(json_bytes), filename))
        await interaction.followup.send(f"✅ Бэкап отправлен в {channel.mention}", ephemeral=True)

    @app_commands.command(name='restore_backup', description='[OWNER] Восстановить магазин из бэкапа')
    async def restore_backup(self, interaction: discord.Interaction):
        if not await self.owner_only(interaction):
            return
        await interaction.response.defer(ephemeral=True)
        result = await db.restore_from_backup_channel(BACKUP_CHANNEL_ID, self.bot)
        if result:
            await send_or_update_shop(interaction.guild, self.bot)
            await interaction.followup.send("✅ Магазин восстановлен из последнего бэкапа!", ephemeral=True)
        else:
            await interaction.followup.send("❌ Не удалось найти бэкап в канале.", ephemeral=True)

async def setup(bot):
    await bot.add_cog(BackupCog(bot))
