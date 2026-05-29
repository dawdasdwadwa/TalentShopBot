import re

def clean_markdown(text: str) -> str:
    """Удаляет markdown-разметку, оставляя чистый текст"""
    text = re.sub(r'^#{1,6}\s+', '', text, flags=re.MULTILINE)
    text = re.sub(r'\*\*(.+?)\*\*', r'\1', text)
    text = re.sub(r'\*(.+?)\*', r'\1', text)
    text = re.sub(r'^[-*_]{3,}$', '', text, flags=re.MULTILINE)
    text = re.sub(r'\[(.+?)\]\(.+?\)', r'\1', text)
    text = re.sub(r'`(.+?)`', r'\1', text)
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text.strip()

def parse_price_rub(price_str: str):
    """Парсит число из строки цены"""
    match = re.search(r'[\d]+(?:[.,]\d+)?', price_str.replace(' ', ''))
    if match:
        return float(match.group().replace(',', '.'))
    return None
