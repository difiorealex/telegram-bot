import logging
import os
import re
import asyncio
import aiohttp
from bs4 import BeautifulSoup
from datetime import datetime
import random
from fake_useragent import UserAgent
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes, CallbackQueryHandler

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.getenv('BOT_TOKEN')
AMAZON_TAG = os.getenv('AMAZON_TAG', 'botaffari-21')

if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN non trovato!")

class AmazonScraper:
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
        """Crea un link di affiliazione da un URL Amazon"""
        # Estrae l'ASIN dall'URL
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

    async def scrape_amazon_deals(self, search_term="", max_deals=5):
        """Scrapa offerte direttamente da Amazon"""
        await self.create_session()
        
        if search_term:
            url = f"https://www.amazon.it/s?k={search_term.replace(' ', '+')}&sort=price-asc-rank"
        else:
            # Pagina delle offerte lampo
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
            await asyncio.sleep(random.uniform(1, 3))  # Rate limiting
            
            async with self.session.get(url, headers=headers) as response:
                if response.status != 200:
                    logger.warning(f"Amazon response status: {response.status}")
                    return []
                
                html = await response.text()
                soup = BeautifulSoup(html, 'lxml')
                
                # Cerca prodotti nelle offerte
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
                        else:
                            continue
                        
                        # Prezzo originale (se in offerta)
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

    async def scrape_pepper_deals(self, max_deals=3):
        """Scrapa offerte da Pepperdeals (Pepper.it)"""
        await self.create_session()
        
        url = "https://www.pepper.it/offerte"
        headers = {
            'User-Agent': self.ua.random,
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
        }
        
        deals = []
        
        try:
            await asyncio.sleep(random.uniform(2, 4))
            
            async with self.session.get(url, headers=headers) as response:
                if response.status != 200:
                    return []
                
                html = await response.text()
                soup = BeautifulSoup(html, 'lxml')
                
                # Cerca deal containers
                deal_elements = soup.find_all('article', class_='thread')
                
                for deal in deal_elements[:max_deals]:
                    try:
                        # Titolo
                        title_elem = deal.find('strong', class_='thread-title')
                        title = title_elem.get_text().strip() if title_elem else "Offerta"
                        
                        # Prezzo
                        price_elem = deal.find('span', class_='thread-price')
                        price = price_elem.get_text().strip() if price_elem else "Prezzo non disponibile"
                        
                        # Link
                        link_elem = deal.find('a', class_='thread-link')
                        deal_url = "https://www.pepper.it" + link_elem.get('href', '') if link_elem else ""
                        
                        # Temperatura (popolarità)
                        temp_elem = deal.find('span', class_='vote-box-temp')
                        temperature = temp_elem.get_text().strip() if temp_elem else "0°"
                        
                        if 'amazon' in deal_url.lower() or 'amazon' in title.lower():
                            deals.append({
                                'title': title[:100] + "..." if len(title) > 100 else title,
                                'price': price,
                                'original_price': '',
                                'url': deal_url,
                                'image': '',
                                'rating': f"🌡️ {temperature}",
                                'source': 'Pepper'
                            })
                    
                    except Exception as e:
                        logger.error(f"Errore parsing Pepper: {e}")
                        continue
        
        except Exception as e:
            logger.error(f"Errore scraping Pepper: {e}")
        
        return deals

    async def get_mixed_deals(self, search_term="", max_total=5):
        """Combina offerte da più fonti"""
        all_deals = []
        
        # Amazon deals
        amazon_deals = await self.scrape_amazon_deals(search_term, max_deals=3)
        all_deals.extend(amazon_deals)
        
        # Pepper deals (solo se non stiamo cercando qualcosa di specifico)
        if not search_term:
            pepper_deals = await self.scrape_pepper_deals(max_deals=2)
            all_deals.extend(pepper_deals)
        
        # Mescola e limita
        random.shuffle(all_deals)
        return all_deals[:max_total]

