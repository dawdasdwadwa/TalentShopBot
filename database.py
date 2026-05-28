import aiosqlite
import asyncio
import uuid
import aiohttp
import os
import json
from typing import Optional, Dict, List, Tuple, Any
from datetime import datetime, timezone, timedelta
from dataclasses import dataclass, field
from contextlib import asynccontextmanager
from contextvars import ContextVar
import logging

logger = logging.getLogger(__name__)

DB_PATH = os.getenv("DATABASE_PATH", "/mnt/data/shop_data.db")
db: Optional[aiosqlite.Connection] = None
_db_lock = asyncio.Lock()
_init_lock = asyncio.Lock()
_initializing = False
_transaction_depth: ContextVar[int] = ContextVar("transaction_depth", default=0)
_transaction_lock = asyncio.Lock()
_transaction_owner: ContextVar[bool] = ContextVar("transaction_owner", default=False)

# Глобальные кэши
categories_cache: Dict[int, 'Category'] = {}
lots_cache: Dict[int, 'Lot'] = {}
promos_cache: Dict[str, 'Promo'] = {}
blacklist_cache: List[int] = []
stats_cache: Dict[int, dict] = {}
warnings_cache: Dict[int, dict] = {}
currency_rates_cache: Dict[str, float] = {}

# ================= DATACLASSES =================
@dataclass
class Lot:
    lot_id: int
    name: str
    price: str
    stock: int = 0
    prices: Dict[str, str] = field(default_factory=dict)
    short_description: str = ""
    full_description: str = ""
    seller_id: int = 0
    category_id: int = 0
    image_url: Optional[str] = None
    role_id: Optional[int] = None

@dataclass
class Category:
    id: int
    name: str
    emoji: str = "📁"
    description: Optional[str] = None
    image_url: Optional[str] = None
    lots: List[int] = field(default_factory=list)

@dataclass
class Warning:
    id: int
    user_id: int
    moderator_id: int
    reason: str
    created_at: str

@dataclass
class Promo:
    code: str
    discount: int
    expires: str
    uses: int = 0
    max_uses: int = 100

# ================= SAFE EXECUTE =================
async def execute(query: str, params: tuple = (), retries: int = 5):
    global _initializing
    while _initializing:
        await asyncio.sleep(0.05)
    # ensure_db вызывается ВНЕ _db_lock, чтобы не было deadlock
    await ensure_db()
    last_error = None
    for attempt in range(retries):
        try:
            async with _db_lock:
                return await db.execute(query, params)
        except aiosqlite.OperationalError as e:
            if "locked" in str(e).lower() and attempt < retries - 1:
                await asyncio.sleep(0.2 * (attempt + 1))
                last_error = e
                continue
            raise
        except Exception:
            raise
    if last_error:
        raise last_error
    raise RuntimeError("База данных заблокирована")

async def _execute_no_lock(query: str, params: tuple = ()):
    return await db.execute(query, params)

async def _executemany_no_lock(query: str, params_list: List[tuple]):
    return await db.executemany(query, params_list)

async def fetchone(query: str, params: tuple = (), retries: int = 5):
    cursor = await execute(query, params, retries)
    return await cursor.fetchone()

async def fetchall(query: str, params: tuple = (), retries: int = 5):
    cursor = await execute(query, params, retries)
    return await cursor.fetchall()

async def executemany(query: str, params_list: List[tuple], retries: int = 5):
    global _initializing
    while _initializing:
        await asyncio.sleep(0.05)
    await ensure_db()
    for attempt in range(retries):
        try:
            async with _db_lock:
                return await db.executemany(query, params_list)
        except aiosqlite.OperationalError as e:
            if "locked" in str(e).lower() and attempt < retries - 1:
                await asyncio.sleep(0.2 * (attempt + 1))
                continue
            raise

# ================= TRANSACTION =================
@asynccontextmanager
async def transaction(read_only: bool = False):
    global _initializing
    while _initializing:
        await asyncio.sleep(0.05)
    await ensure_db()
    depth = _transaction_depth.get()
    is_root = depth == 0
    token = None
    sp_name = None
    if is_root:
        await _transaction_lock.acquire()
        # _db_lock не нужен: _transaction_lock уже гарантирует
        # что только одна транзакция активна в момент времени
        _transaction_owner.set(True)
        if read_only:
            await db.execute("BEGIN DEFERRED")
        else:
            await db.execute("BEGIN IMMEDIATE")
    else:
        sp_name = f"sp_{depth}_{uuid.uuid4().hex}"
        await db.execute(f"SAVEPOINT {sp_name}")
    token = _transaction_depth.set(depth + 1)
    try:
        yield
        # ВАЖНО: не захватываем _db_lock для commit/rollback —
        # BEGIN IMMEDIATE уже владеет эксклюзивным доступом SQLite,
        # а повторный lock вызовет deadlock (asyncio.Lock не реентерабельный)
        if is_root:
            await db.commit()
        else:
            await db.execute(f"RELEASE SAVEPOINT {sp_name}")
    except Exception:
        try:
            if is_root:
                await db.rollback()
            else:
                await db.execute(f"ROLLBACK TO SAVEPOINT {sp_name}")
                await db.execute(f"RELEASE SAVEPOINT {sp_name}")
        except Exception:
            pass
        raise
    finally:
        if token:
            _transaction_depth.reset(token)
        if is_root and _transaction_owner.get():
            _transaction_owner.set(False)
            _transaction_lock.release()

# ================= RECONNECT PROTECTION =================
async def ensure_db():
    """Проверяет соединение с БД и переподключается при необходимости.
    Вызывается ВНЕ _db_lock. Внутри активной транзакции пропускает проверку —
    соединение гарантированно живо, раз транзакция открыта."""
    global db, _initializing
    # Если мы внутри транзакции — соединение точно живо, проверять не нужно
    if _transaction_owner.get():
        return
    # Быстрый путь: соединение уже живо
    if db is not None:
        try:
            await db.execute("SELECT 1")
            return
        except Exception:
            pass
    # Медленный путь: нужно переподключиться
    async with _db_lock:
        # Двойная проверка после захвата lock
        if db is not None:
            try:
                await db.execute("SELECT 1")
                return
            except Exception:
                pass
        if db is not None:
            try:
                await db.close()
            except Exception:
                pass
            db = None
        await init_db()

