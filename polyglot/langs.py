"""Language tables shared by the bot UI and the engines."""
from __future__ import annotations

NAMES: dict[str, str] = {
    "auto": "🌐 Auto-detect",
    "en": "🇬🇧 English",
    "ru": "🇷🇺 Russian",
    "kk": "🇰🇿 Kazakh",
    "uk": "🇺🇦 Ukrainian",
    "de": "🇩🇪 German",
    "fr": "🇫🇷 French",
    "es": "🇪🇸 Spanish",
    "it": "🇮🇹 Italian",
    "pt": "🇧🇷 Portuguese",
    "tr": "🇹🇷 Turkish",
    "pl": "🇵🇱 Polish",
    "zh": "🇨🇳 Chinese",
    "ja": "🇯🇵 Japanese",
    "ko": "🇰🇷 Korean",
    "ar": "🇸🇦 Arabic",
    "hi": "🇮🇳 Hindi",
    "uz": "🇺🇿 Uzbek",
}

SOURCE_CHOICES = ["auto", "en", "ru", "de", "fr", "es", "it", "pt", "ja", "ko", "zh", "ar", "tr", "uk"]
TARGET_CHOICES = ["ru", "en", "kk", "uk", "de", "fr", "es", "it", "pt", "tr", "pl", "zh", "ja", "ko", "ar", "hi", "uz"]

# What the Yandex video translation API accepts (vot.js, Oct 2026)
YANDEX_SRC = {"auto", "ru", "en", "zh", "ko", "ar", "fr", "it", "es", "de", "ja"}
YANDEX_DST = {"ru", "en", "kk"}

# Default edge-tts voices; any other language falls back to the first voice of that locale
TTS_VOICES: dict[str, str] = {
    "ru": "ru-RU-DmitryNeural",
    "en": "en-US-AndrewNeural",
    "kk": "kk-KZ-DauletNeural",
    "uk": "uk-UA-OstapNeural",
    "de": "de-DE-ConradNeural",
    "fr": "fr-FR-HenriNeural",
    "es": "es-ES-AlvaroNeural",
    "it": "it-IT-DiegoNeural",
    "pt": "pt-BR-AntonioNeural",
    "tr": "tr-TR-AhmetNeural",
    "pl": "pl-PL-MarekNeural",
    "zh": "zh-CN-YunxiNeural",
    "ja": "ja-JP-KeitaNeural",
    "ko": "ko-KR-InJoonNeural",
    "ar": "ar-SA-HamedNeural",
    "hi": "hi-IN-MadhurNeural",
    "uz": "uz-UZ-SardorNeural",
}


def name(code: str) -> str:
    return NAMES.get(code, code)


def short(code: str) -> str:
    """Flag (or code) for compact labels like '🇬🇧→🇷🇺'."""
    label = NAMES.get(code)
    return label.split(" ", 1)[0] if label else code


def base(code: str | None) -> str | None:
    """'en-US' -> 'en'."""
    if not code:
        return None
    return code.replace("_", "-").split("-", 1)[0].lower()
