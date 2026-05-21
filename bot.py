import discord
from discord.ext import commands, tasks
from discord.ui import Button, View
from discord import app_commands
import os
import re
import io
import json
import asyncio
import aiohttp
import logging
from typing import Optional
from datetime import datetime, timedelta, timezone
import database as db
from database import (
    has_user_bought, update_stock, get_stock, add_purchase,
    add_review, get_seller_rating, get_seller_reviews,
    get_daily_purchase_count, convert_price_rub
)

# Настройка логирования в консоль для удобного отслеживания на Railway
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# ================= КОНФИГУРАЦИЯ =================
CONFIG = {
    1503041215803822200: {
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
        "ticket_channel": 1500242313211805788,
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

BACKUP_CHANNEL_ID = 1503146387129368718
OWNER_ID = 1500198262026539099
SHOP_IMAGE_URL = "https://discord.com/channels/1462375742401675294/1500263242465935492/1505688385833009273"
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

def is_admin(interaction: discord.Interaction) -> bool:
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
_shop_update_tasks: dict = {}

# ================= МУТ =================
async def mute_member(member: discord.Member, duration_seconds: int, reason: str) -> bool:
    if is_admin_member(member):
        return False
    try:
        await member.timeout(timedelta(seconds=duration_seconds), reason=reason)
        return True
    except Exception as e:
        logger.error(f"Не удалось выдать таймаут: {e}")
        return False

def get_mute_duration(user_id: int) -> int:
    offense_count = user_mention_count.get(user_id, 0)
    if offense_count >= len(MUTE_DURATIONS):
        return MUTE_DURATIONS[-1]
    return MUTE_DURATIONS[max(offense_count - 1, 0)]

# ================= КУРС ВАЛЮТ =================
async def fetch_currency_rates():
    """Получить актуальные курсы валют (RUB -> UAH, USD, EUR)"""
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                'https://api.exchangerate-api.com/v4/latest/RUB',
                timeout=aiohttp.ClientTimeout(total=10)
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    rates = data.get("rates", {})
                    result = {
                        "UAH": rates.get("UAH", 0),
                        "USD": rates.get("USD", 0),
                        "EUR": rates.get("EUR", 0),
                    }
                    await db.update_currency_rates(result)
                    return result
    except Exception as e:
        logger.error(f"Ошибка получения курса валют: {e}")
    return {}

async def parse_price_rub(price_str: str) -> Optional[float]:
    """Парсить число из строки цены"""
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

# ================= МАГАЗИН =================
SHOP_IMAGE_LINK = "https://discord.com/channels/1462375742401675294/1500263242465935492/1505688385833009273"

async def _do_shop_update(guild: discord.Guild):
    config = get_config(guild.id)
    if not config or not config.get("shop_channel"):
        return
    channel = guild.get_channel(config["shop_channel"])
    if not channel:
        return

    await db.refresh_cache()
    categories = db.categories_cache

    if not categories:
        catalog_embed = discord.Embed(
            title="📦 TALENT SHOP",
            description="В магазине пока нет товаров.",
            color=discord.Color.gold()
        )
    else:
        catalog_embed = discord.Embed(
            title="📦 TALENT SHOP — КАТАЛОГ ТОВАРОВ",
            description="**Выберите категорию в меню ниже.**",
            color=discord.Color.gold()
        )
        catalog_embed.set_footer(text="TALENT SHOP | Нажми для выбора")

    view = ShopView()

    try:
        async for msg in channel.history(limit=20):
            if msg.author == bot.user:
                await msg.delete()
    except Exception as e:
        logger.error(f"Не удалось удалить старые сообщения магазина: {e}")

    banner_msg = await channel.send(
        content=f"✨ **КАТАЛОГ** | Много и дёшево\n{SHOP_IMAGE_LINK}"
    )
    await db.set_shop_messages(guild.id, img_id=banner_msg.id)
    await channel.send(embed=catalog_embed, view=view)

async def send_or_update_shop(guild: discord.Guild):
    guild_id = guild.id
    existing = _shop_update_tasks.get(guild_id)
    if existing and not existing.done():
        existing.cancel()

    async def _delayed():
        await asyncio.sleep(2)
        try:
            await _do_shop_update(guild)
        except Exception as e:
            logger.error(f"Ошибка обновления магазина ({guild_id}): {e}")
        finally:
            _shop_update_tasks.pop(guild_id, None)

    _shop_update_tasks[guild_id] = asyncio.create_task(_delayed())

# ================= ПОИСК В МАГАЗИНЕ =================
class ShopSearchModal(discord.ui.Modal, title="🔍 Поиск товара"):
    query = discord.ui.TextInput(
        label="Название товара или категории",
        placeholder="Введите название...",
        min_length=2, max_length=100, required=True
    )

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        search_term = self.query.value.strip()

        cats = db.categories_cache
        matched_cats = [c for c in cats.values() if search_term.lower() in c.name.lower()]
        matched_lots = await db.search_lots(search_term)

        if not matched_cats and not matched_lots:
            await interaction.followup.send(f"❌ По запросу **{search_term}** ничего не найдено.", ephemeral=True)
            return

        embed = discord.Embed(
            title=f"🔍 Результаты поиска: {search_term}",
            color=discord.Color.blue()
        )

        if matched_cats:
            cat_text = "\n".join([f"{c.emoji} **{c.name}** — товаров: {len(c.lots)}" for c in matched_cats[:5]])
            embed.add_field(name="📁 Категории", value=cat_text, inline=False)

        if matched_lots:
            lots_text = ""
            for lot in matched_lots[:10]:
                stock_icon = "✅" if lot.stock > 0 else "❌"
                lots_text += f"{stock_icon} **{lot.name}** — {lot.price}\n"
            embed.add_field(name="🛒 Товары", value=lots_text, inline=False)

        view = discord.ui.View(timeout=120)
        if matched_lots:
            options = [
                discord.SelectOption(
                    label=f"{lot.name[:50]} - {lot.price[:20]}",
                    value=str(lot.lot_id),
                    emoji="🛒"
                ) for lot in matched_lots[:25]
            ]
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

    prices_text = f"💰 **{lot.price}**"
    try:
        price_num = await parse_price_rub(lot.price)
        if price_num:
            converted = await convert_price_rub(price_num)
            prices_text = "\n".join([f"**{v}**" for v in converted.values()])
    except Exception:
        pass

    embed = discord.Embed(
        title=f"🛒 {lot.name}",
        description=f"{prices_text}\n**{stock_text}**\n\n**📝 Описание:**\n{lot.full_description}\n\n**👤 Продавец:** {seller_name}",
        color=discord.Color.green()
    )
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
            search_btn = discord.ui.Button(label="🔍 Поиск", style=discord.ButtonStyle.primary, custom_id="shop_search")
            search_btn.callback = self.search_callback
            self.add_item(search_btn)
            return
        start = self.page * 24
        end = start + 24
        page_categories = self.categories_list[start:end]
        if page_categories:
            options = [
                discord.SelectOption(
                    label=cat.name,
                    description=f"Товаров: {len(cat.lots)}",
                    value=str(cat.id),
                    emoji=cat.emoji
                )
                for cat in page_categories
            ]
            select = discord.ui.Select(
                placeholder="📁 Выберите категорию...",
                options=options, min_values=1, max_values=1,
                custom_id="shop_category_select"
            )
            select.callback = self.category_callback
            self.add_item(select)
        if len(self.categories_list) > 24:
            if self.page > 0:
                prev_btn = discord.ui.Button(label="◀️ Назад", style=discord.ButtonStyle.secondary, custom_id="shop_prev")
                prev_btn.callback = self.prev_page
                self.add_item(prev_btn)
            if end < len(self.categories_list):
                next_btn = discord.ui.Button(label="Вперёд ▶️", style=discord.ButtonStyle.secondary, custom_id="shop_next")
                next_btn.callback = self.next_page
                self.add_item(next_btn)
        search_btn = discord.ui.Button(label="🔍 Поиск", style=discord.ButtonStyle.primary, custom_id="shop_search_btn")
        search_btn.callback = self.search_callback
        self.add_item(search_btn)

    async def search_callback(self, interaction: discord.Interaction):
        await interaction.response.send_modal(ShopSearchModal())

    async def category_callback(self, interaction: discord.Interaction):
        try:
            await interaction.response.defer(ephemeral=True)
            category_id = int(interaction.data['values'][0])
            category = await db.get_category(category_id)
            if not category:
                await interaction.followup.send("❌ Категория не найдена", ephemeral=True)
                return
            lots_in_category = await db.get_lots_by_category_full(category_id)
            if not lots_in_category:
                embed = discord.Embed(
                    title=f"📁 {category.name}",
                    description="В этой категории пока нет товаров.",
                    color=discord.Color.blue()
                )
                await interaction.followup.send(embed=embed, ephemeral=True)
                return
            embed = discord.Embed(
                title=f"📁 {category.name}",
                description="**Выбери товар из списка ниже:**",
                color=discord.Color.blue()
            )
            for lot in lots_in_category:
                seller = interaction.guild.get_member(lot.seller_id)
                seller_name = seller.display_name if seller else "Продавец"
                stock_text = f"📦 В наличии: {lot.stock}" if lot.stock > 0 else "❌ Нет в наличии"
                desc = (lot.short_description or "")[:80]
                embed.add_field(
                    name=f"🛒 {lot.name}",
                    value=f"💰 **Цена:** {lot.price}\n{stock_text}\n📝 {desc}\n👤 **Продавец:** {seller_name}",
                    inline=False
                )
            view = LotsView(category_id, lots_in_category)
            await interaction.followup.send(embed=embed, view=view, ephemeral=True)
        except Exception as e:
            logger.error(f"Ошибка category_callback: {e}")
            try:
                await interaction.followup.send("❌ Ошибка при открытии категории", ephemeral=True)
            except Exception:
                pass

    async def prev_page(self, interaction: discord.Interaction):
        self.page -= 1
        self.update_items()
        await interaction.response.edit_message(view=self)

    async def next_page(self, interaction: discord.Interaction):
        self.page += 1
        self.update_items()
        await interaction.response.edit_message(view=self)

    async def close_view(self, interaction: discord.Interaction):
        await interaction.message.delete()

# ================= СПИСОК ТОВАРОВ =================
class LotsView(discord.ui.View):
    def __init__(self, category_id: int, lots_list: list):
        super().__init__(timeout=300)
        self.category_id = category_id
        options = [
            discord.SelectOption(
                label=f"{lot.name} - {lot.price}"[:100],
                description=(lot.short_description[:50] if lot.short_description else None),
                value=str(lot.lot_id),
                emoji="🛒"
            )
            for lot in lots_list[:25]
        ]
        if options:
            select = discord.ui.Select(
                placeholder="🛍️ Выбери товар",
                options=options, min_values=1, max_values=1,
                custom_id=f"lot_select_{category_id}"
            )
            select.callback = self.lot_callback
            self.add_item(select)
        close_button = discord.ui.Button(label="❌ Закрыть", style=discord.ButtonStyle.danger)
        close_button.callback = self.close_callback
        self.add_item(close_button)

    async def lot_callback(self, interaction: discord.Interaction):
        try:
            await interaction.response.defer(ephemeral=True)
            lot_id = int(interaction.data['values'][0])
            lot = await db.get_lot(lot_id)
            if not lot:
                await interaction.followup.send("❌ Товар не найден", ephemeral=True)
                return
            seller = interaction.guild.get_member(lot.seller_id)
            seller_name = seller.display_name if seller else "Продавец"
            stock_text = f"📦 В наличии: {lot.stock}" if lot.stock > 0 else "❌ Нет в наличии"

            prices_text = f"💰 **{lot.price}**"
            try:
                price_num = await parse_price_rub(lot.price)
                if price_num:
                    rates = await db.get_currency_rates()
                    if rates:
                        converted = await convert_price_rub(price_num)
                        prices_text = "💰 **Цена:**\n" + " | ".join(converted.values())
            except Exception:
                pass

            embed = discord.Embed(
                title=f"🛒 {lot.name}",
                description=(
                    f"{prices_text}\n"
                    f"**{stock_text}**\n\n"
                    f"**📝 Детальное описание:**\n{lot.full_description}\n\n"
                    f"**👤 Продавец:** {seller_name}"
                ),
                color=discord.Color.green()
            )
            embed.set_footer(text="Выбери действие ниже")
            view = LotActionView(lot_id, lot, seller)
            await interaction.followup.send(embed=embed, view=view, ephemeral=True)
        except Exception as e:
            logger.error(f"Ошибка lot_callback: {e}")
            try:
                await interaction.followup.send("❌ Ошибка при выборе товара", ephemeral=True)
            except Exception:
                pass

    async def close_callback(self, interaction: discord.Interaction):
        try:
            await interaction.message.delete()
        except (discord.NotFound, discord.HTTPException):
            await interaction.response.send_message("❌ Сообщение уже удалено", ephemeral=True)

# ================= ДЕЙСТВИЯ С ТОВАРОМ =================
class LotActionView(discord.ui.View):
    def __init__(self, lot_id: int, lot, seller):
        super().__init__(timeout=120)
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
        try:
            await interaction.response.defer(ephemeral=True)

            daily_count = await get_daily_purchase_count(interaction.user.id)
            if daily_count >= DAILY_PURCHASE_LIMIT:
                await interaction.followup.send(f"❌ Достигнут дневной лимит покупок ({DAILY_PURCHASE_LIMIT} в день).", ephemeral=True)
                return

            stock = await get_stock(self.lot_id)
            if stock <= 0:
                await interaction.followup.send("❌ Товар закончился!", ephemeral=True)
                return

            if await has_user_bought(interaction.user.id, self.lot_id):
                await interaction.followup.send("❌ Вы уже покупали этот товар!", ephemeral=True)
                return

            if await db.is_blacklisted(interaction.user.id):
                await interaction.followup.send("❌ Вы в чёрном списке и не можете совершать покупки.", ephemeral=True)
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

                ticket_channel = await interaction.guild.create_text_channel(
                    channel_name, category=category, overwrites=overwrites
                )

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
                    voice_channel = await interaction.guild.create_voice_channel(
                        f"🎙️-{safe_user}", category=category, overwrites=voice_overwrites
                    )
                except Exception as ve:
                    logger.error(f"Не удалось создать голосовой канал: {ve}")

                await db.add_ticket(
                    channel_id=ticket_channel.id,
                    user_id=interaction.user.id,
                    guild_id=interaction.guild_id,
                    voice_channel_id=voice_channel.id if voice_channel else None
                )

                embed = discord.Embed(
                    title="🛒 НОВЫЙ ЗАКАЗ",
                    description=(
                        f"**Покупатель:** {interaction.user.mention}\n"
                        f"**Товар:** {self.lot.name}\n"
                        f"**Цена:** {self.lot.price}\n\n"
                        f"**📝 Детальное описание товара:**\n{self.lot.full_description}\n\n"
                        "**📝 Инструкция для продавца:**\n"
                        "1. Расскажите покупателю о товаре.\n"
                        "2. Отправьте реквизиты для оплаты.\n"
                        "3. После оплаты передайте товар.\n"
                        "4. Закройте тикет кнопкой ниже.\n\n"
                        "**💰 Покупатель:** переведите деньги, напишите «Оплатил», получите товар."
                    ),
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
                await update_stock(self.lot_id, -1)
                await update_stats_for_seller(self.lot.seller_id, self.lot.price)

                ticket_view = View()
                close_button = Button(label="🔒 Закрыть заказ", style=discord.ButtonStyle.danger)

                async def close_ticket_callback(i: discord.Interaction):
                    if i.user == interaction.user or i.user == self.seller or is_admin_member(i.user):
                        await db.close_ticket(ticket_channel.id)
                        if voice_channel:
                            try:
                                await voice_channel.delete()
                            except Exception:
                                pass
                        await i.response.send_message("🔒 Тикет закрыт. Удаление через 24 часа.", ephemeral=False)
                    else:
                        await i.response.send_message("❌ Только покупатель, продавец или админ", ephemeral=True)

                close_button.callback = close_ticket_callback
                ticket_view.add_item(close_button)

                config2 = get_config(interaction.guild_id)
                review_channel_id = config2.get("review_channel") if config2 else None
                if review_channel_id:
                    review_button = Button(label="⭐ Оставить отзыв", style=discord.ButtonStyle.primary)

                    async def review_callback(i: discord.Interaction):
                        if i.user != interaction.user and not is_admin_member(i.user):
                            await i.response.send_message("❌ Только покупатель может оставить отзыв.", ephemeral=True)
                            return
                        modal = ReviewModal(self.seller, self.lot.name, self.lot_id)
                        await i.response.send_modal(modal)

                    review_button.callback = review_callback
                    ticket_view.add_item(review_button)

                await ticket_channel.send("✅ **Для завершения используйте кнопки ниже:**", view=ticket_view)

                if interaction.message:
                    try:
                        await interaction.message.delete()
                    except Exception:
                        pass

                await interaction.followup.send(f"✅ Заказ создан! Перейдите в {ticket_channel.mention}", ephemeral=True)
            finally:
                active_orders.discard(key)
        except Exception as e:
            logger.error(f"Ошибка buy_callback: {e}")
            active_orders.discard((interaction.user.id, self.lot_id))
            try:
                await interaction.followup.send("❌ Ошибка при создании заказа", ephemeral=True)
            except Exception:
                pass

    async def cancel_callback(self, interaction: discord.Interaction):
        await interaction.response.send_message("❌ Покупка отменена", ephemeral=True)
        if interaction.message:
            try:
                await interaction.message.delete()
            except Exception:
                pass

async def update_stats_for_seller(seller_id: int, price_str: str):
    try:
        price_num = await parse_price_rub(price_str)
        revenue = int(price_num) if price_num else 0
        await db.update_stats(seller_id, sales_inc=1, revenue_inc=revenue)
    except Exception as e:
        logger.error(f"Ошибка update_stats: {e}")

# ================= СИСТЕМА ОТЗЫВОВ =================
class ReviewModal(discord.ui.Modal, title="Оставить отзыв"):
    rating = discord.ui.TextInput(
        label="Оценка (1-5)", placeholder="1-5",
        min_length=1, max_length=1, required=True
    )
    comment = discord.ui.TextInput(
        label="Комментарий", placeholder="Ваш отзыв о товаре/продавце",
        style=discord.TextStyle.paragraph, max_length=4000, required=True
    )

    def __init__(self, seller, product: str, lot_id: int):
        super().__init__()
        self.seller = seller
        self.product = product
        self.lot_id = lot_id

    async def on_submit(self, interaction: discord.Interaction):
        if self.rating.value not in '12345':
            await interaction.response.send_message("❌ Оценка должна быть от 1 до 5", ephemeral=True)
            return
        rating = int(self.rating.value)
        stars = "⭐" * rating + "☆" * (5 - rating)

        config = get_config(interaction.guild_id)
        review_channel_id = config.get("review_channel") if config else None
        review_channel = interaction.guild.get_channel(review_channel_id) if review_channel_id else None
        if not review_channel:
            await interaction.response.send_message("❌ Канал отзывов не найден.", ephemeral=True)
            return

        if self.seller:
            await add_review(interaction.user.id, self.seller.id, self.lot_id, rating, self.comment.value)

        embed = discord.Embed(
            title="📝 Отзыв о покупке",
            description=(
                f"**Товар:** {self.product}\n"
                f"**Оценка:** {stars} ({rating}/5)\n\n"
                f"**Отзыв:**\n{self.comment.value}"
            ),
            color=discord.Color.gold()
        )
        embed.set_author(
            name=interaction.user.display_name,
            icon_url=interaction.user.avatar.url if interaction.user.avatar else None
        )
        seller_name = self.seller.name if self.seller else "Неизвестен"
        embed.set_footer(text=f"Покупатель: {interaction.user.name} | Продавец: {seller_name}")
        embed.timestamp = datetime.now(timezone.utc)

        if self.seller:
            await update_seller_review_catalog(interaction.guild, review_channel, self.seller)
        else:
            await review_channel.send(embed=embed)

        await interaction.response.send_message("✅ Спасибо за отзыв!", ephemeral=True)

        if self.seller:
            try:
                await self.seller.send(
                    f"📢 {interaction.user.mention} оставил отзыв о товаре **{self.product}**!\nОценка: {stars}"
                )
            except Exception:
                pass

async def update_seller_review_catalog(guild: discord.Guild, review_channel: discord.TextChannel, seller: discord.Member):
    """Обновляет или создаёт сообщение с отзывами продавца в канале отзывов"""
    reviews = await db.get_seller_reviews(seller.id, 20)
    avg_rating = await db.get_seller_rating(seller.id)

    embed = discord.Embed(
        title=f"⭐ Отзывы о {seller.display_name}",
        description=f"**Средний рейтинг:** {'⭐' * round(avg_rating)}{'☆' * (5 - round(avg_rating))} ({avg_rating}/5)\n**Всего отзывов:** {len(reviews)}",
        color=discord.Color.gold()
    )
    embed.set_thumbnail(url=seller.avatar.url if seller.avatar else None)

    for rev in reviews[:10]:
        buyer = guild.get_member(rev['user_id'])
        buyer_name = buyer.display_name if buyer else f"ID:{rev['user_id']}"
        stars = "⭐" * rev['rating'] + "☆" * (5 - rev['rating'])
        embed.add_field(
            name=f"{stars} от {buyer_name}",
            value=rev['comment'][:200],
            inline=False
        )

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

# ================= ТТИКЕТЫ ПОДДЕРЖКИ =================
class TicketView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="🎫 Создать тикет", style=discord.ButtonStyle.green, custom_id="create_support_ticket")
    async def create_ticket_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await handle_create_ticket(interaction)

async def handle_create_ticket(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    config = get_config(interaction.guild_id)
    admin_role_id = config["roles"].get("admin") if config else None

    now = datetime.now()
    last = user_ticket_cooldown.get(f"support_{interaction.user.id}")
    if last and (now - last).total_seconds() < 30:
        await interaction.followup.send("⏳ Подождите перед созданием нового тикета.", ephemeral=True)
        return
    user_ticket_cooldown[f"support_{interaction.user.id}"] = now

    category = discord.utils.get(interaction.guild.categories, name="Support Tickets")
    if not category:
        category = await interaction.guild.create_category("Support Tickets")

    safe_user = re.sub(r"[^a-zA-Z0-9_-]", "-", interaction.user.name.lower())[:20]
    channel_name = f"ticket-{safe_user}"

    overwrites = {
        interaction.guild.default_role: discord.PermissionOverwrite(read_messages=False),
        interaction.user: discord.PermissionOverwrite(read_messages=True, send_messages=True),
        interaction.guild.me: discord.PermissionOverwrite(read_messages=True, send_messages=True),
    }
    if admin_role_id:
        admin_role = interaction.guild.get_role(admin_role_id)
        if admin_role:
            overwrites[admin_role] = discord.PermissionOverwrite(read_messages=True, send_messages=True)

    ticket_channel = await interaction.guild.create_text_channel(
        channel_name, category=category, overwrites=overwrites
    )

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
                voice_overwrites[admin_role] = discord.PermissionOverwrite(
                    view_channel=True, connect=True, speak=True
                )
        voice_channel = await interaction.guild.create_voice_channel(
            f"🎙️-поддержка-{safe_user}", category=category, overwrites=voice_overwrites
        )
    except Exception as ve:
        logger.error(f"Голосовой канал поддержки: {ve}")

    await db.add_ticket(
        channel_id=ticket_channel.id,
        user_id=interaction.user.id,
        guild_id=interaction.guild_id,
        voice_channel_id=voice_channel.id if voice_channel else None
    )

    embed = discord.Embed(
        title="🎫 Тикет поддержки",
        description=(
            f"**Создатель:** {interaction.user.mention}\n\n"
            "Опишите вашу проблему. Администраторы скоро ответят.\n\n"
            "⏰ Тикет автоматически удаляется:\n"
            "• Через 24 часа после закрытия\n"
            "• Через 7 дней неактивности"
        ),
        color=discord.Color.blue()
    )
    if voice_channel:
        embed.add_field(name="🎙️ Голосовой канал", value=voice_channel.mention, inline=False)

    ticket_manage_view = View()
    close_btn = Button(label="🔒 Закрыть тикет", style=discord.ButtonStyle.danger, custom_id=f"close_ticket_{ticket_channel.id}")

    async def close_btn_callback(i: discord.Interaction):
        if i.user == interaction.user or is_admin_member(i.user):
            await db.close_ticket(ticket_channel.id)
            if voice_channel:
                try:
                    await voice_channel.delete()
                except Exception:
                    pass
            await i.response.send_message("🔒 Тикет закрыт. Удалится через 24 часа.", ephemeral=False)
        else:
            await i.response.send_message("❌ Нет прав для закрытия тикета.", ephemeral=True)

    close_btn.callback = close_btn_callback
    ticket_manage_view.add_item(close_btn)

    admin_mention = f"<@&{admin_role_id}>" if admin_role_id else ""
    await ticket_channel.send(content=f"{interaction.user.mention} {admin_mention}", embed=embed, view=ticket_manage_view)
    await interaction.followup.send(f"✅ Тикет создан: {ticket_channel.mention}", ephemeral=True)

# ================= СТАТУС ПОЛЬЗОВАТЕЛЯ =================
async def show_user_status(interaction: discord.Interaction, target: discord.Member = None):
    user = target or interaction.user
    await interaction.response.defer(ephemeral=True)

    purchases = await db.get_user_purchases(user.id)
    user_reviews = await db.get_user_reviews(user.id)
    stats = await db.get_stats(user.id)
    ref_count = await db.get_referral_count(user.id)

    embed = discord.Embed(
        title=f"👤 Профиль: {user.display_name}",
        color=discord.Color.blue()
    )
    embed.set_thumbnail(url=user.avatar.url if user.avatar else None)

    embed.add_field(
        name="🛒 Статистика покупок",
        value=(
            f"Всего покупок: **{len(purchases)}**\n"
            f"Продаж: **{stats['sales'] if stats else 0}**\n"
            f"Выручка: **{stats['revenue'] if stats else 0} ₽**"
        ),
        inline=False
    )

    ref_link = f"https://discord.gg/ref_{user.id}"
    embed.add_field(
        name="🔗 Реферальная ссылка",
        value=f"`{ref_link}`\nПривёл: **{ref_count}** пользователей",
        inline=False
    )

    if purchases:
        lots_cache = db.lots_cache
        purchase_text = ""
        for p in purchases[:5]:
            lot = lots_cache.get(p['lot_id'])
            lot_name = lot.name if lot else f"Товар #{p['lot_id']}"
            purchase_text += f"• {lot_name} — {p['price']} ({p['created_at'][:10]})\n"
        embed.add_field(name="📦 Последние покупки", value=purchase_text, inline=False)

    if user_reviews:
        reviews_text = ""
        for r in user_reviews[:3]:
            stars = "⭐" * r['rating']
            reviews_text += f"{stars} — {r['comment'][:60]}...\n"
        embed.add_field(name="📝 Мои отзывы", value=reviews_text, inline=False)

    await interaction.followup.send(embed=embed, ephemeral=True)

# ================= КОМАНДЫ =================
async def owner_only(interaction: discord.Interaction) -> bool:
    if not is_owner(interaction):
        await interaction.response.send_message(
            "❌ Эта команда доступна только пользователям с ролью **Owner**.", ephemeral=True
        )
        return False
    return True

async def admin_only(interaction: discord.Interaction) -> bool:
    if not is_admin(interaction):
        await interaction.response.send_message(
            "❌ Эта команда доступна только администраторам.", ephemeral=True
        )
        return False
    return True

# --- Магазин ---
@bot.tree.command(name='add_category', description='[OWNER] Добавить категорию')
@app_commands.describe(name="Название", emoji="Эмодзи")
async def add_category_cmd(interaction: discord.Interaction, name: str, emoji: str = "📁"):
    if not await owner_only(interaction):
        return
    cat_id = await db.add_category(name=name, emoji=emoji)
    await db.refresh_cache()
    await interaction.response.send_message(f"✅ Категория `{emoji} {name}` добавлена (ID: {cat_id})", ephemeral=True)
    await send_or_update_shop(interaction.guild)

@bot.tree.command(name='add_lot', description='[OWNER] Добавить товар')
@app_commands.describe(
    category_id="ID категории", name="Название", price="Цена в рублях (число)",
    stock="Количество", short_description="Короткое описание (до 100 символов)",
    full_description="Детальное описание (до 2000 символов)",
    seller="Продавец", role_id="ID роли (опционально)"
)
async def add_lot_cmd(
    interaction: discord.Interaction,
    category_id: int, name: str, price: str, stock: int,
    short_description: str, full_description: str,
    seller: discord.Member, role_id: str = None
):
    if not await owner_only(interaction):
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

    converted_prices = {}
    price_num = await parse_price_rub(price)
    if price_num:
        converted_prices = await convert_price_rub(price_num)
        price_display = " | ".join(converted_prices.values()) if converted_prices else price
    else:
        price_display = price

    lot_id = await db.add_lot(
        name=name, price=price_display, stock=stock,
        short_description=short_description, full_description=full_description,
        seller_id=seller.id, category_id=category_id, role_id=role_id_int
    )
    if converted_prices:
        await db.update_lot_prices(lot_id, converted_prices)

    await db.refresh_cache()
    await interaction.response.send_message(
        f"✅ Товар `{name}` добавлен (ID: {lot_id})\n💰 Цена: {price_display}",
        ephemeral=True
    )
    await send_or_update_shop(interaction.guild)

@bot.tree.command(name='remove_category', description='[OWNER] Удалить категорию')
@app_commands.describe(category_id="ID категории")
async def remove_category(interaction: discord.Interaction, category_id: int):
    if not await owner_only(interaction):
        return
    category = await db.get_category(category_id)
    if not category:
        await interaction.response.send_message("❌ Категория не найдена", ephemeral=True)
        return
    await db.delete_category(category_id)
    await db.refresh_cache()
    await interaction.response.send_message(f"✅ Категория `{category.name}` удалена", ephemeral=True)
    await send_or_update_shop(interaction.guild)

@bot.tree.command(name='remove_lot', description='[OWNER] Удалить товар')
@app_commands.describe(lot_id="ID товара")
async def remove_lot(interaction: discord.Interaction, lot_id: int):
    if not await owner_only(interaction):
        return
    lot = await db.get_lot(lot_id)
    if not lot:
        await interaction.response.send_message("❌ Товар не найден", ephemeral=True)
        return
    await db.delete_lot(lot_id)
    await db.refresh_cache()
    await interaction.response.send_message(f"✅ Товар `{lot.name}` удалён", ephemeral=True)
    await send_or_update_shop(interaction.guild)

@bot.tree.command(name='list_categories', description='[OWNER] Показать категории')
async def list_categories(interaction: discord.Interaction):
    if not await owner_only(interaction):
        return
    await db.refresh_cache()
    categories = db.categories_cache
    if not categories:
        await interaction.response.send_message("📭 Нет категорий", ephemeral=True)
        return
    embed = discord.Embed(title="📁 Список категорий", color=discord.Color.blue())
    for cat in categories.values():
        embed.add_field(
            name=f"{cat.emoji} {cat.name}",
            value=f"**ID:** `{cat.id}`\n**Товаров:** {len(cat.lots)}",
            inline=False
        )
    await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name='edit_category', description='[OWNER] Редактировать категорию')
@app_commands.describe(category_id="ID категории", new_name="Новое название", new_emoji="Новый эмодзи")
async def edit_category(interaction: discord.Interaction, category_id: int, new_name: str = None, new_emoji: str = None):
    if not await owner_only(interaction):
        return
    category = await db.get_category(category_id)
    if not category:
        await interaction.response.send_message(f"❌ Категория `{category_id}` не найдена", ephemeral=True)
        return
    kwargs = {}
    if new_name:
        kwargs["name"] = new_name
    if new_emoji:
        kwargs["emoji"] = new_emoji
    if kwargs:
        await db.update_category(category_id, **kwargs)
        await db.refresh_cache()
    updated = await db.get_category(category_id)
    await send_or_update_shop(interaction.guild)
    embed = discord.Embed(
        title="✅ Категория обновлена",
        description=f"**ID:** `{category_id}`\n**Название:** {updated.emoji} {updated.name}",
        color=discord.Color.green()
    )
    await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name='edit_lot', description='[OWNER] Редактировать товар')
@app_commands.describe(
    lot_id="ID товара", new_name="Новое название", new_price="Новая цена (в рублях)",
    new_stock="Новое количество", new_short_description="Новое короткое описание",
    new_full_description="Новое детальное описание",
    new_category_id="ID новой категории", new_seller="Новый продавец", new_role_id="ID роли"
)
async def edit_lot(
    interaction: discord.Interaction, lot_id: int,
    new_name: str = None, new_price: str = None, new_stock: int = None,
    new_short_description: str = None, new_full_description: str = None,
    new_category_id: int = None, new_seller: discord.Member = None, new_role_id: str = None
):
    if not await owner_only(interaction):
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
        price_num = await parse_price_rub(new_price)
        if price_num:
            converted = await convert_price_rub(price_num)
            price_display = " | ".join(converted.values())
            await db.update_lot_prices(lot_id, converted)
        else:
            price_display = new_price
        kwargs["price"] = price_display
        changes.append(f"цена: {price_display}")
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
    await send_or_update_shop(interaction.guild)
    embed = discord.Embed(
        title="✅ Товар отредактирован",
        description=f"**ID:** `{lot_id}`\n**Изменения:**\n• " + "\n• ".join(changes),
        color=discord.Color.green()
    )
    await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name='list_lots', description='[OWNER] Обновить магазин')