# ================= INIT =================
async def init_db():
    global db, _initializing, categories_cache, lots_cache, promos_cache, blacklist_cache, stats_cache, warnings_cache
    async with _init_lock:
        if _initializing:
            return
        if db is not None:
            return
        _initializing = True
        try:
            print("🔥 init_db(): начало инициализации БД...")
            logger.info("🔥 init_db(): начало инициализации БД...")
            
            db_dir = os.path.dirname(DB_PATH)
            if db_dir and not os.path.exists(db_dir):
                os.makedirs(db_dir, exist_ok=True)
                print(f"✅ Создана директория БД: {db_dir}")

            print(f"⏳ Подключение к БД: {DB_PATH}")
            db = await aiosqlite.connect(DB_PATH, timeout=20.0)
            db.row_factory = aiosqlite.Row
            print("✅ БД подключена")
            
            print("⏳ Установка PRAGMA...")
            await db.execute("PRAGMA journal_mode=WAL")
            await db.execute("PRAGMA foreign_keys=ON")
            await db.execute("PRAGMA synchronous=NORMAL")
            await db.execute("PRAGMA temp_store=MEMORY")
            await db.execute("PRAGMA mmap_size=268435456")
            await db.execute("PRAGMA cache_size=-64000")
            print("✅ PRAGMA установлены")

            print("⏳ Создание таблиц...")
            await db.execute('''CREATE TABLE IF NOT EXISTS categories (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                emoji TEXT DEFAULT '📁',
                description TEXT,
                image_url TEXT
            )''')

            await db.execute('''CREATE TABLE IF NOT EXISTS lots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                price TEXT,
                stock INTEGER DEFAULT 0,
                short_description TEXT,
                full_description TEXT,
                seller_id INTEGER,
                category_id INTEGER,
                image_url TEXT,
                role_id INTEGER,
                FOREIGN KEY (category_id) REFERENCES categories(id) ON DELETE CASCADE
            )''')

            await db.execute('CREATE INDEX IF NOT EXISTS idx_lots_category ON lots(category_id)')
            await db.execute('CREATE INDEX IF NOT EXISTS idx_lots_seller ON lots(seller_id)')

            await db.execute('''CREATE TABLE IF NOT EXISTS lot_prices (
                lot_id INTEGER,
                currency TEXT,
                price TEXT,
                PRIMARY KEY (lot_id, currency),
                FOREIGN KEY (lot_id) REFERENCES lots(id) ON DELETE CASCADE
            )''')

            await db.execute('''CREATE TABLE IF NOT EXISTS promos (
                code TEXT PRIMARY KEY,
                discount INTEGER,
                expires TEXT,
                uses INTEGER DEFAULT 0,
                max_uses INTEGER DEFAULT 100
            )''')
            await db.execute('CREATE INDEX IF NOT EXISTS idx_promos_expires ON promos(expires)')

            await db.execute('''CREATE TABLE IF NOT EXISTS blacklist (
                user_id INTEGER PRIMARY KEY
            )''')

            await db.execute('''CREATE TABLE IF NOT EXISTS blacklist_extended (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER UNIQUE,
                moderator_id INTEGER,
                reason TEXT,
                warnings_count INTEGER DEFAULT 0,
                created_at TEXT
            )''')

            await db.execute('''CREATE TABLE IF NOT EXISTS ban_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                moderator_id INTEGER,
                action TEXT,
                reason TEXT,
                created_at TEXT
            )''')
            await db.execute('CREATE INDEX IF NOT EXISTS idx_ban_history_user ON ban_history(user_id)')

            await db.execute('''CREATE TABLE IF NOT EXISTS stats (
                user_id INTEGER PRIMARY KEY,
                sales INTEGER DEFAULT 0,
                revenue INTEGER DEFAULT 0
            )''')

            await db.execute('''CREATE TABLE IF NOT EXISTS warnings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                moderator_id INTEGER,
                reason TEXT,
                created_at TEXT
            )''')
            await db.execute('CREATE INDEX IF NOT EXISTS idx_warnings_user ON warnings(user_id)')

            await db.execute('''CREATE TABLE IF NOT EXISTS shop_messages (
                guild_id INTEGER PRIMARY KEY,
                img_id INTEGER,
                stat_id INTEGER
            )''')

            await db.execute('''CREATE TABLE IF NOT EXISTS purchases (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                lot_id INTEGER,
                price TEXT,
                status TEXT DEFAULT 'completed',
                created_at TEXT
            )''')
            await db.execute('CREATE INDEX IF NOT EXISTS idx_purchases_user ON purchases(user_id)')
            await db.execute('CREATE INDEX IF NOT EXISTS idx_purchases_lot ON purchases(lot_id)')
            await db.execute('CREATE INDEX IF NOT EXISTS idx_purchases_user_lot ON purchases(user_id, lot_id)')

            await db.execute('''CREATE TABLE IF NOT EXISTS reviews (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                seller_id INTEGER,
                lot_id INTEGER,
                rating INTEGER,
                comment TEXT,
                created_at TEXT
            )''')
            await db.execute('CREATE INDEX IF NOT EXISTS idx_reviews_seller ON reviews(seller_id)')
            await db.execute('CREATE INDEX IF NOT EXISTS idx_reviews_user ON reviews(user_id)')

            await db.execute('''CREATE TABLE IF NOT EXISTS tickets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                channel_id INTEGER UNIQUE,
                voice_channel_id INTEGER,
                user_id INTEGER,
                guild_id INTEGER,
                status TEXT DEFAULT 'open',
                created_at TEXT,
                closed_at TEXT,
                last_activity TEXT
            )''')
            await db.execute('CREATE INDEX IF NOT EXISTS idx_tickets_user ON tickets(user_id)')
            await db.execute('CREATE INDEX IF NOT EXISTS idx_tickets_status ON tickets(status)')

            await db.execute('''CREATE TABLE IF NOT EXISTS lot_templates (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                price TEXT,
                short_description TEXT,
                full_description TEXT,
                category_id INTEGER,
                seller_id INTEGER,
                created_by INTEGER,
                created_at TEXT
            )''')

            await db.execute('''CREATE TABLE IF NOT EXISTS purchase_limits (
                user_id INTEGER,
                date TEXT,
                count INTEGER DEFAULT 0,
                PRIMARY KEY (user_id, date)
            )''')

            await db.execute('''CREATE TABLE IF NOT EXISTS seller_review_messages (
                seller_id INTEGER PRIMARY KEY,
                message_id INTEGER,
                channel_id INTEGER
            )''')

            await db.execute('''CREATE TABLE IF NOT EXISTS referrals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                referrer_id INTEGER,
                referred_id INTEGER,
                created_at TEXT
            )''')
            await db.execute('CREATE INDEX IF NOT EXISTS idx_referrals_referrer ON referrals(referrer_id)')

            await db.execute('''CREATE TABLE IF NOT EXISTS currency_rates (
                currency TEXT PRIMARY KEY,
                rate REAL,
                updated_at TEXT
            )''')

            print("✅ Все таблицы созданы")
            
            print("⏳ Коммит...")
            await db.commit()
            print("✅ Коммит завершён")
            
            print("⏳ Загрузка кэша...")
            await refresh_cache()
            print("✅ Кэш загружен")

        except Exception as e:
            print(f"❌ Ошибка инициализации БД: {e}")
            logger.exception("❌ Ошибка инициализации БД")
        finally:
            _initializing = False

