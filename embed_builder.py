"""
embed_builder.py — Интерактивный конструктор эмбедов для Discord-бота.

Воспроизводит функциональность Discohook Utils и Embed Generator:
  • Произвольный заголовок, описание, цвет, иконка/изображение
  • Поля (fields) с inline-поддержкой
  • Кнопки-ссылки (Link Buttons) как в Discohook
  • Готовые шаблоны: «Информация», «Роли», «FAQ» и т.д.
  • Отправка в любой канал сервера

Подключение в bot.py:
    from embed_builder import setup_embed_builder
    ...
    async def on_ready():
        setup_embed_builder(bot)
        ...
"""

from __future__ import annotations

import discord
from discord import app_commands
from discord.ui import Modal, TextInput, View, Button, Select
from typing import Optional, List
import re

# ──────────────────────────────────────────────
#  Вспомогательные функции
# ──────────────────────────────────────────────

def _hex_to_color(hex_str: str) -> Optional[discord.Color]:
    """Парсит строку #RRGGBB или RRGGBB в discord.Color."""
    hex_str = hex_str.strip().lstrip("#")
    if re.fullmatch(r"[0-9a-fA-F]{6}", hex_str):
        return discord.Color(int(hex_str, 16))
    return None


def _parse_fields(raw: str) -> List[dict]:
    """
    Формат одного поля в строке:
        [Название] :: Значение :: inline
        [Название] :: Значение
    Поля разделяются строкой «---».
    """
    fields = []
    for block in raw.split("---"):
        block = block.strip()
        if not block:
            continue
        parts = [p.strip() for p in block.split("::")]
        if len(parts) < 2:
            continue
        name = parts[0].strip("[]")
        value = parts[1]
        inline = len(parts) >= 3 and parts[2].lower() == "inline"
        if name and value:
            fields.append({"name": name, "value": value, "inline": inline})
    return fields


def _parse_buttons(raw: str) -> List[dict]:
    """
    Формат: Метка | URL | Эмодзи(необяз.)
    Каждая кнопка на новой строке.
    """
    buttons = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = [p.strip() for p in line.split("|")]
        if len(parts) >= 2:
            label = parts[0]
            url = parts[1]
            emoji = parts[2] if len(parts) >= 3 else None
            if label and url.startswith("http"):
                buttons.append({"label": label, "url": url, "emoji": emoji})
    return buttons


# ──────────────────────────────────────────────
#  Шаблоны эмбедов
# ──────────────────────────────────────────────

TEMPLATES = {
    "info": {
        "label": "📌 Информация",
        "title": "• Информация",
        "description": (
            "Здесь собрана вся важная информация о сервере\n"
            "Награды, роли, часто задаваемые вопросы"
        ),
        "color": "faa61a",
        "fields_raw": "",
        "buttons_raw": (
            "⚙️ Роли | https://example.com\n"
            "⭐ Награды | https://example.com\n"
            "❓ FAQ | https://example.com"
        ),
        "thumbnail": "",
        "image": "",
        "author_name": "",
        "author_icon": "",
        "footer": "",
    },
    "roles": {
        "label": "👤 Роли сервера",
        "title": "• Информация о ролях",
        "description": "",
        "color": "5865f2",
        "fields_raw": (
            "[• Руководство скваде] :: @ Дон — Лидер сквада\n@ Зам — Заместитель лидера\n\n :: \n"
            "---\n"
            "[• Администрация] :: @ Главный Администратор — Следит за администрацией сервера\n@ Администратор — Администратор сервера :: \n"
            "---\n"
            "[• Буcтеры] :: @ Premium — Выдаётся за 2 буста\n@ VIP — Выдаётся за 1 буст :: "
        ),
        "buttons_raw": "",
        "thumbnail": "",
        "image": "",
        "author_name": "Информация о ролях",
        "author_icon": "",
        "footer": "",
    },
    "faq": {
        "label": "❓ FAQ",
        "title": "• Часто задаваемые вопросы",
        "description": "Здесь вы найдёте ответы на часто задаваемые вопросы.",
        "color": "57f287",
        "fields_raw": (
            "[❓ Как купить?] :: Зайди в магазин и нажми «Купить».\n"
            "---\n"
            "[💳 Какие способы оплаты?] :: Карта, CryptoBot, СБП.\n"
            "---\n"
            "[📦 Когда придёт товар?] :: Сразу после подтверждения оплаты продавцом."
        ),
        "buttons_raw": "",
        "thumbnail": "",
        "image": "",
        "author_name": "",
        "author_icon": "",
        "footer": "TALENT SHOP • FAQ",
    },
    "blank": {
        "label": "🔲 Пустой",
        "title": "",
        "description": "",
        "color": "2b2d31",
        "fields_raw": "",
        "buttons_raw": "",
        "thumbnail": "",
        "image": "",
        "author_name": "",
        "author_icon": "",
        "footer": "",
    },
}


