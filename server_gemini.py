import os
import json
import asyncio
import base64
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from dotenv import load_dotenv
import websockets

load_dotenv()

app = FastAPI()

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
GEMINI_WS_URL = (
    "wss://generativelanguage.googleapis.com/ws/google.ai.generativelanguage.v1beta.GenerativeService.BidiGenerateContent"
    f"?key={GEMINI_API_KEY}"
)

SYSTEM_PROMPT = """## Identity
sen konuşkan bir sohbet botusun. kullanıcı ile eğlenceli bir şekilde sohbet et
{{end Call}}

## Important Instructions
- Always speak in Turkish
- Start the conversation by greeting the caller: "Robotpos destek hattına hoş geldiniz, adınızı alabilir miyim?"
- Follow the steps strictly in order
- Keep responses short and natural
"""


@app.get("/", response_class=HTMLResponse)
async def index():
    with open("static/gemini.html", "r", encoding="utf-8") as f:
        return f.read()


@app.websocket("/ws")
async def websocket_proxy(client_ws: WebSocket):
    """Proxy WebSocket between browser and Gemini Live API."""
    await client_ws.accept()

    try:
        async with websockets.connect(
            GEMINI_WS_URL,
            additional_headers={"Content-Type": "application/json"},
            max_size=None,
            ping_interval=30,
            ping_timeout=10,
        ) as gemini_ws:
            # Send setup message
            setup_msg = {
                "setup": {
                    "model": "models/gemini-2.5-flash-native-audio-latest",
                    "generationConfig": {
                        "responseModalities": ["AUDIO"],
                        "speechConfig": {
                            "voiceConfig": {
                                "prebuiltVoiceConfig": {
                                    "voiceName": "Kore"
                                }
                            }
                        },
                    },
                    "systemInstruction": {
                        "parts": [{"text": SYSTEM_PROMPT}]
                    },
                }
            }
            await gemini_ws.send(json.dumps(setup_msg))

            # Wait for setup complete
            setup_response = await gemini_ws.recv()
            setup_data = json.loads(setup_response)
            print("Gemini setup response:", json.dumps(setup_data, indent=2)[:200])

            # Notify client that setup is complete
            await client_ws.send_json({"type": "setup_complete"})

            # Run two tasks: client->gemini and gemini->client
            async def client_to_gemini():
                try:
                    while True:
                        data = await client_ws.receive_text()
                        msg = json.loads(data)

                        if msg.get("type") == "audio":
                            # Forward audio to Gemini
                            gemini_msg = {
                                "realtimeInput": {
                                    "mediaChunks": [
                                        {
                                            "mimeType": "audio/pcm;rate=16000",
                                            "data": msg["data"],
                                        }
                                    ]
                                }
                            }
                            await gemini_ws.send(json.dumps(gemini_msg))

                        elif msg.get("type") == "end":
                            break

                except WebSocketDisconnect:
                    pass

            async def gemini_to_client():
                try:
                    async for message in gemini_ws:
                        data = json.loads(message)

                        # Extract audio data from server response
                        server_content = data.get("serverContent")
                        if server_content:
                            model_turn = server_content.get("modelTurn")
                            if model_turn and "parts" in model_turn:
                                for part in model_turn["parts"]:
                                    if "inlineData" in part:
                                        inline = part["inlineData"]
                                        await client_ws.send_json({
                                            "type": "audio",
                                            "data": inline.get("data", ""),
                                            "mimeType": inline.get("mimeType", ""),
                                        })
                                    elif "text" in part:
                                        await client_ws.send_json({
                                            "type": "text",
                                            "text": part["text"],
                                        })

                            # Check if turn is complete
                            if server_content.get("turnComplete"):
                                await client_ws.send_json({"type": "turn_complete"})

                        # Handle tool calls if any
                        tool_call = data.get("toolCall")
                        if tool_call:
                            await client_ws.send_json({
                                "type": "tool_call",
                                "data": tool_call,
                            })

                except websockets.exceptions.ConnectionClosed:
                    pass

            await asyncio.gather(client_to_gemini(), gemini_to_client())

    except Exception as e:
        print(f"WebSocket error: {e}")
        try:
            await client_ws.send_json({"type": "error", "message": str(e)})
        except Exception:
            pass


app.mount("/static", StaticFiles(directory="static"), name="static")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8081)