async def close_db():
    global db, _initializing
    while _initializing:
        await asyncio.sleep(0.01)
    if db:
        try:
            await db.execute("PRAGMA wal_checkpoint(FULL)")
            await db.commit()
        except:
            pass
        try:
            await db.close()
        except:
            pass
        db = None

# ================= CATEGORIES =================
async def get_all_categories() -> Dict[int, Category]:
    global categories_cache
    if categories_cache:
        return categories_cache
    print("  ⏳ get_all_categories(): entering transaction...")
    try:
        async with transaction(read_only=True):
            print("    ⏳ get_all_categories(): executing SELECT categories...")
            rows = await _execute_no_lock('SELECT * FROM categories ORDER BY id')
            print("    ⏳ get_all_categories(): fetched cursor for categories, now fetching all rows...")
            cat_rows = await rows.fetchall()
            print(f"    ✅ get_all_categories(): categories rows count: {len(cat_rows)}")
            print("    ⏳ get_all_categories(): executing SELECT id, category_id FROM lots...")
            lots_rows = await _execute_no_lock('SELECT id, category_id FROM lots')
            print("    ⏳ get_all_categories(): fetched cursor for lots, now fetching all rows...")
            lot_rows = await lots_rows.fetchall()
            print(f"    ✅ get_all_categories(): lots rows count: {len(lot_rows)}")
    except Exception as e:
        print(f"❌ get_all_categories() error: {e}")
        logger.exception("❌ get_all_categories() error")
        return {}
    lots_by_category = {}
    for row in lot_rows:
        cat_id = row['category_id']
        if cat_id not in lots_by_category:
            lots_by_category[cat_id] = []
        lots_by_category[cat_id].append(row['id'])
    result = {
        row['id']: Category(
            id=row['id'], name=row['name'], emoji=row['emoji'] or "📁",
            description=row['description'], image_url=row['image_url'],
            lots=lots_by_category.get(row['id'], [])
        )
        for row in cat_rows
    }
    categories_cache = result
    print(f"  ✅ get_all_categories(): returning {len(result)} categories")
    return result

async def get_category(category_id: int) -> Optional[Category]:
    global categories_cache
    if category_id in categories_cache:
        return categories_cache[category_id]
    row = await fetchone('SELECT * FROM categories WHERE id = ?', (category_id,))
    if not row:
        return None
    lots_rows = await fetchall('SELECT id FROM lots WHERE category_id = ?', (category_id,))
    result = Category(
        id=row['id'], name=row['name'], emoji=row['emoji'] or "📁",
        description=row['description'], image_url=row['image_url'],
        lots=[r['id'] for r in lots_rows]
    )
    categories_cache[category_id] = result
    return result

async def add_category(name: str, emoji: str = "📁", description: str = None, image_url: str = None) -> int:
    global categories_cache
    async with transaction():
        cursor = await _execute_no_lock(
            'INSERT INTO categories (name, emoji, description, image_url) VALUES (?, ?, ?, ?)',
            (name, emoji, description, image_url)
        )
        cat_id = cursor.lastrowid
        categories_cache = {}
        return cat_id

async def update_category(category_id: int, **kwargs):
    global categories_cache
    allowed = {"name", "emoji", "description", "image_url"}
    updates = [(k, kwargs[k]) for k in allowed if k in kwargs]
    if updates:
        async with transaction():
            set_clause = ", ".join([f"{k} = ?" for k, _ in updates])
            params = [v for _, v in updates] + [category_id]
            await _execute_no_lock(f'UPDATE categories SET {set_clause} WHERE id = ?', tuple(params))
            categories_cache = {}

async def delete_category(category_id: int):
    global categories_cache, lots_cache
    async with transaction():
        await _execute_no_lock('DELETE FROM categories WHERE id = ?', (category_id,))
        categories_cache = {}
        lots_cache = {}

