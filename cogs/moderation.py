import discord
from discord.ext import commands
from discord import app_commands
from typing import Optional

from ..utils.permissions import is_admin_member
from .. import database as db

class ModerationCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    async def admin_only(self, interaction: discord.Interaction) -> bool:
        if not is_admin_member(interaction.user):
            await interaction.response.send_message("❌ Только для администраторов", ephemeral=True)
            return False
        return True

    @app_commands.command(name='warn', description='[ADMIN] Выдать предупреждение')
    @app_commands.describe(member="Пользователь", reason="Причина")
    async def warn(self, interaction: discord.Interaction, member: discord.Member, reason: str):
        if not await self.admin_only(interaction):
            return
        if is_admin_member(member):
            await interaction.response.send_message("❌ Нельзя предупредить администратора", ephemeral=True)
            return
        count = await db.add_warning(member.id, interaction.user.id, reason)
        embed = discord.Embed(
            title="⚠️ Предупреждение",
            description=f"**Пользователь:** {member.mention}\n**Причина:** {reason}\n**Предупреждений:** {count}",
            color=discord.Color.orange()
        )
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name='warnings', description='[ADMIN] Предупреждения пользователя')
    @app_commands.describe(member="Пользователь")
    async def warnings(self, interaction: discord.Interaction, member: discord.Member):
        if not await self.admin_only(interaction):
            return
        warns = await db.get_user_warnings(member.id)
        count = len(warns)
        embed = discord.Embed(
            title=f"⚠️ Предупреждения: {member.display_name}",
            description=f"Всего: **{count}**",
            color=discord.Color.orange()
        )
        for w in warns[:10]:
            embed.add_field(
                name=f"#{w.id} — {w.created_at[:10]}",
                value=f"Причина: {w.reason}\nМодератор: <@{w.moderator_id}>",
                inline=False
            )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name='clearwarnings', description='[ADMIN] Очистить предупреждения')
    @app_commands.describe(member="Пользователь")
    async def clearwarnings(self, interaction: discord.Interaction, member: discord.Member):
        if not await self.admin_only(interaction):
            return
        await db.clear_warnings(member.id)
        await interaction.response.send_message(f"✅ Предупреждения {member.mention} очищены", ephemeral=True)

    @app_commands.command(name='blacklist_add', description='[ADMIN] Добавить в чёрный список')
    @app_commands.describe(member="Пользователь", reason="Причина")
    async def blacklist_add(self, interaction: discord.Interaction, member: discord.Member, reason: str = "Не указана"):
        if not await self.admin_only(interaction):
            return
        await db.add_to_blacklist(member.id, interaction.user.id, reason)
        embed = discord.Embed(
            title="🚫 Пользователь заблокирован",
            description=f"**Пользователь:** {member.mention}\n**Причина:** {reason}\n**Модератор:** {interaction.user.mention}",
            color=discord.Color.red()
        )
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name='blacklist_remove', description='[ADMIN] Убрать из чёрного списка')
    @app_commands.describe(member="Пользователь", reason="Причина")
    async def blacklist_remove(self, interaction: discord.Interaction, member: discord.Member, reason: str = "Разблокировка"):
        if not await self.admin_only(interaction):
            return
        await db.remove_from_blacklist(member.id, interaction.user.id, reason)
        await interaction.response.send_message(f"✅ {member.mention} удалён из чёрного списка.", ephemeral=True)

async def setup(bot):
    await bot.add_cog(ModerationCog(bot))