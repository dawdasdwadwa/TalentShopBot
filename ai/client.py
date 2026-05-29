import os
import groq
import logging

logger = logging.getLogger(__name__)

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
    """Возвращает следующего клиента Groq (циклическая ротация)"""
    global _current_client_index
    if not groq_clients:
        return None
    client = groq_clients[_current_client_index]
    _current_client_index = (_current_client_index + 1) % len(groq_clients)
    return client
