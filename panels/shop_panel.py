import discord
import asyncio
import re
from discord.ui import Button, View, Select
from datetime import datetime, timezone

from ..utils.channel import fetch_channel_safe
from ..utils.permissions import get_config, is_admin_member
from ..config.constants import SHOP_IMAGE_LINK, TICKET_CATEGORY_NAME, TICKET_COOLDOWN_SECONDS
from .. import database as db
from ..database import has_user_bought, update_stock, get_stock, add_purchase

# ================= ВСПОМОГАТЕЛЬНАЯ ФУНКЦИЯ =================
def parse_price_rub(price_str: str):
    match = re.search(r'[\d]+(?:[.,]\d+)?', price_str.replace(' ', ''))
    if match:
        return float(match.group().replace(',', '.'))
    return None

# ================= ГЛОБАЛЬНЫЕ ПЕРЕМЕННЫЕ =================
active_orders = set()
user_ticket_cooldown = {}
_shop_update_lock = asyncio.Lock()

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
                discord.SelectOption(label=cat.name, description=f"Товаров: {len(cat.lots)}", value=str(cat.id), emoji=cat.emoji)
                for cat in page_categories
            ]
            select = discord.ui.Select(placeholder="📁 Выберите категорию...", options=options, min_values=1, max_values=1)
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
        await interaction.response.defer(ephemeral=True)
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
                stock_text = f"📦 В наличии: {lot.stock}" if lot.stock > 0 else "❌ Нет в наличии"
                desc = (lot.short_description or "")[:80]
                embed.add_field(name=f"🛒 {lot.name}", value=f"💰 **Цена:** {lot.price}\n{stock_text}\n📝 {desc}\n👤 **Продавец:** {seller_name}", inline=False)
            view = LotsView(category_id, lots_in_category)
            await interaction.followup.send(embed=embed, view=view, ephemeral=True)
        except Exception as e:
            print(f"Ошибка category_callback: {e}")
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