# ================= LOTS =================
async def get_all_lots() -> Dict[int, Lot]:
    global lots_cache
    if lots_cache:
        return lots_cache
    async with transaction(read_only=True):
        rows = await _execute_no_lock('SELECT * FROM lots')
        lot_rows = await rows.fetchall()
        price_rows = await _execute_no_lock('SELECT lot_id, currency, price FROM lot_prices')
        price_data = await price_rows.fetchall()
    prices_by_lot = {}
    for row in price_data:
        lot_id = row['lot_id']
        if lot_id not in prices_by_lot:
            prices_by_lot[lot_id] = {}
        prices_by_lot[lot_id][row['currency']] = row['price']
    result = {
        row['id']: Lot(
            lot_id=row['id'], name=row['name'], price=row['price'] or "",
            stock=row['stock'] or 0, prices=prices_by_lot.get(row['id'], {}),
            short_description=row['short_description'] or "",
            full_description=row['full_description'] or "",
            seller_id=row['seller_id'], category_id=row['category_id'],
            image_url=row['image_url'], role_id=row['role_id']
        )
        for row in lot_rows
    }
    lots_cache = result
    return result

async def get_lot(lot_id: int) -> Optional[Lot]:
    global lots_cache
    if lot_id in lots_cache:
        return lots_cache[lot_id]
    row = await fetchone('SELECT * FROM lots WHERE id = ?', (lot_id,))
    if not row:
        return None
    price_rows = await fetchall('SELECT currency, price FROM lot_prices WHERE lot_id = ?', (lot_id,))
    result = Lot(
        lot_id=row['id'], name=row['name'], price=row['price'] or "",
        stock=row['stock'] or 0, prices={r['currency']: r['price'] for r in price_rows},
        short_description=row['short_description'] or "",
        full_description=row['full_description'] or "",
        seller_id=row['seller_id'], category_id=row['category_id'],
        image_url=row['image_url'], role_id=row['role_id']
    )
    lots_cache[lot_id] = result
    return result

async def get_lots_by_category(category_id: int) -> List[int]:
    global lots_cache
    if lots_cache:
        return [lot_id for lot_id, lot in lots_cache.items() if lot.category_id == category_id]
    rows = await fetchall('SELECT id FROM lots WHERE category_id = ?', (category_id,))
    return [r['id'] for r in rows]

async def get_lots_by_category_full(category_id: int) -> List['Lot']:
    global lots_cache
    if lots_cache:
        return [lot for lot in lots_cache.values() if lot.category_id == category_id]
    async with transaction(read_only=True):
        rows = await _execute_no_lock('SELECT * FROM lots WHERE category_id = ?', (category_id,))
        lot_rows = await rows.fetchall()
        if not lot_rows:
            return []
        lot_ids = [r['id'] for r in lot_rows]
        placeholders = ','.join('?' * len(lot_ids))
        price_rows_cur = await _execute_no_lock(
            f'SELECT lot_id, currency, price FROM lot_prices WHERE lot_id IN ({placeholders})',
            tuple(lot_ids)
        )
        price_rows = await price_rows_cur.fetchall()
    prices_by_lot: dict = {}
    for row in price_rows:
        prices_by_lot.setdefault(row['lot_id'], {})[row['currency']] = row['price']
    return [
        Lot(
            lot_id=r['id'], name=r['name'], price=r['price'] or "", stock=r['stock'] or 0,
            prices=prices_by_lot.get(r['id'], {}), short_description=r['short_description'] or "",
            full_description=r['full_description'] or "", seller_id=r['seller_id'],
            category_id=r['category_id'], image_url=r['image_url'], role_id=r['role_id']
        )
        for r in lot_rows
    ]

async def get_lots_by_seller(seller_id: int) -> List[int]:
    global lots_cache
    if lots_cache:
        return [lot_id for lot_id, lot in lots_cache.items() if lot.seller_id == seller_id]
    rows = await fetchall('SELECT id FROM lots WHERE seller_id = ?', (seller_id,))
    return [r['id'] for r in rows]

async def search_lots(query: str) -> List[Lot]:
    rows = await fetchall('SELECT * FROM lots WHERE name LIKE ?', (f'%{query}%',))
    if not rows:
        return []
    result = []
    for row in rows:
        price_rows = await fetchall('SELECT currency, price FROM lot_prices WHERE lot_id = ?', (row['id'],))
        result.append(Lot(
            lot_id=row['id'], name=row['name'], price=row['price'] or "", stock=row['stock'] or 0,
            prices={r['currency']: r['price'] for r in price_rows},
            short_description=row['short_description'] or "", full_description=row['full_description'] or "",
            seller_id=row['seller_id'], category_id=row['category_id'],
            image_url=row['image_url'], role_id=row['role_id']
        ))
    return result

async def add_lot(name: str, price: str, short_description: str, full_description: str,
                  seller_id: int, category_id: int, stock: int = 0, image_url: str = None, role_id: int = None) -> int:
    global lots_cache, categories_cache
    async with transaction():
        cursor = await _execute_no_lock(
            'INSERT INTO lots (name, price, stock, short_description, full_description, seller_id, category_id, image_url, role_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)',
            (name, price, stock, short_description, full_description, seller_id, category_id, image_url, role_id)
        )
        lot_id = cursor.lastrowid
        lots_cache = {}
        categories_cache = {}
        return lot_id

async def update_lot(lot_id: int, **kwargs):
    global lots_cache, categories_cache
    allowed = {"name", "price", "stock", "short_description", "full_description", "seller_id", "category_id", "image_url", "role_id"}
    updates = [(k, kwargs[k]) for k in allowed if k in kwargs]
    if updates:
        async with transaction():
            set_clause = ", ".join([f"{k} = ?" for k, _ in updates])
            params = [v for _, v in updates] + [lot_id]
            await _execute_no_lock(f'UPDATE lots SET {set_clause} WHERE id = ?', tuple(params))
            lots_cache = {}
            categories_cache = {}

async def update_lot_prices(lot_id: int, prices: dict):
    global lots_cache
    async with transaction():
        await _execute_no_lock('DELETE FROM lot_prices WHERE lot_id = ?', (lot_id,))
        if prices:
            await _executemany_no_lock(
                'INSERT INTO lot_prices (lot_id, currency, price) VALUES (?, ?, ?)',
                [(lot_id, curr, price) for curr, price in prices.items()]
            )
        lots_cache = {}