# Inizializza il scraper
scraper = AmazonScraper()

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_name = update.effective_user.first_name
    welcome_text = f"""
🛒 **Ciao {user_name}! Bot Offerte Amazon con AI**

🔥 **Cosa posso fare:**
• Trova offerte REALI aggiornate
• Scraping live da Amazon e siti deal
• Link affiliati per supportare il bot
• Ricerca prodotti specifica

**Comandi veloci:**
/offerte - 🔥 Migliori offerte ora
/cerca [prodotto] - 🔍 Ricerca specifica
/trending - 📈 Prodotti di tendenza
/help - 📚 Guida completa

**Inizia subito! ⬇️**
    """
    
    keyboard = [
        [InlineKeyboardButton("🔥 Offerte Live", callback_data='live_deals')],
        [InlineKeyboardButton("🔍 Cerca Prodotto", callback_data='search_mode')],
        [InlineKeyboardButton("📱 Elettronica", callback_data='search_elettronica')],
        [InlineKeyboardButton("🏠 Casa", callback_data='search_casa')]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await update.message.reply_text(welcome_text, reply_markup=reply_markup)

async def offerte_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    loading_msg = await update.message.reply_text(
        "🔍 **Ricerca offerte in corso...**\n"
        "Scansiono Amazon e siti deal per te!\n"
        "⏳ Ci vogliono 10-15 secondi..."
    )
    
    try:
        deals = await scraper.get_mixed_deals(max_total=5)
        
        await loading_msg.delete()
        
        if not deals:
            await update.message.reply_text(
                "😅 **Nessuna offerta trovata al momento**\n\n"
                "Prova:\n"
                "• /cerca [prodotto specifico]\n" 
                "• Riprova tra qualche minuto\n"
                "• /trending per prodotti popolari"
            )
            return
        
        await update.message.reply_text(
            f"🎉 **Ho trovato {len(deals)} offerte per te!**\n"
            f"🕐 Aggiornate: {datetime.now().strftime('%H:%M')}"
        )
        
        for i, deal in enumerate(deals, 1):
            # Crea link affiliato se è Amazon
            if 'amazon' in deal['url'].lower():
                final_url = scraper.create_affiliate_link(deal['url'])
                source_emoji = "🛒"
            else:
                final_url = deal['url']
                source_emoji = "🌶️"
            
            deal_text = f"""
{source_emoji} **{deal['title']}**

💰 **Prezzo**: {deal['price']}
{f"~~{deal['original_price']}~~" if deal['original_price'] and deal['original_price'] != deal['price'] else ""}
⭐ **Rating**: {deal['rating']}
📍 **Fonte**: {deal['source']}

💡 *Offerta #{i} di {len(deals)}*
            """
            
            keyboard = [
                [InlineKeyboardButton("🛒 Vai all'Offerta", url=final_url)],
                [InlineKeyboardButton("🔄 Condividi", callback_data=f"share_{i}")],
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            try:
                if deal.get('image') and deal['image'].startswith('http'):
                    await update.message.reply_photo(
                        photo=deal['image'],
                        caption=deal_text,
                        reply_markup=reply_markup
                    )
                else:
                    await update.message.reply_text(deal_text, reply_markup=reply_markup)
            except:
                await update.message.reply_text(deal_text, reply_markup=reply_markup)
            
            await asyncio.sleep(1)  # Evita rate limiting Telegram
            
    except Exception as e:
        await loading_msg.delete()
        logger.error(f"Errore in offerte_command: {e}")
        await update.message.reply_text(
            "❌ **Errore temporaneo**\n\n"
            "Il servizio è momentaneamente sovraccarico.\n"
            "Riprova tra qualche minuto! 🔄"
        )

async def cerca_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text(
            "🔍 **Ricerca Personalizzata**\n\n"
            "**Formato**: `/cerca nome prodotto`\n\n"
            "**Esempi efficaci:**\n"
            "• `/cerca iPhone 15 Pro`\n"
            "• `/cerca cuffie noise cancelling`\n"
            "• `/cerca robot aspirapolvere`\n"
            "• `/cerca SSD 1TB`\n\n"
            "🎯 Più specifico = risultati migliori!"
        )
        return
    
    query = ' '.join(context.args)
    
    loading_msg = await update.message.reply_text(
        f"🔍 **Cerco '{query}'**\n"
        f"Scansiono i prezzi migliori...\n"
        f"⏳ 10-15 secondi"
    )
    
    try:
        deals = await scraper.scrape_amazon_deals(search_term=query, max_deals=5)
        
        await loading_msg.delete()
        
        if not deals:
            await update.message.reply_text(
                f"😅 **Nessun risultato per '{query}'**\n\n"
                "**Suggerimenti:**\n"
                "• Usa termini più generici\n"
                "• Controlla la scrittura\n" 
                "• Prova in inglese\n"
                "• Usa /offerte per offerte generali"
            )
            return
        
        await update.message.reply_text(
            f"🎯 **{len(deals)} risultati per '{query}'**\n"
            f"📅 Aggiornati: {datetime.now().strftime('%H:%M')}"
        )
        
        for i, deal in enumerate(deals, 1):
            affiliate_link = scraper.create_affiliate_link(deal['url'])
            
            deal_text = f"""
🎯 **{deal['title']}**

💰 **Prezzo**: {deal['price']}
⭐ **Valutazione**: {deal['rating']}
🛒 **Amazon Italia**

🎁 *Risultato #{i} per "{query}"*
            """
            
            keyboard = [
                [InlineKeyboardButton("🛒 Acquista Ora", url=affiliate_link)],
                [InlineKeyboardButton("📊 Altri Modelli", callback_data=f"more_{query}")],
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            try:
                if deal.get('image') and deal['image'].startswith('http'):
                    await update.message.reply_photo(
                        photo=deal['image'],
                        caption=deal_text,
                        reply_markup=reply_markup
                    )
                else:
                    await update.message.reply_text(deal_text, reply_markup=reply_markup)
            except:
                await update.message.reply_text(deal_text, reply_markup=reply_markup)
            
            await asyncio.sleep(1)
            
    except Exception as e:
        await loading_msg.delete()
        logger.error(f"Errore in cerca_command: {e}")
        await update.message.reply_text(
            f"❌ **Errore nella ricerca '{query}'**\n\n"
            "Riprova tra qualche minuto o usa /offerte 🔄"
        )

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    if query.data == 'live_deals':
        await query.message.reply_text("🔍 Carico offerte live...")
        await offerte_command(update, context)
    
    elif query.data == 'search_mode':
        await query.message.reply_text(
            "🔍 **Modalità Ricerca Attivata**\n\n"
            "Scrivi: `/cerca nome prodotto`\n\n"
            "**Esempi:**\n" 
            "• `/cerca MacBook Air`\n"
            "• `/cerca scarpe running Nike`"
        )
    
    elif query.data.startswith('search_'):
        category = query.data.split('_')[1]
        categories = {
            'elettronica': 'smartphone tablet laptop',
            'casa': 'elettrodomestici cucina bagno'
        }
        search_term = categories.get(category, category)
        
        await query.message.reply_text(f"🔍 Cerco offerte {category}...")
        # Simula comando cerca
        context.args = search_term.split()
        await cerca_command(update, context)

async def handle_url(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Gestisce URL Amazon inviati dall'utente"""
    text = update.message.text
    
    if 'amazon.it' in text or 'amazon.com' in text:
        try:
            affiliate_link = scraper.create_affiliate_link(text)
            
            response = f"""
🔗 **Link Amazon Convertito!**

✅ **Link con affiliazione creato**
💰 Supporti il bot senza costi extra

**Link originale:**
`{text[:50]}...`

**Link affiliato:**
            """
            
            keyboard = [[
                InlineKeyboardButton("🛒 Apri Link Affiliato", url=affiliate_link),
                InlineKeyboardButton("📋 Copia Link", callback_data="copy_link")
            ]]
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            await update.message.reply_text(response, reply_markup=reply_markup)
            await update.message.reply_text(f"`{affiliate_link}`")
            
        except Exception as e:
            await update.message.reply_text("❌ Errore nella conversione del link")
    else:
        await update.message.reply_text(
            "🤖 **Non ho capito questo messaggio**\n\n"
            "**Puoi:**\n"
            "• `/offerte` - Vedere offerte del giorno\n" 
            "• `/cerca prodotto` - Cercare qualcosa\n"
            "• Inviare link Amazon per conversione\n"
            "• `/help` - Guida completa"
        )

def main():
    print("🛒 Avvio Amazon Scraper Bot...")
    
    application = Application.builder().token(BOT_TOKEN).build()
    
    # Comandi
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("offerte", offerte_command))
    application.add_handler(CommandHandler("cerca", cerca_command))
    
    # Gestori
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_url))
    application.add_handler(CallbackQueryHandler(button_handler))
    
    print("✅ Amazon Scraper Bot attivo! 🚀")
    application.run_polling(drop_pending_updates=True)

if __name__ == '__main__':
    main()
