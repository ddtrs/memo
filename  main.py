import os
import logging
import re
import io
import asyncio
import nest_asyncio
nest_asyncio.apply()

# --- СЕРВЕР ---
from keep_alive import keep_alive
try:
    keep_alive()
except Exception:
    pass

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup
from telegram.ext import Application, MessageHandler, filters, ContextTypes, CommandHandler, CallbackQueryHandler
from google import genai
from google.genai.types import Content, Part, GenerateContentConfig
from gtts import gTTS

# --- КОНФИГУРАЦИЯ ---
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

# Основная модель
PRIMARY_MODEL = 'gemini-2.5-flash' 
# Запасные (если 2.5 заглючит или не доступна)
FALLBACK_MODELS = ['gemini-1.5-flash-latest', 'gemini-2.0-flash-exp']

if not TELEGRAM_BOT_TOKEN or not GEMINI_API_KEY:
    raise ValueError("❌ ОШИБКА: Добавь ключи в Secrets!")

# --- ИНСТРУКЦИЯ (ЖЕСТКИЙ РУССКИЙ) ---
SYSTEM_INSTRUCTION_MEMO = (
    "Твоя личность — **Помощник Мемо**. Ты — друг и второй мозг.\n"
    "**ЯЗЫК:** Твой основной язык — **РУССКИЙ**. Всегда отвечай на русском, если пользователь не обратился к тебе на другом языке явно.\n"
    "**КОНТЕКСТ:** Ты знаешь украинские реалии (география, сервисы), но описываешь их на русском языке.\n"
    "**НАВЫКИ:** Текст, Фото (вижу детали), Аудио.\n"
    "**СТИЛЬ:** Отвечай живо, дружелюбно и лаконично."
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
    logger.error(f"Error: {e}")
    exit()

# --- ФУНКЦИИ ---

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
    try:
        if response.candidates and response.candidates[0].grounding_metadata:
            grounding = response.candidates[0].grounding_metadata
            if grounding.grounding_attributions:
                sources = [attr.web.title for attr in grounding.grounding_attributions if attr.web]
                if sources: return "\n📚 Источники: " + ", ".join(sources[:3])
    except: pass
    return ""

# --- UI МЕНЮ ---
def get_start_keyboard():
    return ReplyKeyboardMarkup([["🔘 Главное меню"]], resize_keyboard=True)

async def show_root_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [InlineKeyboardButton("📂 Проекты", callback_data="menu_projects"),
         InlineKeyboardButton("⚙️ Настройки", callback_data="menu_settings")],
        [InlineKeyboardButton("❌ Закрыть", callback_data="close_menu")]
    ]
    text = "👋 **Меню Мемо**"
    if update.callback_query:
        await update.callback_query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')
    else:
        await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')

async def show_projects_menu(update: Update, context: ContextTypes.DEFAULT_TYPE, mode="switch"):
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
        keyboard.append([InlineKeyboardButton("➕ Создать", callback_data="new_proj_prompt"),
                         InlineKeyboardButton("🗑 Удалить", callback_data="show_delete_menu")])
        keyboard.append([InlineKeyboardButton("🔙 Назад", callback_data="back_to_root")])
        text = f"📂 **Проекты** (Текущий: `{current}`)"
    elif mode == "delete":
        for p in projects:
            if p == "default": continue 
            keyboard.append([InlineKeyboardButton(f"❌ Удалить {p}", callback_data=f"delete|{p}")])
        keyboard.append([InlineKeyboardButton("🔙 Назад", callback_data="menu_projects")])
        text = "🗑 **Удаление проектов:**"

    await update.callback_query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')

async def show_settings_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    settings = get_settings(user_id)
    mode = settings["voice_mode"]
    if mode != "off": voice_text = "✅ Голос: ВКЛ"
    else: voice_text = "🔇 Голос: ВЫКЛ"

    keyboard = [
        [InlineKeyboardButton(voice_text, callback_data="toggle_voice")],
        [InlineKeyboardButton("ℹ️ Инфо", callback_data="show_info")],
        [InlineKeyboardButton("🔙 Назад", callback_data="back_to_root")]
    ]
    text = "⚙️ **Настройки**"
    await update.callback_query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')