async def delete_lot(lot_id: int):
    global lots_cache, categories_cache
    async with transaction():
        await _execute_no_lock('DELETE FROM lots WHERE id = ?', (lot_id,))
        lots_cache = {}
        categories_cache = {}

async def move_lot_to_category(lot_id: int, new_category_id: int):
    global lots_cache, categories_cache
    async with transaction():
        await _execute_no_lock('UPDATE lots SET category_id = ? WHERE id = ?', (new_category_id, lot_id))
        lots_cache = {}
        categories_cache = {}

# ================= STOCK =================
async def update_stock(lot_id: int, quantity: int = -1):
    global lots_cache, categories_cache
    async with transaction():
        await _execute_no_lock(
            'UPDATE lots SET stock = stock + ? WHERE id = ? AND stock + ? >= 0',
            (quantity, lot_id, quantity)
        )
        lots_cache = {}
        categories_cache = {}

async def get_stock(lot_id: int) -> int:
    row = await fetchone('SELECT stock FROM lots WHERE id = ?', (lot_id,))
    return row['stock'] if row else 0

# ================= PURCHASES =================
async def add_purchase(user_id: int, lot_id: int, price: str) -> int:
    today = datetime.now(timezone.utc).strftime('%Y-%m-%d')
    async with transaction():
        cursor = await _execute_no_lock(
            'INSERT INTO purchases (user_id, lot_id, price, created_at) VALUES (?, ?, ?, datetime("now"))',
            (user_id, lot_id, price)
        )
        await _execute_no_lock(
            'INSERT INTO purchase_limits (user_id, date, count) VALUES (?, ?, 1) ON CONFLICT(user_id, date) DO UPDATE SET count = count + 1',
            (user_id, today)
        )
        return cursor.lastrowid

async def get_daily_purchase_count(user_id: int) -> int:
    today = datetime.now(timezone.utc).strftime('%Y-%m-%d')
    row = await fetchone('SELECT count FROM purchase_limits WHERE user_id = ? AND date = ?', (user_id, today))
    return row['count'] if row else 0

async def has_user_bought(user_id: int, lot_id: int) -> bool:
    row = await fetchone(
        'SELECT 1 FROM purchases WHERE user_id = ? AND lot_id = ? AND status = "completed" LIMIT 1',
        (user_id, lot_id)
    )
    return row is not None

async def get_user_purchases(user_id: int) -> List[dict]:
    rows = await fetchall('SELECT * FROM purchases WHERE user_id = ? ORDER BY id DESC', (user_id,))
    return [dict(row) for row in rows] if rows else []

async def search_purchases_by_user(user_id: int) -> List[dict]:
    rows = await fetchall('SELECT * FROM purchases WHERE user_id = ? ORDER BY created_at DESC', (user_id,))
    return [dict(row) for row in rows] if rows else []

async def get_purchases_by_date_range(start_date: str, end_date: str) -> List[dict]:
    rows = await fetchall(
        'SELECT * FROM purchases WHERE created_at >= ? AND created_at <= ? ORDER BY created_at DESC',
        (start_date, end_date)
    )
    return [dict(row) for row in rows] if rows else []

# ================= TICKETS =================
async def add_ticket(channel_id: int, user_id: int, guild_id: int, voice_channel_id: int = None) -> int:
    created_at = datetime.now(timezone.utc).isoformat()
    async with transaction():
        cursor = await _execute_no_lock(
            'INSERT INTO tickets (channel_id, voice_channel_id, user_id, guild_id, status, created_at, last_activity) VALUES (?, ?, ?, ?, "open", ?, ?)',
            (channel_id, voice_channel_id, user_id, guild_id, created_at, created_at)
        )
        return cursor.lastrowid

async def get_ticket(channel_id: int) -> Optional[dict]:
    row = await fetchone('SELECT * FROM tickets WHERE channel_id = ?', (channel_id,))
    return dict(row) if row else None

async def get_user_active_ticket(user_id: int) -> Optional[dict]:
    """Проверяет, есть ли у пользователя открытый тикет"""
    row = await fetchone(
        'SELECT * FROM tickets WHERE user_id = ? AND status = "open" LIMIT 1',
        (user_id,)
    )
    return dict(row) if row else None

async def close_ticket(channel_id: int):
    closed_at = datetime.now(timezone.utc).isoformat()
    async with transaction():
        await _execute_no_lock(
            'UPDATE tickets SET status = "closed", closed_at = ? WHERE channel_id = ?',
            (closed_at, channel_id)
        )

async def update_ticket_activity(channel_id: int):
    now = datetime.now(timezone.utc).isoformat()
    async with transaction():
        await _execute_no_lock('UPDATE tickets SET last_activity = ? WHERE channel_id = ?', (now, channel_id))

async def delete_ticket_record(channel_id: int):
    async with transaction():
        await _execute_no_lock('DELETE FROM tickets WHERE channel_id = ?', (channel_id,))

async def get_expired_tickets() -> List[dict]:
    now = datetime.now(timezone.utc)
    closed_cutoff = (now - timedelta(hours=24)).isoformat()
    inactive_cutoff = (now - timedelta(days=7)).isoformat()
    rows = await fetchall(
        '''SELECT * FROM tickets WHERE
           (status = "closed" AND closed_at <= ?) OR
           (status = "open" AND last_activity <= ?)''',
        (closed_cutoff, inactive_cutoff)
    )
    return [dict(row) for row in rows] if rows else []

# ================= REVIEWS =================
async def add_review(user_id: int, seller_id: int, lot_id: int, rating: int, comment: str):
    async with transaction():
        await _execute_no_lock(
            'INSERT INTO reviews (user_id, seller_id, lot_id, rating, comment, created_at) VALUES (?, ?, ?, ?, ?, datetime("now"))',
            (user_id, seller_id, lot_id, rating, comment)
        )