async def list_lots(interaction: discord.Interaction):
    if not await owner_only(interaction):
        return
    await interaction.response.defer(ephemeral=True)
    await send_or_update_shop(interaction.guild)
    await interaction.followup.send("✅ Магазин обновлён", ephemeral=True)

# --- Шаблоны товаров ---
@bot.tree.command(name='save_template', description='[OWNER] Сохранить товар как шаблон')
@app_commands.describe(lot_id="ID товара", template_name="Название шаблона")
async def save_template(interaction: discord.Interaction, lot_id: int, template_name: str):
    if not await owner_only(interaction):
        return
    lot = await db.get_lot(lot_id)
    if not lot:
        await interaction.response.send_message("❌ Товар не найден", ephemeral=True)
        return
    tmpl_id = await db.add_lot_template(
        name=template_name, price=lot.price,
        short_description=lot.short_description, full_description=lot.full_description,
        category_id=lot.category_id, seller_id=lot.seller_id, created_by=interaction.user.id
    )
    await interaction.response.send_message(f"✅ Шаблон **{template_name}** сохранён (ID: {tmpl_id})", ephemeral=True)

@bot.tree.command(name='list_templates', description='[OWNER] Список шаблонов товаров')
async def list_templates(interaction: discord.Interaction):
    if not await owner_only(interaction):
        return
    templates = await db.get_lot_templates(interaction.user.id)
    if not templates:
        await interaction.response.send_message("📭 Нет шаблонов", ephemeral=True)
        return
    embed = discord.Embed(title="📋 Шаблоны товаров", color=discord.Color.blue())
    for t in templates[:10]:
        embed.add_field(
            name=f"ID {t['id']}: {t['name']}",
            value=f"Цена: {t['price']}\n{t['short_description'][:50]}...",
            inline=False
        )
    await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name='use_template', description='[OWNER] Создать товар из шаблона')
