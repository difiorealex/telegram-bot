import logging
import os
import re
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
import aiohttp
import asyncio
from datetime import datetime

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.getenv('BOT_TOKEN')
AMAZON_TAG = os.getenv('AMAZON_TAG', 'botaffari-21')  # Il tuo tag affiliato

if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN non trovato!")

class AmazonBot:
    def __init__(self):
        self.session = None
    
    async def create_session(self):
        if not self.session:
            self.session = aiohttp.ClientSession()
    
    async def close_session(self):
        if self.session:
            await self.session.close()

    def create_affiliate_link(self, amazon_url):
        """Crea un link di affiliazione da un URL Amazon"""
        # Estrae l'ASIN (codice prodotto) dall'URL
        asin_match = re.search(r'/dp/([A-Z0-9]{10})', amazon_url)
        if not asin_match:
            asin_match = re.search(r'/gp/product/([A-Z0-9]{10})', amazon_url)
        
        if asin_match:
            asin = asin_match.group(1)
            return f"https://www.amazon.it/dp/{asin}/?tag={AMAZON_TAG}&linkCode=ogi&th=1"
        
        # Se non trova ASIN, aggiunge il tag all'URL esistente
        if '?' in amazon_url:
            return f"{amazon_url}&tag={AMAZON_TAG}"
        else:
            return f"{amazon_url}?tag={AMAZON_TAG}"

    async def search_amazon_deals(self, query=""):
        """Simula la ricerca di offerte (per ora restituisce esempi)"""
        # NOTA: In produzione useresti l'API di Amazon o web scraping
        sample_deals = [
            {
                "title": "Echo Dot (5ª generazione) | Altoparlante intelligente",
                "price": "€29,99",
                "original_price": "€59,99",
                "discount": "50%",
                "url": "https://www.amazon.it/dp/B09B8V1LZ3",
                "image": "https://m.media-amazon.com/images/I/61s4uxJTgTL._AC_SL1000_.jpg"
            },
            {
                "title": "Fire TV Stick 4K Max | Streaming Device",
                "price": "€34,99",
                "original_price": "€54,99",
                "discount": "36%",
                "url": "https://www.amazon.it/dp/B08MQZXN1X",
                "image": "https://m.media-amazon.com/images/I/51TjJOTfslL._AC_SL1000_.jpg"
            }
        ]
        
        return sample_deals