# ──────────────────────────────────────────────
#  Состояние (per-user сессия)
# ──────────────────────────────────────────────

class EmbedSession:
    """Хранит черновик эмбеда для одного пользователя."""

    __slots__ = (
        "title", "description", "color", "fields_raw",
        "buttons_raw", "thumbnail", "image",
        "author_name", "author_icon", "footer",
        "target_channel_id",
    )

    def __init__(self, template: dict | None = None):
        tpl = template or TEMPLATES["blank"]
        self.title: str = tpl["title"]
        self.description: str = tpl["description"]
        self.color: str = tpl["color"]
        self.fields_raw: str = tpl["fields_raw"]
        self.buttons_raw: str = tpl["buttons_raw"]
        self.thumbnail: str = tpl["thumbnail"]
        self.image: str = tpl["image"]
        self.author_name: str = tpl["author_name"]
        self.author_icon: str = tpl["author_icon"]
        self.footer: str = tpl["footer"]
        self.target_channel_id: int | None = None

    def build_embed(self) -> discord.Embed:
        color = _hex_to_color(self.color) or discord.Color(0x2b2d31)
        embed = discord.Embed(
            title=self.title or None,
            description=self.description or None,
            color=color,
        )
        if self.author_name:
            embed.set_author(
                name=self.author_name,
                icon_url=self.author_icon or discord.Embed.Empty,
            )
        if self.thumbnail:
            embed.set_thumbnail(url=self.thumbnail)
        if self.image:
            embed.set_image(url=self.image)
        for field in _parse_fields(self.fields_raw):
            embed.add_field(
                name=field["name"],
                value=field["value"],
                inline=field["inline"],
            )
        if self.footer:
            embed.set_footer(text=self.footer)
        return embed

    def build_view(self) -> View | None:
        buttons = _parse_buttons(self.buttons_raw)
        if not buttons:
            return None
        view = View()
        for btn in buttons[:5]:
            b = Button(
                label=btn["label"],
                url=btn["url"],
                style=discord.ButtonStyle.link,
                emoji=btn["emoji"] or None,
            )
            view.add_item(b)
        return view


# Глобальное хранилище сессий: user_id -> EmbedSession
_sessions: dict[int, EmbedSession] = {}


def get_session(user_id: int) -> EmbedSession:
    if user_id not in _sessions:
        _sessions[user_id] = EmbedSession()
    return _sessions[user_id]


# ──────────────────────────────────────────────
#  Модальные окна
# ──────────────────────────────────────────────

class EmbedBaseModal(Modal, title="✏️ Основные поля эмбеда"):
    title_input = TextInput(
        label="Заголовок (title)", placeholder="• Информация о сервере",
        required=False, max_length=256,
    )
    desc_input = TextInput(
        label="Описание (description)",
        placeholder="Здесь собрана вся важная информация...",
        style=discord.TextStyle.paragraph, required=False, max_length=4000,
    )
    color_input = TextInput(
        label="Цвет (HEX, например faa61a)", placeholder="faa61a",
        required=False, max_length=7,
    )
    footer_input = TextInput(
        label="Footer (текст внизу)", placeholder="TALENT SHOP • 2025",
        required=False, max_length=2048,
    )

    def __init__(self, session: EmbedSession):
        super().__init__()
        self.session = session
        self.title_input.default = session.title
        self.desc_input.default = session.description
        self.color_input.default = session.color
        self.footer_input.default = session.footer

    async def on_submit(self, interaction: discord.Interaction):
        s = self.session
        s.title = self.title_input.value
        s.description = self.desc_input.value
        s.color = self.color_input.value.lstrip("#") or s.color
        s.footer = self.footer_input.value
        await interaction.response.send_message(
            "✅ Основные поля обновлены.", ephemeral=True
        )