# --- VOICE ---
def generate_voice_bytes_sync(text: str, lang_code: str) -> io.BytesIO:
    clean_text = text.replace("*", "").replace("#", "").replace("`", "").replace("_", "")
    if len(clean_text) > 800: clean_text = clean_text[:800]

    # Всегда используем RU для голоса, если явно не определено другое
    short_lang = lang_code[:2] if lang_code else 'ru'
    if short_lang == 'uk': short_lang = 'uk' # Оставляем украинский голос только если текст украинский
    else: short_lang = 'ru' # Иначе принудительно русский голос

    fp = io.BytesIO()
    tts = gTTS(text=clean_text, lang=short_lang)
    tts.write_to_fp(fp)
    fp.seek(0)
    return fp

# --- GEMINI CORE ---

async def send_gemini_query(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    mem_key = get_memory_key(update)
    if mem_key not in memory_store: memory_store[mem_key] = []
    history = memory_store[mem_key]

    user_parts = []
    text_content = update.message.text or update.message.caption

    if text_content: 
        user_parts.append(Part.from_text(text=text_content))

    # ИЗОБРАЖЕНИЕ
    photo_file = None
    mime_type = "image/jpeg"

    if update.message.photo:
        photo_file = await update.message.photo[-1].get_file()
    elif update.message.document and update.message.document.mime_type.startswith('image'):
        photo_file = await update.message.document.get_file()
        mime_type = update.message.document.mime_type

    if photo_file:
        await context.bot.send_chat_action(chat_id=update.effective_chat.id, action='UPLOAD_PHOTO')
        img_byte_arr = io.BytesIO()
        await photo_file.download_to_memory(img_byte_arr)

        try:
            image_part = Part.from_bytes(data=img_byte_arr.getvalue(), mime_type=mime_type)
            user_parts.append(image_part)
        except Exception as e:
            logger.error(f"Error image: {e}")

        if not text_content: 
            user_parts.append(Part.from_text(text="Что на этом изображении? Опиши подробно на русском языке."))

    # АУДИО
    if update.message.voice:
        await context.bot.send_chat_action(chat_id=update.effective_chat.id, action='RECORD_VOICE')
        voice_file = await update.message.voice.get_file()
        voice_byte_arr = io.BytesIO()
        await voice_file.download_to_memory(voice_byte_arr)
        try:
            audio_part = Part.from_bytes(data=voice_byte_arr.getvalue(), mime_type="audio/ogg")
            user_parts.append(audio_part)
        except Exception as e:
            logger.error(f"Error audio: {e}")
        if not text_content: user_parts.append(Part.from_text(text="Прослушай аудио и ответь."))

    if not user_parts: return

    user_content = Content(role="user", parts=user_parts)
    history.append(user_content)

    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action='TYPING')

    try:
        user = update.effective_user
        # Мы больше НЕ берем язык из настроек телефона для промпта, чтобы не сбивать бота
        # user_lang_code = user.language_code ... (УБРАЛИ ЭТО)

        proj_name = get_current_project_name(update)
        settings = get_settings(user.id)
        voice_hint = "Текст удобен для чтения." if settings["voice_mode"] != "off" else ""

        config = GenerateContentConfig(
            tools=[{"google_search": {}}],
            system_instruction=f"{SYSTEM_INSTRUCTION_MEMO}\nКонтекст: {proj_name}\n{voice_hint}"
        )

        # --- УМНЫЙ ВЫБОР МОДЕЛИ (2.5 -> Fallback) ---
        response = None
        models_to_try = [PRIMARY_MODEL] + FALLBACK_MODELS

        for model_name in models_to_try:
            try:
                response = gemini_client.models.generate_content(model=model_name, contents=history, config=config)
                break 
            except Exception as e:
                if model_name == models_to_try[-1]: raise e # Если последняя не сработала - ошибка
                continue

        if not response or not response.text: return
        raw_text = response.text
        sources_text = format_grounding_sources(response)

        header = f"📂 *[{proj_name}]*\n" if (not update.message.is_topic_message and proj_name != "default") else ""
        final_text = header + re.sub(r'([_*\[\]()~`>#+\-=|{}.!])', r'\\\1', raw_text) + sources_text

        history.append(Content(role="model", parts=[Part.from_text(text=raw_text)]))

        await update.message.reply_text(final_text, parse_mode='MarkdownV2')

        if settings["voice_mode"] != "off":
            await context.bot.send_chat_action(chat_id=update.effective_chat.id, action='RECORD_VOICE')
            loop = asyncio.get_running_loop()
            try:
                # Передаем 'ru' как дефолт, но если текст украинский, gTTS сам поймет или мы можем допилить детектор
                # Пока ставим жестко 'ru' для стабильности русского акцента
                voice_audio = await loop.run_in_executor(None, generate_voice_bytes_sync, raw_text, 'ru')
                if voice_audio: await context.bot.send_voice(chat_id=update.effective_chat.id, voice=voice_audio)
            except Exception as e: logger.error(f"Voice Error: {e}")

    except Exception as e:
        if len(history) > 0: history.pop()
        logger.error(f"GEMINI ERROR: {e}")
        await update.message.reply_text("⛔️ Ошибка ИИ. Попробуй еще раз.")

