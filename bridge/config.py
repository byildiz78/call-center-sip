import os
from dotenv import load_dotenv

load_dotenv()

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
GEMINI_MODEL = "models/gemini-2.5-flash-native-audio-latest"
GEMINI_VOICE = "Kore"
GEMINI_WS_URL = (
    "wss://generativelanguage.googleapis.com/ws/"
    "google.ai.generativelanguage.v1beta.GenerativeService.BidiGenerateContent"
    f"?key={GEMINI_API_KEY}"
)

BRIDGE_HOST = os.getenv("BRIDGE_HOST", "0.0.0.0")
BRIDGE_PORT = int(os.getenv("BRIDGE_PORT", "8081"))

DB_PATH = os.getenv("DB_PATH", "data/calls.db")
RECORDINGS_DIR = os.getenv("RECORDINGS_DIR", "recordings")

TICKET_WEBHOOK_URL = os.getenv("TICKET_WEBHOOK_URL", "")
PUBLIC_URL = os.getenv("PUBLIC_URL", "")

GREETING_FILE = os.getenv("GREETING_FILE", "data/greeting.txt")
PROMPT_FILE = os.getenv("PROMPT_FILE", "data/system_prompt.txt")
SETTINGS_FILE = os.getenv("SETTINGS_FILE", "data/settings.json")

_DEFAULT_GREETING = "Robotpos destek hattına hoş geldiniz, adınızı alabilir miyim?"

_DEFAULT_PROMPT = """## Identity
As a call center assistant, your primary duty is to obtain certain information from the caller by following the steps below in order. Do not repeat the information you obtain back to the caller. Focus only on gathering the necessary information. Keep the conversation as short as possible and stay focused on collecting the caller's details. Your manner of speaking should be fluent. Our company's name is Robotpos, and we are a very large firm that provides POS solutions for restaurants. Because our customers are restaurants, it is quite normal for business names to include terms such as "Köfteci," "Dönerci," "Kahveci," etc.; do not wait long between the questions you ask, adhere to the text in quotation marks below, and do not use the customer's name after saying thank you. Do not thank them after receiving the problem description.
Verdiğim Metnin dışında başka bir kelime cümle söyleme.

## Response Guardrails
Karşılama mesajında müşterinin adını aldıktan hemen sonra
Arayan kişi eğer sadece adını söylerse, ona bey yada hanım diye hitap etme, "işletmenizin adını alabilir miyim ?" diye sor sadece. ses erkek sesi ise bey, kadın sesi ise hanım diye hitap et.
İşletme Adını Alma:
"{{Müşteri Adı}} Bey/Hanım işletmenizin adını alabilir miyim?"

Sorun Tanımını Alma:
"Teşekkür ederim, lütfen yaşadığınız sorunu veya talebinizi kısaca anlatır mısınız?"

"Destek talebinizi sistemimize kayıt ettim, size yardımcı olabilecek uzman destek personelimiz kısa süre içinde sizinle iletişime geçecek, Dilerseniz esemes ile gelen bağlantıya tıklayarak WhatsApp destek hattımız ile doğrudan iletişime geçebilirsiniz, İyi günler dilerim."
{{end Call}}

## Important Instructions
- Always speak in Turkish
- Follow the steps strictly in order
- Keep responses short and natural
"""


def _load_file(path: str, default: str) -> str:
    try:
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                content = f.read().strip()
                if content:
                    return content
    except Exception:
        pass
    return default


def _save_file(path: str, content: str):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)


def get_greeting() -> str:
    return _load_file(GREETING_FILE, _DEFAULT_GREETING)


def save_greeting(text: str):
    _save_file(GREETING_FILE, text)


def get_system_prompt() -> str:
    return _load_file(PROMPT_FILE, _DEFAULT_PROMPT)


def save_system_prompt(prompt: str):
    _save_file(PROMPT_FILE, prompt)


# --- Settings (max duration, end phrase) ---
import json as _json

_DEFAULT_SETTINGS = {
    "max_call_duration": 90,
    "end_call_phrase": "iyi günler dilerim",
    "webhook_url": "",
}


def get_settings() -> dict:
    try:
        if os.path.exists(SETTINGS_FILE):
            with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
                return {**_DEFAULT_SETTINGS, **_json.load(f)}
    except Exception:
        pass
    return _DEFAULT_SETTINGS.copy()


def save_settings(settings: dict):
    current = get_settings()
    current.update(settings)
    os.makedirs(os.path.dirname(SETTINGS_FILE) or ".", exist_ok=True)
    with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
        _json.dump(current, f, ensure_ascii=False)