class EmbedAuthorImageModal(Modal, title="🖼️ Автор и изображения"):
    author_name = TextInput(
        label="Имя автора (author name)", placeholder="Информация о ролях",
        required=False, max_length=256,
    )
    author_icon = TextInput(
        label="Иконка автора (URL)", placeholder="https://i.imgur.com/...",
        required=False, max_length=500,
    )
    thumbnail = TextInput(
        label="Миниатюра (thumbnail URL)", placeholder="https://i.imgur.com/...",
        required=False, max_length=500,
    )
    image = TextInput(
        label="Большое изображение (image URL)", placeholder="https://i.imgur.com/...",
        required=False, max_length=500,
    )

    def __init__(self, session: EmbedSession):
        super().__init__()
        self.session = session
        self.author_name.default = session.author_name
        self.author_icon.default = session.author_icon
        self.thumbnail.default = session.thumbnail
        self.image.default = session.image

    async def on_submit(self, interaction: discord.Interaction):
        s = self.session
        s.author_name = self.author_name.value
        s.author_icon = self.author_icon.value
        s.thumbnail = self.thumbnail.value
        s.image = self.image.value
        await interaction.response.send_message(
            "✅ Автор и изображения обновлены.", ephemeral=True
        )


class EmbedFieldsModal(Modal, title="📋 Поля (Fields)"):
    fields_input = TextInput(
        label="Поля",
        placeholder=(
            "[Название поля] :: Значение поля\n"
            "---\n"
            "[Другое поле] :: Значение :: inline"
        ),
        style=discord.TextStyle.paragraph,
        required=False, max_length=4000,
    )

    def __init__(self, session: EmbedSession):
        super().__init__()
        self.session = session
        self.fields_input.default = session.fields_raw

    async def on_submit(self, interaction: discord.Interaction):
        self.session.fields_raw = self.fields_input.value
        await interaction.response.send_message(
            "✅ Поля обновлены.", ephemeral=True
        )


class EmbedButtonsModal(Modal, title="🔗 Кнопки-ссылки (как Discohook)"):
    buttons_input = TextInput(
        label="Кнопки (каждая на новой строке)",
        placeholder=(
            "⚙️ Роли | https://discord.com/channels/...\n"
            "⭐ Награды | https://discord.com/channels/...\n"
            "❓ FAQ | https://discord.com/channels/..."
        ),
        style=discord.TextStyle.paragraph,
        required=False, max_length=1000,
    )

    def __init__(self, session: EmbedSession):
        super().__init__()
        self.session = session
        self.buttons_input.default = session.buttons_raw

    async def on_submit(self, interaction: discord.Interaction):
        self.session.buttons_raw = self.buttons_input.value
        await interaction.response.send_message(
            "✅ Кнопки-ссылки обновлены.", ephemeral=True
        )


# ──────────────────────────────────────────────
#  Главная View конструктора
# ──────────────────────────────────────────────

class EmbedBuilderView(View):
    """Панель управления конструктором эмбеда."""

    def __init__(self, session: EmbedSession, guild: discord.Guild, editor_id: int):
        super().__init__(timeout=600)
        self.session = session
        self.guild = guild
        self.editor_id = editor_id

        # Выбор канала назначения
        channels = [
            ch for ch in guild.text_channels
            if ch.permissions_for(guild.me).send_messages
        ][:25]
        if channels:
            options = [
                discord.SelectOption(label=f"#{ch.name}", value=str(ch.id))
                for ch in channels
            ]
            ch_select = Select(
                placeholder="📤 Выбрать канал для отправки",
                options=options,
                custom_id="eb_channel_select",
            )
            ch_select.callback = self._channel_select_callback
            self.add_item(ch_select)

    async def _channel_select_callback(self, interaction: discord.Interaction):
        self.session.target_channel_id = int(interaction.data["values"][0])
        await interaction.response.send_message(
            f"✅ Канал назначения установлен: <#{self.session.target_channel_id}>",
            ephemeral=True,
        )

    async def _check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.editor_id:
            await interaction.response.send_message("❌ Это не ваша панель.", ephemeral=True)
            return False
        return True

    # ── Кнопки ──

    @discord.ui.button(label="✏️ Основные поля", style=discord.ButtonStyle.blurple, row=1)
    async def btn_base(self, interaction: discord.Interaction, button: Button):
        if not await self._check(interaction):
            return
        await interaction.response.send_modal(EmbedBaseModal(self.session))

    @discord.ui.button(label="🖼️ Автор / Изображения", style=discord.ButtonStyle.blurple, row=1)
    async def btn_images(self, interaction: discord.Interaction, button: Button):
        if not await self._check(interaction):
            return
        await interaction.response.send_modal(EmbedAuthorImageModal(self.session))

    @discord.ui.button(label="📋 Поля (Fields)", style=discord.ButtonStyle.blurple, row=1)
    async def btn_fields(self, interaction: discord.Interaction, button: Button):
        if not await self._check(interaction):
            return
        await interaction.response.send_modal(EmbedFieldsModal(self.session))

    @discord.ui.button(label="🔗 Кнопки-ссылки", style=discord.ButtonStyle.blurple, row=1)
    async def btn_buttons(self, interaction: discord.Interaction, button: Button):
        if not await self._check(interaction):
            return
        await interaction.response.send_modal(EmbedButtonsModal(self.session))

    @discord.ui.button(label="👁️ Предпросмотр", style=discord.ButtonStyle.secondary, row=2)
    async def btn_preview(self, interaction: discord.Interaction, button: Button):
        if not await self._check(interaction):
            return
        embed = self.session.build_embed()
        view = self.session.build_view()
        kwargs = {"embed": embed, "ephemeral": True}
        if view:
            kwargs["view"] = view
        await interaction.response.send_message(**kwargs)

    @discord.ui.button(label="📤 Отправить", style=discord.ButtonStyle.green, row=2)
    async def btn_send(self, interaction: discord.Interaction, button: Button):
        if not await self._check(interaction):
            return
        channel_id = self.session.target_channel_id
        if not channel_id:
            await interaction.response.send_message(
                "❌ Сначала выберите канал в выпадающем меню выше.", ephemeral=True
            )
            return
        channel = interaction.guild.get_channel(channel_id)
        if not channel:
            await interaction.response.send_message("❌ Канал не найден.", ephemeral=True)
            return
        embed = self.session.build_embed()
        view = self.session.build_view()
        if view:
            await channel.send(embed=embed, view=view)
        else:
            await channel.send(embed=embed)
        await interaction.response.send_message(
            f"✅ Эмбед отправлен в {channel.mention}!", ephemeral=True
        )

    @discord.ui.button(label="🗑️ Сбросить", style=discord.ButtonStyle.danger, row=2)
    async def btn_reset(self, interaction: discord.Interaction, button: Button):
        if not await self._check(interaction):
            return
        _sessions[interaction.user.id] = EmbedSession()
        self.session = _sessions[interaction.user.id]
        await interaction.response.send_message("🗑️ Черновик сброшен.", ephemeral=True)


