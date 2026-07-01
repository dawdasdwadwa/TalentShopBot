import discord
from discord.ui import TextInput, Modal
from discord.ext import commands
from discord.ui import Button, View
from discord import app_commands
from embed_builder import setup_embed_builder
import os
import re
import sys
import io
import asyncio
import aiohttp
import logging
from typing import Optional, Dict, List
from datetime import datetime, timedelta, timezone
import database as db
from database import (
    has_user_bought, update_stock, get_stock, add_purchase,
    add_review, get_seller_rating, get_seller_reviews,
    get_daily_purchase_count, convert_price_rub
)

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8')

# ================= КОНСТАНТЫ =================
TICKET_SUPPORT_CATEGORY_ID = 1503176090980454531
TICKET_ARCHIVE_CATEGORY_ID = 1507376570082267167
TICKET_CHANNEL_ID          = 1500242313211805788
BACKUP_CHANNEL_ID          = 1503146387129368718
BACKUP_MAX_MESSAGES        = 50
ADMIN_PANEL_CHANNEL_ID     = 1503168213016641536

# ================= КОНФИГУРАЦИЯ СЕРВЕРА =================
CONFIG = {
    1462375742401675294: {
        "name": "TALENT SHOP",
        "welcome_channel": 1500249815953703004,
        "verify_channel":  1500257894858358895,
        "review_channel":  1500261460075479222,
        "log_channel":     1500263242465935492,
        "admin_log_channel": 1500275827441532948,
        "shop_channel":    1500275827441532948,
        "ticket_channel":  TICKET_CHANNEL_ID,
        "status_channel":  1506750339783725218,
        "roles": {
            "owner":      1500243730618126428,
            "admin":      1500243731519901898,
            "customer":   1500243735143907469,
            "unverified": 1500250293206515762,
            "seller":     1500291856259612672,
            "buyer":      1500243733675773972,
        }
    }
}

OWNER_ID             = 1500198262026539099
SHOP_IMAGE_LINK      = "https://i.postimg.cc/43SZJkLJ/Magazin.png"
DAILY_PURCHASE_LIMIT = 10
TICKET_CATEGORY_NAME = 'Tickets'
TICKET_COOLDOWN_SECONDS = 5

# ================= ПРАВА =================
def get_config(guild_id: int):
    return CONFIG.get(guild_id)

def is_owner(interaction: discord.Interaction) -> bool:
    config = get_config(interaction.guild_id)
    if not config:
        return False
    role_id = config["roles"].get("owner")
    if not role_id:
        return False
    role = interaction.guild.get_role(role_id)
    return role and role in interaction.user.roles

def is_admin_member(member: discord.Member) -> bool:
    config = get_config(member.guild.id)
    if not config:
        return False
    for role_key in ("owner", "admin"):
        role_id = config["roles"].get(role_key)
        if role_id:
            role = member.guild.get_role(role_id)
            if role and role in member.roles:
                return True
    return False

# ================= БОТ =================
intents = discord.Intents.default()
intents.members = True
intents.message_content = True
bot = commands.Bot(command_prefix='!', intents=intents)

_startup_done        = False
active_orders: set   = set()
user_ticket_cooldown: dict = {}
_shop_update_lock    = asyncio.Lock()

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
                    result = {
                        "UAH": rates.get("UAH", 0),
                        "USD": rates.get("USD", 0),
                        "EUR": rates.get("EUR", 0),
                    }
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
        unverified_role = interaction.guild.get_role(config["roles"].get("unverified"))
        customer_role   = interaction.guild.get_role(config["roles"].get("customer"))
        if customer_role:
            if unverified_role and unverified_role in interaction.user.roles:
                await interaction.user.remove_roles(unverified_role)
            await interaction.user.add_roles(customer_role)
            await interaction.followup.send("✅ Вы верифицированы!", ephemeral=True)
        else:
            if unverified_role and unverified_role in interaction.user.roles:
                await interaction.user.remove_roles(unverified_role)
            await interaction.followup.send("✅ Верификация пройдена!", ephemeral=True)

class VerifyView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        self.add_item(VerifyButton())

# ================= ТИКЕТЫ ПОДДЕРЖКИ =================
class TicketModal(discord.ui.Modal, title="Создание тикета поддержки"):
    subject     = discord.ui.TextInput(label="Тема обращения", placeholder="Кратко опишите проблему...", min_length=5, max_length=100)
    description = discord.ui.TextInput(label="Описание", placeholder="Подробно опишите вашу проблему...", style=discord.TextStyle.paragraph, min_length=10, max_length=2000)

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
        safe_user    = re.sub(r'[^a-zA-Z0-9_-]', '-', interaction.user.name.lower())[:20]
        channel_name = f"ticket-{safe_user}-{interaction.user.id % 10000}"
        overwrites = {
            interaction.guild.default_role: discord.PermissionOverwrite(read_messages=False),
            interaction.user:               discord.PermissionOverwrite(read_messages=True, send_messages=True, attach_files=True),
            interaction.guild.me:           discord.PermissionOverwrite(read_messages=True, send_messages=True, manage_channels=True),
        }
        admin_role_id = get_config(interaction.guild_id)["roles"].get("admin")
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
                f"Администраторы скоро ответят.\nДля закрытия используйте кнопку ниже."
            ),
            color=discord.Color.blue()
        )
        view = TicketControlView(ticket_channel.id, interaction.user.id)
        await ticket_channel.send(content=interaction.user.mention, embed=embed, view=view)
        await interaction.followup.send(f"✅ Тикет создан! Перейдите в {ticket_channel.mention}", ephemeral=True)

class TicketControlView(discord.ui.View):
    def __init__(self, channel_id: int, user_id: int):
        super().__init__(timeout=None)
        self.channel_id = channel_id
        self.user_id    = user_id

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

# ================= МАГАЗИН =================
async def _fetch_channel_safe(channel_id: int, retries: int = 5) -> Optional[discord.TextChannel]:
    for _ in range(retries):
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
        async for msg in channel.history(limit=500):
            if msg.author == bot.user:
                messages.append(msg)
        if len(messages) <= BACKUP_MAX_MESSAGES:
            return
        to_delete = messages[BACKUP_MAX_MESSAGES:]
        # bulk purge (сообщения < 14 дней) — иначе по одному
        bulk = [m for m in to_delete if (datetime.now(timezone.utc) - m.created_at).days < 14]
        old  = [m for m in to_delete if m not in bulk]
        if bulk:
            await channel.purge(limit=None, check=lambda m: m in bulk)
        for m in old:
            try:
                await m.delete()
                await asyncio.sleep(0.5)
            except Exception:
                pass
        logger.info(f"✅ Ротация бэкапов: удалено {len(to_delete)}")
    except Exception as e:
        logger.error(f"Ошибка ротации бэкапов: {e}")

