import discord
import logging
from discord.ui import TextInput, Modal, Button, View, Select

logger = logging.getLogger(__name__)

from ..config.constants import CATEGORY_LABELS, CATEGORY_CHANNELS
from .processor import process_ai_request
from .. import database as db

# ================= AIInputModal =================
class AIInputModal(discord.ui.Modal):
    def __init__(self, category: str, use_history: bool, use_server_context: bool):
        super().__init__(title=f"🤖 {CATEGORY_LABELS.get(category, category)}")
        self.category = category
        self.use_history = use_history
        self.use_server_context = use_server_context
        self.prompt_input = TextInput(
            label="Ваш запрос",
            style=discord.TextStyle.paragraph,
            placeholder="Введите ваш запрос...",
            required=True,
            max_length=2000
        )
        self.add_item(self.prompt_input)
    
    async def on_submit(self, interaction: discord.Interaction):
        await process_ai_request(interaction, self.category, self.prompt_input.value, self.use_history, self.use_server_context, "hide")

# ================= AIRequestView =================
class AIRequestView(discord.ui.View):
    def __init__(self, category: str):
        super().__init__(timeout=60)
        self.category = category
    
    @discord.ui.select(placeholder="🧠 Учитывать историю?", options=[
        discord.SelectOption(label="Да, учитывать", value="yes", emoji="🧠"),
        discord.SelectOption(label="Нет, чистый запрос", value="no", emoji="🧹"),
    ])
    async def select_callback(self, interaction: discord.Interaction, select: discord.ui.Select):
        use_history = select.values[0] == "yes"
        
        class ServerContextView(discord.ui.View):
            def __init__(self, cat: str, hist: bool):
                super().__init__(timeout=60)
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
        await interaction.response.edit_message(
            content=f"🎯 **{CATEGORY_LABELS.get(self.category, self.category)}**\n\nУчитывать структуру этого сервера?",
            view=view
        )

# ================= MainSelectView =================
class MainSelectView(discord.ui.View):
    def __init__(self, options: list):
        super().__init__(timeout=120)
        self.select = discord.ui.Select(
            placeholder="🎯 Выберите режим работы",
            options=options,
            min_values=1,
            max_values=1
        )
        self.select.callback = self.select_callback
        self.add_item(self.select)
    
    async def select_callback(self, i: discord.Interaction):
        selected = self.select.values[0]
        if selected == "clear_history":
            await db.clear_user_history(i.user.id)
            await i.response.send_message("✅ История диалогов очищена!", ephemeral=True)
        else:
            view = AIRequestView(category=selected)
            await i.response.edit_message(
                content=f"🎯 **{CATEGORY_LABELS.get(selected, selected)}**\n\n"
                        f"📋 Вы выбрали режим: **{CATEGORY_LABELS.get(selected, selected)}**\n"
                        f"Теперь выберите, нужно ли учитывать историю прошлых сообщений:",
                view=view
            )

# ================= StartAIButton =================
class StartAIButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="🚀 Запустить ИИ-Конвейер", style=discord.ButtonStyle.success, custom_id="start_ai")
    
    async def callback(self, interaction: discord.Interaction):
        try:
            await interaction.response.defer(ephemeral=True)
            
            options = [
                discord.SelectOption(label="💻 Кодинг", value="coding", description="Написание кода для бота на Python", emoji="💻"),
                discord.SelectOption(label="💡 Советы", value="advice", description="Развитие сервера и увеличение активности", emoji="💡"),
                discord.SelectOption(label="🎨 Оформление", value="design", description="Дизайн каналов, ролей, эмбедов", emoji="🎨"),
                discord.SelectOption(label="📊 Аналитика", value="analytics", description="Анализ статистики и отчёты", emoji="📊"),
                discord.SelectOption(label="📝 Контент", value="content", description="Тексты для новостей, правил, анонсов", emoji="📝"),
                discord.SelectOption(label="📈 Маркетинг", value="marketing", description="Стратегии продаж и акции", emoji="📈"),
                discord.SelectOption(label="🤖 Бот-фичи", value="features", description="Идеи для новых функций бота", emoji="🤖"),
                discord.SelectOption(label="🗑️ Очистить историю", value="clear_history", description="Удалить всю историю диалогов", emoji="🗑️"),
            ]
            
            view = MainSelectView(options)
            await interaction.followup.send(
                "🎯 **ИИ-Конвейер — выберите режим работы**\n\n"
                "📋 **Доступные режимы:**\n"
                "• 💻 **Кодинг** — написание кода для бота\n"
                "• 💡 **Советы** — развитие сервера и сообщества\n"
                "• 🎨 **Оформление** — дизайн каналов, роли, эмбеды\n"
                "• 📊 **Аналитика** — анализ статистики сервера\n"
                "• 📝 **Контент** — тексты, новости, правила\n"
                "• 📈 **Маркетинг** — акции, продажи, привлечение\n"
                "• 🤖 **Бот-фичи** — идеи для новых функций\n\n"
                "🧠 **Фишки:** история диалога + учёт структуры сервера\n"
                "🏠 **Новое:** ИИ может учитывать твои роли и каналы!",
                view=view,
                ephemeral=True
            )
            
        except Exception as e:
            logger.error(f"Ошибка в StartAIButton: {e}")
            await interaction.followup.send(f"❌ Ошибка: {e}", ephemeral=True)