# ──────────────────────────────────────────────
#  View выбора шаблона
# ──────────────────────────────────────────────

class TemplateSelectView(View):
    def __init__(self, user_id: int, guild: discord.Guild):
        super().__init__(timeout=60)
        self.user_id = user_id
        self.guild = guild

        options = [
            discord.SelectOption(
                label=tpl["label"],
                value=key,
                description=tpl["title"] or "Пустой шаблон",
            )
            for key, tpl in TEMPLATES.items()
        ]
        select = Select(placeholder="Выбери шаблон…", options=options)
        select.callback = self._on_select
        self.add_item(select)

    async def _on_select(self, interaction: discord.Interaction):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("❌ Это не ваша панель.", ephemeral=True)
            return
        key = interaction.data["values"][0]
        session = EmbedSession(TEMPLATES[key])
        _sessions[self.user_id] = session
        builder_view = EmbedBuilderView(session, self.guild, self.user_id)
        embed = discord.Embed(
            title="🛠️ Конструктор эмбедов",
            description=(
                "**Как пользоваться:**\n"
                "1. Выберите канал в выпадающем меню.\n"
                "2. Нажмите нужную кнопку для редактирования.\n"
                "3. **Предпросмотр** — проверить результат (только вам).\n"
                "4. **Отправить** — опубликовать эмбед в выбранный канал.\n\n"
                "**Формат полей (Fields):**\n"
                "`[Название] :: Значение`  — обычное поле\n"
                "`[Название] :: Значение :: inline`  — inline-поле\n"
                "Разделитель между полями: `---`\n\n"
                "**Формат кнопок (Link Buttons):**\n"
                "`Метка | https://... | 🔗` (эмодзи необязательно)\n"
                "До 5 кнопок, каждая на новой строке."
            ),
            color=discord.Color.gold(),
        )
        await interaction.response.send_message(embed=embed, view=builder_view, ephemeral=True)


# ──────────────────────────────────────────────
#  Регистрация команды
# ──────────────────────────────────────────────

def setup_embed_builder(bot: commands.Bot):  # type: ignore[name-defined]
    """Вызвать в on_ready или сразу после создания bot."""

    @bot.tree.command(
        name="embed_builder",
        description="[ADMIN] Конструктор эмбедов (Discohook / EmbedGenerator стиль)",
    )
    async def embed_builder_cmd(interaction: discord.Interaction):
        # Проверка прав — минимум manage_messages
        if not interaction.user.guild_permissions.manage_messages:
            await interaction.response.send_message(
                "❌ Нужно право **Manage Messages**.", ephemeral=True
            )
            return

        view = TemplateSelectView(interaction.user.id, interaction.guild)
        await interaction.response.send_message(
            "📐 **Конструктор эмбедов** — выберите шаблон для старта:",
            view=view,
            ephemeral=True,
        )
