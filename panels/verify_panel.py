import discord
from ..utils.channel import fetch_channel_safe
from ..utils.permissions import get_config

class VerifyButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="✅ Верифицироваться", style=discord.ButtonStyle.green, custom_id="verify_main_btn")

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        config = get_config(interaction.guild_id)
        if not config:
            await interaction.followup.send("❌ Сервер не настроен", ephemeral=True)
            return
        unverified_role_id = config["roles"].get("unverified")
        customer_role_id = config["roles"].get("customer")
        unverified_role = interaction.guild.get_role(unverified_role_id) if unverified_role_id else None
        customer_role = interaction.guild.get_role(customer_role_id) if customer_role_id else None

        if customer_role:
            if unverified_role and unverified_role in interaction.user.roles:
                await interaction.user.remove_roles(unverified_role)
            await interaction.user.add_roles(customer_role)
            await interaction.followup.send("✅ Вы верифицированы! Добро пожаловать.", ephemeral=True)
        else:
            if unverified_role and unverified_role in interaction.user.roles:
                await interaction.user.remove_roles(unverified_role)
                await interaction.followup.send("✅ Верификация пройдена! Добро пожаловать.", ephemeral=True)
            else:
                await interaction.followup.send("✅ Вы уже верифицированы!", ephemeral=True)

class VerifyView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        self.add_item(VerifyButton())

async def send_verify_panel(guild_config: dict, bot):
    channel_id = guild_config.get("verify_channel")
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
    embed = discord.Embed(title="🔒 Верификация", description="Нажми на кнопку ниже, чтобы получить доступ к серверу.", color=discord.Color.gold())
    try:
        await channel.send(embed=embed, view=VerifyView())
    except Exception as e:
        print(f"Ошибка отправки верификации: {e}")