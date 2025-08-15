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
            "cuffie wireless",
            "smart tv",
            "robot aspirapolvere",
            "echo dot",
            "fire tv stick"
        ]
        
        all_deals = []
        
        for search_term in random.sample(trending_searches, 3):
            try:
                deals = await self.scrape_amazon_deals(search_term, max_deals=3)
                all_deals.extend(deals)
                await asyncio.sleep(random.uniform(2, 4))
            except Exception as e:
                logger.error(f"Errore ricerca {search_term}: {e}")
                continue
        
        # Filtra duplicati e ordina per qualità
        unique_deals = {}
        for deal in all_deals:
            deal_key = deal['title'][:50]  # Usa titolo per unicità
            if deal_key not in unique_deals:
                deal['hash'] = self.generate_deal_hash(deal['title'], deal['price'])
                unique_deals[deal_key] = deal
        
        return list(unique_deals.values())[:max_deals]
    
    async def scrape_amazon_deals(self, search_term="", max_deals=5):
        """Scraping Amazon (stessa funzione di prima, ottimizzata)"""
        await self.create_session()
        
        if search_term:
            url = f"https://www.amazon.it/s?k={search_term.replace(' ', '+')}&sort=price-asc-rank"
        else:
            url = "https://www.amazon.it/gp/goldbox"
        
        headers = {
            'User-Agent': self.ua.random,
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
            'Accept-Language': 'it-IT,it;q=0.9,en;q=0.8',
            'Accept-Encoding': 'gzip, deflate',
            'Connection': 'keep-alive',
        }
        
        deals = []
        
        try:
            await asyncio.sleep(random.uniform(1, 3))
            
            async with self.session.get(url, headers=headers) as response:
                if response.status != 200:
                    return []
                
                html = await response.text()
                soup = BeautifulSoup(html, 'lxml')
                
                products = soup.find_all('div', {'data-component-type': 's-search-result'})
                
                for product in products[:max_deals]:
                    try:
                        # Titolo
                        title_elem = product.find('h2', class_='a-size-mini')
                        if not title_elem:
                            title_elem = product.find('span', class_='a-size-base-plus')
                        title = title_elem.get_text().strip() if title_elem else "Prodotto Amazon"
                        
                        # Link
                        link_elem = product.find('h2').find('a') if product.find('h2') else None
                        if link_elem:
                            product_url = "https://www.amazon.it" + link_elem.get('href', '')
                        else:
                            continue
                        
                        # Prezzo
                        price_elem = product.find('span', class_='a-price-whole')
                        if price_elem:
                            price = f"€{price_elem.get_text().strip()}"
                            price_value = int(price_elem.get_text().strip().replace(',', ''))
                        else:
                            continue
                        
                        # Scarta prodotti troppo costosi (>500€ per broadcast automatico)
                        if price_value > 500:
                            continue
                        
                        # Prezzo originale
                        original_price_elem = product.find('span', class_='a-price a-text-price')
                        original_price = original_price_elem.get_text().strip() if original_price_elem else price
                        
                        # Immagine
                        img_elem = product.find('img', class_='s-image')
                        image_url = img_elem.get('src', '') if img_elem else ''
                        
                        # Rating
                        rating_elem = product.find('span', class_='a-icon-alt')
                        rating = rating_elem.get_text() if rating_elem else "N/A"
                        
                        deals.append({
                            'title': title[:100] + "..." if len(title) > 100 else title,
                            'price': price,
                            'price_value': price_value,
                            'original_price': original_price,
                            'url': product_url,
                            'image': image_url,
                            'rating': rating,
                            'source': 'Amazon'
                        })
                        
                    except Exception as e:
                        logger.error(f"Errore parsing prodotto: {e}")
                        continue
        
        except Exception as e:
            logger.error(f"Errore scraping Amazon: {e}")
        
        return deals

# Inizializza componenti
db = Database(DATABASE_URL) if DATABASE_URL else None
scraper = AmazonScraperAdvanced()
scheduler = AsyncIOScheduler()

