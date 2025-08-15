import logging
import os
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

# Configurazione del logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Token del bot da variabile d'ambiente (più sicuro per il deploy)
BOT_TOKEN = os.getenv('BOT_TOKEN')

if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN non trovato nelle variabili d'ambiente!")

# Le tue funzioni rimangono uguali
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_name = update.effective_user.first_name
    await update.message.reply_text(
        f"Ciao {user_name}! 👋\n"
        f"Benvenuto nel mio bot!\n"
        f"Scrivi /help per vedere cosa posso fare."
    )

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    help_text = """
🤖 **Comandi disponibili:**

/start - Avvia il bot
/help - Mostra questo messaggio
/info - Informazioni sul bot
/saluta - Ti saluto in modo speciale

Puoi anche inviarmi qualsiasi messaggio e ti risponderò!
    """
    await update.message.reply_text(help_text)

async def info(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🔧 **Info Bot**\n"
        "Versione: 1.0\n"
        "Creato con Python\n"
        "Online 24/7! 🚀"
    )

async def saluta(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Ciao! Come stai oggi? 😊")

async def echo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_message = update.message.text
    await update.message.reply_text(f"Hai scritto: '{user_message}' 📝")

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.warning('Update "%s" caused error "%s"', update, context.error)

def main():
    print("🤖 Avvio del bot in corso...")
    
    application = Application.builder().token(BOT_TOKEN).build()
    
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("info", info))
    application.add_handler(CommandHandler("saluta", saluta))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, echo))
    application.add_error_handler(error_handler)
    
    # Per Heroku usiamo il webhook invece di polling
    port = int(os.environ.get('PORT', 8443))
    app_url = os.environ.get('APP_URL')
    
    if app_url:
        # Modalità webhook per production
        application.run_webhook(
            listen="0.0.0.0",
            port=port,
            url_path=BOT_TOKEN,
            webhook_url=f"{app_url}/{BOT_TOKEN}"
        )
    else:
        # Modalità polling per sviluppo locale
        print("✅ Bot avviato in modalità polling! Premi Ctrl+C per fermarlo")
        application.run_polling(drop_pending_updates=True)

if __name__ == '__main__':
    main()