# ================= LotsView =================
class LotsView(discord.ui.View):
    def __init__(self, category_id: int, lots_list: list):
        super().__init__(timeout=300)
        self.category_id = category_id
        options = [
            discord.SelectOption(label=f"{lot.name} - {lot.price}"[:100], description=(lot.short_description[:50] if lot.short_description else None), value=str(lot.lot_id), emoji="🛒")
            for lot in lots_list[:25]
        ]
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
            stock_text = f"📦 В наличии: {lot.stock}" if lot.stock > 0 else "❌ Нет в наличии"
            embed = discord.Embed(
                title=f"🛒 {lot.name}",
                description=f"💰 **{lot.price}**\n{stock_text}\n\n**📝 Детальное описание:**\n{lot.full_description}\n\n**👤 Продавец:** {seller_name}",
                color=discord.Color.green()
            )
            if lot.image_url and lot.image_url.startswith(('http://', 'https://')):
                embed.set_thumbnail(url=lot.image_url)
            embed.set_footer(text="Выбери действие ниже")
            view = LotActionView(lot_id, lot, seller)
            await interaction.followup.send(embed=embed, view=view, ephemeral=True)
        except Exception as e:
            print(f"Ошибка lot_callback: {e}")
            await interaction.followup.send("❌ Ошибка при выборе товара", ephemeral=True)

    async def close_callback(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        try:
            await interaction.delete_original_response()
        except Exception:
            pass

# ================= LotActionView =================
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
        await interaction.response.defer(ephemeral=True)
        try:
            from ..database import get_daily_purchase_count
            daily_count = await get_daily_purchase_count(interaction.user.id)
            if daily_count >= 10:
                await interaction.followup.send(f"❌ Достигнут дневной лимит покупок (10 в день).", ephemeral=True)
                return

            stock = await get_stock(self.lot_id)
            if stock <= 0:
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

            from datetime import datetime
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

                embed = discord.Embed(
                    title="🛒 НОВЫЙ ЗАКАЗ",
                    description=(
                        f"**Покупатель:** {interaction.user.mention}\n**Товар:** {self.lot.name}\n**Цена:** {self.lot.price}\n\n"
                        f"**📝 Детальное описание:**\n{self.lot.full_description}\n\n"
                        "**📝 Инструкция для продавца:**\n1. Расскажите покупателю о товаре.\n2. Отправьте реквизиты для оплаты.\n"
                        "3. После оплаты передайте товар.\n4. Закройте тикет кнопкой ниже.\n\n"
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

                price_num = parse_price_rub(self.lot.price)
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
            print(f"Ошибка buy_callback: {e}")
            active_orders.discard((interaction.user.id, self.lot_id))
            await interaction.followup.send("❌ Ошибка при создании заказа", ephemeral=True)

    async def cancel_callback(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        await interaction.followup.send("❌ Покупка отменена", ephemeral=True)
        try:
            await interaction.delete_original_response()
        except Exception:
            pass

# ================= OrderCloseView =================
class OrderCloseView(discord.ui.View):
    def __init__(self, ticket_channel_id: int, buyer_id: int, seller, voice_channel_id: int = None):
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

        await interaction.followup.send("🔒 Заказ закрыт. Канал будет удален через 24 часа.", ephemeral=False)

# ================= ShopSearchModal =================
class ShopSearchModal(discord.ui.Modal, title="🔍 Поиск товара"):
    query = discord.ui.TextInput(label="Название товара или категории", placeholder="Введите название...", min_length=2, max_length=100, required=True)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        search_term = self.query.value.strip()
        cats = db.categories_cache
        matched_cats = [c for c in cats.values() if search_term.lower() in c.name.lower()]
        matched_lots = await db.search_lots(search_term)

        if not matched_cats and not matched_lots:
            await interaction.followup.send(f"❌ По запросу **{search_term}** ничего не найдено.", ephemeral=True)
            return

        embed = discord.Embed(title=f"🔍 Результаты поиска: {search_term}", color=discord.Color.blue())
        if matched_cats:
            cat_text = "\n".join([f"{c.emoji} **{c.name}** — товаров: {len(c.lots)}" for c in matched_cats[:5]])
            embed.add_field(name="📁 Категории", value=cat_text, inline=False)
        if matched_lots:
            lots_text = "".join([f"{'✅' if lot.stock > 0 else '❌'} **{lot.name}** — {lot.price}\n" for lot in matched_lots[:10]])
            embed.add_field(name="🛒 Товары", value=lots_text, inline=False)

        view = discord.ui.View(timeout=120)
        if matched_lots:
            options = [discord.SelectOption(label=f"{lot.name[:50]} - {lot.price[:20]}", value=str(lot.lot_id), emoji="🛒") for lot in matched_lots[:25]]
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
    embed = discord.Embed(
        title=f"🛒 {lot.name}",
        description=f"💰 **{lot.price}**\n{stock_text}\n\n**📝 Описание:**\n{lot.full_description}\n\n**👤 Продавец:** {seller_name}",
        color=discord.Color.green()
    )
    if lot.image_url and lot.image_url.startswith(('http://', 'https://')):
        embed.set_thumbnail(url=lot.image_url)
    view = LotActionView(lot_id, lot, seller)
    await interaction.followup.send(embed=embed, view=view, ephemeral=True)

# ================= send_or_update_shop =================
async def _do_shop_update(guild: discord.Guild, bot):
    config = get_config(guild.id)
    if not config or not config.get("shop_channel"):
        return
    channel = await fetch_channel_safe(bot, config["shop_channel"])
    if not channel:
        return

    await db.refresh_cache()
    categories = db.categories_cache

    if not categories:
        catalog_embed = discord.Embed(title="TALENT SHOP — КАТАЛОГ ТОВАРОВ", description="В магазине пока нет товаров.", color=discord.Color.from_rgb(0, 0, 0))
    else:
        catalog_embed = discord.Embed(title="TALENT SHOP — КАТАЛОГ ТОВАРОВ", description="Выберите категорию в меню ниже.", color=discord.Color.from_rgb(0, 0, 0))
        catalog_embed.set_footer(text="TALENT SHOP | Нажми для выбора.")

    catalog_embed.set_image(url=SHOP_IMAGE_LINK)
    view = ShopView()

    try:
        async for msg in channel.history(limit=50):
            if msg.author == bot.user and msg.embeds and msg.embeds[0].title == "TALENT SHOP — КАТАЛОГ ТОВАРОВ":
                try:
                    await msg.delete()
                except Exception:
                    pass
    except Exception:
        pass

    msg = await channel.send(embed=catalog_embed, view=view)
    await db.set_shop_messages(guild.id, img_id=msg.id)

async def send_or_update_shop(guild: discord.Guild, bot):
    async with _shop_update_lock:
        await _do_shop_update(guild, bot)