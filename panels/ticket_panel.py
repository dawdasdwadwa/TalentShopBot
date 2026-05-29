import discord
import re
from discord.ui import Button, View, Modal, TextInput
from ..utils.channel import fetch_channel_safe
from ..utils.permissions import get_config, is_admin_member
from ..config.constants import TICKET_SUPPORT_CATEGORY_ID, TICKET_ARCHIVE_CATEGORY_ID
from .. import database as db

class TicketModal(discord.ui.Modal, title="Создание тикета поддержки"):
    subject = discord.ui.TextInput(
        label="Тема обращения",
        placeholder="Кратко опишите проблему...",
        min_length=5,
        max_length=100,
        required=True
    )
    description = discord.ui.TextInput(
        label="Описание",
        placeholder="Подробно опишите вашу проблему...",
        style=discord.TextStyle.paragraph,
        min_length=10,
        max_length=2000,
        required=True
    )

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        existing = await db.get_user_active_ticket(interaction.user.id)
        if existing:
            await interaction.followup.send(
                f"❌ У вас уже есть активный тикет! Канал: <#{existing['channel_id']}>",
                ephemeral=True
            )
            return

        category = interaction.guild.get_channel(TICKET_SUPPORT_CATEGORY_ID)
        if not category:
            await interaction.followup.send("❌ Категория для тикетов не найдена", ephemeral=True)
            return

        safe_user = re.sub(r'[^a-zA-Z0-9_-]', '-', interaction.user.name.lower())[:20]
        channel_name = f"ticket-{safe_user}-{interaction.user.id % 10000}"

        config = get_config(interaction.guild_id)
        admin_role_id = config["roles"].get("admin") if config else None

        overwrites = {
            interaction.guild.default_role: discord.PermissionOverwrite(read_messages=False),
            interaction.user: discord.PermissionOverwrite(read_messages=True, send_messages=True, attach_files=True),
            interaction.guild.me: discord.PermissionOverwrite(read_messages=True, send_messages=True, manage_channels=True)
        }
        if admin_role_id:
            admin_role = interaction.guild.get_role(admin_role_id)
            if admin_role:
                overwrites[admin_role] = discord.PermissionOverwrite(read_messages=True, send_messages=True, manage_messages=True)

        ticket_channel = await interaction.guild.create_text_channel(
            channel_name, category=category, overwrites=overwrites,
            topic=f"Тикет {interaction.user.name} | {self.subject.value[:100]}"
        )
        await db.add_ticket(channel_id=ticket_channel.id, user_id=interaction.user.id, guild_id=interaction.guild_id)

        embed = discord.Embed(
            title="🎫 Тикет поддержки",
            description=(
                f"**Создатель:** {interaction.user.mention}\n"
                f"**Тема:** {self.subject.value}\n"
                f"**Описание:**\n{self.description.value}\n\n"
                "Администраторы скоро ответят.\nДля закрытия используйте кнопку ниже."
            ),
            color=discord.Color.blue()
        )

        view = TicketControlView(ticket_channel.id, interaction.user.id)
        await ticket_channel.send(content=f"{interaction.user.mention}", embed=embed, view=view)
        await interaction.followup.send(f"✅ Тикет создан! Перейдите в {ticket_channel.mention}", ephemeral=True)

class TicketControlView(discord.ui.View):
    def __init__(self, channel_id: int, user_id: int):
        super().__init__(timeout=None)
        self.channel_id = channel_id
        self.user_id = user_id

    @discord.ui.button(label="🔒 Закрыть тикет", style=discord.ButtonStyle.danger, custom_id="close_ticket_btn")
    async def close_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user_id and not is_admin_member(interaction.user):
            await interaction.response.send_message("❌ Только автор или админ могут закрыть тикет.", ephemeral=True)
            return

        await interaction.response.defer()
        channel = interaction.guild.get_channel(self.channel_id)
        if not channel:
            await interaction.followup.send("❌ Канал не найден", ephemeral=True)
            return

        archive_category = interaction.guild.get_channel(TICKET_ARCHIVE_CATEGORY_ID)
        if archive_category:
            await channel.edit(category=archive_category, sync_permissions=False)
            await channel.set_permissions(interaction.user, send_messages=False, read_messages=True)

        await db.close_ticket(self.channel_id)

        embed = discord.Embed(
            title="🔒 Тикет закрыт",
            description=f"Тикет закрыт {interaction.user.mention}\nКанал будет автоматически удалён через **7 дней**.",
            color=discord.Color.dark_red()
        )
        await channel.send(embed=embed)

class TicketCreateButton(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="🎫 Создать тикет", style=discord.ButtonStyle.green, custom_id="create_ticket_btn")
    async def create_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(TicketModal())

async def send_ticket_panel(guild_config: dict, bot):
    channel_id = guild_config.get("ticket_channel")
    if not channel_id:
        return
    channel = await fetch_channel_safe(bot, channel_id)
    if not channel:
        return
    try:
        async for msg in channel.history(limit=50):
            if msg.author == bot.user:
                try:
                    await msg.delete()
                except Exception:
                    pass
    except Exception:
        pass
    embed = discord.Embed(
        title="🎫 Служба поддержки",
        description="**Нажмите на кнопку ниже, чтобы создать обращение.**\n\n📌 Удаление тикета из архива через 7 дней",
        color=discord.Color.blue()
    )
    embed.set_footer(text=f"{guild_config['name']} — Техническая поддержка")
    try:
        await channel.send(embed=embed, view=TicketCreateButton())
    except Exception as e:
        print(f"Ошибка отправки тикетов: {e}")