@app_commands.describe(template_id="ID шаблона", stock="Количество в наличии")
async def use_template(interaction: discord.Interaction, template_id: int, stock: int = 1):
    if not await owner_only(interaction):
        return
    tmpl = await db.get_lot_template(template_id)
    if not tmpl:
        await interaction.response.send_message("❌ Шаблон не найден", ephemeral=True)
        return
    lot_id = await db.add_lot(
        name=tmpl['name'], price=tmpl['price'],
        short_description=tmpl['short_description'], full_description=tmpl['full_description'],
        seller_id=tmpl['seller_id'], category_id=tmpl['category_id'], stock=stock
    )
    await db.refresh_cache()
    await send_or_update_shop(interaction.guild)
    await interaction.response.send_message(
        f"✅ Товар из шаблона **{tmpl['name']}** создан (ID: {lot_id})", ephemeral=True
    )

# --- Верификация ---
@bot.tree.command(name='setup_verify', description='[OWNER] Создать панель верификации')
async def setup_verify(interaction: discord.Interaction):
    if not await owner_only(interaction):
        return
    await interaction.response.defer(ephemeral=True)
    guild = interaction.guild
    config = get_config(guild.id)
    if not config:
        await interaction.followup.send("❌ Сервер не настроен", ephemeral=True)
        return
    verify_channel_id = config.get("verify_channel")
    verify_channel = guild.get_channel(verify_channel_id) if verify_channel_id else None
    if not verify_channel:
        verify_channel = await guild.create_text_channel('верификация')
    embed = discord.Embed(
        title="🔒 Верификация",
        description="Нажми на кнопку ниже, чтобы получить доступ.",
        color=discord.Color.gold()
    )
    await verify_channel.send(embed=embed, view=VerifyView())
    await interaction.followup.send(f'✅ Канал {verify_channel.mention} готов', ephemeral=True)

