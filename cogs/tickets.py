import discord
from discord.ext import commands
from discord import app_commands

from utils.permissions import is_owner, is_admin_member
from panels.ticket_panel import TicketCreateButton, send_ticket_panel
from config.constants import TICKET_CHANNEL_ID
from  import database as db

class TicketsCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    async def owner_only(self, interaction: discord.Interaction) -> bool:
        if not is_owner(interaction):
            await interaction.response.send_message("❌ Только для Owner", ephemeral=True)
            return False
        return True

    @app_commands.command(name='setup_ticket_panel', description='[OWNER] Создать панель тикетов')
    async def setup_ticket_panel(self, interaction: discord.Interaction):
        if not await self.owner_only(interaction):
            return
        await interaction.response.defer(ephemeral=True)
        channel = interaction.guild.get_channel(TICKET_CHANNEL_ID)
        if not channel:
            await interaction.followup.send(f"❌ Канал {TICKET_CHANNEL_ID} не найден", ephemeral=True)
            return
        async for msg in channel.history(limit=50):
            if msg.author == self.bot.user:
                await msg.delete()
        embed = discord.Embed(
            title="🎫 Служба поддержки",
            description="**Нажмите на кнопку ниже, чтобы создать обращение.**\n\n📌 После решения тикет будет закрыт и удалён через 7 дней.",
            color=discord.Color.blue()
        )
        await channel.send(embed=embed, view=TicketCreateButton())
        await interaction.followup.send(f"✅ Панель тикетов создана в {channel.mention}", ephemeral=True)

    @app_commands.command(name='close', description='Закрыть текущий тикет')
    async def close(self, interaction: discord.Interaction):
        name = interaction.channel.name
        if not (name.startswith('ticket-') or name.startswith('заказ-')):
            await interaction.response.send_message('❌ Это не тикет', ephemeral=True)
            return
        ticket = await db.get_ticket(interaction.channel.id)
        if ticket:
            await db.close_ticket(interaction.channel.id)
            if ticket.get('voice_channel_id'):
                vc = interaction.guild.get_channel(ticket['voice_channel_id'])
                if vc:
                    try:
                        await vc.delete()
                    except Exception:
                        pass
        await interaction.response.send_message('🗑️ Тикет закрыт. Удаление через 24 часа...')

async def setup(bot):
    await bot.add_cog(TicketsCog(bot))
