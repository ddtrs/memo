import os
import logging
import re
import io
import asyncio
import nest_asyncio
nest_asyncio.apply()

# --- ИМПОРТ ДЛЯ RENDER (ЧТОБЫ НЕ СПАЛ) ---
from keep_alive import keep_alive
keep_alive()

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup
from telegram.ext import Application, MessageHandler, filters, ContextTypes, CommandHandler, CallbackQueryHandler
from google import genai
from google.genai.types import Content, Part, GenerateContentConfig
# Используем gTTS (Google Translate TTS) - он самый надежный для серверов
from gtts import gTTS

# --- КОНФИГУРАЦИЯ ---
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
GEMINI_MODEL = 'gemini-2.5-flash'

if not TELEGRAM_BOT_TOKEN or not GEMINI_API_KEY:
    raise ValueError("Установите токены в Environment Variables!")

# --- ИНСТРУКЦИЯ ---
SYSTEM_INSTRUCTION_MEMO = (
    "Твоя личность — **Помощник Мемо**. Ты — друг и второй мозг.\n"
    "**КОНТЕКСТ:** Учитывай украинские реалии (новости, география, сервисы).\n"
    "**НАВЫКИ:** Текст, Фото, Аудио.\n"
    "**СТИЛЬ:** Отвечай живо, но лаконично, чтобы ответ было удобно слушать."
)

# --- ДАННЫЕ ---
memory_store = {} 
user_current_project = {}
user_settings = {} 

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

try:
    gemini_client = genai.Client(api_key=GEMINI_API_KEY)
except Exception as e:
    logger.error(f"Ошибка Gemini: {e}")
    exit()

# --- ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ---

def get_settings(user_id: int):
    if user_id not in user_settings:
        user_settings[user_id] = {"voice_mode": "auto"} 
    return user_settings[user_id]

def get_memory_key(update: Update) -> str:
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    if update.message and update.message.is_topic_message:
        return f"topic_{chat_id}_{update.message.message_thread_id}"
    current_proj = user_current_project.get(user_id, "default")
    return f"user_{user_id}_{current_proj}"

def get_current_project_name(update: Update) -> str:
    if update.message and update.message.is_topic_message:
        return f"Тема #{update.message.message_thread_id}"
    return user_current_project.get(update.effective_user.id, "default")

def format_grounding_sources(response) -> str:
    sources_text = ""
    try:
        if response.candidates and response.candidates[0].grounding_metadata:
            grounding = response.candidates[0].grounding_metadata
            if grounding.grounding_attributions:
                sources = []
                for attr in grounding.grounding_attributions:
                    if attr.web and attr.web.uri and attr.web.title:
                        uri = attr.web.uri
                        title = attr.web.title
                        if (uri, title) not in sources:
                            sources.append((uri, title))
                if sources:
                    sources_text += "\n\n📚 **Источники:**\n"
                    for i, (uri, title) in enumerate(sources, 1):
                        safe_title = re.sub(r'([_*\[\]()~`>#+\-=|{}.!])', r'\\\1', title)
                        safe_uri = re.sub(r'([)\]])', r'\\\1', uri) 
                        sources_text += f"{i}\\. [{safe_title}]({safe_uri})\n"
    except Exception: return ""
    return sources_text

# --- UI ---