@bot.tree.command(name='manverify', description='[OWNER] Принудительная верификация')
async def manual_verify(interaction: discord.Interaction, member: discord.Member):
    if not await owner_only(interaction):
        return
    config = get_config(interaction.guild_id)
    unverified_role_id = config["roles"].get("unverified") if config else None
    customer_role_id = config["roles"].get("customer") if config else None
    unverified_role = interaction.guild.get_role(unverified_role_id) if unverified_role_id else None
    customer_role = interaction.guild.get_role(customer_role_id) if customer_role_id else None
    if unverified_role and unverified_role in member.roles:
        await member.remove_roles(unverified_role)
    if customer_role:
        await member.add_roles(customer_role)
    await interaction.response.send_message(f'✅ {member.mention} верифицирован', ephemeral=True)

# --- Тикеты ---
@bot.tree.command(name='setup_tickets', description='[OWNER] Создать панель тикетов в канале поддержки')
async def setup_tickets(interaction: discord.Interaction):
    if not await owner_only(interaction):
        return
    await interaction.response.defer(ephemeral=True)
    config = get_config(interaction.guild_id)
    ticket_channel_id = config.get("ticket_channel") if config else None
    ticket_channel = interaction.guild.get_channel(ticket_channel_id) if ticket_channel_id else interaction.channel
    embed = discord.Embed(
        title="🎫 Поддержка TALENT SHOP",
        description=(
            "Нажмите кнопку ниже, чтобы создать тикет.\n\n"
            "📌 Тикет создаёт приватный канал и голосовой канал.\n"
            "⏰ Автоудаление через 24 часа после закрытия или 7 дней неактивности."
        ),
        color=discord.Color.blue()
    )
    await ticket_channel.send(embed=embed, view=TicketView())
    await interaction.followup.send(f"✅ Панель тикетов создана в {ticket_channel.mention}", ephemeral=True)

