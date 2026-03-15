import os
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from dotenv import load_dotenv
import httpx

load_dotenv()

app = FastAPI()

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

SYSTEM_PROMPT = """## Identity
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
- Start the conversation by greeting the caller: "Robotpos destek hattına hoş geldiniz, adınızı alabilir miyim?"
- Follow the steps strictly in order
- Keep responses short and natural
"""


@app.get("/", response_class=HTMLResponse)
async def index():
    with open("static/index.html", "r", encoding="utf-8") as f:
        return f.read()


@app.post("/api/session")
async def create_session():
    """Create an ephemeral session token for the Realtime API."""
    async with httpx.AsyncClient() as client:
        response = await client.post(
            "https://api.openai.com/v1/realtime/sessions",
            headers={
                "Authorization": f"Bearer {OPENAI_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "model": "gpt-4o-realtime-preview-2024-12-17",
                "voice": "ash",
                "instructions": SYSTEM_PROMPT,
                "input_audio_transcription": {"model": "whisper-1"},
                "turn_detection": {
                    "type": "server_vad",
                    "threshold": 0.5,
                    "prefix_padding_ms": 300,
                    "silence_duration_ms": 500,
                },
            },
        )
        data = response.json()
        return JSONResponse(data)


app.mount("/static", StaticFiles(directory="static"), name="static")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8080)