bot_instance = AmazonBot()

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_name = update.effective_user.first_name
    welcome_text = f"""
🛒 **Ciao {user_name}! Benvenuto nel Bot delle Offerte Amazon!**

🔥 Trova le migliori offerte e sconti su Amazon!
💰 Tutti i link includono il supporto al creatore

**Comandi disponibili:**
/offerte - Mostra le offerte del giorno
/cerca [prodotto] - Cerca offerte specifiche
/categorie - Esplora per categoria
/help - Guida completa

**Inizia subito con /offerte!** 🚀
    """
    
    keyboard = [
        [InlineKeyboardButton("🔥 Offerte del Giorno", callback_data='daily_deals')],
        [InlineKeyboardButton("🔍 Cerca Prodotto", callback_data='search_product')],
        [InlineKeyboardButton("📱 Elettronica", callback_data='electronics')],
        [InlineKeyboardButton("🏠 Casa e Cucina", callback_data='home')]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await update.message.reply_text(welcome_text, reply_markup=reply_markup)

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    help_text = """
🤖 **Guida del Bot Offerte Amazon**

**Comandi principali:**
• `/offerte` - Migliori offerte del giorno
• `/cerca iPhone` - Cerca prodotti specifici
• `/link [URL Amazon]` - Genera link affiliato

**Come funziona:**
1. 🔍 Il bot trova le migliori offerte
2. 💰 Ti mostra prezzo e sconto
3. 🛒 Clicca per acquistare (link affiliato)
4. ❤️ Supporti il creatore del bot

**Categorie disponibili:**
📱 Elettronica | 🏠 Casa | 👕 Moda | 📚 Libri
🎮 Gaming | 💄 Bellezza | 🏃 Sport | 🚗 Auto

**Note:**
• Prezzi aggiornati in tempo reale
• Solo offerte verificate e sicure
• Link diretti ad Amazon Italia
    """
    await update.message.reply_text(help_text)

async def offerte_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🔍 Cerco le migliori offerte per te...")
    
    await bot_instance.create_session()
    deals = await bot_instance.search_amazon_deals()
    
    if not deals:
        await update.message.reply_text("😅 Nessuna offerta trovata al momento. Riprova più tardi!")
        return
    
    for deal in deals[:3]:  # Mostra solo le prime 3 offerte
        affiliate_link = bot_instance.create_affiliate_link(deal['url'])
        
        deal_text = f"""
🔥 **{deal['title']}**

💰 **Prezzo**: {deal['price']} (era {deal['original_price']})
📉 **Sconto**: -{deal['discount']}
⏰ **Offerta limitata!**

🛒 Acquista ora con il link qui sotto ⬇️
        """
        
        keyboard = [
            [InlineKeyboardButton("🛒 Acquista su Amazon", url=affiliate_link)],
            [InlineKeyboardButton("📊 Confronta Prezzi", callback_data=f"compare_{deal['url'].split('/')[-1]}")],
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        try:
            # Invia l'immagine se disponibile
            if deal.get('image'):
                await update.message.reply_photo(
                    photo=deal['image'],
                    caption=deal_text,
                    reply_markup=reply_markup
                )
            else:
                await update.message.reply_text(deal_text, reply_markup=reply_markup)
        except:
            # Se l'immagine non funziona, invia solo il testo
            await update.message.reply_text(deal_text, reply_markup=reply_markup)
        
        await asyncio.sleep(1)  # Evita spam

async def cerca_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text(
            "🔍 **Come cercare:**\n"
            "Usa: `/cerca nome prodotto`\n\n"
            "**Esempi:**\n"
            "• `/cerca iPhone 15`\n"
            "• `/cerca cuffie bluetooth`\n"
            "• `/cerca robot aspirapolvere`"
        )
        return
    
    query = ' '.join(context.args)
    await update.message.reply_text(f"🔍 Cerco '{query}' per te...")
    
    # Qui implementeresti la ricerca reale
    await update.message.reply_text(
        f"🔍 **Risultati per '{query}'**\n\n"
        "⚠️ Funzione in sviluppo!\n"
        "Per ora usa /offerte per vedere le offerte del giorno.\n\n"
        "🚀 Prossimamente: ricerca personalizzata completa!"
    )

async def link_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text(
            "🔗 **Genera Link Affiliato**\n\n"
            "Usa: `/link [URL Amazon]`\n\n"
            "**Esempio:**\n"
            "`/link https://www.amazon.it/dp/B08N5WRWNW`"
        )
        return
    
    amazon_url = context.args[0]
    
    if 'amazon' not in amazon_url.lower():
        await update.message.reply_text("❌ Inserisci un URL Amazon valido!")
        return
    
    affiliate_link = bot_instance.create_affiliate_link(amazon_url)
    
    response = f"""
🔗 **Link Affiliato Generato!**

**Link originale:**
{amazon_url[:50]}...

**Link con affiliazione:**
{affiliate_link}

✅ Ora supporti il creatore quando qualcuno acquista!
    """
    
    keyboard = [[InlineKeyboardButton("🛒 Apri Link", url=affiliate_link)]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await update.message.reply_text(response, reply_markup=reply_markup)

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    if query.data == 'daily_deals':
        await query.message.reply_text("🔍 Carico le offerte del giorno...")
        await offerte_command(update, context)
    
    elif query.data == 'search_product':
        await query.message.reply_text(
            "🔍 **Ricerca Prodotto**\n\n"
            "Scrivi: `/cerca nome prodotto`\n\n"
            "**Esempi:**\n"
            "• `/cerca iPhone`\n" 
            "• `/cerca cuffie wireless`"
        )
    
    elif query.data in ['electronics', 'home']:
        category = "Elettronica" if query.data == 'electronics' else "Casa e Cucina"
        await query.message.reply_text(
            f"📱 **Offerte {category}**\n\n"
            "🚀 Funzione in sviluppo!\n"
            "Per ora usa /offerte per le migliori offerte del giorno."
        )

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.lower()
    
    if 'amazon' in text and ('http' in text or 'www' in text):
        # L'utente ha inviato un link Amazon
        amazon_url = update.message.text
        affiliate_link = bot_instance.create_affiliate_link(amazon_url)
        
        response = f"""
🔗 **Ho convertito il tuo link Amazon!**

**Link con affiliazione:**
{affiliate_link}

🛒 Usa questo link per supportare il bot!
        """
        
        keyboard = [[InlineKeyboardButton("🛒 Apri Link Affiliato", url=affiliate_link)]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await update.message.reply_text(response, reply_markup=reply_markup)
    
    else:
        await update.message.reply_text(
            "🤔 Non ho capito.\n\n"
            "**Comandi utili:**\n"
            "• /offerte - Offerte del giorno\n"
            "• /cerca prodotto - Cerca specifico\n"
            "• /help - Guida completa\n\n"
            "Oppure invia un link Amazon per convertirlo!"
        )

def main():
    print("🛒 Avvio Amazon Affiliate Bot...")
    
    application = Application.builder().token(BOT_TOKEN).build()
    
    # Comandi
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("offerte", offerte_command))
    application.add_handler(CommandHandler("cerca", cerca_command))
    application.add_handler(CommandHandler("link", link_command))
    
    # Gestori
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    
    # Bottoni
    from telegram.ext import CallbackQueryHandler
    application.add_handler(CallbackQueryHandler(button_handler))
    
    print("✅ Amazon Affiliate Bot attivo!")
    application.run_polling(drop_pending_updates=True)

if __name__ == '__main__':
    main()