async def get_seller_reviews(seller_id: int, limit: int = 10) -> List[dict]:
    rows = await fetchall(
        'SELECT * FROM reviews WHERE seller_id = ? ORDER BY created_at DESC LIMIT ?',
        (seller_id, limit)
    )
    return [dict(row) for row in rows] if rows else []

async def get_user_reviews(user_id: int) -> List[dict]:
    rows = await fetchall(
        'SELECT * FROM reviews WHERE user_id = ? ORDER BY created_at DESC',
        (user_id,)
    )
    return [dict(row) for row in rows] if rows else []

async def get_seller_rating(seller_id: int) -> float:
    row = await fetchone('SELECT AVG(rating) as avg_rating FROM reviews WHERE seller_id = ?', (seller_id,))
    return round(row['avg_rating'], 1) if row and row['avg_rating'] else 0.0

async def get_best_review_month(seller_id: int) -> Optional[dict]:
    row = await fetchone(
        '''SELECT * FROM reviews WHERE seller_id = ? AND strftime('%Y-%m', created_at) = strftime('%Y-%m', 'now')
           ORDER BY rating DESC LIMIT 1''',
        (seller_id,)
    )
    return dict(row) if row else None

async def get_seller_review_message(seller_id: int) -> Optional[dict]:
    row = await fetchone('SELECT * FROM seller_review_messages WHERE seller_id = ?', (seller_id,))
    return dict(row) if row else None

async def set_seller_review_message(seller_id: int, message_id: int, channel_id: int):
    async with transaction():
        await _execute_no_lock(
            'INSERT INTO seller_review_messages (seller_id, message_id, channel_id) VALUES (?, ?, ?) ON CONFLICT(seller_id) DO UPDATE SET message_id = ?, channel_id = ?',
            (seller_id, message_id, channel_id, message_id, channel_id)
        )

# ================= SELLER STATS =================
async def get_seller_stats_top(limit: int = 10) -> List[dict]:
    rows = await fetchall('SELECT * FROM stats ORDER BY revenue DESC LIMIT ?', (limit,))
    return [dict(row) for row in rows] if rows else []

async def get_seller_top_sales(limit: int = 10) -> List[dict]:
    rows = await fetchall('SELECT * FROM stats ORDER BY sales DESC LIMIT ?', (limit,))
    return [dict(row) for row in rows] if rows else []

# ================= PROMOS =================
async def get_all_promos() -> Dict[str, Promo]:
    global promos_cache
    if promos_cache:
        return promos_cache
    rows = await fetchall('SELECT * FROM promos')
    result = {r['code']: Promo(code=r['code'], discount=r['discount'], expires=r['expires'], uses=r['uses'], max_uses=r['max_uses']) for r in rows}
    promos_cache = result
    return result

async def get_promo(code: str) -> Optional[Promo]:
    global promos_cache
    if code in promos_cache:
        return promos_cache[code]
    row = await fetchone('SELECT * FROM promos WHERE code = ?', (code,))
    if not row:
        return None
    return Promo(code=row['code'], discount=row['discount'], expires=row['expires'], uses=row['uses'], max_uses=row['max_uses'])

async def add_promo(code: str, discount: int, expires: str, max_uses: int = 100):
    global promos_cache
    async with transaction():
        await _execute_no_lock('INSERT INTO promos (code, discount, expires, max_uses) VALUES (?, ?, ?, ?)', (code, discount, expires, max_uses))
        promos_cache = {}

async def delete_promo(code: str):
    global promos_cache
    async with transaction():
        await _execute_no_lock('DELETE FROM promos WHERE code = ?', (code,))
        promos_cache = {}

async def increment_promo_uses(code: str):
    global promos_cache
    async with transaction():
        await _execute_no_lock('UPDATE promos SET uses = uses + 1 WHERE code = ?', (code,))
        promos_cache = {}

async def get_expired_promos(current_date: str) -> List[str]:
    rows = await fetchall('SELECT code FROM promos WHERE expires <= ?', (current_date,))
    return [r['code'] for r in rows]

# ================= BLACKLIST =================
async def get_blacklist() -> List[int]:
    global blacklist_cache
    if blacklist_cache:
        return blacklist_cache
    rows = await fetchall('SELECT user_id FROM blacklist')
    result = [r['user_id'] for r in rows]
    blacklist_cache = result
    return result

async def is_blacklisted(user_id: int) -> bool:
    blacklist = await get_blacklist()
    return user_id in blacklist

async def add_to_blacklist(user_id: int, moderator_id: int = 0, reason: str = ""):
    global blacklist_cache
    async with transaction():
        await _execute_no_lock('INSERT OR IGNORE INTO blacklist (user_id) VALUES (?)', (user_id,))
        created_at = datetime.now(timezone.utc).isoformat()
        await _execute_no_lock(
            'INSERT INTO blacklist_extended (user_id, moderator_id, reason, created_at) VALUES (?, ?, ?, ?) ON CONFLICT(user_id) DO UPDATE SET moderator_id=?, reason=?, created_at=?',
            (user_id, moderator_id, reason, created_at, moderator_id, reason, created_at)
        )
        await _execute_no_lock(
            'INSERT INTO ban_history (user_id, moderator_id, action, reason, created_at) VALUES (?, ?, ?, ?, ?)',
            (user_id, moderator_id, 'ban', reason, created_at)
        )
        blacklist_cache = []

async def remove_from_blacklist(user_id: int, moderator_id: int = 0, reason: str = ""):
    global blacklist_cache
    async with transaction():
        await _execute_no_lock('DELETE FROM blacklist WHERE user_id = ?', (user_id,))
        await _execute_no_lock('DELETE FROM blacklist_extended WHERE user_id = ?', (user_id,))
        created_at = datetime.now(timezone.utc).isoformat()
        await _execute_no_lock(
            'INSERT INTO ban_history (user_id, moderator_id, action, reason, created_at) VALUES (?, ?, ?, ?, ?)',
            (user_id, moderator_id, 'unban', reason, created_at)
        )
        blacklist_cache = []