@bot.tree.command(name='close', description='Закрыть текущий тикет')
async def close(interaction: discord.Interaction):
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

# --- Чёрный список ---
@bot.tree.command(name='blacklist_add', description='[ADMIN] Добавить в чёрный список')
@app_commands.describe(member="Пользователь", reason="Причина")
async def blacklist_add(interaction: discord.Interaction, member: discord.Member, reason: str = "Не указана"):
    if not await admin_only(interaction):
        return
    await db.add_to_blacklist(member.id, interaction.user.id, reason)
    embed = discord.Embed(
        title="🚫 Пользователь заблокирован",
        description=f"**Пользователь:** {member.mention}\n**Причина:** {reason}\n**Модератор:** {interaction.user.mention}",
        color=discord.Color.red()
    )
    await interaction.response.send_message(embed=embed)
    try:
        await member.send(f"🚫 Вы добавлены в чёрный список.\n**Причина:** {reason}")
    except Exception:
        pass

@bot.tree.command(name='blacklist_remove', description='[ADMIN] Убрать из чёрного списка')
@app_commands.describe(member="Пользователь", reason="Причина разблокировки")
async def blacklist_remove(interaction: discord.Interaction, member: discord.Member, reason: str = "Разблокировка"):
    if not await admin_only(interaction):
        return
    await db.remove_from_blacklist(member.id, interaction.user.id, reason)
    await interaction.response.send_message(f"✅ {member.mention} удалён из чёрного списка.", ephemeral=True)

