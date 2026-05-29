import asyncio
import re
import logging

from client import get_next_groq_client
from prompts import MODE_MODELS, SYSTEM_PROMPTS, MODEL_CODING
from utils.helpers import clean_markdown

logger = logging.getLogger(__name__)

async def ask_groq_with_retry(model: str, messages: list, max_retries: int = 4, temperature: float = 0.2) -> str:
    """Запрос к Groq с автоматическим переключением ключей при 429 ошибке"""
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
            # Фикс кодировки
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

async def ask_groq_mode(mode: str, prompt: str, history: list = None, show_think: str = "hide") -> str:
    """Запрос к Groq с учётом режима и истории"""
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
