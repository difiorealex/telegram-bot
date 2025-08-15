import logging
import os
import re
import asyncio
import aiohttp
from bs4 import BeautifulSoup
from datetime import datetime, timedelta
import random
import json
from fake_useragent import UserAgent
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes, CallbackQueryHandler
from apscheduler.schedulers.asyncio import AsyncIOScheduler
import asyncpg
from typing import List, Dict, Optional

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Configurazione
BOT_TOKEN = os.getenv('BOT_TOKEN')
AMAZON_TAG = os.getenv('AMAZON_TAG', 'botaffari-21')
DATABASE_URL = os.getenv('DATABASE_URL')  # PostgreSQL URL da Render
CHANNEL_ID = os.getenv('CHANNEL_ID', '121413748')  # ID del canale Telegram

if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN non trovato!")

class Database:
    def __init__(self, database_url):
        self.database_url = database_url
        self.pool = None
    
    async def connect(self):
        """Connessione al database PostgreSQL"""
        try:
            self.pool = await asyncpg.create_pool(self.database_url, min_size=1, max_size=5)
            await self.create_tables()
            logger.info("✅ Database connesso e tabelle create")
        except Exception as e:
            logger.error(f"❌ Errore connessione database: {e}")
    
    async def create_tables(self):
        """Crea automaticamente tutte le tabelle necessarie"""
        async with self.pool.acquire() as conn:
            # 1. Tabella utenti
            await conn.execute('''
                CREATE TABLE IF NOT EXISTS users (
                    user_id BIGINT PRIMARY KEY,
                    username VARCHAR(100),
                    first_name VARCHAR(100),
                    notifications_enabled BOOLEAN DEFAULT true,
                    categories TEXT[] DEFAULT '{}',
                    max_price INTEGER DEFAULT 500,
                    min_discount INTEGER DEFAULT 10,
                    preferred_brands TEXT[] DEFAULT '{}',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    last_activity TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    total_clicks INTEGER DEFAULT 0
                )
            ''')
            
            # 2. Tabella prodotti monitorati
            await conn.execute('''
                CREATE TABLE IF NOT EXISTS watched_products (
                    id SERIAL PRIMARY KEY,
                    user_id BIGINT REFERENCES users(user_id) ON DELETE CASCADE,
                    product_name VARCHAR(300),
                    amazon_asin VARCHAR(20),
                    target_price INTEGER,
                    current_price INTEGER,
                    original_price INTEGER,
                    url TEXT,
                    image_url TEXT,
                    last_check TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    price_dropped BOOLEAN DEFAULT false,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            
            # 3. Tabella offerte inviate (anti-duplicati)
            await conn.execute('''
                CREATE TABLE IF NOT EXISTS sent_deals (
                    id SERIAL PRIMARY KEY,
                    deal_hash VARCHAR(100) UNIQUE,
                    title VARCHAR(300),
                    price VARCHAR(50),
                    original_price VARCHAR(50),
                    discount_percent INTEGER,
                    url TEXT,
                    image_url TEXT,
                    sent_to_channel BOOLEAN DEFAULT false,
                    sent_to_users INTEGER DEFAULT 0,
                    clicks INTEGER DEFAULT 0,
                    sent_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            
            # 4. Tabella statistiche
            await conn.execute('''
                CREATE TABLE IF NOT EXISTS bot_stats (
                    id SERIAL PRIMARY KEY,
                    date DATE DEFAULT CURRENT_DATE,
                    total_users INTEGER DEFAULT 0,
                    active_users INTEGER DEFAULT 0,
                    deals_sent INTEGER DEFAULT 0,
                    channel_messages INTEGER DEFAULT 0,
                    total_clicks INTEGER DEFAULT 0,
                    revenue_estimate DECIMAL(10,2) DEFAULT 0.00
                )
            ''')
            
            # 5. Crea indici per performance
            await conn.execute('CREATE INDEX IF NOT EXISTS idx_users_notifications ON users(notifications_enabled)')
            await conn.execute('CREATE INDEX IF NOT EXISTS idx_users_activity ON users(last_activity)')
            await conn.execute('CREATE INDEX IF NOT EXISTS idx_deals_hash ON sent_deals(deal_hash)')
            await conn.execute('CREATE INDEX IF NOT EXISTS idx_deals_sent_at ON sent_deals(sent_at)')
            
            logger.info("✅ Tutte le tabelle create con successo")
    
    async def add_user(self, user_id: int, username: str, first_name: str):
        """Aggiunge o aggiorna un utente"""
        async with self.pool.acquire() as conn:
            await conn.execute('''
                INSERT INTO users (user_id, username, first_name, last_activity)
                VALUES ($1, $2, $3, CURRENT_TIMESTAMP)
                ON CONFLICT (user_id) 
                DO UPDATE SET last_activity = CURRENT_TIMESTAMP
            ''', user_id, username, first_name)
    
    async def get_all_users(self) -> List[Dict]:
        """Ottiene tutti gli utenti attivi"""
        async with self.pool.acquire() as conn:
            rows = await conn.fetch('''
                SELECT user_id, notifications_enabled, categories, max_price 
                FROM users 
                WHERE notifications_enabled = true
                AND last_activity > CURRENT_TIMESTAMP - INTERVAL '30 days'
            ''')
            return [dict(row) for row in rows]
    
    async def update_user_preferences(self, user_id: int, notifications: bool = None, 
                                    categories: List[str] = None, max_price: int = None):
        """Aggiorna preferenze utente"""
        async with self.pool.acquire() as conn:
            if notifications is not None:
                await conn.execute(
                    'UPDATE users SET notifications_enabled = $1 WHERE user_id = $2',
                    notifications, user_id
                )
            if categories is not None:
                await conn.execute(
                    'UPDATE users SET categories = $1 WHERE user_id = $2',
                    categories, user_id
                )
            if max_price is not None:
                await conn.execute(
                    'UPDATE users SET max_price = $1 WHERE user_id = $2',
                    max_price, user_id
                )
    
    async def is_deal_sent(self, deal_hash: str) -> bool:
        """Controlla se un'offerta è già stata inviata"""
        async with self.pool.acquire() as conn:
            result = await conn.fetchval(
                'SELECT COUNT(*) FROM sent_deals WHERE deal_hash = $1',
                deal_hash
            )
            return result > 0
    
    async def mark_deal_sent(self, deal_hash: str, title: str, price: str, url: str):
        """Segna un'offerta come inviata"""
        async with self.pool.acquire() as conn:
            await conn.execute('''
                INSERT INTO sent_deals (deal_hash, title, price, url)
                VALUES ($1, $2, $3, $4)
                ON CONFLICT (deal_hash) DO NOTHING
            ''', deal_hash, title, price, url)

# Funzione test database (POSIZIONATA CORRETTAMENTE)
async def test_database_connection():
    """Testa connessione e creazione tabelle"""
    try:
        if not DATABASE_URL:
            logger.error("❌ DATABASE_URL non configurato")
            return False
            
        # Test connessione
        pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=2)
        
        async with pool.acquire() as conn:
            # Test query semplice
            result = await conn.fetchval('SELECT version()')
            logger.info(f"✅ PostgreSQL connesso: {result[:50]}...")
            
            # Test che le tabelle esistano
            tables = await conn.fetch('''
                SELECT table_name FROM information_schema.tables 
                WHERE table_schema = 'public'
            ''')
            
            table_names = [table['table_name'] for table in tables]
            required_tables = ['users', 'watched_products', 'sent_deals', 'bot_stats']
            
            for table in required_tables:
                if table in table_names:
                    logger.info(f"✅ Tabella '{table}' esistente")
                else:
                    logger.warning(f"⚠️ Tabella '{table}' mancante")
            
            # Test inserimento utente
            await conn.execute('''
                INSERT INTO users (user_id, username, first_name) 
                VALUES ($1, $2, $3) 
                ON CONFLICT (user_id) DO NOTHING
            ''', 999999, 'test_user', 'Test User')
            
            logger.info("✅ Test inserimento utente riuscito")
            
        await pool.close()
        return True
        
    except Exception as e:
        logger.error(f"❌ Errore test database: {e}")
        return False

class AmazonScraperAdvanced:
    def __init__(self):
        self.session = None
        self.ua = UserAgent()
        
    async def create_session(self):
        if not self.session:
            connector = aiohttp.TCPConnector(limit=10)
            timeout = aiohttp.ClientTimeout(total=30)
            self.session = aiohttp.ClientSession(
                connector=connector,
                timeout=timeout,
                headers={'User-Agent': self.ua.random}
            )
    
    async def close_session(self):
        if self.session:
            await self.session.close()
    
    def create_affiliate_link(self, amazon_url):
        """Crea un link di affiliazione"""
        asin_match = re.search(r'/dp/([A-Z0-9]{10})', amazon_url)
        if not asin_match:
            asin_match = re.search(r'/gp/product/([A-Z0-9]{10})', amazon_url)
        
        if asin_match:
            asin = asin_match.group(1)
            return f"https://www.amazon.it/dp/{asin}/?tag={AMAZON_TAG}&linkCode=ogi&th=1&psc=1"
        
        if '?' in amazon_url:
            return f"{amazon_url}&tag={AMAZON_TAG}"
        else:
            return f"{amazon_url}?tag={AMAZON_TAG}"
    
    def generate_deal_hash(self, title: str, price: str) -> str:
        """Genera hash unico per l'offerta"""
        import hashlib
        content = f"{title}_{price}".lower()
        return hashlib.md5(content.encode()).hexdigest()[:16]
    
    async def get_trending_deals(self, max_deals=8) -> List[Dict]:
        """Ottiene offerte di tendenza per broadcast automatico"""
        await self.create_session()
        
        trending_searches = [
            "offerte lampo",
            "smartphone", 
            "cuffie wireless