@bot.tree.command(name='blacklist_info', description='[ADMIN] Информация о бане пользователя')
@app_commands.describe(member="Пользователь")
async def blacklist_info(interaction: discord.Interaction, member: discord.Member):
    if not await admin_only(interaction):
        return
    info = await db.get_blacklist_info(member.id)
    history = await db.get_ban_history(member.id)
    is_banned = await db.is_blacklisted(member.id)

    embed = discord.Embed(
        title=f"📋 История банов: {member.display_name}",
        color=discord.Color.red() if is_banned else discord.Color.green()
    )
    embed.add_field(name="Статус", value="🚫 В чёрном списке" if is_banned else "✅ Чист", inline=False)
    if info:
        embed.add_field(name="Причина бана", value=info['reason'], inline=False)
        embed.add_field(name="Дата", value=info['created_at'][:10], inline=True)
    if history:
        hist_text = ""
        for h in history[:5]:
            action_icon = "🚫" if h['action'] == 'ban' else "✅"
            hist_text += f"{action_icon} {h['action']} — {h['reason'][:50]} ({h['created_at'][:10]})\n"
        embed.add_field(name="История", value=hist_text, inline=False)
    await interaction.response.send_message(embed=embed, ephemeral=True)

# --- Предупреждения ---
@bot.tree.command(name='warn', description='[ADMIN] Выдать предупреждение')
@app_commands.describe(member="Пользователь", reason="Причина")
async def warn(interaction: discord.Interaction, member: discord.Member, reason: str):
    if not await admin_only(interaction):
        return
    if is_admin_member(member):
        await interaction.response.send_message("❌ Нельзя предупредить администратора", ephemeral=True)
        return
    count = await db.add_warning(member.id, interaction.user.id, reason)
    embed = discord.Embed(
        title="⚠️ Предупреждение",
        description=f"**Пользователь:** {member.mention}\n**Причина:** {reason}\n**Предупреждений:** {count}/{MAX_WARNINGS_BEFORE_BAN}",
        color=discord.Color.orange()
    )
    await interaction.response.send_message(embed=embed)
    if count >= MAX_WARNINGS_BEFORE_BAN:
        await db.add_to_blacklist(member.id, interaction.user.id, f"Автоматический бан: {count} предупреждений")
        try:
            await member.ban(reason=f"Автобан: {count} предупреждений")
        except Exception as e:
            logger.error(f"Не удалось забанить пользователя {member.id} на сервере: {e}")
        await interaction.channel.send(f"🚫 {member.mention} забанен автоматически за {count} предупреждения.")

@bot.tree.command(name='warnings', description='[ADMIN] Предупреждения пользователя')
@app_commands.describe(member="Пользователь")
async def warnings_cmd(interaction: discord.Interaction, member: discord.Member):
    if not await admin_only(interaction):
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

@bot.tree.command(name='clearwarnings', description='[ADMIN] Очистить предупреждения')
@app_commands.describe(member="Пользователь")
async def clearwarnings(interaction: discord.Interaction, member: discord.Member):
    if not await admin_only(interaction):
        return
    await db.clear_warnings(member.id)
    await interaction.response.send_message(f"✅ Предупреждения {member.mention} очищены", ephemeral=True)

# --- Роли продавца/покупателя ---
@bot.tree.command(name='add_seller_role', description='[OWNER] Выдать роль продавца')
async def add_seller_role(interaction: discord.Interaction, member: discord.Member):
    if not await owner_only(interaction):
        return
    config = get_config(interaction.guild_id)
    seller_role_id = config["roles"].get("seller") if config else None
    if not seller_role_id:
        await interaction.response.send_message("❌ Нет роли продавца", ephemeral=True)
        return
    seller_role = interaction.guild.get_role(seller_role_id)
    if seller_role in member.roles:
        await interaction.response.send_message(f"⚠️ У {member.mention} уже есть роль продавца", ephemeral=True)
        return
    await member.add_roles(seller_role)
    await interaction.response.send_message(f"✅ {member.mention} → роль продавца", ephemeral=True)

@bot.tree.command(name='buy', description='[OWNER] Выдать роль Buyer')
async def buy_role(interaction: discord.Interaction, member: discord.Member, product: str):
    if not await owner_only(interaction):
        return
    config = get_config(interaction.guild_id)
    buyer_role_id = config["roles"].get("buyer") if config else None
    if not buyer_role_id:
        await interaction.response.send_message("⚠️ Нет роли Buyer", ephemeral=True)
        return
    buyer_role = interaction.guild.get_role(buyer_role_id)
    customer_role = interaction.guild.get_role(config["roles"].get("customer")) if config else None
    unverified_role = interaction.guild.get_role(config["roles"].get("unverified")) if config else None
    await member.add_roles(buyer_role)
    if customer_role and customer_role in member.roles:
        await member.remove_roles(customer_role)
    if unverified_role and unverified_role in member.roles:
        await member.remove_roles(unverified_role)
    await interaction.response.send_message(f'✅ {member.mention} получил роль Buyer', ephemeral=True)

# --- Рейтинг продавца ---
@bot.tree.command(name='seller_rating', description='Посмотреть рейтинг продавца')
async def seller_rating(interaction: discord.Interaction, seller: discord.Member):
    rating = await get_seller_rating(seller.id)
    reviews = await get_seller_reviews(seller.id, 5)
    embed = discord.Embed(
        title=f"⭐ Рейтинг {seller.display_name}",
        description=f"**Средняя оценка:** {rating}/5.0",
        color=discord.Color.gold()
    )
    if reviews:
        text = ""
        for r in reviews[:5]:
            stars = "⭐" * r['rating'] + "☆" * (5 - r['rating'])
            text += f"{stars} — {r['comment'][:50]}\n"
        embed.add_field(name="Последние отзывы", value=text, inline=False)
    await interaction.response.send_message(embed=embed, ephemeral=True)

