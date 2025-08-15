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
CHANNEL_ID = os.getenv('121413748')  # ID del canale Telegram

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

            ''', deal_hash, title, price, url)

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
    
    async def send_personal_notifications(self, deals: List[Dict]):
        """Invia notifiche personali agli utenti iscritti"""
        if not db or not deals:
            return
        
        users = await db.get_all_users()
        
        for user in users:
            try:
                user_id = user['user_id']
                max_price = user.get('max_price', 1000)
                categories = user.get('categories', [])
                
                # Filtra offerte per preferenze utente
                filtered_deals = []
                for deal in deals:
                    if deal.get('price_value', 0) <= max_price:
                        filtered_deals.append(deal)
                
                if not filtered_deals:
                    continue
                
                # Invia massimo 2 offerte personali
                for deal in filtered_deals[:2]:
                    # Controlla se già inviata a questo utente (opzionale)
                    affiliate_link = scraper.create_affiliate_link(deal['url'])
                    
                    personal_text = f"""
🎁 **OFFERTA PERSONALE PER TE!**

**{deal['title']}**

💰 **{deal['price']}** (sotto il tuo limite di €{max_price})
⭐ {deal.get('rating', 'N/A')}

🔥 Offerta trovata automaticamente dal bot!
                    """
                    
                    keyboard = [
                        [InlineKeyboardButton("🛒 Acquista", url=affiliate_link)],
                        [InlineKeyboardButton("⚙️ Modifica Preferenze", callback_data='settings')]
                    ]
                    reply_markup = InlineKeyboardMarkup(keyboard)
                    
                    await self.app.bot.send_message(
                        user_id,
                        personal_text,
                        reply_markup=reply_markup
                    )
                    
                    await asyncio.sleep(1)  # Rate limiting
                    
            except Exception as e:
                logger.error(f"Errore notifica personale utente {user_id}: {e}")

# Funzioni scheduling
async def automatic_deal_broadcast():
    """Funzione chiamata ogni 3 ore per broadcast automatico"""
    logger.info("🔄 Avvio broadcast automatico offerte...")
    
    try:
        # Ottieni offerte fresche
        deals = await scraper.get_trending_deals(max_deals=8)
        
        if not deals:
            logger.warning("Nessuna offerta trovata per broadcast")
            return
        
        # Filtra offerte già inviate
        if db:
            new_deals = []
            for deal in deals:
                if not await db.is_deal_sent(deal['hash']):
                    new_deals.append(deal)
            deals = new_deals
        
        if not deals:
            logger.info("Tutte le offerte sono già state inviate")
            return
        
        logger.info(f"🎯 Invio {len(deals)} nuove offerte")
        
        # Invia al canale e agli utenti
        notification_system = NotificationSystem(application)
        
        # Prima al canale pubblico
        await notification_system.send_to_channel(deals)
        await asyncio.sleep(5)
        
        # Poi notifiche personali
        await notification_system.send_personal_notifications(deals)
        
        logger.info("✅ Broadcast completato con successo")
        
    except Exception as e:
        logger.error(f"❌ Errore broadcast automatico: {e}")

# Comandi bot aggiornati
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
/preferenze - ⚙️ Imposta filtri personali
/canale - 📢 Link al canale offerte
/cerca [prodotto] - 🎯 Ricerca specifica

**🎁 Inizia subito!**
    """
    
    keyboard = [
        [InlineKeyboardButton("🔔 Attiva Notifiche", callback_data='enable_notifications')],
        [InlineKeyboardButton("📢 Canale Offerte", url=f"https://t.me/{CHANNEL_ID}")],
        [InlineKeyboardButton("🔍 Cerca Offerte", callback_data='search_deals')],
        [InlineKeyboardButton("⚙️ Preferenze", callback_data='settings')]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await update.message.reply_text(welcome_text, reply_markup=reply_markup)

async def notifiche_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Gestione notifiche personali"""
    if not db:
        await update.message.reply_text("❌ Database non disponibile")
        return
    
    user_id = update.effective_user.id
    
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
        [InlineKeyboardButton("💰 Budget Max", callback_data='set_budget')],
        [InlineKeyboardButton("📱 Categorie", callback_data='set_categories')]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await update.message.reply_text(text, reply_markup=reply_markup)

async def canale_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Link al canale"""
    text = f"""
📢 **CANALE UFFICIALE OFFERTE**

🔗 **@{CHANNEL_ID}**

**Cosa trovi:**
• 🕐 Offerte automatiche ogni 3 ore
• 🔥 Migliori deal Amazon del momento  
• 💰 Sconti verificati e sicuri
• 🚀 Offerte lampo esclusive

**Iscriviti ora per non perdere nessuna offerta!**
    """
    
    keyboard = [[InlineKeyboardButton("📢 Vai al Canale", url=f"https://t.me/{CHANNEL_ID}")]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await update.message.reply_text(text, reply_markup=reply_markup)

# Mantieni le funzioni offerte_command e cerca_command come prima...
# (Stesso codice di prima)

async def button_handler_advanced(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    
    if query.data == 'enable_notifications':
        if db:
            await db.update_user_preferences(user_id, notifications=True)
        await query.message.reply_text(
            "✅ **Notifiche attivate!**\n\n"
            "Riceverai offerte personalizzate ogni 3 ore.\n"
            "Usa /preferenze per configurare filtri."
        )
    
    elif query.data == 'disable_notif':
        if db:
            await db.update_user_preferences(user_id, notifications=False)
        await query.message.reply_text("❌ Notifiche disattivate.")
    
    elif query.data == 'set_budget':
        await query.message.reply_text(
            "💰 **Imposta Budget Massimo**\n\n"
            "Invia un messaggio con il budget massimo in euro.\n"
            "Esempio: `50` per ricevere solo offerte sotto i 50€"
        )
        context.user_data['awaiting'] = 'budget'
    
    elif query.data == 'settings':
        await notifiche_command(update, context)

def main():
    global application
    
    print("🚀 Avvio Amazon Bot PREMIUM con notifiche automatiche...")
    
    application = Application.builder().token(BOT_TOKEN).build()
    
    # Inizializza database
    if DATABASE_URL:
        asyncio.create_task(db.connect())
    
    # Comandi
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("notifiche", notifiche_command))
    application.add_handler(CommandHandler("canale", canale_command))
    # Aggiungi altri handler...
    
    application.add_handler(CallbackQueryHandler(button_handler_advanced))
    
    # Avvia scheduler per broadcast automatico
    scheduler.add_job(
        automatic_deal_broadcast,
        'interval',
        hours=3,  # Ogni 3 ore
        start_date=datetime.now() + timedelta(minutes=5)  # Inizia dopo 5 minuti
    )
    
    scheduler.start()
    print("⏰ Scheduler avviato - broadcast ogni 3 ore")
    
    print("✅ Amazon Bot PREMIUM attivo!")
    application.run_polling(drop_pending_updates=True)

if __name__ == '__main__':
    main()

async def test_database():
    """Testa la connessione al database"""
    try:
        if not DATABASE_URL:
            logger.error("❌ DATABASE_URL non configurato")
            return False
            
        pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=2)
        
        async with pool.acquire() as conn:
            result = await conn.fetchval('SELECT version()')
            logger.info(f"✅ Database connesso: {result[:50]}...")
            
        await pool.close()
        return True
        
    except Exception as e:
        logger.error(f"❌ Errore connessione database: {e}")
        return False

# Chiamala in main() prima di avviare il bot
async def main():
    # Test database
    db_ok = await test_database()
    if not db_ok:
        logger.error("Database non disponibile - alcune funzioni saranno limitate")
    
    # Continua con l'avvio normale...