def get_main_menu_keyboard():
    keyboard = [
        ["📂 Мои проекты", "➕ Новый проект"],
        ["⚙️ Настройки"]
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

def get_settings_keyboard(user_id):
    settings = get_settings(user_id)
    mode = settings["voice_mode"]
    
    if mode != "off":
        voice_btn = "✅ Голос: ВКЛЮЧЕН"
    else:
        voice_btn = "🔇 Голос: ВЫКЛЮЧЕН"
    
    keyboard = [
        [voice_btn],
        ["ℹ️ Инфо"],
        ["🔙 Назад в меню"]
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

# --- ГЕНЕРАЦИЯ ГОЛОСА (gTTS) ---
def generate_voice_bytes_sync(text: str, lang_code: str) -> io.BytesIO:
    """Синхронная функция генерации (выполняется в отдельном потоке)."""
    clean_text = text.replace("*", "").replace("#", "").replace("`", "").replace("_", "")
    if len(clean_text) > 800: clean_text = clean_text[:800]
    
    # gTTS использует короткие коды 'ru', 'en', 'uk'
    short_lang = lang_code[:2] if lang_code else 'ru'
    
    fp = io.BytesIO()
    tts = gTTS(text=clean_text, lang=short_lang)
    tts.write_to_fp(fp)
    fp.seek(0)
    return fp

# --- GEMINI ---

async def send_gemini_query(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    mem_key = get_memory_key(update)
    if mem_key not in memory_store: memory_store[mem_key] = []
    history = memory_store[mem_key]

    user_parts = []
    text_content = update.message.text or update.message.caption
    
    if text_content: user_parts.append(Part(text=text_content))
    
    if update.message.photo:
        await context.bot.send_chat_action(chat_id=update.effective_chat.id, action='UPLOAD_PHOTO')
        photo_file = await update.message.photo[-1].get_file()
        img_byte_arr = io.BytesIO()
        await photo_file.download_to_memory(img_byte_arr)
        user_parts.append(Part(inline_data={"mime_type": "image/jpeg", "data": img_byte_arr.getvalue()}))
        if not text_content: user_parts.append(Part(text="Что на фото?"))

    if update.message.voice:
        await context.bot.send_chat_action(chat_id=update.effective_chat.id, action='RECORD_VOICE')
        voice_file = await update.message.voice.get_file()
        voice_byte_arr = io.BytesIO()
        await voice_file.download_to_memory(voice_byte_arr)
        user_parts.append(Part(inline_data={"mime_type": "audio/ogg", "data": voice_byte_arr.getvalue()}))
        if not text_content: user_parts.append(Part(text="Ответь на аудио."))

    if not user_parts: return
    user_content = Content(role="user", parts=user_parts)
    history.append(user_content)

    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action='TYPING')

    try:
        user = update.effective_user
        user_lang_code = user.language_code if user.language_code else 'ru'
        proj_name = get_current_project_name(update)
        settings = get_settings(user.id)
        
        voice_hint = "Текст должен быть удобен для чтения вслух." if settings["voice_mode"] != "off" else ""

        dynamic_instruction = (
            f"{SYSTEM_INSTRUCTION_MEMO}\n"
            f"**ЯЗЫК:** Твой базовый язык — **{user_lang_code}**. Отвечай на нем, либо на языке сообщения.\n"
            f"**Контекст:** {proj_name}\n{voice_hint}"
        )

        config = GenerateContentConfig(
            tools=[{"google_search": {}}],
            system_instruction=dynamic_instruction
        )
        
        response = gemini_client.models.generate_content(
            model=GEMINI_MODEL,
            contents=history,
            config=config
        )
        
        if not response.text: return
        raw_text = response.text
        sources_text = format_grounding_sources(response)
        
        header = ""
        if not update.message.is_topic_message and proj_name != "default":
             header = f"📂 *[{proj_name}]*\n"
        
        final_text = header + re.sub(r'([_*\[\]()~`>#+\-=|{}.!])', r'\\\1', raw_text) + sources_text
        
        model_content = Content(role="model", parts=[Part(text=raw_text)])
        history.append(model_content)

        current_kb = get_main_menu_keyboard()
        if update.message.is_topic_message: current_kb = None
        
        # 1. ТЕКСТ
        await update.message.reply_text(final_text, parse_mode='MarkdownV2', reply_markup=current_kb)

        # 2. ГОЛОС (через gTTS в отдельном потоке)
        if settings["voice_mode"] != "off":
            await context.bot.send_chat_action(chat_id=update.effective_chat.id, action='RECORD_VOICE')
            
            # Запускаем генерацию в фоне, чтобы не тормозить бота
            loop = asyncio.get_running_loop()
            try:
                voice_audio = await loop.run_in_executor(None, generate_voice_bytes_sync, raw_text, user_lang_code)
                if voice_audio:
                    await context.bot.send_voice(chat_id=update.effective_chat.id, voice=voice_audio)
            except Exception as e:
                logger.error(f"Ошибка отправки голоса: {e}")

    except Exception as e:
        logger.error(f"Ошибка: {e}")
        if len(history) > 0: history.pop()
        await update.message.reply_text("⚠️ Ошибка обработки.")

# --- ОБРАБОТЧИКИ ---

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message.is_topic_message:
        await send_gemini_query(update, context)
        return

    text = update.message.text
    user_id = update.effective_user.id
    
    if text == "⚙️ Настройки":
        await update.message.reply_text("🛠 **Настройки:**", reply_markup=get_settings_keyboard(user_id), parse_mode='Markdown')
        return
    if text == "🔙 Назад в меню":
        await update.message.reply_text("🏠 **Главное меню:**", reply_markup=get_main_menu_keyboard(), parse_mode='Markdown')
        return

    if text and "Голос:" in text:
        settings = get_settings(user_id)
        current = settings["voice_mode"]
        if current == "off":
            settings["voice_mode"] = "on"
            msg = "✅ Озвучка ВКЛЮЧЕНА (Google Voice)."
        else:
            settings["voice_mode"] = "off"
            msg = "🔇 Озвучка ВЫКЛЮЧЕНА."
        await update.message.reply_text(msg, reply_markup=get_settings_keyboard(user_id))
        return

    if text == "📂 Мои проекты":
        await list_projects_inline(update, context, mode="switch")
    elif text == "➕ Новый проект":
        await update.message.reply_text("Напиши: `/new название`", parse_mode='Markdown')
    else:
        await send_gemini_query(update, context)

# --- СПИСКИ И КОМАНДЫ ---

async def list_projects_inline(update: Update, context: ContextTypes.DEFAULT_TYPE, mode="switch") -> None:
    user_id = update.effective_user.id
    current = user_current_project.get(user_id, "default")
    projects = set(["default"])
    prefix = f"user_{user_id}_"
    for k in memory_store.keys():
        if k.startswith(prefix): projects.add(k.replace(prefix, ""))
    
    keyboard = []
    if mode == "switch":
        for p in projects:
            status = "✅" if p == current else "⚪️"
            keyboard.append([InlineKeyboardButton(f"{status} {p}", callback_data=f"switch|{p}")])
        keyboard.append([InlineKeyboardButton("🗑 Удалить проект", callback_data="show_delete_menu")])
        text = "🗂 **Ваши проекты:**"
    elif mode == "delete":
        for p in projects:
            if p == "default": continue 
            keyboard.append([InlineKeyboardButton(f"❌ Удалить {p}", callback_data=f"delete|{p}")])
        keyboard.append([InlineKeyboardButton("🔙 Назад", callback_data="back_to_list")])
        text = "🗑 **Удаление:**"

    if update.callback_query:
        await update.callback_query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')
    else:
        await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')

async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    if query.data == "show_delete_menu": await list_projects_inline(update, context, mode="delete"); return
    if query.data == "back_to_list": await list_projects_inline(update, context, mode="switch"); return
    data = query.data.split("|")
    if len(data) < 2: return
    action, proj = data[0], data[1]
    user_id = update.effective_user.id
    if action == "switch":
        user_current_project[user_id] = proj
        await list_projects_inline(update, context, mode="switch")
    elif action == "delete":
        key = f"user_{user_id}_{proj}"
        if key in memory_store: del memory_store[key]
        if user_current_project.get(user_id) == proj: user_current_project[user_id] = "default"
        await list_projects_inline(update, context, mode="delete")

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("👋 Привет! Я Мемо.", reply_markup=get_main_menu_keyboard())

async def new_project_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message.is_topic_message: return
    if not context.args: return
    name = context.args[0]
    user_id = update.effective_user.id
    user_current_project[user_id] = name
    key = f"user_{user_id}_{name}"
    if key not in memory_store: memory_store[key] = []
    await update.message.reply_text(f"✅ Проект **{name}** создан!", parse_mode='Markdown')

async def reset_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    key = get_memory_key(update)
    memory_store[key] = []
    await update.message.reply_text("✅ Очищено.", parse_mode='Markdown')

def main() -> None:
    application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("new", new_project_command))
    application.add_handler(CommandHandler("reset", reset_command))
    application.add_handler(CallbackQueryHandler(button_callback))
    application.add_handler(MessageHandler((filters.TEXT | filters.PHOTO | filters.VOICE) & ~filters.COMMAND, handle_message))
    
    logger.info("Бот Мемо (Render Edition) запущен...")
    application.run_polling(drop_pending_updates=True)

if __name__ == '__main__':
    main()
