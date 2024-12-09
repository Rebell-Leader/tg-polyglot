from aiogram import Bot, Dispatcher, types
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton
from aiogram.filters import Command
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.types.input_file import FSInputFile
from aiogram.fsm.state import StatesGroup, State
from translation import cleanup_files, process_video
from database import UserDatabase
import config

# Bot setup
bot = Bot(token=config.TELEGRAM_TOKEN)
dp = Dispatcher()

# Database setup
db = UserDatabase(config.DATABASE_PATH)

# State Machine
class TranslationState(StatesGroup):
    awaiting_video_link = State()
    awaiting_language_settings = State()
    awaiting_processing_mode = State()

# Keyboards
main_menu_keyboard = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="Translate Video"), KeyboardButton(text="View Stats")],
        [KeyboardButton(text="Set Source/Target Languages"), KeyboardButton(text="Premium Status")]
    ],
    resize_keyboard=True
)

processing_mode_keyboard = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="Generate Video with Translated Audio")],
        [KeyboardButton(text="Only Translated Audio"), KeyboardButton(text="Translated Transcript Only")]
    ],
    resize_keyboard=True
)

language_selection_keyboard = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="Set Source: English"), KeyboardButton(text="Set Target: Russian")],
        [KeyboardButton(text="Back to Main Menu")]
    ],
    resize_keyboard=True
)

# Default language settings
DEFAULT_SOURCE_LANG = "en"
DEFAULT_TARGET_LANG = "ru"

# Handlers
@dp.message(Command(commands=["start"]))
async def cmd_start(message: types.Message):
    """Handle /start command."""
    user_id = message.from_user.id
    username = message.from_user.username
    db.add_user(user_id, username)
    await message.answer("Welcome to the Video Translation Bot! Use the menu below to get started.", reply_markup=main_menu_keyboard)

@dp.message(lambda message: message.text == "Translate Video")
async def start_translation(message: types.Message, state: FSMContext):
    """Start translation process."""
    await state.set_state(TranslationState.awaiting_video_link)
    await message.answer("Please send the video link (e.g., YouTube, VK, Vimeo).")

@dp.message(TranslationState.awaiting_video_link)
async def receive_video_link(message: types.Message, state: FSMContext):
    """Receive video link and ask for processing mode."""
    video_url = message.text
    if not video_url.startswith(("http://", "https://")):
        await message.answer("Invalid URL. Please send a valid video link.")
        return

    await state.update_data(video_url=video_url, source_lang=DEFAULT_SOURCE_LANG, target_lang=DEFAULT_TARGET_LANG)
    await state.set_state(TranslationState.awaiting_processing_mode)
    await message.answer("Select the processing mode:", reply_markup=processing_mode_keyboard)

@dp.message(TranslationState.awaiting_processing_mode)
async def select_processing_mode(message: types.Message, state: FSMContext):
    """Handle processing mode selection."""
    processing_mode = message.text
    if processing_mode not in ["Generate Video with Translated Audio", "Only Translated Audio", "Translated Transcript Only"]:
        await message.answer("Invalid choice. Please select a valid processing mode.")
        return
    processing_mode_to_str = {"Generate Video with Translated Audio": "video", "Only Translated Audio": "audio", "Translated Transcript Only": "text"}

    user_id = message.from_user.id
    username = message.from_user.username
    data = await state.get_data()
    video_url = data.get("video_url")
    source_lang = data.get("source_lang")
    target_lang = data.get("target_lang")

    # Check user quota
    if not db.can_translate(user_id):
        if db.is_premium(user_id):
            await message.answer("You have unlimited translations as a Premium user.")
        else:
            await message.answer("You’ve reached your free translation limit for today. Upgrade to Premium for unlimited translations.")
            return

    await message.answer("Processing your request. This may take some time...")
    try:
        result_type, result_path = process_video(video_url, f"{source_lang} to {target_lang}", processing_mode_to_str[processing_mode])
        if result_type == "video":
            video = FSInputFile(result_path)
            await bot.send_video(chat_id=message.chat.id, video=video)
        elif result_type == "audio":
            audio = FSInputFile(result_path)
            await bot.send_audio(chat_id=message.chat.id, audio=audio)
        elif result_type == "text":
            text = open(result_path).read()
            await message.answer(text)

        cleanup_files(result_path)
        db.log_translation(user_id, username, video_url, processing_mode)
        db.log_successful_translation(user_id)
    except Exception as e:
        #await message.answer("Something went wrong. Please upload your video to Yandex Disk and share the link.")
        await message.answer(str(e))
    finally:
        await state.clear()

@dp.message(lambda message: message.text == "View Stats")
async def view_stats(message: types.Message):
    """Show user's translation stats."""
    user_id = message.from_user.id
    stats = db.get_user_stats(user_id)
    if stats:
        await message.answer(f"Total translations: {stats['total_translations']}\n"
                             f"Premium status: {'Yes' if stats['is_premium'] else 'No'}")
    else:
        await message.answer("You have no translation history.")

@dp.message(lambda message: message.text == "Set Source/Target Languages")
async def set_languages(message: types.Message):
    """Show language selection options."""
    await message.answer("Choose a language to set:", reply_markup=language_selection_keyboard)

@dp.message(lambda message: message.text.startswith("Set Source:"))
async def set_source_language(message: types.Message, state: FSMContext):
    """Set the source language."""
    source_lang = message.text.split(": ")[1].lower()
    await state.update_data(source_lang=source_lang)
    await message.answer(f"Source language set to {source_lang}.")

@dp.message(lambda message: message.text.startswith("Set Target:"))
async def set_target_language(message: types.Message, state: FSMContext):
    """Set the target language."""
    target_lang = message.text.split(": ")[1].lower()
    await state.update_data(target_lang=target_lang)
    await message.answer(f"Target language set to {target_lang}.")

if __name__ == "__main__":
    dp.run_polling(bot)
