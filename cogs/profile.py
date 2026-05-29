import discord
from discord.ext import commands
from discord import app_commands
from typing import Optional

from panels.status_panel import build_status_embed

class ProfileCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(name='profile', description='Посмотреть профиль пользователя')
    @app_commands.describe(target="Пользователь (опционально)")
    async def profile(self, interaction: discord.Interaction, target: Optional[discord.Member] = None):
        user = target or interaction.user
        await interaction.response.defer(ephemeral=True)
        embed = await build_status_embed(interaction.guild, user)
        await interaction.followup.send(embed=embed, ephemeral=True)

async def setup(bot):
    await bot.add_cog(ProfileCog(bot))