# --- Статистика продавцов ---
@bot.tree.command(name='seller_stats', description='[ADMIN] Статистика продавцов')
async def seller_stats(interaction: discord.Interaction):
    if not await admin_only(interaction):
        return
    top_revenue = await db.get_seller_stats_top(10)
    top_sales = await db.get_seller_top_sales(10)
    embed = discord.Embed(title="📊 Статистика продавцов", color=discord.Color.gold())
    if top_revenue:
        rev_text = ""
        for i, s in enumerate(top_revenue[:5], 1):
            seller = interaction.guild.get_member(s['user_id'])
            name = seller.display_name if seller else f"ID:{s['user_id']}"
            rev_text += f"{i}. {name} — {s['revenue']} ₽\n"
        embed.add_field(name="💰 Топ по выручке", value=rev_text or "Нет данных", inline=False)
    if top_sales:
        sales_text = ""
        for i, s in enumerate(top_sales[:5], 1):
            seller = interaction.guild.get_member(s['user_id'])
            name = seller.display_name if seller else f"ID:{s['user_id']}"
            sales_text += f"{i}. {name} — {s['sales']} продаж\n"
        embed.add_field(name="🛒 Топ по продажам", value=sales_text or "Нет данных", inline=False)
    await interaction.response.send_message(embed=embed, ephemeral=True)

# --- Профиль пользователя ---
@bot.tree.command(name='profile', description='Статус и статистика пользователя')
@app_commands.describe(member="Пользователь (опционально)")
async def profile(interaction: discord.Interaction, member: discord.Member = None):
    await show_user_status(interaction, member)

# --- Поиск заказов ---
@bot.tree.command(name='search_orders', description='[ADMIN] Поиск заказов по пользователю или датам')
@app_commands.describe(
    member="Пользователь (опционально)",
    start_date="С даты (YYYY-MM-DD, опционально)",
    end_date="По дату (YYYY-MM-DD, опционально)"
)
async def search_orders(
    interaction: discord.Interaction,
    member: discord.Member = None,
    start_date: str = None,
    end_date: str = None
):
    if not await admin_only(interaction):
        return
    await interaction.response.defer(ephemeral=True)
    purchases = []
    if member:
        purchases = await db.search_purchases_by_user(member.id)
    elif start_date or end_date:
        sd = start_date or "2000-01-01"
        ed = end_date or datetime.now().strftime("%Y-%m-%d")
        purchases = await db.get_purchases_by_date_range(sd, ed)
    else:
        await interaction.followup.send("❌ Укажите пользователя или диапазон дат.", ephemeral=True)
        return

    if not purchases:
        await interaction.followup.send("📭 Заказы не найдены.", ephemeral=True)
        return

    embed = discord.Embed(title=f"🔍 Результаты поиска ({len(purchases)} заказов)", color=discord.Color.blue())
    lots_cache = db.lots_cache
    for p in purchases[:15]:
        lot = lots_cache.get(p['lot_id'])
        lot_name = lot.name if lot else f"ID:{p['lot_id']}"
        user = interaction.guild.get_member(p['user_id'])
        user_name = user.display_name if user else f"ID:{p['user_id']}"
        embed.add_field(
            name=f"#{p['id']} — {lot_name}",
            value=f"Покупатель: {user_name}\nЦена: {p['price']}\nДата: {p['created_at'][:10]}",
            inline=False
        )
    await interaction.followup.send(embed=embed, ephemeral=True)

# --- Отзывы ---
@bot.tree.command(name='review', description='Оставить отзыв о покупке')
async def review(interaction: discord.Interaction, seller: discord.Member, product: str, lot_id: int):
    config = get_config(interaction.guild_id)
    if not config or not config.get("review_channel"):
        await interaction.response.send_message("❌ На этом сервере нет системы отзывов", ephemeral=True)
        return
    await interaction.response.send_modal(ReviewModal(seller, product, lot_id))

@bot.tree.command(name='reviews', description='Посмотреть отзывы о продавце')
async def reviews(interaction: discord.Interaction, seller: discord.Member):
    rating = await get_seller_rating(seller.id)
    reviews_list = await get_seller_reviews(seller.id, 10)
    embed = discord.Embed(
        title=f"⭐ Отзывы о {seller.display_name}",
        description=f"**Средний рейтинг:** {rating}/5.0",
        color=discord.Color.gold()
    )
    if reviews_list:
        text = ""
        for r in reviews_list[:10]:
            stars = "⭐" * r['rating'] + "☆" * (5 - r['rating'])
            text += f"{stars} — {r['comment'][:80]}\n"
        embed.add_field(name="Отзывы", value=text, inline=False)
    await interaction.response.send_message(embed=embed, ephemeral=True)

# --- Создать тикет через команду ---
@bot.tree.command(name='create_ticket', description='Создать тикет поддержки')
async def create_ticket_cmd(interaction: discord.Interaction):
    await handle_create_ticket(interaction)

# --- Бэкап ---
@bot.tree.command(name='backup', description='[OWNER] Экспортировать данные в JSON')
async def backup_data(interaction: discord.Interaction):
    if not await owner_only(interaction):
        return
    await interaction.response.defer(ephemeral=True)
    await db.refresh_cache()
    categories = db.categories_cache
    lots = db.lots_cache
    export = {
        "categories": {str(cat.id): {"name": cat.name, "emoji": cat.emoji, "lots": cat.lots} for cat in categories.values()},
        "lots": {str(lot.lot_id): {"name": lot.name, "price": lot.price, "stock": lot.stock, "seller_id": lot.seller_id, "category_id": lot.category_id, "role_id": lot.role_id} for lot in lots.values()},
        "exported_at": datetime.now(timezone.utc).isoformat()
    }
    channel = bot.get_channel(BACKUP_CHANNEL_ID)
    if not channel:
        await interaction.followup.send("❌ Канал для бэкапов не найден", ephemeral=True)
        return
    json_bytes = json.dumps(export, ensure_ascii=False, indent=2).encode('utf-8')
    filename = f"shop_backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    await channel.send(file=discord.File(io.BytesIO(json_bytes), filename))
    await interaction.followup.send(f"✅ Бэкап отправлен в {channel.mention}", ephemeral=True)

# --- Служебные ---
@bot.tree.command(name='show_path', description='[OWNER] Показать путь к БД')
async def show_path(interaction: discord.Interaction):
    if not await owner_only(interaction):
        return
    await interaction.response.send_message(
        f"📁 **База данных:**\n`{db.DB_PATH}`\n**Файл существует:** {os.path.exists(db.DB_PATH)}",
        ephemeral=True
    )

@bot.tree.command(name='admin_panel', description='[OWNER] Открыть админ-панель')
async def admin_panel(interaction: discord.Interaction):
    if not await owner_only(interaction):
        return
    embed = discord.Embed(
        title="🎛️ Админ-панель TALENT SHOP",
        color=discord.Color.gold()
    )
    embed.add_field(name="📁 Категории", value="• `/add_category`\n• `/edit_category`\n• `/remove_category`\n• `/list_categories`", inline=True)
    embed.add_field(name="🛒 Товары", value="• `/add_lot`\n• `/edit_lot`\n• `/remove_lot`\n• `/list_lots`", inline=True)
    embed.add_field(name="📋 Шаблоны", value="• `/save_template`\n• `/list_templates`\n• `/use_template`", inline=True)
    embed.add_field(name="⭐ Отзывы", value="• `/review`\n• `/reviews`\n• `/seller_rating`", inline=True)
    embed.add_field(name="🚫 Модерация", value="• `/warn`\n• `/warnings`\n• `/clearwarnings`\n• `/blacklist_add`\n• `/blacklist_remove`\n• `/blacklist_info`", inline=True)
    embed.add_field(name="📊 Статистика", value="• `/seller_stats`\n• `/profile`\n• `/search_orders`", inline=True)
    embed.add_field(name="⚙️ Прочее", value="• `/backup`\n• `/setup_verify`\n• `/setup_tickets`\n• `/manverify`\n• `/add_seller_role`", inline=True)
    await interaction.response.send_message(embed=embed, ephemeral=True)