async def get_blacklist_info(user_id: int) -> Optional[dict]:
    row = await fetchone('SELECT * FROM blacklist_extended WHERE user_id = ?', (user_id,))
    return dict(row) if row else None

async def get_ban_history(user_id: int) -> List[dict]:
    rows = await fetchall('SELECT * FROM ban_history WHERE user_id = ? ORDER BY created_at DESC', (user_id,))
    return [dict(row) for row in rows] if rows else []

# ================= STATS =================
async def get_all_stats() -> Dict[int, dict]:
    global stats_cache
    if stats_cache:
        return stats_cache
    rows = await fetchall('SELECT * FROM stats')
    result = {r['user_id']: {"sales": r['sales'], "revenue": r['revenue']} for r in rows}
    stats_cache = result
    return result

async def get_stats(user_id: int) -> Optional[dict]:
    global stats_cache
    if user_id in stats_cache:
        return stats_cache[user_id]
    row = await fetchone('SELECT sales, revenue FROM stats WHERE user_id = ?', (user_id,))
    if not row:
        return None
    return {"sales": row['sales'], "revenue": row['revenue']}

async def update_stats(user_id: int, sales_inc: int = 1, revenue_inc: int = 0):
    global stats_cache
    async with transaction():
        await _execute_no_lock(
            'INSERT INTO stats (user_id, sales, revenue) VALUES (?, ?, ?) ON CONFLICT(user_id) DO UPDATE SET sales = sales + ?, revenue = revenue + ?',
            (user_id, sales_inc, revenue_inc, sales_inc, revenue_inc)
        )
        stats_cache = {}

# ================= WARNINGS =================
async def get_all_warnings() -> Dict[int, dict]:
    global warnings_cache
    if warnings_cache:
        return warnings_cache
    rows = await fetchall('SELECT user_id, id, moderator_id, reason, created_at FROM warnings ORDER BY created_at DESC')
    warnings_by_user = {}
    for row in rows:
        uid = row['user_id']
        if uid not in warnings_by_user:
            warnings_by_user[uid] = {"count": 0, "reasons": []}
        warnings_by_user[uid]["count"] += 1
        warnings_by_user[uid]["reasons"].append(Warning(
            id=row['id'], user_id=row['user_id'], moderator_id=row['moderator_id'],
            reason=row['reason'], created_at=row['created_at']
        ))
    warnings_cache = warnings_by_user
    return warnings_by_user

async def get_user_warnings(user_id: int) -> List[Warning]:
    all_warnings = await get_all_warnings()
    if user_id in all_warnings:
        return all_warnings[user_id]["reasons"]
    return []

async def get_user_warning_count(user_id: int) -> int:
    all_warnings = await get_all_warnings()
    if user_id in all_warnings:
        return all_warnings[user_id]["count"]
    return 0

async def add_warning(user_id: int, moderator_id: int, reason: str) -> int:
    global warnings_cache
    created_at = datetime.now(timezone.utc).isoformat()
    async with transaction():
        await _execute_no_lock(
            'INSERT INTO warnings (user_id, moderator_id, reason, created_at) VALUES (?, ?, ?, ?)',
            (user_id, moderator_id, reason, created_at)
        )
        row = await _execute_no_lock('SELECT COUNT(*) as cnt FROM warnings WHERE user_id = ?', (user_id,))
        result = await row.fetchone()
        warnings_cache = {}
        return result['cnt'] if result else 0

async def clear_warnings(user_id: int):
    global warnings_cache
    async with transaction():
        await _execute_no_lock('DELETE FROM warnings WHERE user_id = ?', (user_id,))
        warnings_cache = {}

async def remove_warning(warning_id: int):
    global warnings_cache
    async with transaction():
        await _execute_no_lock('DELETE FROM warnings WHERE id = ?', (warning_id,))
        warnings_cache = {}

# ================= SHOP MESSAGES =================
async def get_shop_messages(guild_id: int) -> dict:
    row = await fetchone('SELECT img_id, stat_id FROM shop_messages WHERE guild_id = ?', (guild_id,))
    if row:
        return {"img": row['img_id'], "stat": row['stat_id']}
    return {}

async def set_shop_messages(guild_id: int, img_id: int = None, stat_id: int = None):
    async with transaction():
        await _execute_no_lock(
            """INSERT INTO shop_messages (guild_id, img_id, stat_id) VALUES (?, ?, ?)
               ON CONFLICT(guild_id) DO UPDATE SET
               img_id = COALESCE(excluded.img_id, img_id),
               stat_id = COALESCE(excluded.stat_id, stat_id)""",
            (guild_id, img_id, stat_id)
        )

async def delete_shop_messages(guild_id: int):
    async with transaction():
        await _execute_no_lock('DELETE FROM shop_messages WHERE guild_id = ?', (guild_id,))

# ================= LOT TEMPLATES =================
async def add_lot_template(name: str, price: str, short_description: str, full_description: str,
                            category_id: int, seller_id: int, created_by: int) -> int:
    created_at = datetime.now(timezone.utc).isoformat()
    async with transaction():
        cursor = await _execute_no_lock(
            'INSERT INTO lot_templates (name, price, short_description, full_description, category_id, seller_id, created_by, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
            (name, price, short_description, full_description, category_id, seller_id, created_by, created_at)
        )
        return cursor.lastrowid

async def get_lot_templates(created_by: int = None) -> List[dict]:
    if created_by:
        rows = await fetchall('SELECT * FROM lot_templates WHERE created_by = ? ORDER BY id DESC', (created_by,))
    else:
        rows = await fetchall('SELECT * FROM lot_templates ORDER BY id DESC')
    return [dict(row) for row in rows] if rows else []

async def get_lot_template(template_id: int) -> Optional[dict]:
    row = await fetchone('SELECT * FROM lot_templates WHERE id = ?', (template_id,))
    return dict(row) if row else None

async def delete_lot_template(template_id: int):
    async with transaction():
        await _execute_no_lock('DELETE FROM lot_templates WHERE id = ?', (template_id,))

