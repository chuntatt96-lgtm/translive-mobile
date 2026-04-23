"""TransLive Mobile — FastAPI backend server."""

import os
import asyncio
from pathlib import Path
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
import websockets
import json

app = FastAPI()

DEEPGRAM_API_KEY = os.environ.get("DEEPGRAM_API_KEY", "")
DEEPGRAM_URL = (
    "wss://api.deepgram.com/v1/listen"
    "?encoding=linear16&sample_rate=16000&model=nova-3"
    "&punctuate=true&smart_format=true"
)

# Serve static files
app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")


@app.get("/")
async def index():
    return FileResponse(Path(__file__).parent / "static" / "index.html")


@app.websocket("/ws/transcribe")
async def transcribe_ws(client_ws: WebSocket):
    """Proxy audio from browser → Deepgram → translated text back to browser."""
    await client_ws.accept()

    from deep_translator import GoogleTranslator
    import unicodedata

    # Read config message first
    config_msg = await client_ws.receive_text()
    config = json.loads(config_msg)
    source_lang = config.get("source", "auto")
    target_lang = config.get("target", "en")

    headers = {"Authorization": f"Token {DEEPGRAM_API_KEY}"}

    try:
        async with websockets.connect(DEEPGRAM_URL, additional_headers=headers) as dg_ws:

            async def receive_from_deepgram():
                async for message in dg_ws:
                    try:
                        data = json.loads(message)
                        msg_type = data.get("type", "")
                        if msg_type in ("Metadata", "SpeechStarted"):
                            continue
                        text = data.get("channel", {}).get("alternatives", [{}])[0].get("transcript", "")
                        is_final = data.get("is_final", False)
                        if not text.strip():
                            continue

                        # Translate
                        try:
                            translated = await asyncio.get_event_loop().run_in_executor(
                                None,
                                lambda t=text: GoogleTranslator(
                                    source=source_lang, target=target_lang
                                ).translate(t)
                            )
                        except Exception:
                            translated = text

                        await client_ws.send_json({
                            "original": text,
                            "translated": translated or text,
                            "is_final": is_final,
                        })
                    except Exception:
                        continue

            async def receive_from_client():
                while True:
                    try:
                        data = await client_ws.receive_bytes()
                        await dg_ws.send(data)
                    except WebSocketDisconnect:
                        break
                    except Exception:
                        break

            await asyncio.gather(
                receive_from_deepgram(),
                receive_from_client(),
                return_exceptions=True,
            )

    except Exception as e:
        try:
            await client_ws.send_json({"error": str(e)})
        except Exception:
            pass
