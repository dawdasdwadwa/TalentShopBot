import discord
from discord.ui import TextInput, Modal
from discord.ext import commands, tasks
from discord.ui import Button, View
from discord import app_commands
import random
import os
import re
import sys
import io
import json
import asyncio
import aiohttp
import logging
from typing import Optional, Dict, List, Any
from datetime import datetime, timedelta, timezone
import database as db
from database import (
    has_user_bought, update_stock, get_stock, add_purchase,
    add_review, get_seller_rating, get_seller_reviews,
    get_daily_purchase_count, convert_price_rub
)
import groq

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8')

# ================= КОНСТАНТЫ =================
COPY_PANEL_CHANNEL_ID = 1511942497574719639
AI_CONVEYOR_CHANNEL_ID = 1509333979713769612
TICKET_SUPPORT_CATEGORY_ID = 1503176090980454531
TICKET_ARCHIVE_CATEGORY_ID = 1507376570082267167
TICKET_CHANNEL_ID = 1500242313211805788
BACKUP_CHANNEL_ID = 1503146387129368718
BACKUP_MAX_MESSAGES = 50
LOG_CHANNEL_ID = 1509707240792133824
ADMIN_PANEL_CHANNEL_ID = 1503168213016641536
WEBHOOK_SPAM_CHANNEL_ID = 1511587835734659164

CATEGORY_CHANNELS = {
    "coding": 1509706870141747391,
    "advice": 1509706898868277259,
    "design": 1509706937976229928,
    "analytics": 1509706964551078071,
    "content": 1509706993361748078,
    "marketing": 1509707013670834306,
    "features": 1509707039272599602,
}

CATEGORY_LABELS = {
    "coding": "💻 Кодинг",
    "advice": "💡 Советы",
    "design": "🎨 Оформление",
    "analytics": "📊 Аналитика",
    "content": "📝 Контент",
    "marketing": "📈 Маркетинг",
    "features": "🤖 Бот-фичи",
}

# ================= GROQ =================
GROQ_API_KEYS = [
    os.getenv("GROQ_API_KEY"),
    os.getenv("GROQ_API_KEY_2"),
    os.getenv("GROQ_API_KEY_3"),
    os.getenv("GROQ_API_KEY_4"),
]

VALID_API_KEYS = [key for key in GROQ_API_KEYS if key]

if not VALID_API_KEYS:
    logger.warning("⚠️ Ни одного GROQ_API_KEY не найдено!")
    groq_clients = []
else:
    groq_clients = [groq.Groq(api_key=key) for key in VALID_API_KEYS]
    logger.info(f"✅ Groq клиенты: {len(groq_clients)} ключей")

_current_client_index = 0

def get_next_groq_client():
    global _current_client_index
    if not groq_clients:
        return None
    client = groq_clients[_current_client_index]
    _current_client_index = (_current_client_index + 1) % len(groq_clients)
    return client

# ================= КОНФИГУРАЦИЯ МОДЕЛЕЙ =================
MODEL_CODING = "llama-3.3-70b-versatile"
MODEL_CREATIVE = "qwen/qwen3-32b"

MODE_MODELS = {
    "coding": MODEL_CODING,
    "advice": MODEL_CREATIVE,
    "design": MODEL_CREATIVE,
    "analytics": MODEL_CODING,
    "content": MODEL_CREATIVE,
    "marketing": MODEL_CODING,
    "features": MODEL_CODING,
}

SYSTEM_PROMPTS = {
    "coding": "Ты — Senior Python Developer с 20-летним стажем. Пиши чистый, документированный код. Отвечай на русском.",
    "advice": "Ты — эксперт по Discord серверам. Давай конкретные, практичные советы. Отвечай на русском.",
    "design": "Ты — UI/UX дизайнер Discord серверов. Предлагай красивые и функциональные решения. Отвечай на русском.",
    "analytics": "Ты — аналитик данных. Делай выводы, находи узкие места. Отвечай на русском.",
    "content": "Ты — копирайтер. Пиши вовлекающие тексты, эмбеды. Отвечай на русском.",
    "marketing": "Ты — маркетолог. Разрабатывай стратегии продаж. Отвечай на русском.",
    "features": "Ты — продакт-менеджер. Генерируй идеи новых функций. Отвечай на русском.",
}

# ================= КОНФИГУРАЦИЯ СЕРВЕРА =================
CONFIG = {
    1502675378051879063: {
        "name": "RP CENTER",
        "welcome_channel": 1503041215803822200,
        "verify_channel": 1503048325614538902,
        "log_channel": 1503045466697240616,
        "review_channel": None,
        "shop_channel": None,
        "roles": {
            "owner": 1503050047947014356,
            "admin": 1503050094361186424,
            "unverified": 1503056964425355324,
        }
    },
    1462375742401675294: {
        "name": "TALENT SHOP",
        "welcome_channel": 1500249815953703004,
        "verify_channel": 1500257894858358895,
        "review_channel": 1500261460075479222,
        "log_channel": 1500263242465935492,
        "admin_log_channel": 1500275827441532948,
        "shop_channel": 1500275827441532948,
        "ticket_channel": TICKET_CHANNEL_ID,
        "status_channel": 1506750339783725218,
        "roles": {
            "owner": 1500243730618126428,
            "admin": 1500243731519901898,
            "customer": 1500243735143907469,
            "unverified": 1500250293206515762,
            "seller": 1500291856259612672,
            "buyer": 1500243733675773972,
        }
    }
}

OWNER_ID = 1500198262026539099
SHOP_IMAGE_LINK = "https://i.postimg.cc/43SZJkLJ/Magazin.png"
DAILY_PURCHASE_LIMIT = 10
MAX_WARNINGS_BEFORE_BAN = 3

# ================= ПРАВА =================
def get_config(guild_id: int):
    return CONFIG.get(guild_id)

def is_owner(interaction: discord.Interaction) -> bool:
    config = get_config(interaction.guild_id)
    if not config:
        return False
    owner_role_id = config["roles"].get("owner")
    if not owner_role_id:
        return False
    owner_role = interaction.guild.get_role(owner_role_id)
    return owner_role and owner_role in interaction.user.roles

def is_admin_member(member: discord.Member) -> bool:
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

# ================= БАЗОВЫЕ НАСТРОЙКИ =================
intents = discord.Intents.default()
intents.members = True
intents.message_content = True
bot = commands.Bot(command_prefix='!', intents=intents)

TICKET_CATEGORY_NAME = 'Tickets'
MENTION_LIMIT = 10
MUTE_DURATIONS = [3600, 7200, 14400, 28800, 86400]
user_mention_count: dict = {}
user_mention_last_reset: dict = {}
BANNED_PATTERNS = [
    r'discord\.gg\/\S+',
    r'discord\.com\/invite\/\S+',
    r'dis?cord(?:app)?\.com\/\S*invite',
]
_startup_done = False
active_orders: set = set()
user_ticket_cooldown: dict = {}
TICKET_COOLDOWN_SECONDS = 5
_shop_update_lock = asyncio.Lock()
ai_cooldowns = {}

# ================= ФУНКЦИИ GROQ =================
async def ask_groq_with_retry(model: str, messages: List[Dict[str, str]], max_retries: int = 4, temperature: float = 0.2) -> str:
    for attempt in range(max_retries):
        client = get_next_groq_client()
        if not client:
            return "Ошибка: Нет доступных Groq клиентов"
        try:
            response = await asyncio.to_thread(
                client.chat.completions.create,
                model=model,
                messages=messages,
                temperature=temperature,
            )
            text = response.choices[0].message.content
            try:
                text = text.encode('latin1').decode('utf-8')
            except:
                pass
            return text
        except Exception as e:
            error_msg = str(e)
            if "429" in error_msg and attempt < max_retries - 1:
                logger.warning(f"429 ошибка, переключаем ключ... Попытка {attempt + 2}")
                await asyncio.sleep(1)
                continue
            return f"Ошибка Groq API: {error_msg[:200]}"
    return "Ошибка: Все ключи исчерпали лимиты"

def clean_markdown(text: str) -> str:
    text = re.sub(r'^#{1,6}\s+', '', text, flags=re.MULTILINE)
    text = re.sub(r'\*\*(.+?)\*\*', r'\1', text)
    text = re.sub(r'\*(.+?)\*', r'\1', text)
    text = re.sub(r'^[-*_]{3,}$', '', text, flags=re.MULTILINE)
    text = re.sub(r'\[(.+?)\]\(.+?\)', r'\1', text)
    text = re.sub(r'`(.+?)`', r'\1', text)
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text.strip()

async def ask_groq_mode(mode: str, prompt: str, history: List[Dict[str, str]] = None, show_think: str = "hide") -> str:
    model = MODE_MODELS.get(mode, MODEL_CODING)
    system_prompt = SYSTEM_PROMPTS.get(mode, SYSTEM_PROMPTS["coding"])
    
    messages = [{"role": "system", "content": system_prompt}]
    if history:
        messages.extend(history)
    messages.append({"role": "user", "content": prompt})
    
    temperature = 0.4 if mode in ["content", "design", "advice"] else 0.2
    response = await ask_groq_with_retry(model, messages, temperature=temperature)
    
    if show_think == "hide":
        response = re.sub(r'<think>.*?</think>', '', response, flags=re.DOTALL)
    
    response = clean_markdown(response)
    return response

# ================= КОНТЕКСТ СЕРВЕРА =================
async def build_server_context(guild: discord.Guild) -> str:
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

# ================= ОСНОВНАЯ ФУНКЦИЯ ОБРАБОТКИ ИИ =================
async def process_ai_request(interaction: discord.Interaction, category: str, prompt: str, use_history: bool, use_server_context: bool = False, show_think: str = "hide"):
    user = interaction.user
    
    logger.info(f"AI Request: user={user.id}, category={category}, use_server_context={use_server_context}")
    
    try:
        await interaction.response.defer(ephemeral=True)
    except discord.InteractionResponded:
        pass
    
    history_messages = []
    if use_history:
        history_data = await db.get_history(user.id, category, limit=6)
        for msg in history_data:
            history_messages.append({"role": msg["role"], "content": msg["content"]})
    
    final_prompt = prompt
    if use_server_context and interaction.guild:
        server_info = await build_server_context(interaction.guild)
        final_prompt = f"{server_info}\n\nВопрос пользователя: {prompt}"
    
    try:
        response = await ask_groq_mode(category, final_prompt, history_messages if use_history else None, show_think)
        
        if response.startswith("Ошибка"):
            await interaction.followup.send(f"❌ {response}", ephemeral=True)
            return
        
        if use_history:
            await db.add_to_history(user.id, category, "user", prompt)
            await db.add_to_history(user.id, category, "assistant", response[:3000])
        
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

# ================= ИИ-ИНТЕРФЕЙСЫ =================
class AIInputModal(discord.ui.Modal):
    def __init__(self, category: str, use_history: bool, use_server_context: bool):
        super().__init__(title=f"🤖 {CATEGORY_LABELS.get(category, category)}")
        self.category = category
        self.use_history = use_history
        self.use_server_context = use_server_context
        self.prompt_input = TextInput(label="Ваш запрос", style=discord.TextStyle.paragraph, placeholder="Введите ваш запрос...", required=True, max_length=2000)
        self.add_item(self.prompt_input)
    
    async def on_submit(self, interaction: discord.Interaction):
        await process_ai_request(interaction, self.category, self.prompt_input.value, self.use_history, self.use_server_context, "hide")

class AIRequestView(discord.ui.View):
    def __init__(self, category: str):
        super().__init__(timeout=None)
        self.category = category
    
    @discord.ui.select(placeholder="🧠 Учитывать историю?", options=[
        discord.SelectOption(label="Да, учитывать", value="yes", emoji="🧠"),
        discord.SelectOption(label="Нет, чистый запрос", value="no", emoji="🧹"),
    ])
    async def select_callback(self, interaction: discord.Interaction, select: discord.ui.Select):
        use_history = select.values[0] == "yes"
        
        class ServerContextView(discord.ui.View):
            def __init__(self, cat: str, hist: bool):
                super().__init__(timeout=None)
                self.cat = cat
                self.hist = hist
            
            @discord.ui.select(placeholder="🏠 Учитывать структуру сервера?", options=[
                discord.SelectOption(label="Да, брать за основу", value="yes", emoji="🏠"),
                discord.SelectOption(label="Нет, общие советы", value="no", emoji="🌍"),
            ])
            async def server_callback(self, i: discord.Interaction, s: discord.ui.Select):
                use_server = s.values[0] == "yes"
                modal = AIInputModal(category=self.cat, use_history=self.hist, use_server_context=use_server)
                await i.response.send_modal(modal)
        
        view = ServerContextView(self.category, use_history)
        await interaction.response.edit_message(content=f"🎯 **{CATEGORY_LABELS.get(self.category, self.category)}**\n\nУчитывать структуру этого сервера?", view=view)

class StartAIButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="🚀 Запустить ИИ-Конвейер", style=discord.ButtonStyle.success, custom_id="start_ai")
    
    async def callback(self, interaction: discord.Interaction):
        options = [discord.SelectOption(label=label, value=value) for value, label in CATEGORY_LABELS.items()]
        options.append(discord.SelectOption(label="🗑️ Очистить историю", value="clear_history", emoji="🗑️"))
        
        class MainSelectView(discord.ui.View):
            def __init__(self):
                super().__init__(timeout=None)
                self.select = discord.ui.Select(placeholder="🎯 Выберите режим", options=options)
                self.select.callback = self.select_callback
                self.add_item(self.select)
            
            async def select_callback(self, i: discord.Interaction):
                if self.select.values[0] == "clear_history":
                    await db.clear_user_history(i.user.id)
                    await i.response.send_message("✅ История очищена!", ephemeral=True)
                else:
                    view = AIRequestView(category=self.select.values[0])
                    await i.response.edit_message(content=f"🎯 **{CATEGORY_LABELS.get(self.select.values[0], self.select.values[0])}**\n\nУчитывать историю?", view=view)
        
        await interaction.response.send_message("🎯 **Выберите режим работы:**", view=MainSelectView(), ephemeral=True)

# ================= КУРС ВАЛЮТ =================
async def fetch_currency_rates():
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                'https://api.exchangerate-api.com/v4/latest/RUB',
                timeout=aiohttp.ClientTimeout(total=10)
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    rates = data.get("rates", {})
                    result = {"UAH": rates.get("UAH", 0), "USD": rates.get("USD", 0), "EUR": rates.get("EUR", 0)}
                    await db.update_currency_rates(result)
                    return result
    except Exception:
        pass
    return {}

async def parse_price_rub(price_str: str) -> Optional[float]:
    match = re.search(r'[\d]+(?:[.,]\d+)?', price_str.replace(' ', ''))
    if match:
        return float(match.group().replace(',', '.'))
    return None