# ================= REFERRALS =================
async def add_referral(referrer_id: int, referred_id: int):
    created_at = datetime.now(timezone.utc).isoformat()
    async with transaction():
        await _execute_no_lock(
            'INSERT OR IGNORE INTO referrals (referrer_id, referred_id, created_at) VALUES (?, ?, ?)',
            (referrer_id, referred_id, created_at)
        )

async def get_referral_count(referrer_id: int) -> int:
    row = await fetchone('SELECT COUNT(*) as cnt FROM referrals WHERE referrer_id = ?', (referrer_id,))
    return row['cnt'] if row else 0

async def get_referrer(referred_id: int) -> Optional[int]:
    row = await fetchone('SELECT referrer_id FROM referrals WHERE referred_id = ?', (referred_id,))
    return row['referrer_id'] if row else None

# ================= CURRENCY RATES =================
async def update_currency_rates(rates: Dict[str, float]):
    global currency_rates_cache
    updated_at = datetime.now(timezone.utc).isoformat()
    async with transaction():
        for currency, rate in rates.items():
            await _execute_no_lock(
                'INSERT INTO currency_rates (currency, rate, updated_at) VALUES (?, ?, ?) ON CONFLICT(currency) DO UPDATE SET rate=?, updated_at=?',
                (currency, rate, updated_at, rate, updated_at)
            )
    currency_rates_cache = rates

async def get_currency_rates() -> Dict[str, float]:
    global currency_rates_cache
    if currency_rates_cache:
        return currency_rates_cache
    rows = await fetchall('SELECT currency, rate FROM currency_rates')
    result = {row['currency']: row['rate'] for row in rows}
    currency_rates_cache = result
    return result

async def convert_price_rub(price_rub: float) -> Dict[str, str]:
    rates = await get_currency_rates()
    result = {"RUB": f"{price_rub:.0f} ₽"}
    if "UAH" in rates and rates["UAH"]:
        result["UAH"] = f"{price_rub * rates['UAH']:.0f} ₴"
    if "USD" in rates and rates["USD"]:
        result["USD"] = f"${price_rub * rates['USD']:.2f}"
    if "EUR" in rates and rates["EUR"]:
        result["EUR"] = f"€{price_rub * rates['EUR']:.2f}"
    return result

# ================= BACKUP & RESTORE =================
async def restore_from_backup_channel(channel_id: int, bot):
    """Восстанавливает категории и товары из последнего бэкапа в канале"""
    channel = bot.get_channel(channel_id)
    if not channel:
        try:
            channel = await bot.fetch_channel(channel_id)
        except Exception as e:
            logger.error(f"Backup channel {channel_id} not found: {e}")
            return False
    
    try:
        async for msg in channel.history(limit=50):
            if not msg.attachments:
                continue
            
            for attachment in msg.attachments:
                if attachment.filename.endswith('.json') and ('shop_backup_' in attachment.filename or 'scheduled_backup_' in attachment.filename):
                    content = await attachment.read()
                    data = json.loads(content.decode('utf-8'))
                    
                    categories_data = data.get('categories', {})
                    lots_data = data.get('lots', {})
                    
                    if not categories_data:
                        continue
                    
                    async with transaction():
                        await _execute_no_lock('DELETE FROM categories')
                        await _execute_no_lock('DELETE FROM lots')
                        await _execute_no_lock('DELETE FROM lot_prices')
                        
                        for cat_id_str, cat_data in categories_data.items():
                            await _execute_no_lock(
                                'INSERT INTO categories (name, emoji, description, image_url) VALUES (?, ?, ?, ?)',
                                (cat_data['name'], cat_data.get('emoji', '📁'),
                                 cat_data.get('description'), cat_data.get('image_url'))
                            )
                        
                        for lot_id_str, lot_data in lots_data.items():
                            await _execute_no_lock(
                                '''INSERT INTO lots (name, price, short_description, full_description,
                                   seller_id, category_id, stock, image_url, role_id)
                                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                                (lot_data['name'], lot_data.get('price', ''),
                                 lot_data.get('short_description', ''), lot_data.get('full_description', ''),
                                 lot_data.get('seller_id', 0), lot_data.get('category_id', 1),
                                 lot_data.get('stock', 0), lot_data.get('image_url'),
                                 lot_data.get('role_id'))
                            )
                    
                    await refresh_cache()
                    logger.info(f"✅ Database restored from {attachment.filename}")
                    return True
    except Exception as e:
        logger.exception(f"Restore failed: {e}")
    
    return False

# ================= CACHE =================
async def refresh_cache():
    global categories_cache, lots_cache, promos_cache, blacklist_cache, stats_cache, warnings_cache
    print("⏳ refresh_cache(): начало загрузки кэша...")
    try:
        print("  ⏳ get_all_categories()...")
        categories_cache = await get_all_categories()
        print(f"    ✅ Категории загружены: {len(categories_cache)}")
        
        print("  ⏳ get_all_lots()...")
        lots_cache = await get_all_lots()
        print(f"    ✅ Товары загружены: {len(lots_cache)}")
        
        print("  ⏳ get_all_promos()...")
        promos_cache = await get_all_promos()
        print(f"    ✅ Промо загружены: {len(promos_cache)}")
        
        print("  ⏳ get_blacklist()...")
        blacklist_cache = await get_blacklist()
        print(f"    ✅ Чёрный список загружен: {len(blacklist_cache)}")
        
        print("  ⏳ get_all_stats()...")
        stats_cache = await get_all_stats()
        print(f"    ✅ Статистика загружена: {len(stats_cache)}")
        
        print("  ⏳ get_all_warnings()...")
        warnings_cache = await get_all_warnings()
        print(f"    ✅ Предупреждения загружены: {len(warnings_cache)}")
        
        print("✅ Кэш полностью загружен!")
        logger.info("✅ Cache refreshed successfully")
    except Exception as e:
        print(f"❌ Ошибка при загрузке кэша: {e}")
        logger.exception("❌ Ошибка при загрузке кэша")