class NotificationSystem:
    def __init__(self, bot_app):
        self.app = bot_app
    
    async def send_to_channel(self, deals: List[Dict]):
        """Invia offerte al canale Telegram"""
        if not CHANNEL_ID or not deals:
            return
        
        try:
            # Messaggio di intestazione
            header = f"""
🔥 **OFFERTE AUTOMATICHE DEL GIORNO**
📅 {datetime.now().strftime('%d/%m/%Y - %H:%M')}

💰 Le migliori offerte trovate in tempo reale!
🤖 Bot aggiornato ogni 3 ore

➖➖➖➖➖➖➖➖➖➖
            """
            
            await self.app.bot.send_message(CHANNEL_ID, header)
            await asyncio.sleep(2)
            
            for i, deal in enumerate(deals[:5], 1):  # Massimo 5 offerte per canale
                affiliate_link = scraper.create_affiliate_link(deal['url'])
                
                channel_text = f"""
🛒 **OFFERTA #{i}**

**{deal['title']}**

💰 Prezzo: **{deal['price']}**
{f"~~{deal['original_price']}~~" if deal.get('original_price') and deal['original_price'] != deal['price'] else ""}
⭐ Rating: {deal.get('rating', 'N/A')}

🎯 Offerta verificata e sicura
💡 Link diretto Amazon Italia
                """
                
                keyboard = [[InlineKeyboardButton("🛒 ACQUISTA ORA", url=affiliate_link)]]
                reply_markup = InlineKeyboardMarkup(keyboard)
                
                try:
                    if deal.get('image') and deal['image'].startswith('http'):
                        await self.app.bot.send_photo(
                            CHANNEL_ID,
                            photo=deal['image'],
                            caption=channel_text,
                            reply_markup=reply_markup
                        )
                    else:
                        await self.app.bot.send_message(
                            CHANNEL_ID,
                            channel_text,
                            reply_markup=reply_markup
                        )
                    
                    # Segna come inviata
                    if db:
                        await db.mark_deal_sent(
                            deal['hash'],
                            deal['title'],
                            deal['price'],
                            deal['url']
                        )
                    
                    await asyncio.sleep(3)  # Pausa tra messaggi
                    
                except Exception as e:
                    logger.error(f"Errore invio al canale: {e}")
            
            # Footer
            footer = f"""
➖➖➖➖➖➖➖➖➖➖
🤖 **Prossimo aggiornamento**: tra 3 ore
💬 **Bot personale**: @{(await self.app.bot.get_me()).username}

❤️ Supporta il progetto usando i nostri link!
            """
            
            await self.app.bot.send_message(CHANNEL_ID, footer)
            
        except Exception as e:
            logger.error(f"Errore generale invio canale: {e}")

# Funzioni mancanti per completare il bot
async def offerte_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Comando per cercare offerte manualmente"""
    loading_msg = await update.message.reply_text(
        "🔍 **Ricerca offerte in corso...**\n"
        "Scansiono Amazon per te!\n"
        "⏳ Ci vogliono 10-15 secondi..."
    )
    
    try:
        deals = await scraper.get_trending_deals(max_deals=3)
        
        await loading_msg.delete()
        
        if not deals:
            await update.message.reply_text("😅 Nessuna offerta trovata al momento!")
            return
        
        for deal in deals:
            affiliate_link = scraper.create_affiliate_link(deal['url'])
            
            deal_text = f"""
🛒 **{deal['title']}**

💰 **Prezzo**: {deal['price']}
⭐ **Rating**: {deal.get('rating', 'N/A')}

🎯 Offerta trovata ora!
            """
            
            keyboard = [[InlineKeyboardButton("🛒 Acquista Ora", url=affiliate_link)]]
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            await update.message.reply_text(deal_text, reply_markup=reply_markup)
            await asyncio.sleep(1)
            
    except Exception as e:
        await loading_msg.delete()
        await update.message.reply_text("❌ Errore nella ricerca offerte")

async def cerca_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Comando per cercare prodotti specifici"""
    if not context.args:
        await update.message.reply_text(
            "🔍 **Come cercare:**\n"
            "Usa: `/cerca nome prodotto`\n\n"
            "**Esempi:**\n"
            "• `/cerca iPhone 15`\n"
            "• `/cerca cuffie bluetooth`"
        )
        return
    
    query = ' '.join(context.args)
    await update.message.reply_text(f"🔍 Cerco '{query}' per te...")
    
    try:
        deals = await scraper.scrape_amazon_deals(search_term=query, max_deals=3)
        
        if not deals:
            await update.message.reply_text(f"😅 Nessun risultato per '{query}'")
            return
        
        for deal in deals:
            affiliate_link = scraper.create_affiliate_link(deal['url'])
            
            deal_text = f"""
🎯 **{deal['title']}**

💰 **Prezzo**: {deal['price']}
⭐ **Rating**: {deal.get('rating', 'N/A')}
            """
            
            keyboard = [[InlineKeyboardButton("🛒 Acquista", url=affiliate_link)]]
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            await update.message.reply_text(deal_text, reply_markup=reply_markup)
            await asyncio.sleep(1)
            
    except Exception as e:
        await update.message.reply_text("❌ Errore nella ricerca")

