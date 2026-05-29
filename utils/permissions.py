from typing import Optional
import discord

from config.settings import CONFIG

def get_config(guild_id: int) -> Optional[dict]:
    """Получает конфигурацию сервера по ID"""
    return CONFIG.get(guild_id)

def is_owner(interaction: discord.Interaction) -> bool:
    """Проверяет, является ли пользователь владельцем сервера"""
    config = get_config(interaction.guild_id)
    if not config:
        return False
    owner_role_id = config["roles"].get("owner")
    if not owner_role_id:
        return False
    owner_role = interaction.guild.get_role(owner_role_id)
    return owner_role is not None and owner_role in interaction.user.roles

def is_admin(interaction: discord.Interaction) -> bool:
    """Проверяет, является ли пользователь администратором"""
    config = get_config(interaction.guild_id)
    if not config:
        return False
    admin_role_id = config["roles"].get("admin")
    if admin_role_id:
        admin_role = interaction.guild.get_role(admin_role_id)
        if admin_role and admin_role in interaction.user.roles:
            return True
    return is_owner(interaction)

def is_admin_member(member: discord.Member) -> bool:
    """Проверяет, является ли участник администратором (для методов без interaction)"""
    guild = member.guild
    config = get_config(guild.id)
    if not config:
        return False
    for role_key in ("owner", "admin"):
        role_id = config["roles"].get(role_key)
        if role_id:
            role = guild.get_role(role_id)
            if role and role in member.roles:
                return True
    return False