# ================= ВЕРИФИКАЦИЯ =================
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
            await interaction.followup.send("✅ Вы верифицированы!", ephemeral=True)
        else:
            if unverified_role and unverified_role in interaction.user.roles:
                await interaction.user.remove_roles(unverified_role)
                await interaction.followup.send("✅ Верификация пройдена!", ephemeral=True)
            else:
                await interaction.followup.send("✅ Вы уже верифицированы!", ephemeral=True)

class VerifyView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        self.add_item(VerifyButton())

# ================= ТИКЕТЫ ПОДДЕРЖКИ =================
class TicketModal(discord.ui.Modal, title="Создание тикета поддержки"):
    subject = discord.ui.TextInput(label="Тема обращения", placeholder="Кратко опишите проблему...", min_length=5, max_length=100, required=True)
    description = discord.ui.TextInput(label="Описание", placeholder="Подробно опишите вашу проблему...", style=discord.TextStyle.paragraph, min_length=10, max_length=2000, required=True)
    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        existing = await db.get_user_active_ticket(interaction.user.id)
        if existing:
            await interaction.followup.send(f"❌ У вас уже есть активный тикет! Канал: <#{existing['channel_id']}>", ephemeral=True)
            return
        category = interaction.guild.get_channel(TICKET_SUPPORT_CATEGORY_ID)
        if not category:
            await interaction.followup.send("❌ Категория для тикетов не найдена", ephemeral=True)
            return
        safe_user = re.sub(r'[^a-zA-Z0-9_-]', '-', interaction.user.name.lower())[:20]
        channel_name = f"ticket-{safe_user}-{interaction.user.id % 10000}"
        overwrites = {
            interaction.guild.default_role: discord.PermissionOverwrite(read_messages=False),
            interaction.user: discord.PermissionOverwrite(read_messages=True, send_messages=True, attach_files=True),
            interaction.guild.me: discord.PermissionOverwrite(read_messages=True, send_messages=True, manage_channels=True)
        }
        admin_role_id = get_config(interaction.guild_id)["roles"].get("admin")
        if admin_role_id:
            admin_role = interaction.guild.get_role(admin_role_id)
            if admin_role:
                overwrites[admin_role] = discord.PermissionOverwrite(read_messages=True, send_messages=True, manage_messages=True)
        ticket_channel = await interaction.guild.create_text_channel(channel_name, category=category, overwrites=overwrites, topic=f"Тикет {interaction.user.name} | {self.subject.value[:100]}")
        await db.add_ticket(channel_id=ticket_channel.id, user_id=interaction.user.id, guild_id=interaction.guild_id)
        embed = discord.Embed(title="🎫 Тикет поддержки", description=f"**Создатель:** {interaction.user.mention}\n**Тема:** {self.subject.value}\n**Описание:**\n{self.description.value}\n\nАдминистраторы скоро ответят.\nДля закрытия используйте кнопку ниже.", color=discord.Color.blue())
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
        embed = discord.Embed(title="🔒 Тикет закрыт", description=f"Тикет закрыт {interaction.user.mention}\nКанал будет автоматически удалён через **7 дней**.", color=discord.Color.dark_red())
        await channel.send(embed=embed)