async def save_backup(reason: str = "manual"):
    try:
        backup_json = await db.create_backup(bot)
        if not backup_json:
            logger.error("Не удалось создать бэкап")
            return False
        backup_file = discord.File(
            io.BytesIO(backup_json.encode('utf-8')),
            filename=f"shop_backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
        )
        channel = bot.get_channel(BACKUP_CHANNEL_ID) or await bot.fetch_channel(BACKUP_CHANNEL_ID)
        if channel:
            await channel.send(f"💾 Бэкап ({reason}) от {datetime.now().strftime('%d.%m.%Y %H:%M:%S')}", file=backup_file)
            await asyncio.sleep(1)
            await rotate_backup_channel(channel)
            logger.info(f"✅ Бэкап сохранён: {reason}")
            return True
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
        await channel.purge(limit=100, check=lambda m: m.author == bot.user)
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
    query = discord.ui.TextInput(label="Название товара или категории", placeholder="Введите название...", min_length=1, max_length=200)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        search_term  = self.query.value.strip()
        matched_cats = [c for c in db.categories_cache.values() if search_term.lower() in c.name.lower()]
        matched_lots = await db.search_lots(search_term)
        if not matched_cats and not matched_lots:
            await interaction.followup.send(f"❌ По запросу **{search_term}** ничего не найдено.", ephemeral=True)
            return
        embed = discord.Embed(title=f"🔍 Результаты поиска: {search_term}", color=discord.Color.blue())
        if matched_cats:
            embed.add_field(
                name="📁 Категории",
                value="\n".join(f"{c.emoji} **{c.name}** — товаров: {len(c.lots)}" for c in matched_cats),
                inline=False
            )
        if matched_lots:
            lots_text = "".join(f"{'✅' if lot.stock > 0 else '❌'} **{lot.name}** — {lot.price}\n" for lot in matched_lots)
            embed.add_field(name="🛒 Товары", value=lots_text[:1000], inline=False)
        view = discord.ui.View(timeout=None)
        if matched_lots:
            options = [
                discord.SelectOption(label=f"{lot.name[:50]} - {lot.price[:20]}", value=str(lot.lot_id), emoji="🛒")
                for lot in matched_lots[:25]
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
    seller      = interaction.guild.get_member(lot.seller_id)
    seller_name = seller.display_name if seller else "Продавец"
    stock_text  = f"📦 В наличии: {lot.stock}" if lot.stock > 0 else "❌ Нет в наличии"
    embed = discord.Embed(
        title=f"🛒 {lot.name}",
        description=f"💰 **{lot.price}**\n{stock_text}\n\n**📝 Описание:**\n{lot.full_description}\n\n**👤 Продавец:** {seller_name}",
        color=discord.Color.green()
    )
    if lot.image_url and lot.image_url.startswith(('http://', 'https://')):
        embed.set_thumbnail(url=lot.image_url)
    await interaction.followup.send(embed=embed, view=LotActionView(lot_id, lot, seller), ephemeral=True)

# ================= ShopView =================
class ShopView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        self._build()

    def _build(self):
        self.clear_items()
        options = [
            discord.SelectOption(label=cat.name, value=str(cat.id), emoji=cat.emoji, description=f"Товаров: {len(cat.lots)}")
            for cat in db.categories_cache.values()
            if getattr(cat, 'parent_id', None) is None
        ]
        if options:
            select = discord.ui.Select(placeholder="📁 Выберите категорию...", options=options[:25])
            select.callback = self.category_callback
            self.add_item(select)
        search_btn          = discord.ui.Button(label="🔍 Поиск", style=discord.ButtonStyle.primary)
        search_btn.callback = self.search_callback
        self.add_item(search_btn)

    async def search_callback(self, interaction: discord.Interaction):
        await interaction.response.send_modal(ShopSearchModal())

    async def category_callback(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        category_id = int(interaction.data['values'][0])
        subcats     = await db.get_subcategories(category_id)
        if subcats:
            category = await db.get_category(category_id)
            embed    = discord.Embed(title=f"{category.emoji} {category.name}", color=discord.Color.blue())
            await interaction.followup.send(embed=embed, view=SubCategoryView(subcats), ephemeral=True)
        else:
            await _show_lots(interaction, category_id)

# ================= ПОДКАТЕГОРИИ (бесконечная рекурсия) =================
class SubCategoryView(discord.ui.View):
    def __init__(self, subcats: list):
        super().__init__(timeout=None)
        options = [
            discord.SelectOption(label=cat.name, value=str(cat.id), emoji=cat.emoji)
            for cat in subcats
        ]
        select          = discord.ui.Select(placeholder="📂 Выберите подкатегорию...", options=options[:25])
        select.callback = self.subcat_callback
        self.add_item(select)

    async def subcat_callback(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        category_id = int(interaction.data['values'][0])
        subcats     = await db.get_subcategories(category_id)
        if subcats:
            category = await db.get_category(category_id)
            embed    = discord.Embed(title=f"{category.emoji} {category.name}", color=discord.Color.blue())
            await interaction.followup.send(embed=embed, view=SubCategoryView(subcats), ephemeral=True)
        else:
            await _show_lots(interaction, category_id)

async def _show_lots(interaction: discord.Interaction, category_id: int):
    lots = await db.get_lots_by_category_full(category_id)
    if not lots:
        await interaction.followup.send("В этой категории нет товаров.", ephemeral=True)
        return
    category = await db.get_category(category_id)
    embed    = discord.Embed(title=f"{category.emoji} {category.name}", color=discord.Color.blue())
    for lot in lots:
        stock_text = "♾️" if lot.stock == -1 else (f"📦 {lot.stock}" if lot.stock > 0 else "❌")
        embed.add_field(name=f"🛒 {lot.name}", value=f"💰 {lot.price}\n{stock_text}", inline=False)
    await interaction.followup.send(embed=embed, view=LotsView(category_id, lots), ephemeral=True)

# ================= СПИСОК ТОВАРОВ =================
class LotsView(discord.ui.View):
    def __init__(self, category_id: int, lots_list: list):
        super().__init__(timeout=None)
        self.category_id = category_id
        options = [
            discord.SelectOption(
                label=f"{lot.name} - {lot.price}"[:100],
                description=lot.short_description[:50] if lot.short_description else None,
                value=str(lot.lot_id), emoji="🛒"
            )
            for lot in lots_list[:25]
        ]
        if options:
            select          = discord.ui.Select(placeholder="🛍️ Выбери товар", options=options)
            select.callback = self.lot_callback
            self.add_item(select)
        close_btn          = discord.ui.Button(label="❌ Закрыть", style=discord.ButtonStyle.danger)
        close_btn.callback = self.close_callback
        self.add_item(close_btn)

    async def lot_callback(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        try:
            lot_id = int(interaction.data['values'][0])
            lot    = await db.get_lot(lot_id)
            if not lot:
                await interaction.followup.send("❌ Товар не найден", ephemeral=True)
                return
            seller      = interaction.guild.get_member(lot.seller_id)
            seller_name = seller.display_name if seller else "Продавец"
            if lot.stock == -1:
                stock_text = "♾️ Бесконечно"
            elif lot.stock > 0:
                stock_text = f"📦 В наличии: {lot.stock} шт."
            else:
                stock_text = "❌ Нет в наличии"
            embed = discord.Embed(
                title=f"🛒 {lot.name}",
                description=f"💰 **{lot.price}**\n{stock_text}\n\n**📝 Детальное описание:**\n{lot.full_description}\n\n**👤 Продавец:** {seller_name}",
                color=discord.Color.green()
            )
            if lot.image_url and lot.image_url.startswith(('http://', 'https://')):
                embed.set_thumbnail(url=lot.image_url)
            embed.set_footer(text="Выбери действие ниже")
            await interaction.followup.send(embed=embed, view=LotActionView(lot_id, lot, seller), ephemeral=True)
        except Exception:
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
        self.lot    = lot
        self.seller = seller
        buy_btn          = discord.ui.Button(label="🛒 Купить", style=discord.ButtonStyle.green)
        cancel_btn       = discord.ui.Button(label="❌ Отмена", style=discord.ButtonStyle.danger)
        buy_btn.callback    = self.buy_callback
        cancel_btn.callback = self.cancel_callback
        self.add_item(buy_btn)
        self.add_item(cancel_btn)

    async def buy_callback(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        try:
            if await get_daily_purchase_count(interaction.user.id) >= DAILY_PURCHASE_LIMIT:
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
            now  = datetime.now()
            last = user_ticket_cooldown.get(interaction.user.id)
            if last and (now - last).total_seconds() < TICKET_COOLDOWN_SECONDS:
                await interaction.followup.send(f"⏳ Подождите {TICKET_COOLDOWN_SECONDS} секунд.", ephemeral=True)
                return

            active_orders.add(key)
            user_ticket_cooldown[interaction.user.id] = now
            try:
                config         = get_config(interaction.guild_id)
                customer_role  = interaction.guild.get_role(config["roles"].get("customer")) if config else None
                if customer_role and customer_role not in interaction.user.roles:
                    await interaction.followup.send("⚠️ Пройдите верификацию в канале #верификация", ephemeral=True)
                    return

                category = discord.utils.get(interaction.guild.categories, name=TICKET_CATEGORY_NAME)
                if not category:
                    category = await interaction.guild.create_category(TICKET_CATEGORY_NAME)

                safe_lot  = re.sub(r"[^a-zA-Z0-9а-яА-Я_-]", "-", self.lot.name.lower())[:15]
                safe_user = re.sub(r"[^a-zA-Z0-9_-]", "-", interaction.user.name.lower())[:15]

                seller_role_id = config["roles"].get("seller") if config else None
                admin_role_id  = config["roles"].get("admin")  if config else None

                overwrites = {
                    interaction.guild.default_role: discord.PermissionOverwrite(read_messages=False),
                    interaction.user:               discord.PermissionOverwrite(read_messages=True, send_messages=True),
                    interaction.guild.me:           discord.PermissionOverwrite(read_messages=True, send_messages=True),
                }
                if self.seller:
                    overwrites[self.seller] = discord.PermissionOverwrite(read_messages=True, send_messages=True)
                if seller_role_id:
                    r = interaction.guild.get_role(seller_role_id)
                    if r:
                        overwrites[r] = discord.PermissionOverwrite(read_messages=True, send_messages=True)
                if admin_role_id:
                    r = interaction.guild.get_role(admin_role_id)
                    if r:
                        overwrites[r] = discord.PermissionOverwrite(read_messages=True, send_messages=True)

                ticket_channel = await interaction.guild.create_text_channel(
                    f"заказ-{safe_lot}-{safe_user}", category=category, overwrites=overwrites
                )

                voice_channel = None
                try:
                    voice_overwrites = {
                        interaction.guild.default_role: discord.PermissionOverwrite(view_channel=False),
                        interaction.user:               discord.PermissionOverwrite(view_channel=True, connect=True, speak=True),
                        interaction.guild.me:           discord.PermissionOverwrite(view_channel=True, connect=True),
                    }
                    if admin_role_id:
                        r = interaction.guild.get_role(admin_role_id)
                        if r:
                            voice_overwrites[r] = discord.PermissionOverwrite(view_channel=True, connect=True, speak=True)
                    voice_channel = await interaction.guild.create_voice_channel(
                        f"🎙️-{safe_user}", category=category, overwrites=voice_overwrites
                    )
                except Exception:
                    pass

                await db.add_ticket(
                    channel_id=ticket_channel.id, user_id=interaction.user.id,
                    guild_id=interaction.guild_id,
                    voice_channel_id=voice_channel.id if voice_channel else None
                )

                if lot_data.stock == -1:
                    stock_display = "♾️ Бесконечно"
                elif lot_data.stock > 0:
                    stock_display = f"📦 Осталось: {lot_data.stock} шт."
                else:
                    stock_display = "❌ Нет в наличии"

                embed = discord.Embed(
                    title="🛒 НОВЫЙ ЗАКАЗ",
                    description=(
                        f"**Покупатель:** {interaction.user.mention}\n"
                        f"**Товар:** {self.lot.name}\n"
                        f"**Цена:** {self.lot.price}\n"
                        f"{stock_display}\n\n"
                        f"**📝 Детальное описание:**\n{self.lot.full_description}\n\n"
                        f"**📝 Инструкция для продавца:**\n"
                        f"1. Расскажите покупателю о товаре.\n"
                        f"2. Отправьте реквизиты для оплаты.\n"
                        f"3. После оплаты передайте товар.\n"
                        f"4. Закройте тикет кнопкой ниже.\n\n"
                        f"**💰 Покупатель:** переведите деньги, напишите «Оплатил», получите товар."
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
                if lot_data.stock != -1:
                    await update_stock(self.lot_id, -1)

                price_num = await parse_price_rub(self.lot.price)
                await db.update_stats(self.lot.seller_id, sales_inc=1, revenue_inc=int(price_num) if price_num else 0)

                ticket_view = OrderCloseView(
                    ticket_channel.id, interaction.user.id, self.seller,
                    voice_channel.id if voice_channel else None,
                    lot_product_code=lot_data.product_code,
                    lot_duration=lot_data.duration
                )
                await ticket_channel.send("✅ **Для завершения используйте кнопки ниже:**", view=ticket_view)
                try:
                    await interaction.delete_original_response()
                except Exception:
                    pass
                await interaction.followup.send(f"✅ Заказ создан! Перейдите в {ticket_channel.mention}", ephemeral=True)
            finally:
                active_orders.discard(key)
        except Exception:
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

# ================= ЛИЦЕНЗИОННЫЕ КЛЮЧИ =================
LICENSE_API_URL     = os.getenv("LICENSE_API_URL", "")
LICENSE_ADMIN_TOKEN = os.getenv("LICENSE_ADMIN_TOKEN", "")

async def generate_license_key(product: str, duration: str) -> Optional[str]:
    if not LICENSE_API_URL or not LICENSE_ADMIN_TOKEN:
        return None
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{LICENSE_API_URL}/generate_key",
                json={"duration": duration, "product": product},
                headers={"x-admin-token": LICENSE_ADMIN_TOKEN},
                timeout=aiohttp.ClientTimeout(total=10)
            ) as resp:
                if resp.status == 200:
                    return (await resp.json()).get("license_key")
    except Exception as e:
        logger.error(f"generate_license_key error: {e}")
    return None

class OrderCloseView(discord.ui.View):
    def __init__(self, ticket_channel_id: int, buyer_id: int, seller,
                 voice_channel_id: Optional[int] = None,
                 lot_product_code: str = 'none', lot_duration: str = '30d'):
        super().__init__(timeout=None)
        self.ticket_channel_id = ticket_channel_id
        self.buyer_id          = buyer_id
        self.seller            = seller
        self.voice_channel_id  = voice_channel_id
        self.lot_product_code  = lot_product_code
        self.lot_duration      = lot_duration

    @discord.ui.button(label="🔑 Выдать ключ", style=discord.ButtonStyle.green, custom_id="issue_key_btn")
    async def issue_key(self, interaction: discord.Interaction, button: discord.ui.Button):
        is_seller = self.seller and interaction.user.id == self.seller.id
        if not (is_seller or is_admin_member(interaction.user)):
            await interaction.response.send_message("❌ Только продавец или админ.", ephemeral=True)
            return
        if self.lot_product_code == 'none':
            await interaction.response.send_message("❌ У этого товара нет лицензионного ключа.", ephemeral=True)
            return
        await interaction.response.defer()
        license_key = await generate_license_key(self.lot_product_code, self.lot_duration)
        if not license_key:
            await interaction.followup.send("❌ Ошибка генерации ключа. Проверьте настройки API.", ephemeral=True)
            return
        buyer          = interaction.guild.get_member(self.buyer_id)
        product_label  = 'АХК Рыбалка' if self.lot_product_code == 'rybalka' else 'АХК Грузчик'
        duration_label = {'1d': '1 день', '7d': '7 дней', '30d': '30 дней', 'lifetime': 'Lifetime'}.get(self.lot_duration, self.lot_duration)
        try:
            await buyer.send(
                f"🔑 **Ваш лицензионный ключ TALENT SHOP:**\n"
                f"```{license_key}```\n"
                f"📦 Продукт: **{product_label}**\n"
                f"⏳ Срок: **{duration_label}**\n\n"
                f"Введите ключ при первом запуске программы."
            )
            await interaction.channel.send(f"✅ Ключ отправлен {buyer.mention if buyer else 'покупателю'} в ЛС.")
        except discord.Forbidden:
            await interaction.channel.send(f"⚠️ Не удалось отправить в ЛС. Ключ:\n||`{license_key}`||")
        button.disabled = True
        button.label    = "✅ Ключ выдан"
        await interaction.message.edit(view=self)

    @discord.ui.button(label="🔒 Закрыть заказ", style=discord.ButtonStyle.danger, custom_id="close_order_btn")
    async def close_order(self, interaction: discord.Interaction, button: discord.ui.Button):
        is_buyer  = interaction.user.id == self.buyer_id
        is_seller = self.seller and interaction.user.id == self.seller.id
        if not (is_buyer or is_seller or is_admin_member(interaction.user)):
            await interaction.response.send_message("❌ Только покупатель, продавец или админ.", ephemeral=True)
            return
        await interaction.response.defer()
        await db.close_ticket(self.ticket_channel_id)
        if self.voice_channel_id:
            vc = interaction.guild.get_channel(self.voice_channel_id)
            if vc:
                try:
                    await vc.delete()
                except Exception:
                    pass
        await interaction.followup.send("🔒 Заказ закрыт. Канал удалён через 24 часа.")

# ================= СИСТЕМА ОТЗЫВОВ =================
class ReviewModal(discord.ui.Modal, title="Оставить отзыв"):
    rating  = discord.ui.TextInput(label="Оценка (1-5)", placeholder="1-5", min_length=1, max_length=1)
    comment = discord.ui.TextInput(label="Комментарий", placeholder="Ваш отзыв о товаре/продавце", style=discord.TextStyle.paragraph, max_length=4000)

    def __init__(self, seller, product: str, lot_id: int):
        super().__init__()
        self.seller  = seller
        self.product = product
        self.lot_id  = lot_id

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        if self.rating.value not in '12345':
            await interaction.followup.send("❌ Оценка должна быть от 1 до 5", ephemeral=True)
            return
        rating  = int(self.rating.value)
        stars   = "⭐" * rating + "☆" * (5 - rating)
        config  = get_config(interaction.guild_id)
        review_channel = interaction.guild.get_channel(config.get("review_channel")) if config else None
        if not review_channel:
            await interaction.followup.send("❌ Канал отзывов не найден.", ephemeral=True)
            return
        if self.seller:
            await add_review(interaction.user.id, self.seller.id, self.lot_id, rating, self.comment.value)
        embed = discord.Embed(
            title="📝 Отзыв о покупке",
            description=f"**Товар:** {self.product}\n**Оценка:** {stars} ({rating}/5)\n\n**Отзыв:**\n{self.comment.value}",
            color=discord.Color.gold()
        )
        embed.set_author(name=interaction.user.display_name, icon_url=interaction.user.avatar.url if interaction.user.avatar else None)
        embed.set_footer(text=f"Покупатель: {interaction.user.name} | Продавец: {self.seller.name if self.seller else 'Неизвестен'}")
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
    reviews    = await db.get_seller_reviews(seller.id, 20)
    avg_rating = await db.get_seller_rating(seller.id)
    embed = discord.Embed(
        title=f"⭐ Отзывы о {seller.display_name}",
        description=f"**Средний рейтинг:** {'⭐' * round(avg_rating)}{'☆' * (5 - round(avg_rating))} ({avg_rating}/5)\n**Всего отзывов:** {len(reviews)}",
        color=discord.Color.gold()
    )
    embed.set_thumbnail(url=seller.avatar.url if seller.avatar else None)
    for rev in reviews[:10]:
        buyer      = guild.get_member(rev['user_id'])
        buyer_name = buyer.display_name if buyer else f"ID:{rev['user_id']}"
        embed.add_field(name=f"{'⭐' * rev['rating']}{'☆' * (5 - rev['rating'])} от {buyer_name}", value=rev['comment'][:200], inline=False)
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

# ================= ПРОФИЛЬ =================
async def build_status_embed(guild: discord.Guild, user: discord.Member) -> discord.Embed:
    purchases   = await db.get_user_purchases(user.id)
    user_reviews = await db.get_user_reviews(user.id)
    stats       = await db.get_stats(user.id)
    ref_count   = await db.get_referral_count(user.id)
    embed = discord.Embed(title=f"👤 Профиль участника: {user.display_name}", color=discord.Color.blue())
    embed.set_thumbnail(url=user.avatar.url if user.avatar else None)
    embed.add_field(
        name="🛒 Статистика",
        value=(
            f"Всего покупок: **{len(purchases)}**\n"
            f"Продаж: **{stats['sales'] if stats else 0}**\n"
            f"Выручка: **{stats['revenue'] if stats else 0} ₽**"
        ),
        inline=False
    )
    embed.add_field(name="🔗 Рефералов", value=f"**{ref_count}**", inline=False)
    if purchases:
        purchase_text = ""
        for p in purchases[:5]:
            lot_item = db.lots_cache.get(p['lot_id'])
            lot_name = lot_item.name if lot_item else f"Товар #{p['lot_id']}"
            purchase_text += f"• {lot_name} — {p['price']} ({p['created_at'][:10]})\n"
        embed.add_field(name="📦 Последние покупки", value=purchase_text, inline=False)
    if user_reviews:
        embed.add_field(
            name="📝 Последние отзывы",
            value="".join(f"{'⭐' * r['rating']} — {r['comment'][:60]}...\n" for r in user_reviews[:3]),
            inline=False
        )
    return embed

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
    channel = interaction.guild.get_channel(config.get("verify_channel"))
    if channel:
        await channel.purge(limit=50, check=lambda m: m.author == bot.user)
        embed = discord.Embed(title="🔒 Верификация", description="Нажми на кнопку ниже, чтобы получить доступ к серверу.", color=discord.Color.gold())
        await channel.send(embed=embed, view=VerifyView())
    await interaction.followup.send("✅ Панель верификации обновлена!", ephemeral=True)

@bot.tree.command(name='profile', description='Посмотреть профиль пользователя')
@app_commands.describe(target="Пользователь")
async def profile_cmd(interaction: discord.Interaction, target: Optional[discord.Member] = None):
    user = target or interaction.user
    await interaction.response.defer(ephemeral=True)
    embed = await build_status_embed(interaction.guild, user)
    await interaction.followup.send(embed=embed, ephemeral=True)

@bot.tree.command(name='setup_ticket_panel', description='[OWNER] Создать панель тикетов')
async def setup_ticket_panel(interaction: discord.Interaction):
    if not await owner_only(interaction):
        return
    await interaction.response.defer(ephemeral=True)
    channel = interaction.guild.get_channel(TICKET_CHANNEL_ID)
    if not channel:
        await interaction.followup.send(f"❌ Канал {TICKET_CHANNEL_ID} не найден", ephemeral=True)
        return
    await channel.purge(limit=50, check=lambda m: m.author == bot.user)
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
@app_commands.describe(name="Название", emoji="Эмодзи", parent_id="ID родительской категории (необязательно)")
async def add_category_cmd(interaction: discord.Interaction, name: str, emoji: str = "📁", parent_id: int = None):
    if not await owner_only(interaction):
        return
    await interaction.response.defer(ephemeral=True)
    cat_id = await db.add_category(name=name, emoji=emoji, parent_id=parent_id)
    await db.refresh_cache()
    parent_str = f" (подкатегория {parent_id})" if parent_id else ""
    await interaction.followup.send(f"✅ Категория `{emoji} {name}` добавлена (ID: {cat_id}){parent_str}", ephemeral=True)
    await send_or_update_shop(interaction.guild)

@bot.tree.command(name='list_lots', description='[OWNER] Обновить магазин')
async def list_lots(interaction: discord.Interaction):
    if not await owner_only(interaction):
        return
    await interaction.response.defer(ephemeral=True)
    await send_or_update_shop(interaction.guild)
    await interaction.followup.send("✅ Магазин обновлён", ephemeral=True)

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

# ================= ОТПРАВКА ПАНЕЛЕЙ =================
async def _send_verify_panel(guild_config: dict):
    channel = await _fetch_channel_safe(guild_config.get("verify_channel"))
    if not channel:
        return
    await channel.purge(limit=50, check=lambda m: m.author == bot.user)
    embed = discord.Embed(title="🔒 Верификация", description="Нажми на кнопку ниже, чтобы получить доступ к серверу.", color=discord.Color.gold())
    try:
        await channel.send(embed=embed, view=VerifyView())
    except Exception as e:
        logger.error(f"Ошибка отправки верификации: {e}")

async def _send_ticket_panel_from_config(guild_config: dict):
    channel = await _fetch_channel_safe(guild_config.get("ticket_channel"))
    if not channel:
        return
    await channel.purge(limit=50, check=lambda m: m.author == bot.user)
    embed = discord.Embed(
        title="🎫 Служба поддержки",
        description="**Нажмите на кнопку ниже, чтобы создать обращение.**\n\n📌 Удаление тикета из архива через 7 дней",
        color=discord.Color.blue()
    )
    embed.set_footer(text=f"{guild_config['name']} — Техническая поддержка")
    try:
        await channel.send(embed=embed, view=TicketCreateButton())
    except Exception as e:
        logger.error(f"Ошибка отправки тикетов: {e}")

async def _assign_unverified_roles():
    for guild in bot.guilds:
        config = get_config(guild.id)
        if not config:
            continue
        unverified_role = guild.get_role(config["roles"].get("unverified"))
        if not unverified_role:
            continue
        customer_role = guild.get_role(config["roles"].get("customer", 0))
        buyer_role    = guild.get_role(config["roles"].get("buyer",    0))
        candidates    = [
            m for m in guild.members
            if not m.bot and not is_admin_member(m)
            and not (customer_role  and customer_role  in m.roles)
            and not (buyer_role     and buyer_role     in m.roles)
            and unverified_role not in m.roles
        ]
        for i in range(0, len(candidates), 10):
            await asyncio.gather(*[m.add_roles(unverified_role) for m in candidates[i:i+10]], return_exceptions=True)
            if i + 10 < len(candidates):
                await asyncio.sleep(0.5)

# ================= АДМИН ПАНЕЛЬ =================
class AdminPanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Управление", style=discord.ButtonStyle.secondary, custom_id="admin_main_menu", row=0)
    async def main_menu_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_owner(interaction):
            await interaction.response.send_message("❌ Только для Owner", ephemeral=True)
            return
        embed = discord.Embed(title="● **Админ панель**", description="Выберите раздел для управления", color=discord.Color.light_gray())
        embed.add_field(name="● **Категории**", value="Управление категориями товаров", inline=False)
        embed.add_field(name="● **Товары**",    value="Управление товарами и ассортиментом", inline=False)
        embed.add_field(name="● **Настройки**", value="Статистика и резервное копирование", inline=False)
        embed.set_footer(text="Панель управления")
        await interaction.response.edit_message(embed=embed, view=AdminMainMenu())

class AdminMainMenu(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Категории", style=discord.ButtonStyle.secondary, custom_id="admin_cats_menu", row=0)
    async def categories_menu_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        embed = discord.Embed(title="● **Управление категориями**", color=discord.Color.light_gray())
        embed.add_field(name="● **Добавить категорию**", value="Создать новую категорию", inline=False)
        embed.add_field(name="● **Удалить категорию**",  value="Удалить существующую категорию", inline=False)
        embed.add_field(name="● **Список категорий**",   value="Показать все категории с ID", inline=False)
        await interaction.response.edit_message(embed=embed, view=CategoriesMenuView())

    @discord.ui.button(label="Товары", style=discord.ButtonStyle.secondary, custom_id="admin_lots_menu", row=0)
    async def lots_menu_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        embed = discord.Embed(title="● **Управление товарами**", color=discord.Color.light_gray())
        embed.add_field(name="● **Добавить товар**", value="Создать новый товар", inline=False)
        embed.add_field(name="● **Удалить товар**",  value="Удалить существующий товар", inline=False)
        embed.add_field(name="● **Список товаров**", value="Показать все товары с ID", inline=False)
        await interaction.response.edit_message(embed=embed, view=LotsMenuView())

    @discord.ui.button(label="Настройки", style=discord.ButtonStyle.secondary, custom_id="admin_settings_menu", row=0)
    async def settings_menu_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        embed = discord.Embed(title="● **Настройки и статистика**", color=discord.Color.light_gray())
        embed.add_field(name="● **Статистика**",      value="Показать статистику магазина", inline=False)
        embed.add_field(name="● **Обновить магазин**", value="Принудительное обновление", inline=False)
        embed.add_field(name="● **Бэкап**",            value="Создать резервную копию", inline=False)
        await interaction.response.edit_message(embed=embed, view=SettingsMenuView())

    @discord.ui.button(label="Назад", style=discord.ButtonStyle.secondary, custom_id="admin_back_main", row=1)
    async def back_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        embed = discord.Embed(title="● **Админ панель**", description="Выберите раздел", color=discord.Color.light_gray())
        await interaction.response.edit_message(embed=embed, view=AdminPanelView())

class CategoriesMenuView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="➕ Добавить категорию", style=discord.ButtonStyle.success, custom_id="cat_add", row=0)
    async def add_category_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_owner(interaction):
            await interaction.response.send_message("❌ Только для Owner", ephemeral=True)
            return

        class AddCategoryModal(discord.ui.Modal, title="Добавить категорию"):
            name      = discord.ui.TextInput(label="Название", placeholder="Введите название...", required=True)
            emoji     = discord.ui.TextInput(label="Эмодзи", placeholder="📁", required=False, default="📁")
            parent_id = discord.ui.TextInput(label="ID родителя (пусто = корневая)", placeholder="Например: 3", required=False)

            async def on_submit(self, i: discord.Interaction):
                await i.response.defer(ephemeral=True)
                pid = None
                if self.parent_id.value.strip():
                    try:
                        pid = int(self.parent_id.value.strip())
                        if not await db.get_category(pid):
                            await i.followup.send(f"❌ Категория с ID {pid} не найдена", ephemeral=True)
                            return
                    except ValueError:
                        await i.followup.send("❌ ID должен быть числом", ephemeral=True)
                        return
                cat_id = await db.add_category(name=self.name.value, emoji=self.emoji.value or "📁", parent_id=pid)
                await db.refresh_cache()
                parent_str = f" → подкатегория {pid}" if pid else " (корневая)"
                await i.followup.send(f"✅ `{self.emoji.value or '📁'} {self.name.value}` добавлена (ID: {cat_id}){parent_str}", ephemeral=True)
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
                try:
                    cid = int(self.cat_id.value)
                except ValueError:
                    await i.followup.send("❌ ID должен быть числом", ephemeral=True)
                    return
                category = await db.get_category(cid)
                if not category:
                    await i.followup.send(f"❌ Категория `{cid}` не найдена", ephemeral=True)
                    return
                await db.delete_category(cid)
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
            pid_str = f" (дочерняя → {cat.parent_id})" if getattr(cat, 'parent_id', None) else ""
            embed.add_field(name=f"{cat.emoji} {cat.name}{pid_str}", value=f"**ID:** `{cat.id}`\n**Товаров:** {len(cat.lots)}", inline=False)
        await interaction.followup.send(embed=embed, ephemeral=True)

    @discord.ui.button(label="➕ Добавить подкатегорию", style=discord.ButtonStyle.success, custom_id="subcat_add", row=1)
    async def add_subcategory_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_owner(interaction):
            await interaction.response.send_message("❌ Только для Owner", ephemeral=True)
            return
        await db.refresh_cache()
        if not db.categories_cache:
            await interaction.response.send_message("❌ Сначала создайте категорию", ephemeral=True)
            return

        class AddSubcategoryModal(discord.ui.Modal, title="Добавить подкатегорию"):
            name      = discord.ui.TextInput(label="Название", placeholder="Введите название...", required=True)
            emoji     = discord.ui.TextInput(label="Эмодзи", placeholder="📂", required=False, default="📂")
            parent_id = discord.ui.TextInput(label="ID родительской категории", placeholder="Например: 3", required=True)

            async def on_submit(self, i: discord.Interaction):
                await i.response.defer(ephemeral=True)
                try:
                    pid = int(self.parent_id.value.strip())
                except ValueError:
                    await i.followup.send("❌ ID должен быть числом", ephemeral=True)
                    return
                parent = await db.get_category(pid)
                if not parent:
                    await i.followup.send(f"❌ Категория с ID `{pid}` не найдена", ephemeral=True)
                    return
                cat_id = await db.add_category(name=self.name.value, emoji=self.emoji.value or "📂", parent_id=pid)
                await db.refresh_cache()
                await i.followup.send(
                    f"✅ Подкатегория `{self.emoji.value or '📂'} {self.name.value}` добавлена "
                    f"(ID: `{cat_id}`) → в `{parent.emoji} {parent.name}` (ID: `{pid}`)",
                    ephemeral=True
                )
                config = get_config(i.guild_id)
                if config and config.get("shop_channel"):
                    await send_or_update_shop(i.guild)

        await interaction.response.send_modal(AddSubcategoryModal())

    @discord.ui.button(label="◀️ Назад", style=discord.ButtonStyle.secondary, custom_id="cat_back", row=2)
    async def back_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        embed = discord.Embed(title="● **Админ панель**", description="Выберите раздел для управления", color=discord.Color.light_gray())
        embed.add_field(name="● **Категории**", value="Управление категориями товаров", inline=False)
        embed.add_field(name="● **Товары**",    value="Управление товарами и ассортиментом", inline=False)
        embed.add_field(name="● **Настройки**", value="Статистика и резервное копирование", inline=False)
        await interaction.response.edit_message(embed=embed, view=AdminMainMenu())

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
            name      = discord.ui.TextInput(label="Название", placeholder="Введите название...", required=True, max_length=100)
            price     = discord.ui.TextInput(label="Цена", placeholder="1000 ₽", required=True, max_length=50)
            seller    = discord.ui.TextInput(label="Продавец (ID или @ник)", placeholder="Введите ID пользователя или @ник", required=True, max_length=100)
            full_desc = discord.ui.TextInput(label="Описание", placeholder="Подробное описание товара...", style=discord.TextStyle.paragraph, required=False, max_length=2000)
            stock     = discord.ui.TextInput(label="Количество (безлимит: -1)", placeholder="0", required=False, default="0")

            async def on_submit(self, i: discord.Interaction):
                await i.response.defer(ephemeral=True)
                price_value = self.price.value.strip()
                if not price_value:
                    await i.followup.send("❌ Введите цену", ephemeral=True)
                    return
                try:
                    stock_val = int(self.stock.value) if self.stock.value else 0
                except Exception:
                    stock_val = 0
                seller_input = self.seller.value.strip()
                seller_id    = None
                if seller_input.startswith('<@') and seller_input.endswith('>'):
                    seller_id = int(seller_input.replace('<@', '').replace('>', '').replace('!', ''))
                elif seller_input.isdigit():
                    seller_id = int(seller_input)
                else:
                    member = i.guild.get_member_named(seller_input)
                    if member:
                        seller_id = member.id
                if not seller_id or not i.guild.get_member(seller_id):
                    await i.followup.send("❌ Продавец не найден!", ephemeral=True)
                    return

                class ProductSelectView(discord.ui.View):
                    def __init__(self, lot_name, lot_price, lot_full, lot_stock, seller_id_val, cats):
                        super().__init__(timeout=60)
                        self.d = (lot_name, lot_price, lot_full, lot_stock, seller_id_val, cats)
                        select = discord.ui.Select(
                            placeholder="🎮 Продукт",
                            options=[
                                discord.SelectOption(label="АХК Рыбалка", value="rybalka", emoji="🎣"),
                                discord.SelectOption(label="АХК Грузчик", value="gruzchik", emoji="📦"),
                                discord.SelectOption(label="Без ключа",   value="none",    emoji="❌"),
                            ]
                        )
                        select.callback = self.product_callback
                        self.add_item(select)

                    async def product_callback(self, si: discord.Interaction):
                        self.product_code = si.data['values'][0]
                        if self.product_code == 'none':
                            await si.response.edit_message(content="📁 **Выберите категорию:**", view=CategorySelectView(*self.d, product_code='none', duration='30d'))
                        else:
                            dur_select = discord.ui.Select(
                                placeholder="⏳ Тариф",
                                options=[
                                    discord.SelectOption(label="1 день",   value="1d"),
                                    discord.SelectOption(label="7 дней",   value="7d"),
                                    discord.SelectOption(label="30 дней",  value="30d"),
                                    discord.SelectOption(label="Lifetime", value="lifetime"),
                                ]
                            )
                            view = discord.ui.View(timeout=60)
                            dur_select.callback = lambda si2: self.duration_callback(si2, dur_select)
                            view.add_item(dur_select)
                            await si.response.edit_message(content="⏳ **Выберите тариф:**", view=view)

                    async def duration_callback(self, si: discord.Interaction, select):
                        await si.response.edit_message(content="📁 **Выберите категорию:**", view=CategorySelectView(*self.d, product_code=self.product_code, duration=select.values[0]))

                class CategorySelectView(discord.ui.View):
                    def __init__(self, lot_name, lot_price, lot_full, lot_stock, seller_id_val, cats, product_code='none', duration='30d'):
                        super().__init__(timeout=60)
                        self.product_code   = product_code
                        self.duration       = duration
                        self.lot_name       = lot_name
                        self.lot_price      = lot_price
                        self.lot_full       = lot_full
                        self.lot_stock      = lot_stock
                        self.seller_id_val  = seller_id_val
                        self.cats           = cats
                        options = [discord.SelectOption(label=cat.name, value=str(cat.id), emoji=cat.emoji) for cat in self.cats.values()]
                        select  = discord.ui.Select(placeholder="📁 Выберите категорию", options=options)
                        select.callback = self.select_callback
                        self.add_item(select)

                    async def select_callback(self, si: discord.Interaction):
                        cat_id = int(si.data['values'][0])
                        lot_id = await db.add_lot(
                            name=self.lot_name, price=self.lot_price,
                            short_description="", full_description=self.lot_full or "",
                            seller_id=self.seller_id_val, category_id=cat_id,
                            stock=self.lot_stock, product_code=self.product_code,
                            duration=self.duration
                        )
                        await db.refresh_cache()
                        stock_text = "♾️ Бесконечно" if self.lot_stock == -1 else f"{self.lot_stock} шт."
                        await si.response.send_message(
                            f"✅ Товар **{self.lot_name}** добавлен!\n"
                            f"💰 Цена: {self.lot_price}\n"
                            f"👤 Продавец: <@{self.seller_id_val}>\n"
                            f"📦 Количество: {stock_text}\n"
                            f"🆔 ID товара: `{lot_id}`",
                            ephemeral=True
                        )
                        config = get_config(si.guild_id)
                        if config and config.get("shop_channel"):
                            await send_or_update_shop(si.guild)
                        self.stop()

                await i.followup.send("🎮 **Выберите продукт:**", view=ProductSelectView(
                    lot_name=self.name.value, lot_price=price_value,
                    lot_full=self.full_desc.value, lot_stock=stock_val,
                    seller_id_val=seller_id, cats=categories
                ), ephemeral=True)

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
                options = [discord.SelectOption(label=f"{lot.name} (ID:{lot.lot_id})", value=str(lot.lot_id), description=f"Цена: {lot.price}") for lot in list(lots.values())[:25]]
                select  = discord.ui.Select(placeholder="Выберите товар для удаления", options=options)
                select.callback = self.select_callback
                self.add_item(select)
                cancel = discord.ui.Button(label="❌ Отмена", style=discord.ButtonStyle.secondary)
                cancel.callback = self.cancel_callback
                self.add_item(cancel)

            async def select_callback(self, si: discord.Interaction):
                lot_id = int(si.data['values'][0])
                lot    = await db.get_lot(lot_id)
                if not lot:
                    await si.response.send_message("❌ Товар не найден", ephemeral=True)
                    return
                await db.delete_lot(lot_id)
                await db.refresh_cache()
                await si.response.send_message(f"✅ Товар **{lot.name}** удалён!", ephemeral=True)
                config = get_config(si.guild_id)
                if config and config.get("shop_channel"):
                    await send_or_update_shop(si.guild)
                self.stop()

            async def cancel_callback(self, si: discord.Interaction):
                await si.response.send_message("❌ Отменено", ephemeral=True)
                self.stop()

        await interaction.response.send_message("🗑️ **Выберите товар для удаления:**", view=DeleteLotView(), ephemeral=True)

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
            stock_text = "♾️ Бесконечно" if lot.stock == -1 else (f"📦 В наличии: {lot.stock} шт." if lot.stock > 0 else "❌ Нет в наличии")
            embed.add_field(name=lot.name, value=f"**ID:** `{lot.lot_id}`\n**Цена:** {lot.price}\n{stock_text}", inline=False)
        if len(lots) > 20:
            embed.set_footer(text=f"Показано 20 из {len(lots)} товаров")
        await interaction.followup.send(embed=embed, ephemeral=True)

    @discord.ui.button(label="◀️ Назад", style=discord.ButtonStyle.secondary, custom_id="lot_back", row=1)
    async def back_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        embed = discord.Embed(title="● **Админ панель**", description="Выберите раздел для управления", color=discord.Color.light_gray())
        embed.add_field(name="● **Категории**", value="Управление категориями товаров", inline=False)
        embed.add_field(name="● **Товары**",    value="Управление товарами и ассортиментом", inline=False)
        embed.add_field(name="● **Настройки**", value="Статистика и резервное копирование", inline=False)
        await interaction.response.edit_message(embed=embed, view=AdminMainMenu())

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
        embed = discord.Embed(title="📊 Статистика магазина", color=discord.Color.gold())
        embed.add_field(name="📁 Категорий", value=str(len(db.categories_cache)), inline=True)
        embed.add_field(name="🛒 Товаров",   value=str(len(db.lots_cache)),       inline=True)
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
        await interaction.followup.send("✅ Бэкап создан!" if result else "❌ Ошибка создания бэкапа", ephemeral=True)

    @discord.ui.button(label="◀️ Назад", style=discord.ButtonStyle.secondary, custom_id="settings_back", row=1)
    async def back_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        embed = discord.Embed(title="● **Админ панель**", description="Выберите раздел для управления", color=discord.Color.light_gray())
        embed.add_field(name="● **Категории**", value="Управление категориями товаров", inline=False)
        embed.add_field(name="● **Товары**",    value="Управление товарами и ассортиментом", inline=False)
        embed.add_field(name="● **Настройки**", value="Статистика и резервное копирование", inline=False)
        await interaction.response.edit_message(embed=embed, view=AdminMainMenu())

async def setup_admin_panel():
    channel = bot.get_channel(ADMIN_PANEL_CHANNEL_ID)
    if not channel:
        try:
            channel = await bot.fetch_channel(ADMIN_PANEL_CHANNEL_ID)
        except Exception as e:
            logger.error(f"Админ канал {ADMIN_PANEL_CHANNEL_ID} не найден: {e}")
            return
    await channel.purge(limit=50, check=lambda m: m.author == bot.user)
    embed = discord.Embed(title="🛠️ Админ панель", description="Нажмите на кнопку ниже для открытия меню управления", color=discord.Color.blurple())
    await channel.send(embed=embed, view=AdminPanelView())
    logger.info("✅ Админ панель отправлена")

# ================= ЗАПУСК =================
async def _safe_task(coro, name: str):
    try:
        await coro
    except Exception:
        logger.exception(f"❌ Необработанное исключение в задаче '{name}'")

async def _setup_single_guild(guild_id: int, g_config: dict):
    guild = bot.get_guild(guild_id)
    if not guild:
        logger.warning(f"⚠️ Гильдия {guild_id} не найдена")
        return
    logger.info(f"⏳ Настройка панелей для {g_config['name']}...")
    tasks = [
        _send_verify_panel(g_config),
        _send_ticket_panel_from_config(g_config),
    ]
    if g_config.get("shop_channel"):
        tasks.append(send_or_update_shop(guild))
    await asyncio.gather(*tasks, return_exceptions=True)

async def setup_panels():
    logger.info("⏳ setup_panels(): НАЧАЛО")
    try:
        await db.refresh_cache()
    except Exception as e:
        logger.error(f"❌ setup_panels(): ошибка обновления кэша: {e}")
        return
    await asyncio.gather(
        *[_setup_single_guild(gid, gcfg) for gid, gcfg in CONFIG.items()],
        _assign_unverified_roles(),
        return_exceptions=True,
    )
    logger.info("✅ setup_panels(): ЗАВЕРШЕНО")

async def _startup_background():
    logger.info("🔥 _startup_background() начал работу...")
    await _safe_task(db.restore_from_backup_channel(BACKUP_CHANNEL_ID, bot), "restore_backup")
    await asyncio.gather(
        _safe_task(setup_panels(),      "setup_panels"),
        _safe_task(setup_admin_panel(), "setup_admin_panel"),
    )
    logger.info("✅ _startup_background() завершён!")

async def _sync_commands():
    try:
        await bot.tree.sync()
        logger.info("✅ Слеш-команды синхронизированы")
    except Exception as e:
        logger.error(f"❌ Ошибка синхронизации команд: {e}")

@bot.event
async def on_ready():
    logger.info(f"Logged in as {bot.user.name} ({bot.user.id})")
    global _startup_done
    if _startup_done:
        return
    _startup_done = True

    await db.init_db()

    bot.add_view(VerifyView())
    bot.add_view(TicketCreateButton())
    bot.add_view(AdminPanelView())
    bot.add_view(OrderCloseView(0, 0, None, None))
    setup_embed_builder(bot)

    asyncio.create_task(_safe_task(auto_cleanup_tickets(), "auto_cleanup_tickets"))
    asyncio.create_task(_safe_task(auto_update_currency(), "auto_update_currency"))
    asyncio.create_task(_safe_task(auto_backup_task(),     "auto_backup_task"))
    asyncio.create_task(_safe_task(_startup_background(),  "_startup_background"))
    asyncio.create_task(_safe_task(_sync_commands(),       "tree_sync"))

    print("🚀 Бот принял управление (фоновая инициализация запущена)")

@bot.event
async def on_message(message: discord.Message):
    await bot.process_commands(message)

@bot.event
async def on_member_join(member: discord.Member):
    config = get_config(member.guild.id)
    if not config:
        return
    unverified_role = member.guild.get_role(config["roles"].get("unverified"))
    if unverified_role:
        await member.add_roles(unverified_role)
    welcome_channel = member.guild.get_channel(config.get("welcome_channel"))
    if welcome_channel:
        await welcome_channel.send(
            f"Добро пожаловать, {member.mention}!\n"
            f"Добро пожаловать в **TALENT SHOP** | НЕДЕЛЯ ДО ОТКРЫТИЯ!\n\n"
            f"Как пройти верификацию:\n"
            f"1. Перейдите в <#{config['verify_channel']}>\n"
            f"2. Нажмите «Верифицироваться»"
        )

token = os.getenv('DISCORD_TOKEN')
if not token:
    raise RuntimeError("❌ DISCORD_TOKEN не задан!")

bot.run(token)