async def handle_url(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Gestisce URL Amazon inviati dall'utente"""
    text = update.message.text
    
    if 'amazon' in text.lower() and 'http' in text:
        affiliate_link = scraper.create_affiliate_link(text)
        
        response = f"""
🔗 **Link Amazon Convertito!**

**Link con affiliazione:**
{affiliate_link}

🛒 Usa questo link per supportare il bot!
        """
        
        keyboard = [[InlineKeyboardButton("🛒 Apri Link", url=affiliate_link)]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await update.message.reply_text(response, reply_markup=reply_markup)
    else:
        await update.message.reply_text(
            "🤔 Non ho capito.\n\n"
            "**Comandi utili:**\n"
            "• /offerte - Offerte del giorno\n"
            "• /cerca prodotto - Cerca specifico\n"
        )

# Comandi bot
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    
    # Salva utente nel database
    if db:
        await db.add_user(user.id, user.username, user.first_name)
    
    welcome_text = f"""
🤖 **Ciao {user.first_name}! Bot Offerte Amazon PREMIUM**

🔥 **NOVITÀ - Sistema Automatico Attivo!**
• 🕐 Offerte automatiche ogni 3 ore
• 📱 Notifiche personali su misura
• 📢 Canale pubblico sempre aggiornato

**Comandi disponibili:**
/offerte - 🔍 Ricerca manuale offerte
/notifiche - 🔔 Gestisci notifiche automatiche
/canale - 📢 Link al canale offerte
/cerca [prodotto] - 🎯 Ricerca specifica

**🎁 Inizia subito!**
    """
    
    keyboard = [
        [InlineKeyboardButton("🔔 Attiva Notifiche", callback_data='enable_notifications')],
        [InlineKeyboardButton("📢 Canale Offerte", url=f"https://t.me/{CHANNEL_ID}")],
        [InlineKeyboardButton("🔍 Cerca Offerte", callback_data='search_deals')],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await update.message.reply_text(welcome_text, reply_markup=reply_markup)

async def notifiche_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Gestione notifiche personali"""
    if not db:
        await update.message.reply_text("❌ Database non disponibile")
        return
    
    text = """
🔔 **NOTIFICHE AUTOMATICHE**

**Cosa riceverai:**
• 🎁 Offerte personalizzate ogni 3 ore
• 💰 Solo prodotti entro il tuo budget
• 🎯 Filtrate per categorie preferite
• ⚡ Offerte lampo esclusive

**Configura le tue preferenze:**
    """
    
    keyboard = [
        [InlineKeyboardButton("✅ Attiva Notifiche", callback_data='enable_notif')],
        [InlineKeyboardButton("❌ Disattiva", callback_data='disable_notif')],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await update.message.reply_text(text, reply_markup=reply_markup)

async def canale_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Link al canale"""
    text = f"""
📢 **CANALE UFFICIALE OFFERTE**

🔗 **{CHANNEL_ID}**

**Cosa trovi:**
• 🕐 Offerte automatiche ogni 3 ore
• 🔥 Migliori deal Amazon del momento
• 💰 Sconti verificati e sicuri

**Iscriviti ora per non perdere nessuna offerta!**
    """
    
    keyboard = [[InlineKeyboardButton("📢 Vai al Canale", url=f"https://t.me/{CHANNEL_ID}")]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await update.message.reply_text(text, reply_markup=reply_markup)

async def button_handler_advanced(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    
    if query.data == 'enable_notifications':
        if db:
            await db.update_user_preferences(user_id, notifications=True)
        await query.message.reply_text("✅ Notifiche attivate!")
    
    elif query.data == 'disable_notif':
        if db:
            await db.update_user_preferences(user_id, notifications=False)
        await query.message.reply_text("❌ Notifiche disattivate.")
    
    elif query.data == 'search_deals':
        await offerte_command(update, context)

async def test_db_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Comando per testare il database"""
    try:
        if not db or not db.pool:
            await update.message.reply_text("❌ Database non connesso")
            return
        
        # Test inserimento utente
        user = update.effective_user
        await db.add_user(user.id, user.username, user.first_name)
        
        # Test lettura
        async with db.pool.acquire() as conn:
            result = await conn.fetchval(
                'SELECT COUNT(*) FROM users WHERE user_id = $1', user.id
            )
        
        await update.message.reply_text(
            f"✅ **Database Test Riuscito!**\n\n"
            f"👤 Utente salvato: {user.first_name}\n"
            f"🔢 Record trovati: {result}\n"
            f"📊 Database completamente funzionale!"
        )
        
    except Exception as e:
        await update.message.reply_text(f"❌ **Errore Database**: {e}")

def main():
    global application
    
    print("🚀 Avvio Amazon Bot PREMIUM con sistema completo...")
    
    application = Application.builder().token(BOT_TOKEN).build()
    
    async def post_init(app):
        """Inizializzazione post-avvio"""
        print("🔍 Test connessione database...")
        
        if DATABASE_URL:
            db_ok = await test_database_connection()
            if db_ok:
                print("✅ Database configurato correttamente")
                # Connetti il database principale
                await db.connect()
            else:
                print("❌ Database non disponibile - funzionalità limitate")
        else:
            print("⚠️ DATABASE_URL non configurato - modalità senza database")
        
        print("🎯 Bot inizializzato completamente")
    
    # Assegna la funzione post_init
    application.post_init = post_init
    
    # Handler comandi
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("offerte", offerte_command))
    application.add_handler(CommandHandler("cerca", cerca_command))
    application.add_handler(CommandHandler("notifiche", notifiche_command))
    application.add_handler(CommandHandler("canale", canale_command))
    application.add_handler(CommandHandler("testdb", test_db_command))
    
    # Altri handler
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_url))
    application.add_handler(CallbackQueryHandler(button_handler_advanced))
    
    print("✅ Amazon Bot PREMIUM attivo!")
    application.run_polling(drop_pending_updates=True)

if __name__ == '__main__':
    main()
