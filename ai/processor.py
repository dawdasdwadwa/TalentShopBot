import io
import logging
from datetime import datetime, timezone
import discord

from config.constants import CATEGORY_CHANNELS, CATEGORY_LABELS, LOG_CHANNEL_ID
from handlers import ask_groq_mode
from  import database as db

logger = logging.getLogger(__name__)

async def build_server_context(guild: discord.Guild) -> str:
    """Собирает информацию о сервере для передачи ИИ"""
    roles = [f"{r.name} (ID: {r.id})" for r in guild.roles[:20]]
    channels = [f"#{c.name} (ID: {c.id}, тип: {c.type})" for c in guild.channels[:30]]
    
    return f"""
=== ИНФОРМАЦИЯ О СЕРВЕРЕ {guild.name} ===
Название: {guild.name}
ID сервера: {guild.id}
Участников: {guild.member_count}

Каналы:
{chr(10).join(channels[:20])}

Роли:
{chr(10).join(roles[:15])}
"""

async def process_ai_request(interaction: discord.Interaction, category: str, prompt: str, use_history: bool, use_server_context: bool = False, show_think: str = "hide"):
    """Основная функция обработки ИИ-запроса"""
    user = interaction.user
    
    logger.info(f"AI Request: user={user.id}, category={category}, use_server_context={use_server_context}")
    
    try:
        await interaction.response.defer(ephemeral=True)
    except discord.InteractionResponded:
        pass
    
    # 1. Формируем контекст из истории
    history_messages = []
    if use_history:
        history_data = await db.get_history(user.id, category, limit=6)
        for msg in history_data:
            history_messages.append({"role": msg["role"], "content": msg["content"]})
    
    # 2. Добавляем контекст сервера (если нужно)
    final_prompt = prompt
    if use_server_context and interaction.guild:
        server_info = await build_server_context(interaction.guild)
        final_prompt = f"{server_info}\n\nВопрос пользователя: {prompt}"
    
    try:
        # 3. Запрос к Groq
        response = await ask_groq_mode(category, final_prompt, history_messages if use_history else None, show_think)
        
        if response.startswith("Ошибка"):
            await interaction.followup.send(f"❌ {response}", ephemeral=True)
            return
        
        # 4. Сохраняем в историю (если включено)
        if use_history:
            await db.add_to_history(user.id, category, "user", prompt)
            await db.add_to_history(user.id, category, "assistant", response[:3000])
        
        # 5. Отправка в публичный канал
        channel_id = CATEGORY_CHANNELS.get(category)
        channel_mention = f"<#{channel_id}>" if channel_id else "канал с ответами"
        
        if channel_id:
            channel = interaction.guild.get_channel(channel_id)
            if channel:
                try:
                    embed_chat = discord.Embed(
                        title=f"{CATEGORY_LABELS.get(category, category)} | Ответ ИИ",
                        color=discord.Color.blue(),
                        timestamp=datetime.now(timezone.utc)
                    )
                    embed_chat.add_field(name=f"👤 {user.display_name}", value=f"**Вопрос:** {prompt[:900]}", inline=False)
                    
                    if len(response) <= 950:
                        embed_chat.add_field(name="🤖 Ответ ИИ:", value=response, inline=False)
                        await channel.send(content=user.mention, embed=embed_chat)
                    else:
                        filename = f"ai_answer_{category}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"
                        file_bytes = b'\xef\xbb\xbf' + response.encode('utf-8', errors='replace')
                        file_obj = io.BytesIO(file_bytes)
                        await channel.send(
                            content=f"{user.mention} 📄 **Полный ответ ИИ в файле:**",
                            file=discord.File(file_obj, filename=filename)
                        )
                        short_answer = response[:500] + "... (полный ответ в файле выше)"
                        embed_chat.add_field(name="🤖 Ответ ИИ (кратко):", value=short_answer, inline=False)
                        await channel.send(embed=embed_chat)
                    
                    if use_server_context:
                        embed_chat.set_footer(text="🏠 Учтена структура этого сервера")
                    
                except Exception as e:
                    logger.warning(f"Не удалось отправить в канал {channel_id}: {e}")
        
        # 6. Логирование для админов
        log_channel = interaction.guild.get_channel(LOG_CHANNEL_ID)
        if log_channel:
            try:
                embed_log = discord.Embed(
                    title="📜 Лог запроса ИИ",
                    color=discord.Color.orange(),
                    timestamp=datetime.now(timezone.utc)
                )
                embed_log.add_field(name="👤 Пользователь", value=f"{user.mention} ({user.id})", inline=True)
                embed_log.add_field(name="📁 Категория", value=CATEGORY_LABELS.get(category, category), inline=True)
                embed_log.add_field(name="🧠 История", value="✅ Да" if use_history else "❌ Нет", inline=True)
                embed_log.add_field(name="🏠 Контекст", value="✅ Да" if use_server_context else "❌ Нет", inline=True)
                embed_log.add_field(name="📝 Запрос", value=prompt[:500], inline=False)
                await log_channel.send(embed=embed_log)
            except Exception as e:
                logger.warning(f"Не удалось отправить в лог-канал: {e}")
        
        # 7. Короткий ответ пользователю
        await interaction.followup.send(
            f"✅ **{CATEGORY_LABELS.get(category, category)}**\n"
            f"🤖 ИИ ответил! Ваш ответ находится в канале {channel_mention}\n"
            f"📝 **Ваш запрос:** {prompt[:200]}",
            ephemeral=True
        )
        
    except Exception as e:
        logger.exception("Ошибка в process_ai_request")
        try:
            await interaction.followup.send(f"❌ Ошибка: {e}", ephemeral=True)
        except Exception:
            pass