# ================= ФОНОВЫЕ ЗАДАЧИ =================
async def send_scheduled_backup():
    await db.refresh_cache()
    categories = db.categories_cache
    lots = db.lots_cache
    export = {
        "categories": {str(c.id): {"name": c.name, "emoji": c.emoji, "lots": c.lots} for c in categories.values()},
        "lots": {str(l.lot_id): {"name": l.name, "price": l.price, "stock": l.stock, "seller_id": l.seller_id, "category_id": l.category_id} for l in lots.values()},
        "exported_at": datetime.now(timezone.utc).isoformat()
    }
    channel = bot.get_channel(BACKUP_CHANNEL_ID)
    if channel:
        json_bytes = json.dumps(export, ensure_ascii=False, indent=2).encode('utf-8')
        await channel.send(
            content="🔄 **Плановый бэкап**",
            file=discord.File(io.BytesIO(json_bytes), f"scheduled_backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json")
        )
        logger.info("✅ Плановый бэкап отправлен")

async def auto_backup_scheduler():
    await bot.wait_until_ready()
    while not bot.is_closed():
        await asyncio.sleep(43200)  # 12 часов
        try:
            await send_scheduled_backup()
        except Exception as e:
            logger.error(f"Ошибка бэкапа: {e}")

async def auto_cleanup_tickets():
    """Автоматическое удаление тикетов"""
    await bot.wait_until_ready()
    while not bot.is_closed():
        await asyncio.sleep(3600)  # каждый час
        try:
            expired = await db.get_expired_tickets()
            for ticket in expired:
                guild = bot.get_guild(ticket['guild_id'])
                if not guild:
                    continue
                channel = guild.get_channel(ticket['channel_id'])
                if channel:
                    try:
                        await channel.delete(reason="Автоудаление тикета")
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
            if expired:
                logger.info(f"🗑️ Удалено тикетов: {len(expired)}")
        except Exception as e:
            logger.error(f"Ошибка очистки тикетов: {e}")

async def auto_update_currency():
    """Обновление курса валют каждые 6 часов"""
    await bot.wait_until_ready()
    while not bot.is_closed():
        try:
            rates = await fetch_currency_rates()
            if rates:
                logger.info(f"💱 Курсы обновлены: {rates}")
        except Exception as e:
            logger.error(f"Ошибка обновления курсов: {e}")
        await asyncio.sleep(21600)  # 6 часов

async def cleanup_spam_cache():
    await bot.wait_until_ready()
    while not bot.is_closed():
        await asyncio.sleep(86400)
        cutoff = datetime.now() - timedelta(hours=2)
        stale = [uid for uid, ts in user_mention_last_reset.items() if ts < cutoff]
        for uid in stale:
            user_mention_count.pop(uid, None)
            user_mention_last_reset.pop(uid, None)
        if stale:
            logger.info(f"🧹 Очищено {len(stale)} записей анти-спам кэша")

# ================= СОБЫТИЯ =================
@bot.event
async def on_ready():
    global _startup_done
    if _startup_done:
        logger.info(f"♻️ Reconnect: {bot.user}")
        return
    _startup_done = True

    await db.init_db()
    await db.refresh_cache()
    logger.info(f'✅ БД инициализирована')
    logger.info(f'✅ Бот готов: {bot.user}')

    await bot.tree.sync()
    logger.info('✅ Слеш-команды синхронизированы')

    bot.add_view(VerifyView())
    bot.add_view(TicketView())

    bot.loop.create_task(auto_backup_scheduler())
    bot.loop.create_task(cleanup_spam_cache())
    bot.loop.create_task(auto_cleanup_tickets())
    bot.loop.create_task(auto_update_currency())

    VERIFY_CHANNEL_ID = 1500257894858358895
    SHOP_CHANNEL_ID = 1500275827441532948
    TALENT_SHOP_GUILD_ID = 1462375742401675294

    verify_channel = bot.get_channel(VERIFY_CHANNEL_ID)
    if verify_channel:
        try:
            async for msg in verify_channel.history(limit=100):
                if msg.author == bot.user:
                    await msg.delete()
        except Exception as e:
            logger.error(f"Не удалось удалить старые сообщения верификации: {e}")
        embed = discord.Embed(
            title="🔒 Верификация",
            description="Нажми на кнопку ниже, чтобы получить доступ к серверу.",
            color=discord.Color.gold()
        )
        await verify_channel.send(embed=embed, view=VerifyView())
        logger.info("✅ Панель верификации отправлена")

    guild = bot.get_guild(TALENT_SHOP_GUILD_ID)
    shop_channel = bot.get_channel(SHOP_CHANNEL_ID)
    if shop_channel and guild:
        await send_or_update_shop(guild)
        logger.info("✅ Панель магазина отправлена")

    for guild in bot.guilds:
        config = get_config(guild.id)
        if not config:
            continue
        unverified_role_id = config["roles"].get("unverified")
        customer_role_id = config["roles"].get("customer")
        buyer_role_id = config["roles"].get("buyer")
        if not unverified_role_id:
            continue
        unverified_role = guild.get_role(unverified_role_id)
        if not unverified_role:
            continue
        customer_role = guild.get_role(customer_role_id) if customer_role_id else None
        buyer_role = guild.get_role(buyer_role_id) if buyer_role_id else None
        for member in guild.members:
            if member.bot or is_admin_member(member):
                continue
            has_role = (
                (customer_role and customer_role in member.roles) or
                (buyer_role and buyer_role in member.roles)
            )
            if not has_role and unverified_role not in member.roles:
                try:
                    await member.add_roles(unverified_role)
                except Exception:
                    pass

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
            embed = discord.Embed(
                title=f"👋 Добро пожаловать, {member.name}!",
                description=(
                    f"**Добро пожаловать в {member.guild.name}!**\n\n"
                    f"📌 **Как пройти верификацию:**\n"
                    f"1. Перейдите в <#{config['verify_channel']}>\n"
                    f"2. Нажмите «✅ Верифицироваться»"
                ),
                color=discord.Color.green()
            )
            await welcome_channel.send(content=member.mention, embed=embed)

@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        await bot.process_commands(message)
        return
    if is_admin_member(message.author):
        await bot.process_commands(message)
        return

    ticket = await db.get_ticket(message.channel.id)
    if ticket and ticket['status'] == 'open':
        await db.update_ticket_activity(message.channel.id)

    for pattern in BANNED_PATTERNS:
        if re.search(pattern, message.content.lower()):
            try:
                await message.delete()
                await mute_member(message.author, 3600, "Запрещённая ссылка")
            except Exception:
                pass
            await bot.process_commands(message)
            return
    mention_count = len(message.mentions) + len(message.role_mentions)
    if mention_count >= MENTION_LIMIT:
        user_id = message.author.id
        now = datetime.now()
        if user_id in user_mention_last_reset:
            if (now - user_mention_last_reset[user_id]).seconds > 60:
                user_mention_count[user_id] = 1
            else:
                user_mention_count[user_id] = user_mention_count.get(user_id, 0) + 1
        else:
            user_mention_count[user_id] = 1
        user_mention_last_reset[user_id] = now
        duration = get_mute_duration(user_id)
        try:
            await mute_member(message.author, duration, f"Спам упоминаниями ({mention_count})")
            await message.delete()
        except Exception:
            pass
        await bot.process_commands(message)
        return
    await bot.process_commands(message)

# ================= ЗАПУСК =================
token = os.getenv('DISCORD_TOKEN')
if not token:
    raise RuntimeError("❌ DISCORD_TOKEN не задан!")

bot.run(token)