# --- HANDLERS ---

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message.is_topic_message:
        await send_gemini_query(update, context)
        return

    text = update.message.text

    if text == "🔘 Главное меню":
        try: await update.message.delete()
        except: pass
        await show_root_menu(update, context)
        return

    if text and text.startswith("/new"):
        await new_project_command(update, context)
        return

    await send_gemini_query(update, context)

async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    data = query.data
    user_id = update.effective_user.id

    if data == "back_to_root": await show_root_menu(update, context); return
    if data == "menu_projects": await show_projects_menu(update, context, mode="switch"); return
    if data == "menu_settings": await show_settings_menu(update, context); return
    if data == "close_menu": await query.delete_message(); return 

    if data == "show_delete_menu": await show_projects_menu(update, context, mode="delete"); return

    if data == "new_proj_prompt":
        await query.answer("Напишите в чат /new имя", show_alert=True)
        return

    if data == "toggle_voice":
        s = get_settings(user_id)
        s["voice_mode"] = "on" if s["voice_mode"] == "off" else "off"
        await show_settings_menu(update, context) 
        return

    if data == "show_info":
        await query.answer("Мемо v8.5 (Russian Fixed)", show_alert=True)
        return

    if "|" in data:
        action, proj = data.split("|")
        if action == "switch":
            user_current_project[user_id] = proj
            await query.answer(f"Выбран: {proj}")
            await show_projects_menu(update, context, mode="switch")
        elif action == "delete":
            key = f"user_{user_id}_{proj}"
            if key in memory_store: del memory_store[key]
            if user_current_project.get(user_id) == proj: user_current_project[user_id] = "default"
            await query.answer(f"Удален: {proj}")
            await show_projects_menu(update, context, mode="delete")

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try: await update.message.delete()
    except: pass
    await update.message.reply_text("👋 Привет! Я Мемо.", reply_markup=get_start_keyboard())

async def new_project_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message.is_topic_message: return
    if not context.args: 
        await update.message.reply_text("Укажи имя: `/new работа`", parse_mode='Markdown')
        return
    name = context.args[0]
    user_id = update.effective_user.id
    user_current_project[user_id] = name
    key = f"user_{user_id}_{name}"
    if key not in memory_store: memory_store[key] = []
    try: await update.message.delete()
    except: pass
    await update.message.reply_text(f"✅ Создан: **{name}**", parse_mode='Markdown')
    await show_root_menu(update, context)

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
    application.add_handler(MessageHandler(
        (filters.TEXT | filters.PHOTO | filters.VOICE | filters.Document.IMAGE) & ~filters.COMMAND, 
        handle_message
    ))
    logger.info("Бот Мемо (Russian Language Fixed) запущен...")
    application.run_polling(drop_pending_updates=True)

if __name__ == '__main__':
    main()