class TicketCreateButton(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
    @discord.ui.button(label="🎫 Создать тикет", style=discord.ButtonStyle.green, custom_id="create_ticket_btn")
    async def create_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(TicketModal())

# ================= МАГАЗИН =================
async def _fetch_channel_safe(channel_id: int, retries: int = 5) -> Optional[discord.TextChannel]:
    for attempt in range(retries):
        ch = bot.get_channel(channel_id)
        if ch:
            return ch
        try:
            ch = await bot.fetch_channel(channel_id)
            if ch:
                return ch
        except Exception:
            pass
        await asyncio.sleep(2)
    return None

async def rotate_backup_channel(channel):
    try:
        messages = []
        async for msg in channel.history(limit=200):
            if msg.author == bot.user:
                messages.append(msg)
        
        if len(messages) > BACKUP_MAX_MESSAGES:
            to_delete = messages[BACKUP_MAX_MESSAGES:]
            for msg in to_delete:
                try:
                    await msg.delete()
                    await asyncio.sleep(0.5)
                except Exception as e:
                    logger.error(f"Ошибка удаления сообщения {msg.id}: {e}")
            logger.info(f"✅ Удалено {len(to_delete)} старых сообщений бэкапа. Осталось: {BACKUP_MAX_MESSAGES}")
    except Exception as e:
        logger.error(f"Ошибка ротации бэкапов: {e}")

async def save_backup(reason: str = "manual"):
    try:
        backup_json = await db.create_backup(bot)
        if not backup_json:
            logger.error("Не удалось создать бэкап")
            return False
        
        backup_bytes = backup_json.encode('utf-8')
        backup_file = discord.File(
            io.BytesIO(backup_bytes),
            filename=f"shop_backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
        )
        
        channel = bot.get_channel(BACKUP_CHANNEL_ID)
        if not channel:
            channel = await bot.fetch_channel(BACKUP_CHANNEL_ID)
        
        if channel:
            await channel.send(f"💾 Бэкап ({reason}) от {datetime.now().strftime('%d.%m.%Y %H:%M:%S')}", file=backup_file)
            await asyncio.sleep(1)
            await rotate_backup_channel(channel)
            logger.info(f"✅ Бэкап сохранён: {reason}")
            return True
        else:
            logger.error(f"Канал бэкапа {BACKUP_CHANNEL_ID} не найден")
            return False
    except Exception as e:
        logger.error(f"Ошибка сохранения бэкапа: {e}")
        return False

async def auto_backup_task():
    await bot.wait_until_ready()
    while not bot.is_closed():
        try:
            await save_backup("auto (12 hours)")
        except Exception as e:
            logger.error(f"Ошибка авто-бэкапа: {e}")
        await asyncio.sleep(43200)

async def _do_shop_update(guild: discord.Guild):
    config = get_config(guild.id)
    if not config or not config.get("shop_channel"):
        return
    channel = await _fetch_channel_safe(config["shop_channel"])
    if not channel:
        return
    await db.refresh_cache()
    
    view = ShopView()
    
    try:
        async for msg in channel.history(limit=100):
            if msg.author == bot.user:
                try:
                    await msg.delete()
                    await asyncio.sleep(0.3)
                except Exception:
                    pass
    except Exception:
        pass
    
    if SHOP_IMAGE_LINK and SHOP_IMAGE_LINK.startswith(('http://', 'https://')):
        try:
            await channel.send(SHOP_IMAGE_LINK)
        except Exception as e:
            logger.error(f"Не удалось отправить картинку: {e}")
    
    msg = await channel.send(view=view)
    await db.set_shop_messages(guild.id, img_id=msg.id)

async def send_or_update_shop(guild: discord.Guild):
    async with _shop_update_lock:
        await _do_shop_update(guild)
        await save_backup("shop_update")

# ================= ПОИСК В МАГАЗИНЕ =================
class ShopSearchModal(discord.ui.Modal, title="🔍 Поиск товара"):
    query = discord.ui.TextInput(label="Название товара или категории", placeholder="Введите название...", min_length=1, max_length=200, required=True)
    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        search_term = self.query.value.strip()
        cats = db.categories_cache
        matched_cats = [c for c in cats.values() if search_term.lower() in c.name.lower()]
        matched_lots = await db.search_lots(search_term)
        if not matched_cats and not matched_lots:
            await interaction.followup.send(f"❌ По запросу **{search_term}** ничего не найдено.", ephemeral=True)
            return
        embed = discord.Embed(title=f"🔍 Результаты поиска: {search_term}", color=discord.Color.blue())
        if matched_cats:
            cat_text = "\n".join([f"{c.emoji} **{c.name}** — товаров: {len(c.lots)}" for c in matched_cats])
            embed.add_field(name="📁 Категории", value=cat_text, inline=False)
        if matched_lots:
            lots_text = "".join([f"{'✅' if lot.stock > 0 else '❌'} **{lot.name}** — {lot.price}\n" for lot in matched_lots])
            if len(lots_text) > 1000:
                lots_text = lots_text[:1000] + "..."
            embed.add_field(name="🛒 Товары", value=lots_text, inline=False)
        view = discord.ui.View(timeout=None)
        if matched_lots:
            options = [discord.SelectOption(label=f"{lot.name[:50]} - {lot.price[:20]}", value=str(lot.lot_id), emoji="🛒") for lot in matched_lots]
            if len(options) > 25:
                options = options[:25]
            select = discord.ui.Select(placeholder="Выбрать товар из результатов", options=options)
            select.callback = lambda i: lot_select_callback(i, select)
            view.add_item(select)
        await interaction.followup.send(embed=embed, view=view, ephemeral=True)

async def lot_select_callback(interaction: discord.Interaction, select):
    await interaction.response.defer(ephemeral=True)
    lot_id = int(select.values[0])
    lot = await db.get_lot(lot_id)
    if not lot:
        await interaction.followup.send("❌ Товар не найден", ephemeral=True)
        return
    seller = interaction.guild.get_member(lot.seller_id)
    seller_name = seller.display_name if seller else "Продавец"
    stock_text = f"📦 В наличии: {lot.stock}" if lot.stock > 0 else "❌ Нет в наличии"
    embed = discord.Embed(title=f"🛒 {lot.name}", description=f"💰 **{lot.price}**\n{stock_text}\n\n**📝 Описание:**\n{lot.full_description}\n\n**👤 Продавец:** {seller_name}", color=discord.Color.green())
    if lot.image_url and lot.image_url.startswith(('http://', 'https://')):
        embed.set_thumbnail(url=lot.image_url)
    view = LotActionView(lot_id, lot, seller)
    await interaction.followup.send(embed=embed, view=view, ephemeral=True)

# ================= ShopView =================
class ShopView(discord.ui.View):
    def __init__(self, page: int = 0):
        super().__init__(timeout=None)
        self.page = page
        self.categories_list = list(db.categories_cache.values())
        self.update_items()
    
    def update_items(self):
        self.clear_items()
        if not self.categories_list:
            search_btn = discord.ui.Button(label="🔍 Поиск", style=discord.ButtonStyle.primary)
            search_btn.callback = self.search_callback
            self.add_item(search_btn)
            return
        
        start = self.page * 24
        end = start + 24
        page_categories = self.categories_list[start:end]
        if page_categories:
            options = [discord.SelectOption(label=cat.name, description=f"Товаров: {len(cat.lots)}", value=str(cat.id), emoji=cat.emoji) for cat in page_categories]
            select = discord.ui.Select(placeholder="📁 Выберите категорию...", options=options)
            select.callback = self.category_callback
            self.add_item(select)
        
        if len(self.categories_list) > 24:
            if self.page > 0:
                prev_btn = discord.ui.Button(label="◀️ Назад", style=discord.ButtonStyle.secondary)
                prev_btn.callback = self.prev_page
                self.add_item(prev_btn)
            if end < len(self.categories_list):
                next_btn = discord.ui.Button(label="Вперёд ▶️", style=discord.ButtonStyle.secondary)
                next_btn.callback = self.next_page
                self.add_item(next_btn)
        
        search_btn = discord.ui.Button(label="🔍 Поиск", style=discord.ButtonStyle.primary)
        search_btn.callback = self.search_callback
        self.add_item(search_btn)
    
    async def search_callback(self, interaction: discord.Interaction):
        await interaction.response.send_modal(ShopSearchModal())
    
    async def category_callback(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            category_id = int(interaction.data['values'][0])
            category = await db.get_category(category_id)
            if not category:
                await interaction.followup.send("❌ Категория не найдена", ephemeral=True)
                return
            lots_in_category = await db.get_lots_by_category_full(category_id)
            if not lots_in_category:
                embed = discord.Embed(title=f"📁 {category.name}", description="В этой категории пока нет товаров.", color=discord.Color.blue())
                await interaction.followup.send(embed=embed, ephemeral=True)
                return
            embed = discord.Embed(title=f"📁 {category.name}", description="**Выбери товар из списка ниже:**", color=discord.Color.blue())
            if category.image_url and category.image_url.startswith(('http://', 'https://')):
                embed.set_image(url=category.image_url)
            for lot in lots_in_category:
                seller = interaction.guild.get_member(lot.seller_id)
                seller_name = seller.display_name if seller else "Продавец"
                
                if lot.stock == -1:
                    stock_text = "♾️ Бесконечно"
                elif lot.stock > 0:
                    stock_text = f"📦 В наличии: {lot.stock}"
                else:
                    stock_text = "❌ Нет в наличии"
                
                desc = (lot.short_description or "")[:80]
                embed.add_field(name=f"🛒 {lot.name}", value=f"💰 **Цена:** {lot.price}\n{stock_text}\n📝 {desc}\n👤 **Продавец:** {seller_name}", inline=False)
            view = LotsView(category_id, lots_in_category)
            await interaction.followup.send(embed=embed, view=view, ephemeral=True)
        except Exception as e:
            logger.exception("Ошибка category_callback")
            await interaction.followup.send(f"❌ Ошибка: {e}", ephemeral=True)
    
    async def prev_page(self, interaction: discord.Interaction):
        self.page -= 1
        self.update_items()
        await interaction.response.edit_message(view=self)
    
    async def next_page(self, interaction: discord.Interaction):
        self.page += 1
        self.update_items()
        await interaction.response.edit_message(view=self)

# ================= СПИСОК ТОВАРОВ =================
class LotsView(discord.ui.View):
    def __init__(self, category_id: int, lots_list: list):
        super().__init__(timeout=None)
        self.category_id = category_id
        options = [discord.SelectOption(label=f"{lot.name} - {lot.price}"[:100], description=(lot.short_description[:50] if lot.short_description else None), value=str(lot.lot_id), emoji="🛒") for lot in lots_list[:25]]
        if options:
            select = discord.ui.Select(placeholder="🛍️ Выбери товар", options=options, min_values=1, max_values=1)
            select.callback = self.lot_callback
            self.add_item(select)
        close_button = discord.ui.Button(label="❌ Закрыть", style=discord.ButtonStyle.danger)
        close_button.callback = self.close_callback
        self.add_item(close_button)
    async def lot_callback(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        try:
            lot_id = int(interaction.data['values'][0])
            lot = await db.get_lot(lot_id)
            if not lot:
                await interaction.followup.send("❌ Товар не найден", ephemeral=True)
                return
            seller = interaction.guild.get_member(lot.seller_id)
            seller_name = seller.display_name if seller else "Продавец"
            
            if lot.stock == -1:
                stock_text = "♾️ Бесконечно"
            elif lot.stock > 0:
                stock_text = f"📦 В наличии: {lot.stock} шт."
            else:
                stock_text = "❌ Нет в наличии"
            
            embed = discord.Embed(title=f"🛒 {lot.name}", description=f"💰 **{lot.price}**\n{stock_text}\n\n**📝 Детальное описание:**\n{lot.full_description}\n\n**👤 Продавец:** {seller_name}", color=discord.Color.green())
            if lot.image_url and lot.image_url.startswith(('http://', 'https://')):
                embed.set_thumbnail(url=lot.image_url)
            embed.set_footer(text="Выбери действие ниже")
            view = LotActionView(lot_id, lot, seller)
            await interaction.followup.send(embed=embed, view=view, ephemeral=True)
        except Exception as e:
            logger.exception("Ошибка lot_callback")
            await interaction.followup.send("❌ Ошибка при выборе товара", ephemeral=True)

    async def close_callback(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        try:
            await interaction.delete_original_response()
        except Exception:
            pass

# ================= ДЕЙСТВИЯ С ТОВАРОМ =================
class LotActionView(discord.ui.View):
    def __init__(self, lot_id: int, lot, seller):
        super().__init__(timeout=None)
        self.lot_id = lot_id
        self.lot = lot
        self.seller = seller
        buy_button = discord.ui.Button(label="🛒 Купить", style=discord.ButtonStyle.green)
        cancel_button = discord.ui.Button(label="❌ Отмена", style=discord.ButtonStyle.danger)
        buy_button.callback = self.buy_callback
        cancel_button.callback = self.cancel_callback
        self.add_item(buy_button)
        self.add_item(cancel_button)
    
    async def buy_callback(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        try:
            daily_count = await get_daily_purchase_count(interaction.user.id)
            if daily_count >= DAILY_PURCHASE_LIMIT:
                await interaction.followup.send(f"❌ Достигнут дневной лимит покупок ({DAILY_PURCHASE_LIMIT} в день).", ephemeral=True)
                return
            
            lot_data = await db.get_lot(self.lot_id)
            if not lot_data:
                await interaction.followup.send("❌ Товар не найден!", ephemeral=True)
                return
            
            if lot_data.stock != -1 and lot_data.stock <= 0:
                await interaction.followup.send("❌ Товар закончился!", ephemeral=True)
                return
            
            if await has_user_bought(interaction.user.id, self.lot_id):
                await interaction.followup.send("❌ Вы уже покупали этот товар!", ephemeral=True)
                return
            
            if await db.is_blacklisted(interaction.user.id):
                await interaction.followup.send("❌ Вы в чёрном списке.", ephemeral=True)
                return
            
            key = (interaction.user.id, self.lot_id)
            if key in active_orders:
                await interaction.followup.send("⚠️ Заказ уже создаётся, подождите.", ephemeral=True)
                return
            
            now = datetime.now()
            last = user_ticket_cooldown.get(interaction.user.id)
            if last and (now - last).total_seconds() < TICKET_COOLDOWN_SECONDS:
                await interaction.followup.send(f"⏳ Подождите {TICKET_COOLDOWN_SECONDS} секунд.", ephemeral=True)
                return
            
            active_orders.add(key)
            user_ticket_cooldown[interaction.user.id] = now
            try:
                config = get_config(interaction.guild_id)
                customer_role_id = config["roles"].get("customer") if config else None
                customer_role = interaction.guild.get_role(customer_role_id) if customer_role_id else None
                if customer_role and customer_role not in interaction.user.roles:
                    await interaction.followup.send("⚠️ Пройдите верификацию в канале #верификация", ephemeral=True)
                    return
                
                category = discord.utils.get(interaction.guild.categories, name=TICKET_CATEGORY_NAME)
                if not category:
                    category = await interaction.guild.create_category(TICKET_CATEGORY_NAME)
                
                safe_lot = re.sub(r"[^a-zA-Z0-9а-яА-Я_-]", "-", self.lot.name.lower())[:15]
                safe_user = re.sub(r"[^a-zA-Z0-9_-]", "-", interaction.user.name.lower())[:15]
                channel_name = f"заказ-{safe_lot}-{safe_user}"
                
                seller_role_id = config["roles"].get("seller") if config else None
                admin_role_id = config["roles"].get("admin") if config else None
                
                overwrites = {
                    interaction.guild.default_role: discord.PermissionOverwrite(read_messages=False),
                    interaction.user: discord.PermissionOverwrite(read_messages=True, send_messages=True),
                    interaction.guild.me: discord.PermissionOverwrite(read_messages=True, send_messages=True),
                }
                if self.seller:
                    overwrites[self.seller] = discord.PermissionOverwrite(read_messages=True, send_messages=True)
                if seller_role_id:
                    seller_role = interaction.guild.get_role(seller_role_id)
                    if seller_role:
                        overwrites[seller_role] = discord.PermissionOverwrite(read_messages=True, send_messages=True)
                if admin_role_id:
                    admin_role = interaction.guild.get_role(admin_role_id)
                    if admin_role:
                        overwrites[admin_role] = discord.PermissionOverwrite(read_messages=True, send_messages=True)
                
                ticket_channel = await interaction.guild.create_text_channel(channel_name, category=category, overwrites=overwrites)
                
                voice_channel = None
                try:
                    voice_overwrites = {
                        interaction.guild.default_role: discord.PermissionOverwrite(view_channel=False),
                        interaction.user: discord.PermissionOverwrite(view_channel=True, connect=True, speak=True),
                        interaction.guild.me: discord.PermissionOverwrite(view_channel=True, connect=True),
                    }
                    if admin_role_id:
                        admin_role = interaction.guild.get_role(admin_role_id)
                        if admin_role:
                            voice_overwrites[admin_role] = discord.PermissionOverwrite(view_channel=True, connect=True, speak=True)
                    voice_channel = await interaction.guild.create_voice_channel(f"🎙️-{safe_user}", category=category, overwrites=voice_overwrites)
                except Exception:
                    pass
                
                await db.add_ticket(channel_id=ticket_channel.id, user_id=interaction.user.id, guild_id=interaction.guild_id, voice_channel_id=voice_channel.id if voice_channel else None)
                
                if lot_data.stock == -1:
                    stock_display = "♾️ Бесконечно"
                elif lot_data.stock > 0:
                    stock_display = f"📦 Осталось: {lot_data.stock} шт."
                else:
                    stock_display = "❌ Нет в наличии"
                
                embed = discord.Embed(
                    title="🛒 НОВЫЙ ЗАКАЗ",
                    description=f"**Покупатель:** {interaction.user.mention}\n"
                                f"**Товар:** {self.lot.name}\n"
                                f"**Цена:** {self.lot.price}\n"
                                f"{stock_display}\n\n"
                                f"**📝 Детальное описание:**\n{self.lot.full_description}\n\n"
                                f"**📝 Инструкция для продавца:**\n"
                                f"1. Расскажите покупателю о товаре.\n"
                                f"2. Отправьте реквизиты для оплаты.\n"
                                f"3. После оплаты передайте товар.\n"
                                f"4. Закройте тикет кнопкой ниже.\n\n"
                                f"**💰 Покупатель:** переведите деньги, напишите «Оплатил», получите товар.",
                    color=discord.Color.green()
                )
                
                if voice_channel:
                    embed.add_field(name="🎙️ Голосовой канал", value=voice_channel.mention, inline=False)
                
                seller_mention = self.seller.mention if self.seller else "Продавец"
                await ticket_channel.send(content=seller_mention, embed=embed)
                await ticket_channel.send(f"{interaction.user.mention}, ожидайте ответа продавца.")
                
                if self.lot.role_id:
                    role = interaction.guild.get_role(self.lot.role_id)
                    if role:
                        await interaction.user.add_roles(role)
                
                await add_purchase(interaction.user.id, self.lot_id, self.lot.price)
                
                if lot_data.stock != -1:
                    await update_stock(self.lot_id, -1)
                
                price_num = await parse_price_rub(self.lot.price)
                revenue = int(price_num) if price_num else 0
                await db.update_stats(self.lot.seller_id, sales_inc=1, revenue_inc=revenue)
                
                ticket_view = OrderCloseView(ticket_channel.id, interaction.user.id, self.seller, voice_channel.id if voice_channel else None)
                await ticket_channel.send("✅ **Для завершения используйте кнопки ниже:**", view=ticket_view)
                
                try:
                    await interaction.delete_original_response()
                except Exception:
                    pass
                
                await interaction.followup.send(f"✅ Заказ создан! Перейдите в {ticket_channel.mention}", ephemeral=True)
                
            finally:
                active_orders.discard(key)
        except Exception as e:
            logger.exception("Ошибка buy_callback")
            active_orders.discard((interaction.user.id, self.lot_id))
            await interaction.followup.send("❌ Ошибка при создании заказа", ephemeral=True)
    
    async def cancel_callback(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        await interaction.followup.send("❌ Покупка отменена", ephemeral=True)
        try:
            await interaction.delete_original_response()
        except Exception:
            pass

class OrderCloseView(discord.ui.View):
    def __init__(self, ticket_channel_id: int, buyer_id: int, seller, voice_channel_id: Optional[int] = None):
        super().__init__(timeout=None)
        self.ticket_channel_id = ticket_channel_id
        self.buyer_id = buyer_id
        self.seller = seller
        self.voice_channel_id = voice_channel_id
    @discord.ui.button(label="🔒 Закрыть заказ", style=discord.ButtonStyle.danger, custom_id="close_order_btn")
    async def close_order(self, interaction: discord.Interaction, button: discord.ui.Button):
        is_buyer = interaction.user.id == self.buyer_id
        is_seller = self.seller and interaction.user.id == self.seller.id
        if not (is_buyer or is_seller or is_admin_member(interaction.user)):
            await interaction.response.send_message("❌ Только покупатель, продавец или админ могут закрыть заказ.", ephemeral=True)
            return
        await interaction.response.defer()
        ticket_channel = interaction.guild.get_channel(self.ticket_channel_id)
        if ticket_channel:
            await db.close_ticket(self.ticket_channel_id)
        if self.voice_channel_id:
            vc = interaction.guild.get_channel(self.voice_channel_id)
            if vc:
                try:
                    await vc.delete()
                except Exception:
                    pass
        await interaction.followup.send("🔒 Заказ закрыт. Канал будет удалён через 24 часа.", ephemeral=False)

# ================= СИСТЕМА ОТЗЫВОВ =================
class ReviewModal(discord.ui.Modal, title="Оставить отзыв"):
    rating = discord.ui.TextInput(label="Оценка (1-5)", placeholder="1-5", min_length=1, max_length=1, required=True)
    comment = discord.ui.TextInput(label="Комментарий", placeholder="Ваш отзыв о товаре/продавце", style=discord.TextStyle.paragraph, max_length=4000, required=True)
    def __init__(self, seller, product: str, lot_id: int):
        super().__init__()
        self.seller = seller
        self.product = product
        self.lot_id = lot_id
    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        if self.rating.value not in '12345':
            await interaction.followup.send("❌ Оценка должна быть от 1 до 5", ephemeral=True)
            return
        rating = int(self.rating.value)
        stars = "⭐" * rating + "☆" * (5 - rating)
        config = get_config(interaction.guild_id)
        review_channel_id = config.get("review_channel") if config else None
        review_channel = interaction.guild.get_channel(review_channel_id) if review_channel_id else None
        if not review_channel:
            await interaction.followup.send("❌ Канал отзывов не найден.", ephemeral=True)
            return
        if self.seller:
            await add_review(interaction.user.id, self.seller.id, self.lot_id, rating, self.comment.value)
        embed = discord.Embed(title="📝 Отзыв о покупке", description=f"**Товар:** {self.product}\n**Оценка:** {stars} ({rating}/5)\n\n**Отзыв:**\n{self.comment.value}", color=discord.Color.gold())
        embed.set_author(name=interaction.user.display_name, icon_url=interaction.user.avatar.url if interaction.user.avatar else None)
        seller_name = self.seller.name if self.seller else "Неизвестен"
        embed.set_footer(text=f"Покупатель: {interaction.user.name} | Продавец: {seller_name}")
        embed.timestamp = datetime.now(timezone.utc)
        if self.seller:
            await update_seller_review_catalog(interaction.guild, review_channel, self.seller)
        else:
            await review_channel.send(embed=embed)
        await interaction.followup.send("✅ Спасибо за отзыв!", ephemeral=True)
        if self.seller:
            try:
                await self.seller.send(f"📢 {interaction.user.mention} оставил отзыв о товаре **{self.product}**!\nОценка: {stars}")
            except Exception:
                pass

async def update_seller_review_catalog(guild: discord.Guild, review_channel: discord.TextChannel, seller: discord.Member):
    reviews = await db.get_seller_reviews(seller.id, 20)
    avg_rating = await db.get_seller_rating(seller.id)
    embed = discord.Embed(title=f"⭐ Отзывы о {seller.display_name}", description=f"**Средний рейтинг:** {'⭐' * round(avg_rating)}{'☆' * (5 - round(avg_rating))} ({avg_rating}/5)\n**Всего отзывов:** {len(reviews)}", color=discord.Color.gold())
    embed.set_thumbnail(url=seller.avatar.url if seller.avatar else None)
    for rev in reviews[:10]:
        buyer = guild.get_member(rev['user_id'])
        buyer_name = buyer.display_name if buyer else f"ID:{rev['user_id']}"
        stars = "⭐" * rev['rating'] + "☆" * (5 - rev['rating'])
        embed.add_field(name=f"{stars} от {buyer_name}", value=rev['comment'][:200], inline=False)
    existing = await db.get_seller_review_message(seller.id)
    if existing:
        try:
            msg = await review_channel.fetch_message(existing['message_id'])
            await msg.edit(embed=embed)
            return
        except (discord.NotFound, discord.HTTPException):
            pass
    msg = await review_channel.send(content=f"**📋 Продавец: {seller.mention}**", embed=embed)
    await db.set_seller_review_message(seller.id, msg.id, review_channel.id)

# ================= ПРОФИЛЬ / СТАТУС =================
async def build_status_embed(guild: discord.Guild, user: discord.Member) -> discord.Embed:
    purchases = await db.get_user_purchases(user.id)
    user_reviews = await db.get_user_reviews(user.id)
    stats = await db.get_stats(user.id)
    ref_count = await db.get_referral_count(user.id)
    embed = discord.Embed(title=f"👤 Профиль участника: {user.display_name}", color=discord.Color.blue())
    embed.set_thumbnail(url=user.avatar.url if user.avatar else None)
    embed.add_field(name="🛒 Статистика покупок", value=f"Всего покупок: **{len(purchases)}**\nПродаж: **{stats['sales'] if stats else 0}**\nВыручка: **{stats['revenue'] if stats else 0} ₽**", inline=False)
    ref_link = f"https://discord.gg/ref_{user.id}"
    embed.add_field(name="🔗 Реферальная ссылка", value=f"`{ref_link}`\nПривёл: **{ref_count}** пользователей", inline=False)
    if purchases:
        purchase_text = ""
        for p in purchases[:5]:
            lot_item = db.lots_cache.get(p['lot_id'])
            lot_name = lot_item.name if lot_item else f"Товар #{p['lot_id']}"
            purchase_text += f"• {lot_name} — {p['price']} ({p['created_at'][:10]})\n"
        embed.add_field(name="📦 Последние покупки", value=purchase_text, inline=False)
    if user_reviews:
        reviews_text = "".join([f"{'⭐' * r['rating']} — {r['comment'][:60]}...\n" for r in user_reviews[:3]])
        embed.add_field(name="📝 Последние отзывы", value=reviews_text, inline=False)
    return embed

async def show_user_status(interaction: discord.Interaction, target: discord.Member = None):
    user = target or interaction.user
    await interaction.response.defer(ephemeral=True)
    embed = await build_status_embed(interaction.guild, user)
    await interaction.followup.send(embed=embed, ephemeral=True)

# ================= КОМАНДЫ =================
async def owner_only(interaction: discord.Interaction) -> bool:
    if not is_owner(interaction):
        await interaction.response.send_message("❌ Только для Owner", ephemeral=True)
        return False
    return True

@bot.tree.command(name='setup_verify', description='[OWNER] Пересоздать панель верификации')
async def setup_verify_cmd(interaction: discord.Interaction):
    if not await owner_only(interaction):
        return
    await interaction.response.defer(ephemeral=True)
    config = get_config(interaction.guild_id)
    if not config:
        await interaction.followup.send("❌ Сервер не настроен", ephemeral=True)
        return
    channel_id = config.get("verify_channel")
    if channel_id:
        channel = interaction.guild.get_channel(channel_id)
        if channel:
            async for msg in channel.history(limit=50):
                if msg.author == bot.user:
                    await msg.delete()
            embed = discord.Embed(title="🔒 Верификация", description="Нажми на кнопку ниже, чтобы получить доступ к серверу.", color=discord.Color.gold())
            await channel.send(embed=embed, view=VerifyView())
    await interaction.followup.send("✅ Панель верификации обновлена!", ephemeral=True)

@bot.tree.command(name='profile', description='Посмотреть профиль пользователя')
@app_commands.describe(target="Пользователь")
async def profile_cmd(interaction: discord.Interaction, target: Optional[discord.Member] = None):
    await show_user_status(interaction, target)

@bot.tree.command(name='setup_ticket_panel', description='[OWNER] Создать панель тикетов')
async def setup_ticket_panel(interaction: discord.Interaction):
    if not await owner_only(interaction):
        return
    await interaction.response.defer(ephemeral=True)
    channel = interaction.guild.get_channel(TICKET_CHANNEL_ID)
    if not channel:
        await interaction.followup.send(f"❌ Канал {TICKET_CHANNEL_ID} не найден", ephemeral=True)
        return
    async for msg in channel.history(limit=50):
        if msg.author == bot.user:
            await msg.delete()
    embed = discord.Embed(title="🎫 Служба поддержки", description="**Нажмите на кнопку ниже, чтобы создать обращение.**\n\n📌 После решения тикет будет закрыт и удалён через 7 дней.", color=discord.Color.blue())
    await channel.send(embed=embed, view=TicketCreateButton())
    await interaction.followup.send(f"✅ Панель тикетов создана в {channel.mention}", ephemeral=True)

@bot.tree.command(name='restore_backup', description='[OWNER] Восстановить магазин из бэкапа')
async def restore_backup(interaction: discord.Interaction):
    if not await owner_only(interaction):
        return
    await interaction.response.defer(ephemeral=True)
    result = await db.restore_from_backup_channel(BACKUP_CHANNEL_ID, bot)
    if result:
        await send_or_update_shop(interaction.guild)
        await interaction.followup.send("✅ Магазин восстановлен из последнего бэкапа!", ephemeral=True)
    else:
        await interaction.followup.send("❌ Не удалось найти бэкап в канале.", ephemeral=True)

@bot.tree.command(name='add_category', description='[OWNER] Добавить категорию')
@app_commands.describe(name="Название", emoji="Эмодзи")
async def add_category_cmd(interaction: discord.Interaction, name: str, emoji: str = "📁"):
    if not await owner_only(interaction):
        return
    await interaction.response.defer(ephemeral=True)
    cat_id = await db.add_category(name=name, emoji=emoji)
    await db.refresh_cache()
    await interaction.followup.send(f"✅ Категория `{emoji} {name}` добавлена (ID: {cat_id})", ephemeral=True)
    await send_or_update_shop(interaction.guild)

@bot.tree.command(name='list_lots', description='[OWNER] Обновить магазин')
async def list_lots(interaction: discord.Interaction):
    if not await owner_only(interaction):
        return
    await interaction.response.defer(ephemeral=True)
    await send_or_update_shop(interaction.guild)
    await interaction.followup.send("✅ Магазин обновлён", ephemeral=True)

@bot.tree.command(name='restore_server_backup', description='[OWNER] Восстановить сервер из JSON бэкапа')
async def restore_backup_cmd(interaction: discord.Interaction, backup_json: str, clear_existing: str = "нет"):
    if not is_owner(interaction):
        await interaction.response.send_message("❌ Только для Owner", ephemeral=True)
        return
    
    await interaction.response.defer(ephemeral=True, thinking=True)
    
    try:
        backup_data = json.loads(backup_json)
        guild = interaction.guild
        clear_flag = clear_existing.lower().strip() in ["да", "yes", "true", "1"]
        
        if clear_flag:
            for channel in guild.channels:
                try:
                    await channel.delete()
                    await asyncio.sleep(0.3)
                except:
                    pass
            
            for role in guild.roles:
                if role.name == "@everyone" or role.managed:
                    continue
                try:
                    await role.delete()
                    await asyncio.sleep(0.3)
                except:
                    pass
        
        # Восстанавливаем настройки
        if backup_data.get("name"):
            await guild.edit(name=backup_data["name"])
        
        # Восстанавливаем роли
        role_positions = {}
        for role_data in backup_data.get("roles", []):
            try:
                new_role = await guild.create_role(
                    name=role_data["name"],
                    color=role_data["color"],
                    permissions=discord.Permissions(role_data["permissions"]),
                    hoist=role_data["hoist"],
                    mentionable=role_data["mentionable"]
                )
                role_positions[role_data["position"]] = new_role
                await asyncio.sleep(0.3)
            except:
                pass
        
        for pos in sorted(role_positions.keys(), reverse=True):
            try:
                await role_positions[pos].edit(position=pos)
            except:
                pass
        
        # Восстанавливаем категории и каналы
        category_map = {}
        for cat_data in backup_data.get("categories", []):
            try:
                new_category = await guild.create_category(
                    name=cat_data["name"],
                    position=cat_data["position"]
                )
                category_map[cat_data["name"]] = new_category
                
                for channel_data in cat_data.get("channels", []):
                    try:
                        if channel_data["type"] == "text":
                            await new_category.create_text_channel(
                                name=channel_data["name"],
                                position=channel_data["position"],
                                topic=channel_data.get("topic"),
                                slowmode_delay=channel_data.get("slowmode_delay", 0),
                                nsfw=channel_data.get("nsfw", False)
                            )
                        elif channel_data["type"] == "voice":
                            await new_category.create_voice_channel(
                                name=channel_data["name"],
                                position=channel_data["position"],
                                bitrate=min(channel_data.get("bitrate", 64000), 96000),
                                user_limit=channel_data.get("user_limit", 0)
                            )
                        await asyncio.sleep(0.3)
                    except:
                        pass
                await asyncio.sleep(0.3)
            except:
                pass
        
        for channel_data in backup_data.get("channels", []):
            try:
                if channel_data["type"] == "text":
                    await guild.create_text_channel(
                        name=channel_data["name"],
                        position=channel_data["position"],
                        topic=channel_data.get("topic"),
                        slowmode_delay=channel_data.get("slowmode_delay", 0),
                        nsfw=channel_data.get("nsfw", False)
                    )
                elif channel_data["type"] == "voice":
                    await guild.create_voice_channel(
                        name=channel_data["name"],
                        position=channel_data["position"],
                        bitrate=min(channel_data.get("bitrate", 64000), 96000),
                        user_limit=channel_data.get("user_limit", 0)
                    )
                await asyncio.sleep(0.3)
            except:
                pass
        
        await interaction.followup.send(f"✅ **Бэкап восстановлен на сервер `{guild.name}`!**", ephemeral=True)
        
    except json.JSONDecodeError:
        await interaction.followup.send("❌ Неверный формат JSON", ephemeral=True)
    except Exception as e:
        await interaction.followup.send(f"❌ Ошибка: {e}", ephemeral=True)

# ================= ФОНОВЫЕ ЗАДАЧИ =================
async def auto_cleanup_tickets():
    await bot.wait_until_ready()
    while not bot.is_closed():
        await asyncio.sleep(3600)
        try:
            seven_days_ago = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
            rows = await db.fetchall('SELECT * FROM tickets WHERE status = "closed" AND closed_at <= ?', (seven_days_ago,))
            for ticket in rows:
                guild = bot.get_guild(ticket['guild_id'])
                if not guild:
                    continue
                channel = guild.get_channel(ticket['channel_id'])
                if channel:
                    try:
                        await channel.delete()
                    except Exception:
                        pass
                if ticket.get('voice_channel_id'):
                    vc = guild.get_channel(ticket['voice_channel_id'])
                    if vc:
                        try:
                            await vc.delete()
                        except Exception:
                            pass
                await db.delete_ticket_record(ticket['channel_id'])
        except Exception:
            pass

async def auto_update_currency():
    await bot.wait_until_ready()
    while not bot.is_closed():
        try:
            await fetch_currency_rates()
        except Exception:
            pass
        await asyncio.sleep(21600)

async def cleanup_spam_cache():
    await bot.wait_until_ready()
    while not bot.is_closed():
        await asyncio.sleep(86400)
        cutoff = datetime.now() - timedelta(hours=2)
        stale = [uid for uid, ts in user_mention_last_reset.items() if ts < cutoff]
        for uid in stale:
            user_mention_count.pop(uid, None)
            user_mention_last_reset.pop(uid, None)

# ================= ОТПРАВКА ПАНЕЛЕЙ =================
async def _send_verify_panel(guild_config: dict):
    channel_id = guild_config.get("verify_channel")
    if not channel_id:
        return
    channel = await _fetch_channel_safe(channel_id)
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
        logger.error(f"Ошибка отправки верификации: {e}")

async def _send_ticket_panel_from_config(guild_config: dict):
    channel_id = guild_config.get("ticket_channel")
    if not channel_id:
        return
    channel = await _fetch_channel_safe(channel_id)
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
    embed = discord.Embed(title="🎫 Служба поддержки", description="**Нажмите на кнопку ниже, чтобы создать обращение.**\n\n📌 Удаление тикета из архива через 7 дней", color=discord.Color.blue())
    embed.set_footer(text=f"{guild_config['name']} — Техническая поддержка")
    try:
        await channel.send(embed=embed, view=TicketCreateButton())
    except Exception as e:
        logger.error(f"Ошибка отправки тикетов: {e}")

async def _send_status_channel_panel(guild: discord.Guild, guild_config: dict):
    channel_id = guild_config.get("status_channel")
    if not channel_id:
        return
    channel = await _fetch_channel_safe(channel_id)
    if not channel:
        return
    try:
        async for msg in channel.history(limit=30):
            if msg.author == bot.user:
                try:
                    await msg.delete()
                except Exception:
                    pass
    except Exception:
        pass
    owner_member = guild.get_member(OWNER_ID) or guild.owner
    if not owner_member:
        return
    embed = await build_status_embed(guild, owner_member)
    embed.title = "📊 Текущий статус системы"
    embed.add_field(name="🌐 Статус хостинга", value="🟢 Система запущена на Railway\n📅 " + datetime.now().strftime("%d.%m.%Y %H:%M:%S"), inline=False)
    try:
        await channel.send(embed=embed)
    except Exception as e:
        logger.error(f"Ошибка отправки статуса: {e}")

async def _assign_unverified_roles():
    for guild in bot.guilds:
        config = get_config(guild.id)
        if not config:
            continue
        unverified_role_id = config["roles"].get("unverified")
        if not unverified_role_id:
            continue
        unverified_role = guild.get_role(unverified_role_id)
        if not unverified_role:
            continue
        customer_role = guild.get_role(config["roles"].get("customer")) if config.get("roles", {}).get("customer") else None
        buyer_role = guild.get_role(config["roles"].get("buyer")) if config.get("roles", {}).get("buyer") else None
        for member in guild.members:
            if member.bot or is_admin_member(member):
                continue
            has_role = (customer_role and customer_role in member.roles) or (buyer_role and buyer_role in member.roles)
            if not has_role and unverified_role not in member.roles:
                try:
                    await member.add_roles(unverified_role)
                except Exception:
                    pass

# ================= АДМИН ПАНЕЛЬ =================
class AdminPanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
    
    @discord.ui.button(label="🛠️ Управление", style=discord.ButtonStyle.primary, custom_id="admin_main_menu", row=0)
    async def main_menu_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_owner(interaction):
            await interaction.response.send_message("❌ Только для Owner", ephemeral=True)
            return
        
        embed = discord.Embed(
            title="🛠️ Админ панель",
            description="Выберите раздел для управления",
            color=discord.Color.blurple()
        )
        embed.add_field(name="📁 Категории", value="Управление категориями (добавление, удаление, список)", inline=False)
        embed.add_field(name="🛒 Товары", value="Управление товарами (добавление, удаление, список)", inline=False)
        embed.add_field(name="⚙️ Настройки", value="Статистика, обновление магазина, бэкап", inline=False)
        
        view = AdminMainMenu()
        await interaction.response.edit_message(embed=embed, view=view)

class AdminMainMenu(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
    
    @discord.ui.button(label="📁 Категории", style=discord.ButtonStyle.success, custom_id="admin_cats_menu", row=0)
    async def categories_menu_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_owner(interaction):
            await interaction.response.send_message("❌ Только для Owner", ephemeral=True)
            return
        
        embed = discord.Embed(title="📁 Управление категориями", color=discord.Color.blue())
        embed.add_field(name="➕ Добавить категорию", value="Создать новую категорию", inline=False)
        embed.add_field(name="🗑️ Удалить категорию", value="Удалить существующую категорию", inline=False)
        embed.add_field(name="📋 Список категорий", value="Показать все категории с ID", inline=False)
        
        view = CategoriesMenuView()
        await interaction.response.edit_message(embed=embed, view=view)
    
    @discord.ui.button(label="🛒 Товары", style=discord.ButtonStyle.success, custom_id="admin_lots_menu", row=0)
    async def lots_menu_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_owner(interaction):
            await interaction.response.send_message("❌ Только для Owner", ephemeral=True)
            return
        
        embed = discord.Embed(title="🛒 Управление товарами", color=discord.Color.green())
        embed.add_field(name="➕ Добавить товар", value="Создать новый товар", inline=False)
        embed.add_field(name="🗑️ Удалить товар", value="Удалить существующий товар", inline=False)
        embed.add_field(name="📋 Список товаров", value="Показать все товары с ID", inline=False)
        
        view = LotsMenuView()
        await interaction.response.edit_message(embed=embed, view=view)
    
    @discord.ui.button(label="⚙️ Настройки", style=discord.ButtonStyle.success, custom_id="admin_settings_menu", row=0)
    async def settings_menu_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_owner(interaction):
            await interaction.response.send_message("❌ Только для Owner", ephemeral=True)
            return
        
        categories = db.categories_cache
        total_lots = len(db.lots_cache)
        
        embed = discord.Embed(title="⚙️ Настройки и статистика", color=discord.Color.gold())
        embed.add_field(name="📊 Статистика", value=f"📁 Категорий: {len(categories)}\n🛒 Товаров: {total_lots}", inline=False)
        embed.add_field(name="🔄 Обновить магазин", value="Принудительное обновление магазина", inline=False)
        embed.add_field(name="💾 Бэкап", value="Создать резервную копию", inline=False)
        
        view = SettingsMenuView()
        await interaction.response.edit_message(embed=embed, view=view)
    
    @discord.ui.button(label="◀️ Назад", style=discord.ButtonStyle.secondary, custom_id="admin_back_main", row=1)
    async def back_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        embed = discord.Embed(
            title="🛠️ Админ панель",
            description="Нажмите на кнопку ниже для открытия меню управления",
            color=discord.Color.blurple()
        )
        view = AdminPanelView()
        await interaction.response.edit_message(embed=embed, view=view)

class CategoriesMenuView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
    
    @discord.ui.button(label="➕ Добавить категорию", style=discord.ButtonStyle.success, custom_id="cat_add", row=0)
    async def add_category_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_owner(interaction):
            await interaction.response.send_message("❌ Только для Owner", ephemeral=True)
            return
        
        class AddCategoryModal(discord.ui.Modal, title="Добавить категорию"):
            name = discord.ui.TextInput(label="Название", placeholder="Введите название...", required=True)
            emoji = discord.ui.TextInput(label="Эмодзи", placeholder="📁", required=False, default="📁")
            
            async def on_submit(self, i: discord.Interaction):
                await i.response.defer(ephemeral=True)
                cat_id = await db.add_category(name=self.name.value, emoji=self.emoji.value)
                await db.refresh_cache()
                await i.followup.send(f"✅ Категория `{self.emoji.value} {self.name.value}` добавлена (ID: {cat_id})", ephemeral=True)
                config = get_config(i.guild_id)
                if config and config.get("shop_channel"):
                    await send_or_update_shop(i.guild)
        
        await interaction.response.send_modal(AddCategoryModal())
    
    @discord.ui.button(label="🗑️ Удалить категорию", style=discord.ButtonStyle.danger, custom_id="cat_del", row=0)
    async def delete_category_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_owner(interaction):
            await interaction.response.send_message("❌ Только для Owner", ephemeral=True)
            return
        
        class DeleteCategoryModal(discord.ui.Modal, title="Удалить категорию"):
            cat_id = discord.ui.TextInput(label="ID категории", placeholder="Введите ID...", required=True)
            
            async def on_submit(self, i: discord.Interaction):
                await i.response.defer(ephemeral=True)
                cat_id = int(self.cat_id.value)
                category = await db.get_category(cat_id)
                if not category:
                    await i.followup.send(f"❌ Категория `{cat_id}` не найдена", ephemeral=True)
                    return
                await db.delete_category(cat_id)
                await db.refresh_cache()
                await i.followup.send(f"✅ Категория `{category.emoji} {category.name}` удалена", ephemeral=True)
                config = get_config(i.guild_id)
                if config and config.get("shop_channel"):
                    await send_or_update_shop(i.guild)
        
        await interaction.response.send_modal(DeleteCategoryModal())
    
    @discord.ui.button(label="📋 Список категорий", style=discord.ButtonStyle.primary, custom_id="cat_list", row=0)
    async def list_categories_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_owner(interaction):
            await interaction.response.send_message("❌ Только для Owner", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        await db.refresh_cache()
        categories = db.categories_cache
        if not categories:
            await interaction.followup.send("📭 Нет категорий", ephemeral=True)
            return
        embed = discord.Embed(title="📁 Список категорий", color=discord.Color.blue())
        for cat in categories.values():
            embed.add_field(name=f"{cat.emoji} {cat.name}", value=f"**ID:** `{cat.id}`\n**Товаров:** {len(cat.lots)}", inline=False)
        await interaction.followup.send(embed=embed, ephemeral=True)
    
    @discord.ui.button(label="◀️ Назад", style=discord.ButtonStyle.secondary, custom_id="cat_back", row=1)
    async def back_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        embed = discord.Embed(
            title="🛠️ Админ панель",
            description="Выберите раздел для управления",
            color=discord.Color.blurple()
        )
        embed.add_field(name="📁 Категории", value="Управление категориями (добавление, удаление, список)", inline=False)
        embed.add_field(name="🛒 Товары", value="Управление товарами (добавление, удаление, список)", inline=False)
        embed.add_field(name="⚙️ Настройки", value="Статистика, обновление магазина, бэкап", inline=False)
        
        view = AdminMainMenu()
        await interaction.response.edit_message(embed=embed, view=view)

class LotsMenuView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
    
    @discord.ui.button(label="➕ Добавить товар", style=discord.ButtonStyle.success, custom_id="lot_add", row=0)
    async def add_lot_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_owner(interaction):
            await interaction.response.send_message("❌ Только для Owner", ephemeral=True)
            return
        
        await db.refresh_cache()
        categories = db.categories_cache
        if not categories:
            await interaction.response.send_message("❌ Сначала создайте категорию", ephemeral=True)
            return
        
        class AddLotModal(discord.ui.Modal, title="Добавить товар"):
            name = discord.ui.TextInput(label="Название", placeholder="Введите название...", required=True, max_length=100)
            price = discord.ui.TextInput(label="Цена", placeholder="1000 ₽", required=True, max_length=50)
            seller = discord.ui.TextInput(label="Продавец (ID или @ник)", placeholder="Введите ID пользователя или @ник", required=True, max_length=100)
            full_desc = discord.ui.TextInput(label="Описание", placeholder="Подробное описание товара...", style=discord.TextStyle.paragraph, required=False, max_length=2000)
            stock = discord.ui.TextInput(label="Количество", placeholder="0 (безлимит: -1)", required=False, default="0")
            
            async def on_submit(self, i: discord.Interaction):
                await i.response.defer(ephemeral=True)
                
                price_value = self.price.value.strip()
                if not price_value:
                    await i.followup.send("❌ Введите цену", ephemeral=True)
                    return
                
                try:
                    stock_val = int(self.stock.value) if self.stock.value else 0
                except:
                    stock_val = 0
                
                seller_input = self.seller.value.strip()
                seller_id = None
                
                if seller_input.startswith('<@') and seller_input.endswith('>'):
                    seller_id = int(seller_input.replace('<@', '').replace('>', '').replace('!', ''))
                elif seller_input.isdigit():
                    seller_id = int(seller_input)
                else:
                    member = i.guild.get_member_named(seller_input)
                    if member:
                        seller_id = member.id
                
                if not seller_id:
                    await i.followup.send("❌ Продавец не найден! Укажите корректный ID или @ник", ephemeral=True)
                    return
                
                seller_member = i.guild.get_member(seller_id)
                if not seller_member:
                    await i.followup.send(f"❌ Пользователь с ID {seller_id} не найден на сервере", ephemeral=True)
                    return
                
                class CategorySelectView(discord.ui.View):
                    def __init__(self, lot_name, lot_price, lot_full, lot_stock, seller_id_val, cats):
                        super().__init__(timeout=60)
                        self.lot_name = lot_name
                        self.lot_price = lot_price
                        self.lot_full = lot_full
                        self.lot_stock = lot_stock
                        self.seller_id_val = seller_id_val
                        self.cats = cats
                        
                        options = []
                        for cat in self.cats.values():
                            options.append(discord.SelectOption(label=cat.name, value=str(cat.id), emoji=cat.emoji))
                        
                        select = discord.ui.Select(placeholder="📁 Выберите категорию", options=options)
                        select.callback = self.select_callback
                        self.add_item(select)
                    
                    async def select_callback(self, select_interaction: discord.Interaction):
                        cat_id = int(select_interaction.data['values'][0])
                        lot_id = await db.add_lot(
                            name=self.lot_name,
                            price=self.lot_price,
                            short_description="",
                            full_description=self.lot_full or "",
                            seller_id=self.seller_id_val,
                            category_id=cat_id,
                            stock=self.lot_stock
                        )
                        await db.refresh_cache()
                        seller_mention = f"<@{self.seller_id_val}>"
                        stock_text = "♾️ Бесконечно" if self.lot_stock == -1 else f"{self.lot_stock} шт."
                        await select_interaction.response.send_message(
                            f"✅ Товар **{self.lot_name}** добавлен!\n"
                            f"💰 Цена: {self.lot_price}\n"
                            f"👤 Продавец: {seller_mention}\n"
                            f"📦 Количество: {stock_text}\n"
                            f"🆔 ID товара: `{lot_id}`",
                            ephemeral=True
                        )
                        config = get_config(select_interaction.guild_id)
                        if config and config.get("shop_channel"):
                            await send_or_update_shop(select_interaction.guild)
                        self.stop()
                
                view = CategorySelectView(
                    lot_name=self.name.value,
                    lot_price=price_value,
                    lot_full=self.full_desc.value,
                    lot_stock=stock_val,
                    seller_id_val=seller_id,
                    cats=categories
                )
                await i.followup.send("📁 **Выберите категорию для товара:**", view=view, ephemeral=True)
        
        await interaction.response.send_modal(AddLotModal())
    
    @discord.ui.button(label="🗑️ Удалить товар", style=discord.ButtonStyle.danger, custom_id="lot_del", row=0)
    async def delete_lot_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_owner(interaction):
            await interaction.response.send_message("❌ Только для Owner", ephemeral=True)
            return
        
        await db.refresh_cache()
        lots = db.lots_cache
        if not lots:
            await interaction.response.send_message("❌ Нет товаров для удаления", ephemeral=True)
            return
        
        class DeleteLotView(discord.ui.View):
            def __init__(self):
                super().__init__(timeout=60)
                options = []
                for lot in list(lots.values())[:25]:
                    options.append(discord.SelectOption(label=f"{lot.name} (ID:{lot.lot_id})", value=str(lot.lot_id), description=f"Цена: {lot.price}"))
                
                select = discord.ui.Select(placeholder="Выберите товар для удаления", options=options)
                select.callback = self.select_callback
                self.add_item(select)
                
                cancel = discord.ui.Button(label="❌ Отмена", style=discord.ButtonStyle.secondary)
                cancel.callback = self.cancel_callback
                self.add_item(cancel)
            
            async def select_callback(self, select_interaction: discord.Interaction):
                lot_id = int(select_interaction.data['values'][0])
                lot = await db.get_lot(lot_id)
                if not lot:
                    await select_interaction.response.send_message("❌ Товар не найден", ephemeral=True)
                    return
                await db.delete_lot(lot_id)
                await db.refresh_cache()
                await select_interaction.response.send_message(f"✅ Товар **{lot.name}** удалён!", ephemeral=True)
                config = get_config(select_interaction.guild_id)
                if config and config.get("shop_channel"):
                    await send_or_update_shop(select_interaction.guild)
                self.stop()
            
            async def cancel_callback(self, select_interaction: discord.Interaction):
                await select_interaction.response.send_message("❌ Отменено", ephemeral=True)
                self.stop()
        
        view = DeleteLotView()
        await interaction.response.send_message("🗑️ **Выберите товар для удаления:**", view=view, ephemeral=True)
    
    @discord.ui.button(label="📋 Список товаров", style=discord.ButtonStyle.primary, custom_id="lot_list", row=0)
    async def list_lots_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_owner(interaction):
            await interaction.response.send_message("❌ Только для Owner", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        await db.refresh_cache()
        lots = db.lots_cache
        if not lots:
            await interaction.followup.send("📭 Нет товаров", ephemeral=True)
            return
        
        embed = discord.Embed(title="🛒 Список товаров", color=discord.Color.green())
        for lot in list(lots.values())[:20]:
            if lot.stock == -1:
                stock_text = "♾️ Бесконечно"
            elif lot.stock > 0:
                stock_text = f"📦 В наличии: {lot.stock} шт."
            else:
                stock_text = "❌ Нет в наличии"
            embed.add_field(name=f"{lot.name}", value=f"**ID:** `{lot.lot_id}`\n**Цена:** {lot.price}\n{stock_text}", inline=False)
        
        if len(lots) > 20:
            embed.set_footer(text=f"Показано 20 из {len(lots)} товаров")
        
        await interaction.followup.send(embed=embed, ephemeral=True)
    
    @discord.ui.button(label="◀️ Назад", style=discord.ButtonStyle.secondary, custom_id="lot_back", row=1)
    async def back_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        embed = discord.Embed(
            title="🛠️ Админ панель",
            description="Выберите раздел для управления",
            color=discord.Color.blurple()
        )
        embed.add_field(name="📁 Категории", value="Управление категориями (добавление, удаление, список)", inline=False)
        embed.add_field(name="🛒 Товары", value="Управление товарами (добавление, удаление, список)", inline=False)
        embed.add_field(name="⚙️ Настройки", value="Статистика, обновление магазина, бэкап", inline=False)
        
        view = AdminMainMenu()
        await interaction.response.edit_message(embed=embed, view=view)

class SettingsMenuView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
    
    @discord.ui.button(label="📊 Статистика", style=discord.ButtonStyle.primary, custom_id="settings_stats", row=0)
    async def stats_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_owner(interaction):
            await interaction.response.send_message("❌ Только для Owner", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        await db.refresh_cache()
        categories = db.categories_cache
        total_lots = len(db.lots_cache)
        
        embed = discord.Embed(title="📊 Статистика магазина", color=discord.Color.gold())
        embed.add_field(name="📁 Категорий", value=str(len(categories)), inline=True)
        embed.add_field(name="🛒 Товаров", value=str(total_lots), inline=True)
        await interaction.followup.send(embed=embed, ephemeral=True)
    
    @discord.ui.button(label="🔄 Обновить магазин", style=discord.ButtonStyle.success, custom_id="settings_refresh", row=0)
    async def refresh_shop_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_owner(interaction):
            await interaction.response.send_message("❌ Только для Owner", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        await db.refresh_cache()
        await send_or_update_shop(interaction.guild)
        await interaction.followup.send("✅ Магазин обновлён!", ephemeral=True)
    
    @discord.ui.button(label="💾 Создать бэкап", style=discord.ButtonStyle.secondary, custom_id="settings_backup", row=0)
    async def backup_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_owner(interaction):
            await interaction.response.send_message("❌ Только для Owner", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        result = await save_backup("manual (admin panel)")
        if result:
            await interaction.followup.send("✅ Бэкап создан и отправлен в канал!", ephemeral=True)
        else:
            await interaction.followup.send("❌ Ошибка создания бэкапа", ephemeral=True)
    
    @discord.ui.button(label="◀️ Назад", style=discord.ButtonStyle.secondary, custom_id="settings_back", row=1)
    async def back_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        embed = discord.Embed(
            title="🛠️ Админ панель",
            description="Выберите раздел для управления",
            color=discord.Color.blurple()
        )
        embed.add_field(name="📁 Категории", value="Управление категориями (добавление, удаление, список)", inline=False)
        embed.add_field(name="🛒 Товары", value="Управление товарами (добавление, удаление, список)", inline=False)
        embed.add_field(name="⚙️ Настройки", value="Статистика, обновление магазина, бэкап", inline=False)
        
        view = AdminMainMenu()
        await interaction.response.edit_message(embed=embed, view=view)

async def setup_admin_panel():
    channel = bot.get_channel(ADMIN_PANEL_CHANNEL_ID)
    if not channel:
        try:
            channel = await bot.fetch_channel(ADMIN_PANEL_CHANNEL_ID)
        except Exception as e:
            logger.error(f"Админ канал {ADMIN_PANEL_CHANNEL_ID} не найден: {e}")
            return
    
    try:
        async for msg in channel.history(limit=50):
            if msg.author == bot.user:
                await msg.delete()
    except Exception:
        pass
    
    embed = discord.Embed(
        title="🛠️ Админ панель",
        description="Нажмите на кнопку ниже для открытия меню управления",
        color=discord.Color.blurple()
    )
    view = AdminPanelView()
    await channel.send(embed=embed, view=view)
    logger.info("✅ Админ панель отправлена")

async def setup_panels():
    logger.info("⏳ setup_panels(): НАЧАЛО")
    try:
        await db.refresh_cache()
        logger.info(f"✅ setup_panels(): кэш обновлён (категорий: {len(db.categories_cache)})")
    except Exception as e:
        logger.error(f"❌ setup_panels(): ошибка обновления кэша: {e}")
        return
    for guild_id, g_config in CONFIG.items():
        guild = bot.get_guild(guild_id)
        if not guild:
            logger.warning(f"⚠️ Гильдия {guild_id} не найдена")
            continue
        logger.info(f"⏳ Настройка панелей для {g_config['name']}...")
        try:
            await _send_verify_panel(g_config)
        except Exception as e:
            logger.error(f"❌ Ошибка верификации: {e}")
        try:
            await _send_ticket_panel_from_config(g_config)
        except Exception as e:
            logger.error(f"❌ Ошибка тикетов: {e}")
        if g_config.get("shop_channel"):
            try:
                await send_or_update_shop(guild)
            except Exception as e:
                logger.error(f"❌ Ошибка магазина: {e}")
        if g_config.get("status_channel"):
            try:
                await _send_status_channel_panel(guild, g_config)
            except Exception as e:
                logger.error(f"❌ Ошибка статуса: {e}")
    try:
        await _assign_unverified_roles()
        logger.info("✅ Роли unverified назначены")
    except Exception as e:
        logger.error(f"❌ Ошибка назначения ролей: {e}")
    logger.info("✅ setup_panels(): ЗАВЕРШЕНО")

async def setup_ai_panel():
    channel = await _fetch_channel_safe(AI_CONVEYOR_CHANNEL_ID)
    if not channel:
        logger.warning(f"⚠️ Канал ИИ-конвейера {AI_CONVEYOR_CHANNEL_ID} не найден")
        return
    try:
        async for msg in channel.history(limit=30):
            if msg.author == bot.user:
                try:
                    await msg.delete()
                    await asyncio.sleep(0.5)
                except Exception:
                    pass
    except Exception as e:
        logger.warning(f"Не удалось очистить канал ИИ-конвейера: {e}")
    embed = discord.Embed(
        title="🤖 ИИ-Конвейер",
        description="**Нажми на кнопку ниже, выбери режим и задай вопрос.**\n\n"
                    "📋 **Режимы:** Кодинг, Советы, Оформление, Аналитика, Контент, Маркетинг, Бот-фичи\n"
                    "🧠 **Фишки:** История диалога, учёт структуры сервера, 4 API ключа в ротации\n"
                    "🏠 **Новое:** Можешь включить учёт структуры этого сервера — ИИ учтёт твои роли и каналы!",
        color=discord.Color.blurple()
    )
    view = View()
    view.add_item(StartAIButton())
    try:
        await channel.send(embed=embed, view=view)
        logger.info("✅ Панель ИИ-конвейера отправлена")
    except Exception as e:
        logger.error(f"❌ Ошибка отправки панели ИИ-конвейера: {e}")

# ================= ВЕБХУК СПАМ ПАНЕЛЬ (БЕЗОПАСНАЯ ВЕРСИЯ) =================
spam_stop_flag = False

class WebhookSpamPanel(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
    
    @discord.ui.button(label="📨 Discohook", style=discord.ButtonStyle.danger, row=0)
    async def discohook_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_owner(interaction):
            await interaction.response.send_message("❌ Только Owner", ephemeral=True)
            return
        modal = DiscohookModal()
        await interaction.response.send_modal(modal)
    
    @discord.ui.button(label="⚡ Быстрый", style=discord.ButtonStyle.primary, row=0)
    async def quick_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_owner(interaction):
            await interaction.response.send_message("❌ Только Owner", ephemeral=True)
            return
        modal = QuickSpamModal()
        await interaction.response.send_modal(modal)
    
    @discord.ui.button(label="🎭 Сменить имя", style=discord.ButtonStyle.secondary, row=1)
    async def name_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_owner(interaction):
            await interaction.response.send_message("❌ Только Owner", ephemeral=True)
            return
        modal = ChangeNameModal()
        await interaction.response.send_modal(modal)
    
    @discord.ui.button(label="🗑️ Очистка", style=discord.ButtonStyle.danger, row=1)
    async def clear_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_owner(interaction):
            await interaction.response.send_message("❌ Только Owner", ephemeral=True)
            return
        modal = ClearWebhookModal()
        await interaction.response.send_modal(modal)
    
    @discord.ui.button(label="🛑 Стоп", style=discord.ButtonStyle.danger, row=1)
    async def stop_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_owner(interaction):
            await interaction.response.send_message("❌ Только Owner", ephemeral=True)
            return
        global spam_stop_flag
        spam_stop_flag = True
        await interaction.response.send_message("🛑 Остановка", ephemeral=True)


class DiscohookModal(discord.ui.Modal, title="Discohook"):
    webhooks = discord.ui.TextInput(label="Вебхуки", placeholder="https://... (каждый с новой строки)", required=True, style=discord.TextStyle.paragraph)
    messages = discord.ui.TextInput(label="Сообщения", placeholder="Текст 1\nТекст 2", required=True, style=discord.TextStyle.paragraph)
    embed = discord.ui.TextInput(label="Эмбед", placeholder="Заголовок | Описание | URL | Футер", required=False)
    settings = discord.ui.TextInput(label="Настройки", placeholder="Циклы | Задержка(сек) | Цвет", required=False, default="0 | 1 | RANDOM")
    mention = discord.ui.TextInput(label="Упоминание", placeholder="ID / everyone / here", required=False)
    
    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        global spam_stop_flag
        spam_stop_flag = False
        
        webhook_list = [u.strip() for u in self.webhooks.value.split('\n') if u.strip()]
        if not webhook_list:
            await interaction.followup.send("❌ Введите вебхук", ephemeral=True)
            return
        
        msg_list = [m.strip() for m in self.messages.value.split('\n') if m.strip()]
        if not msg_list:
            await interaction.followup.send("❌ Введите сообщение", ephemeral=True)
            return
        
        parts = self.settings.value.split('|')
        try:
            loop_count = int(parts[0].strip()) if parts else 0
        except:
            loop_count = 0
        try:
            delay = max(1.0, float(parts[1].strip()) if len(parts) > 1 else 1.0)
        except:
            delay = 1.0
        
        color_str = parts[2].strip().upper() if len(parts) > 2 else "RANDOM"
        color_map = {"RED": 0xFF0000, "GREEN": 0x00FF00, "BLUE": 0x0000FF, "YELLOW": 0xFFFF00, "RANDOM": None}
        embed_color = color_map.get(color_str, 0xFF0000) if color_str != "RANDOM" else None
        
        infinite = loop_count == 0
        mention_val = self.mention.value.strip().lower()
        mention_id = None
        mention_type = None
        if mention_val:
            if mention_val.isdigit():
                mention_id = int(mention_val)
            elif mention_val == "everyone":
                mention_type = "everyone"
            elif mention_val == "here":
                mention_type = "here"
        
        embed_payload = None
        if self.embed.value:
            ep = self.embed.value.split('|')
            edata = {}
            if len(ep) > 0 and ep[0].strip():
                edata["title"] = ep[0].strip()
            if len(ep) > 1 and ep[1].strip():
                edata["description"] = ep[1].strip()
            if len(ep) > 2 and ep[2].strip():
                edata["image"] = {"url": ep[2].strip()}
            if len(ep) > 3 and ep[3].strip():
                edata["footer"] = {"text": ep[3].strip()}
            if edata:
                edata["color"] = embed_color if embed_color is not None else random.randint(0, 0xFFFFFF)
                embed_payload = [edata]
        
        total_sent = 0
        total_failed = 0
        cycle = 0
        await interaction.followup.send(f"🔄 Запуск (задержка {delay}с)...", ephemeral=True)
        
        async def worker(url):
            nonlocal total_sent, total_failed
            idx = 0
            while not spam_stop_flag and (infinite or cycle < loop_count):
                msg = msg_list[idx % len(msg_list)]
                idx += 1
                if mention_id:
                    msg = f"<@{mention_id}> {msg}"
                elif mention_type:
                    msg = f"@{mention_type} {msg}"
                payload = {"content": msg} if msg else {}
                if embed_payload:
                    payload["embeds"] = embed_payload
                try:
                    async with aiohttp.ClientSession() as session:
                        async with session.post(url, json=payload) as resp:
                            if resp.status in [200, 204]:
                                total_sent += 1
                            else:
                                total_failed += 1
                except:
                    total_failed += 1
                await asyncio.sleep(delay)
        
        while not spam_stop_flag and (infinite or cycle < loop_count):
            cycle += 1
            tasks = [asyncio.create_task(worker(url)) for url in webhook_list]
            await asyncio.gather(*tasks)
            if infinite and not spam_stop_flag:
                await asyncio.sleep(2)
        
        try:
            msg = f"✅ Циклов: {cycle} | Отправлено: {total_sent} | Ошибок: {total_failed}"
            if spam_stop_flag:
                msg = "🛑 " + msg
            await interaction.followup.send(msg, ephemeral=True)
        except:
            pass


class QuickSpamModal(discord.ui.Modal, title="Быстрый спам"):
    webhooks = discord.ui.TextInput(label="Вебхуки", placeholder="https://... (каждый с новой строки)", required=True, style=discord.TextStyle.paragraph)
    messages = discord.ui.TextInput(label="Сообщения", placeholder="Текст 1\nТекст 2", required=True, style=discord.TextStyle.paragraph)
    count = discord.ui.TextInput(label="Кол-во", placeholder="50", default="50")
    mention = discord.ui.TextInput(label="Упоминание", placeholder="ID / everyone / here", required=False)
    
    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            cnt = max(1, min(int(self.count.value), 200))
        except:
            cnt = 50
        
        webhook_list = [u.strip() for u in self.webhooks.value.split('\n') if u.strip()]
        if not webhook_list:
            await interaction.followup.send("❌ Введите вебхук", ephemeral=True)
            return
        
        msg_list = [m.strip() for m in self.messages.value.split('\n') if m.strip()]
        if not msg_list:
            await interaction.followup.send("❌ Введите сообщение", ephemeral=True)
            return
        
        mention_val = self.mention.value.strip().lower()
        mention_id = None
        mention_type = None
        if mention_val:
            if mention_val.isdigit():
                mention_id = int(mention_val)
            elif mention_val == "everyone":
                mention_type = "everyone"
            elif mention_val == "here":
                mention_type = "here"
        
        total_sent = 0
        await interaction.followup.send(f"🔄 Запуск ({cnt} сообщений)...", ephemeral=True)
        
        async with aiohttp.ClientSession() as session:
            for url in webhook_list:
                for i in range(cnt):
                    msg = random.choice(msg_list)
                    if mention_id:
                        msg = f"<@{mention_id}> {msg}"
                    elif mention_type:
                        msg = f"@{mention_type} {msg}"
                    try:
                        async with session.post(url, json={"content": msg}) as resp:
                            if resp.status in [200, 204]:
                                total_sent += 1
                    except:
                        pass
                    await asyncio.sleep(0.5)
        
        await interaction.followup.send(f"✅ Отправлено: {total_sent}", ephemeral=True)


class ChangeNameModal(discord.ui.Modal, title="Смена имени"):
    webhooks = discord.ui.TextInput(label="Вебхуки", placeholder="https://...\nhttps://...", required=True, style=discord.TextStyle.paragraph)
    new_name = discord.ui.TextInput(label="Новое имя", placeholder="Имя", required=True, max_length=80)
    
    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        webhook_list = [u.strip() for u in self.webhooks.value.split('\n') if u.strip()]
        success = 0
        async with aiohttp.ClientSession() as session:
            for url in webhook_list:
                try:
                    async with session.patch(url, json={"name": self.new_name.value}) as resp:
                        if resp.status in [200, 204]:
                            success += 1
                except:
                    pass
                await asyncio.sleep(0.5)
        await interaction.followup.send(f"✅ Изменено: {success} из {len(webhook_list)}", ephemeral=True)


class ClearWebhookModal(discord.ui.Modal, title="Очистка"):
    webhook_url = discord.ui.TextInput(label="URL", placeholder="https://discord.com/api/webhooks/...", required=True)
    count = discord.ui.TextInput(label="Кол-во", placeholder="50", default="50")
    
    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            parts = self.webhook_url.value.split('/')
            wid = parts[-2]
            token = parts[-1]
            del_count = int(self.count.value) if self.count.value else 50
            deleted = 0
            async with aiohttp.ClientSession() as session:
                async with session.get(f"https://discord.com/api/v10/webhooks/{wid}/{token}/messages?limit=50") as resp:
                    if resp.status == 200:
                        for msg in await resp.json():
                            if deleted >= del_count:
                                break
                            await session.delete(f"https://discord.com/api/v10/webhooks/{wid}/{token}/messages/{msg['id']}")
                            deleted += 1
                            await asyncio.sleep(0.3)
            await interaction.followup.send(f"✅ Удалено: {deleted}", ephemeral=True)
        except Exception as e:
            await interaction.followup.send(f"❌ {e}", ephemeral=True)


async def setup_webhook_spam_panel():
    channel = bot.get_channel(WEBHOOK_SPAM_CHANNEL_ID)
    if not channel:
        try:
            channel = await bot.fetch_channel(WEBHOOK_SPAM_CHANNEL_ID)
        except:
            return
    async for msg in channel.history(limit=50):
        if msg.author == bot.user:
            await msg.delete()
    embed = discord.Embed(title="💣 ВЕБХУК ПАНЕЛЬ", description="📨 Discohook | ⚡ Быстрый | 🎭 Сменить имя | 🗑️ Очистка | 🛑 Стоп\n\n⚠️ Задержка 1 секунда для защиты от бана", color=discord.Color.red())
    await channel.send(embed=embed, view=WebhookSpamPanel())

# ================= ПАНЕЛЬ КОПИРОВАНИЯ СЕРВЕРА =================
class CopyServerPanel(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
    
    @discord.ui.button(label="📋 Копировать сервер", style=discord.ButtonStyle.primary, custom_id="copy_server_btn", row=0)
    async def copy_server_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_owner(interaction):
            await interaction.response.send_message("❌ Только для Owner", ephemeral=True)
            return
        modal = CopyServerModal()
        await interaction.response.send_modal(modal)
    
    @discord.ui.button(label="🔄 Копировать роли", style=discord.ButtonStyle.secondary, custom_id="copy_roles_btn", row=0)
    async def copy_roles_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_owner(interaction):
            await interaction.response.send_message("❌ Только для Owner", ephemeral=True)
            return
        modal = CopyRolesModal()
        await interaction.response.send_modal(modal)
    
    @discord.ui.button(label="📁 Копировать каналы", style=discord.ButtonStyle.secondary, custom_id="copy_channels_btn", row=0)
    async def copy_channels_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_owner(interaction):
            await interaction.response.send_message("❌ Только для Owner", ephemeral=True)
            return
        modal = CopyChannelsModal()
        await interaction.response.send_modal(modal)
    
    @discord.ui.button(label="🔗 Копировать вебхуки", style=discord.ButtonStyle.success, custom_id="copy_webhooks_btn", row=1)
    async def copy_webhooks_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_owner(interaction):
            await interaction.response.send_message("❌ Только для Owner", ephemeral=True)
            return
        modal = CopyWebhooksModal()
        await interaction.response.send_modal(modal)
    
    @discord.ui.button(label="⚙️ Копировать настройки", style=discord.ButtonStyle.success, custom_id="copy_settings_btn", row=1)
    async def copy_settings_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_owner(interaction):
            await interaction.response.send_message("❌ Только для Owner", ephemeral=True)
            return
        modal = CopySettingsModal()
        await interaction.response.send_modal(modal)
    
    @discord.ui.button(label="💾 Сохранить бэкап", style=discord.ButtonStyle.success, custom_id="backup_save", row=2)
    async def backup_save_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_owner(interaction):
            await interaction.response.send_message("❌ Только для Owner", ephemeral=True)
            return
        await self.save_backup(interaction)
    
    @discord.ui.button(label="📂 Загрузить бэкап", style=discord.ButtonStyle.primary, custom_id="backup_load", row=2)
    async def backup_load_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_owner(interaction):
            await interaction.response.send_message("❌ Только для Owner", ephemeral=True)
            return
        modal = LoadBackupModal()
        await interaction.response.send_modal(modal)
    
    async def save_backup(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        
        try:
            guild = interaction.guild
            backup_data = {
                "name": guild.name,
                "icon_url": str(guild.icon.url) if guild.icon else None,
                "banner_url": str(guild.banner.url) if guild.banner else None,
                "verification_level": guild.verification_level.value,
                "explicit_content_filter": guild.explicit_content_filter.value,
                "preferred_locale": str(guild.preferred_locale),
                "roles": [],
                "categories": [],
                "channels": []
            }
            
            # Сохраняем роли (кроме @everyone)
            for role in guild.roles:
                if role.name == "@everyone":
                    continue
                backup_data["roles"].append({
                    "name": role.name,
                    "color": role.color.value,
                    "permissions": role.permissions.value,
                    "hoist": role.hoist,
                    "mentionable": role.mentionable,
                    "position": role.position
                })
            
            # Сохраняем категории и каналы
            for category in guild.categories:
                cat_data = {
                    "name": category.name,
                    "position": category.position,
                    "channels": []
                }
                for channel in category.channels:
                    if isinstance(channel, discord.TextChannel):
                        cat_data["channels"].append({
                            "type": "text",
                            "name": channel.name,
                            "position": channel.position,
                            "topic": channel.topic,
                            "slowmode_delay": channel.slowmode_delay,
                            "nsfw": channel.nsfw
                        })
                    elif isinstance(channel, discord.VoiceChannel):
                        cat_data["channels"].append({
                            "type": "voice",
                            "name": channel.name,
                            "position": channel.position,
                            "bitrate": channel.bitrate,
                            "user_limit": channel.user_limit
                        })
                backup_data["categories"].append(cat_data)
            
            # Сохраняем каналы без категорий
            for channel in guild.channels:
                if channel.category_id is None and not isinstance(channel, discord.CategoryChannel):
                    if isinstance(channel, discord.TextChannel):
                        backup_data["channels"].append({
                            "type": "text",
                            "name": channel.name,
                            "position": channel.position,
                            "topic": channel.topic,
                            "slowmode_delay": channel.slowmode_delay,
                            "nsfw": channel.nsfw
                        })
                    elif isinstance(channel, discord.VoiceChannel):
                        backup_data["channels"].append({
                            "type": "voice",
                            "name": channel.name,
                            "position": channel.position,
                            "bitrate": channel.bitrate,
                            "user_limit": channel.user_limit
                        })
            
            # Сохраняем в JSON файл
            backup_json = json.dumps(backup_data, ensure_ascii=False, indent=2)
            backup_bytes = backup_json.encode('utf-8')
            backup_file = discord.File(
                io.BytesIO(backup_bytes),
                filename=f"server_backup_{guild.name}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
            )
            
            await interaction.followup.send(
                f"✅ **Бэкап сервера `{guild.name}` создан!**\n"
                f"📁 Ролей: {len(backup_data['roles'])}\n"
                f"📂 Категорий: {len(backup_data['categories'])}\n"
                f"💬 Каналов: {len(backup_data['channels'])}",
                file=backup_file,
                ephemeral=True
            )
            
        except Exception as e:
            await interaction.followup.send(f"❌ Ошибка: {e}", ephemeral=True)


class CopyServerModal(discord.ui.Modal, title="Копирование сервера"):
    target_guild_id = discord.ui.TextInput(
        label="ID сервера для копирования",
        placeholder="123456789012345678",
        required=True
    )
    
    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        
        try:
            target_id = int(self.target_guild_id.value.strip())
            target_guild = bot.get_guild(target_id)
            if not target_guild:
                target_guild = await bot.fetch_guild(target_id)
            
            current_guild = interaction.guild
            created = {"roles": 0, "categories": 0, "channels": 0}
            
            status_msg = await interaction.followup.send("🔄 Начинаю копирование...", ephemeral=True)
            
            # 1. Копируем роли
            role_positions = {}
            for role in reversed(target_guild.roles):
                if role.name == "@everyone":
                    continue
                try:
                    new_role = await current_guild.create_role(
                        name=role.name,
                        color=role.color,
                        permissions=role.permissions,
                        hoist=role.hoist,
                        mentionable=role.mentionable
                    )
                    role_positions[role.position] = new_role
                    created["roles"] += 1
                    await asyncio.sleep(0.3)
                except:
                    pass
                
                if created["roles"] % 5 == 0:
                    await status_msg.edit(content=f"🔄 Копирование ролей: {created['roles']}...")
            
            for pos in sorted(role_positions.keys(), reverse=True):
                try:
                    await role_positions[pos].edit(position=pos)
                except:
                    pass
            
            await status_msg.edit(content=f"✅ Роли скопированы ({created['roles']}). Копирую категории...")
            
            # 2. Копируем категории
            category_map = {}
            for category in target_guild.categories:
                try:
                    new_category = await current_guild.create_category(
                        name=category.name,
                        position=category.position
                    )
                    category_map[category.id] = new_category
                    created["categories"] += 1
                    await asyncio.sleep(0.3)
                except:
                    pass
            
            await status_msg.edit(content=f"✅ Категории скопированы ({created['categories']}). Копирую каналы...")
            
            # 3. Копируем каналы
            for channel in target_guild.channels:
                if isinstance(channel, discord.TextChannel):
                    try:
                        parent = category_map.get(channel.category_id) if channel.category_id else None
                        await current_guild.create_text_channel(
                            name=channel.name,
                            category=parent,
                            position=channel.position,
                            topic=channel.topic[:100] if channel.topic else None,
                            slowmode_delay=channel.slowmode_delay
                        )
                        created["channels"] += 1
                        await asyncio.sleep(0.3)
                    except:
                        pass
                        
                elif isinstance(channel, discord.VoiceChannel):
                    try:
                        parent = category_map.get(channel.category_id) if channel.category_id else None
                        await current_guild.create_voice_channel(
                            name=channel.name,
                            category=parent,
                            position=channel.position,
                            bitrate=min(channel.bitrate, 96000),
                            user_limit=channel.user_limit
                        )
                        created["channels"] += 1
                        await asyncio.sleep(0.3)
                    except:
                        pass
            
            await interaction.followup.send(
                f"✅ **Копирование завершено!**\n"
                f"📁 Ролей: {created['roles']}\n"
                f"📂 Категорий: {created['categories']}\n"
                f"💬 Каналов: {created['channels']}",
                ephemeral=True
            )
            
        except Exception as e:
            await interaction.followup.send(f"❌ Ошибка: {e}", ephemeral=True)


class CopyRolesModal(discord.ui.Modal, title="Копирование ролей"):
    target_guild_id = discord.ui.TextInput(
        label="ID сервера для копирования",
        placeholder="123456789012345678",
        required=True
    )
    
    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        
        try:
            target_id = int(self.target_guild_id.value.strip())
            target_guild = bot.get_guild(target_id)
            if not target_guild:
                target_guild = await bot.fetch_guild(target_id)
            
            current_guild = interaction.guild
            created = 0
            
            role_positions = {}
            for role in reversed(target_guild.roles):
                if role.name == "@everyone":
                    continue
                try:
                    new_role = await current_guild.create_role(
                        name=role.name,
                        color=role.color,
                        permissions=role.permissions,
                        hoist=role.hoist,
                        mentionable=role.mentionable
                    )
                    role_positions[role.position] = new_role
                    created += 1
                    await asyncio.sleep(0.3)
                except:
                    pass
            
            for pos in sorted(role_positions.keys(), reverse=True):
                try:
                    await role_positions[pos].edit(position=pos)
                except:
                    pass
            
            await interaction.followup.send(f"✅ Скопировано ролей: {created}", ephemeral=True)
            
        except Exception as e:
            await interaction.followup.send(f"❌ Ошибка: {e}", ephemeral=True)


class CopyChannelsModal(discord.ui.Modal, title="Копирование каналов"):
    target_guild_id = discord.ui.TextInput(
        label="ID сервера для копирования",
        placeholder="123456789012345678",
        required=True
    )
    
    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        
        try:
            target_id = int(self.target_guild_id.value.strip())
            target_guild = bot.get_guild(target_id)
            if not target_guild:
                target_guild = await bot.fetch_guild(target_id)
            
            current_guild = interaction.guild
            created = {"categories": 0, "channels": 0}
            
            category_map = {}
            for category in target_guild.categories:
                try:
                    new_category = await current_guild.create_category(
                        name=category.name,
                        position=category.position
                    )
                    category_map[category.id] = new_category
                    created["categories"] += 1
                    await asyncio.sleep(0.3)
                except:
                    pass
            
            for channel in target_guild.channels:
                if isinstance(channel, discord.TextChannel):
                    try:
                        parent = category_map.get(channel.category_id) if channel.category_id else None
                        await current_guild.create_text_channel(
                            name=channel.name,
                            category=parent,
                            position=channel.position,
                            topic=channel.topic[:100] if channel.topic else None,
                            slowmode_delay=channel.slowmode_delay
                        )
                        created["channels"] += 1
                        await asyncio.sleep(0.3)
                    except:
                        pass
                        
                elif isinstance(channel, discord.VoiceChannel):
                    try:
                        parent = category_map.get(channel.category_id) if channel.category_id else None
                        await current_guild.create_voice_channel(
                            name=channel.name,
                            category=parent,
                            position=channel.position,
                            bitrate=min(channel.bitrate, 96000),
                            user_limit=channel.user_limit
                        )
                        created["channels"] += 1
                        await asyncio.sleep(0.3)
                    except:
                        pass
            
            await interaction.followup.send(
                f"✅ Скопировано:\n📂 Категорий: {created['categories']}\n💬 Каналов: {created['channels']}",
                ephemeral=True
            )
            
        except Exception as e:
            await interaction.followup.send(f"❌ Ошибка: {e}", ephemeral=True)


class CopyWebhooksModal(discord.ui.Modal, title="Копирование вебхуков"):
    target_guild_id = discord.ui.TextInput(
        label="ID сервера откуда копировать",
        placeholder="123456789012345678",
        required=True
    )
    
    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        
        try:
            target_id = int(self.target_guild_id.value.strip())
            target_guild = bot.get_guild(target_id)
            if not target_guild:
                target_guild = await bot.fetch_guild(target_id)
            
            current_guild = interaction.guild
            created = 0
            failed = 0
            
            status_msg = await interaction.followup.send("🔄 Копирование вебхуков...", ephemeral=True)
            
            # Сопоставляем каналы по имени
            channel_map = {}
            for target_channel in target_guild.channels:
                for source_channel in current_guild.channels:
                    if source_channel.name == target_channel.name:
                        channel_map[target_channel.id] = source_channel
                        break
            
            for target_channel in target_guild.channels:
                target_webhooks = await target_channel.webhooks()
                source_channel = channel_map.get(target_channel.id)
                
                if source_channel and target_webhooks:
                    for webhook in target_webhooks:
                        try:
                            await source_channel.create_webhook(
                                name=webhook.name,
                                reason="Копирование вебхуков"
                            )
                            created += 1
                            await asyncio.sleep(0.5)
                        except Exception:
                            failed += 1
            
            await interaction.followup.send(
                f"✅ **Копирование вебхуков завершено!**\n"
                f"🔗 Создано: {created}\n"
                f"❌ Ошибок: {failed}",
                ephemeral=True
            )
            
        except Exception as e:
            await interaction.followup.send(f"❌ Ошибка: {e}", ephemeral=True)


class CopySettingsModal(discord.ui.Modal, title="Копирование настроек сервера"):
    target_guild_id = discord.ui.TextInput(
        label="ID сервера откуда копировать",
        placeholder="123456789012345678",
        required=True
    )
    copy_boost = discord.ui.TextInput(
        label="Копировать настройки буста",
        placeholder="да/нет",
        required=False,
        default="нет"
    )
    copy_bans = discord.ui.TextInput(
        label="Копировать баны",
        placeholder="да/нет",
        required=False,
        default="нет"
    )
    
    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        
        try:
            target_id = int(self.target_guild_id.value.strip())
            target_guild = bot.get_guild(target_id)
            if not target_guild:
                target_guild = await bot.fetch_guild(target_id)
            
            current_guild = interaction.guild
            copy_bans_flag = self.copy_bans.value.lower().strip() in ["да", "yes", "true", "1"]
            
            settings = {}
            
            # 1. Копируем название сервера
            try:
                await current_guild.edit(name=target_guild.name)
                settings["name"] = "✅"
            except:
                settings["name"] = "❌"
            
            # 2. Копируем иконку сервера
            if target_guild.icon:
                try:
                    icon_data = await target_guild.icon.read()
                    await current_guild.edit(icon=icon_data)
                    settings["icon"] = "✅"
                except:
                    settings["icon"] = "❌"
            else:
                settings["icon"] = "⏭️"
            
            # 3. Копируем баннер (требуется буст)
            if self.copy_boost.value.lower().strip() in ["да", "yes", "true", "1"] and target_guild.banner:
                try:
                    banner_data = await target_guild.banner.read()
                    await current_guild.edit(banner=banner_data)
                    settings["banner"] = "✅"
                except:
                    settings["banner"] = "❌ (нужен буст)"
            else:
                settings["banner"] = "⏭️"
            
            # 4. Копируем уровень верификации
            try:
                await current_guild.edit(verification_level=target_guild.verification_level)
                settings["verification"] = "✅"
            except:
                settings["verification"] = "❌"
            
            # 5. Копируем уровень модерации контента
            try:
                await current_guild.edit(content_filter=target_guild.explicit_content_filter)
                settings["content_filter"] = "✅"
            except:
                settings["content_filter"] = "❌"
            
            # 6. Копируем язык
            try:
                await current_guild.edit(preferred_locale=target_guild.preferred_locale)
                settings["locale"] = "✅"
            except:
                settings["locale"] = "❌"
            
            # 7. Копируем баннер приглашений
            try:
                await current_guild.edit(splash=target_guild.splash)
                settings["splash"] = "✅"
            except:
                settings["splash"] = "❌"
            
            # 8. Копируем список банов
            if copy_bans_flag:
                banned_users = []
                async for entry in target_guild.bans():
                    banned_users.append(entry.user)
                    await asyncio.sleep(0.3)
                
                banned_count = 0
                for user in banned_users:
                    try:
                        await current_guild.ban(user, reason="Копирование банов")
                        banned_count += 1
                        await asyncio.sleep(0.5)
                    except:
                        pass
                settings["bans"] = f"✅ ({banned_count})"
            else:
                settings["bans"] = "⏭️"
            
            report = f"**Настройки сервера:**\n"
            report += f"📛 Название: {settings['name']}\n"
            report += f"🖼️ Иконка: {settings['icon']}\n"
            report += f"🎨 Баннер: {settings['banner']}\n"
            report += f"🔒 Верификация: {settings['verification']}\n"
            report += f"📋 Модерация: {settings['content_filter']}\n"
            report += f"🌐 Язык: {settings['locale']}\n"
            report += f"✨ Сплаш: {settings['splash']}\n"
            report += f"🚫 Баны: {settings['bans']}"
            
            await interaction.followup.send(report, ephemeral=True)
            
        except Exception as e:
            await interaction.followup.send(f"❌ Ошибка: {e}", ephemeral=True)


class LoadBackupModal(discord.ui.Modal, title="Загрузка бэкапа"):
    backup_json = discord.ui.TextInput(
        label="JSON бэкапа",
        placeholder="Вставьте JSON содержимое бэкапа сюда...",
        required=True,
        style=discord.TextStyle.paragraph,
        max_length=10000
    )
    clear_existing = discord.ui.TextInput(
        label="Очистить текущий сервер?",
        placeholder="да/нет",
        required=False,
        default="нет"
    )
    
    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        
        try:
            backup_data = json.loads(self.backup_json.value)
            guild = interaction.guild
            clear_flag = self.clear_existing.value.lower().strip() in ["да", "yes", "true", "1"]
            
            if clear_flag:
                await interaction.followup.send("🔄 Очистка сервера...", ephemeral=True)
                
                # Удаляем все каналы и категории
                for channel in guild.channels:
                    try:
                        await channel.delete()
                        await asyncio.sleep(0.3)
                    except:
                        pass
                
                # Удаляем все роли (кроме @everyone и ботовых)
                for role in guild.roles:
                    if role.name == "@everyone" or role.managed:
                        continue
                    try:
                        await role.delete()
                        await asyncio.sleep(0.3)
                    except:
                        pass
            
            # Восстанавливаем настройки сервера
            try:
                if backup_data.get("name"):
                    await guild.edit(name=backup_data["name"])
            except:
                pass
            
            # Восстанавливаем роли
            role_positions = {}
            for role_data in backup_data.get("roles", []):
                try:
                    new_role = await guild.create_role(
                        name=role_data["name"],
                        color=role_data["color"],
                        permissions=discord.Permissions(role_data["permissions"]),
                        hoist=role_data["hoist"],
                        mentionable=role_data["mentionable"]
                    )
                    role_positions[role_data["position"]] = new_role
                    await asyncio.sleep(0.3)
                except:
                    pass
            
            # Восстанавливаем позиции ролей
            for pos in sorted(role_positions.keys(), reverse=True):
                try:
                    await role_positions[pos].edit(position=pos)
                except:
                    pass
            
            # Восстанавливаем категории
            category_map = {}
            for cat_data in backup_data.get("categories", []):
                try:
                    new_category = await guild.create_category(
                        name=cat_data["name"],
                        position=cat_data["position"]
                    )
                    category_map[cat_data["name"]] = new_category
                    
                    # Восстанавливаем каналы в категории
                    for channel_data in cat_data.get("channels", []):
                        try:
                            if channel_data["type"] == "text":
                                await new_category.create_text_channel(
                                    name=channel_data["name"],
                                    position=channel_data["position"],
                                    topic=channel_data.get("topic"),
                                    slowmode_delay=channel_data.get("slowmode_delay", 0),
                                    nsfw=channel_data.get("nsfw", False)
                                )
                            elif channel_data["type"] == "voice":
                                await new_category.create_voice_channel(
                                    name=channel_data["name"],
                                    position=channel_data["position"],
                                    bitrate=min(channel_data.get("bitrate", 64000), 96000),
                                    user_limit=channel_data.get("user_limit", 0)
                                )
                            await asyncio.sleep(0.3)
                        except:
                            pass
                    await asyncio.sleep(0.3)
                except:
                    pass
            
            # Восстанавливаем каналы без категорий
            for channel_data in backup_data.get("channels", []):
                try:
                    if channel_data["type"] == "text":
                        await guild.create_text_channel(
                            name=channel_data["name"],
                            position=channel_data["position"],
                            topic=channel_data.get("topic"),
                            slowmode_delay=channel_data.get("slowmode_delay", 0),
                            nsfw=channel_data.get("nsfw", False)
                        )
                    elif channel_data["type"] == "voice":
                        await guild.create_voice_channel(
                            name=channel_data["name"],
                            position=channel_data["position"],
                            bitrate=min(channel_data.get("bitrate", 64000), 96000),
                            user_limit=channel_data.get("user_limit", 0)
                        )
                    await asyncio.sleep(0.3)
                except:
                    pass
            
            await interaction.followup.send(
                f"✅ **Бэкап восстановлен на сервер `{guild.name}`!**\n"
                f"📁 Восстановлено ролей: {len(role_positions)}\n"
                f"📂 Восстановлено категорий: {len(backup_data.get('categories', []))}\n"
                f"💬 Восстановлено каналов: {len(backup_data.get('categories', [])) + len(backup_data.get('channels', []))}",
                ephemeral=True
            )
            
        except json.JSONDecodeError:
            await interaction.followup.send("❌ Неверный формат JSON бэкапа", ephemeral=True)
        except Exception as e:
            await interaction.followup.send(f"❌ Ошибка: {e}", ephemeral=True)


async def setup_copy_panel():
    channel = bot.get_channel(COPY_PANEL_CHANNEL_ID)
    if not channel:
        try:
            channel = await bot.fetch_channel(COPY_PANEL_CHANNEL_ID)
        except Exception as e:
            logger.error(f"Канал копирования {COPY_PANEL_CHANNEL_ID} не найден: {e}")
            return
    
    try:
        async for msg in channel.history(limit=50):
            if msg.author == bot.user:
                await msg.delete()
    except Exception:
        pass
    
    embed = discord.Embed(
        title="📋 ПАНЕЛЬ КОПИРОВАНИЯ СЕРВЕРА",
        description="**Выберите действие:**\n\n"
                    "📋 **Копировать сервер** - полное копирование (роли, категории, каналы)\n"
                    "🔄 **Копировать роли** - только роли\n"
                    "📁 **Копировать каналы** - только категории и каналы\n"
                    "🔗 **Копировать вебхуки** - перенос вебхуков\n"
                    "⚙️ **Копировать настройки** - название, иконка, уровень верификации, баны\n"
                    "💾 **Сохранить бэкап** - создать JSON бэкап текущего сервера\n"
                    "📂 **Загрузить бэкап** - восстановить сервер из JSON бэкапа\n\n"
                    "⚠️ Требуются права администратора",
        color=discord.Color.blue()
    )
    view = CopyServerPanel()
    await channel.send(embed=embed, view=view)
    logger.info("✅ Панель копирования сервера отправлена")

# ================= ЗАПУСК =================
async def _safe_task(coro, name: str):
    try:
        await coro
    except Exception:
        logger.exception(f"❌ Необработанное исключение в задаче '{name}'")

async def _startup_background():
    logger.info("🔥 _startup_background() начал работу...")
    try:
        await setup_webhook_spam_panel()
        logger.info("✅ Вебхук спам панель настроена")
    except Exception as e:  
        logger.error(f"❌ Ошибка настройки спам панели: {e}")
    try:
        await setup_copy_panel()
        logger.info("✅ Панель копирования настроена")
    except Exception as e:
        logger.error(f"❌ Ошибка настройки панели копирования: {e}")
    try:
        await db.restore_from_backup_channel(BACKUP_CHANNEL_ID, bot)
        logger.info("✅ Восстановление из бэкапа завершено")
    except Exception as e:
        logger.error(f"❌ Ошибка восстановления бэкапа: {e}")
    try:
        await setup_panels()
        logger.info("✅ Панели серверов настроены")
    except Exception as e:
        logger.error(f"❌ Ошибка настройки панелей: {e}")
    try:
        await setup_ai_panel()
        logger.info("✅ Панель ИИ-конвейера настроена")
    except Exception as e:
        logger.error(f"❌ Ошибка настройки ИИ-панели: {e}")
    try:
        await setup_admin_panel()
        logger.info("✅ Админ панель настроена")
    except Exception as e:
        logger.error(f"❌ Ошибка настройки админ панели: {e}")
    logger.info("✅ _startup_background() завершён!")

@bot.event
async def on_ready():
    logger.info(f"Logged in as {bot.user.name} ({bot.user.id})")
    print(f"🤖 Бот {bot.user.name} успешно запущен и готов к работе!")
    
    global _startup_done
    if _startup_done:
        return
    _startup_done = True
    
    await db.init_db()
    await db.refresh_cache()
    
    # Регистрация постоянных View
    bot.add_view(VerifyView())
    bot.add_view(TicketCreateButton())
    bot.add_view(AdminPanelView())
    bot.add_view(OrderCloseView(0, 0, None, None))
    
    # Запуск фоновых задач
    asyncio.create_task(_safe_task(auto_cleanup_tickets(), "auto_cleanup_tickets"))
    asyncio.create_task(_safe_task(auto_update_currency(), "auto_update_currency"))
    asyncio.create_task(_safe_task(cleanup_spam_cache(), "cleanup_spam_cache"))
    asyncio.create_task(_safe_task(auto_backup_task(), "auto_backup_task"))
    asyncio.create_task(_safe_task(_startup_background(), "_startup_background"))
    
    try:
        await bot.tree.sync()
        logger.info("✅ Слеш-команды синхронизированы")
    except Exception as e:
        logger.error(f"❌ Ошибка синхронизации команд: {e}")
    
    print("🚀 Бот успешно инициализирован!")

# ================= ОБРАБОТКА СООБЩЕНИЙ В ИИ-КАНАЛАХ =================
@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        await bot.process_commands(message)
        return
    
    category = None
    for cat, cat_channel_id in CATEGORY_CHANNELS.items():
        if cat_channel_id == message.channel.id:
            category = cat
            break
    
    if not category:
        await bot.process_commands(message)
        return
    
    if message.content.startswith('!'):
        await bot.process_commands(message)
        return
    
    async with message.channel.typing():
        try:
            history_messages = []
            async for msg in message.channel.history(limit=15):
                if msg.author.bot:
                    continue
                if len(history_messages) < 10:
                    history_messages.insert(0, {"role": "user", "content": msg.content})
            
            final_prompt = message.content
            
            use_server_context = True
            if use_server_context and message.guild:
                server_info = await build_server_context(message.guild)
                final_prompt = f"{server_info}\n\nВопрос пользователя: {message.content}"
            
            response = await ask_groq_mode(category, final_prompt, history_messages, "hide")
            
            if response.startswith("Ошибка"):
                await message.reply(f"❌ {response}")
                return
            
            await db.add_to_history(message.author.id, category, "user", message.content)
            await db.add_to_history(message.author.id, category, "assistant", response[:3000])
            
            if len(response) <= 1900:
                await message.reply(response)
            else:
                filename = f"ai_answer_{category}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"
                file_bytes = b'\xef\xbb\xbf' + response.encode('utf-8', errors='replace')
                file_obj = io.BytesIO(file_bytes)
                await message.reply(
                    content="📄 **Ответ ИИ (полный):**",
                    file=discord.File(file_obj, filename=filename)
                )
                embed = discord.Embed(
                    title=f"{CATEGORY_LABELS.get(category, category)}",
                    description=response[:500] + "... (полный ответ в файле)",
                    color=discord.Color.blue()
                )
                await message.channel.send(embed=embed)
                
        except Exception as e:
            logger.exception("Ошибка в on_message (ИИ)")
            await message.reply(f"❌ Ошибка: {e}")
    
    await bot.process_commands(message)

@bot.event
async def on_member_join(member: discord.Member):
    config = get_config(member.guild.id)
    if not config:
        return
    unverified_role_id = config["roles"].get("unverified")
    if unverified_role_id:
        unverified_role = member.guild.get_role(unverified_role_id)
        if unverified_role:
            await member.add_roles(unverified_role)
    welcome_channel_id = config.get("welcome_channel")
    if welcome_channel_id:
        welcome_channel = member.guild.get_channel(welcome_channel_id)
        if welcome_channel:
            embed = discord.Embed(title=f"👋 Добро пожаловать, {member.name}!", description=f"📌 Пройдите верификацию в <#{config['verify_channel']}>", color=discord.Color.green())
            await welcome_channel.send(content=member.mention, embed=embed)

token = os.getenv('DISCORD_TOKEN')
if not token:
    raise RuntimeError("❌ DISCORD_TOKEN не задан!")

bot.run(token)