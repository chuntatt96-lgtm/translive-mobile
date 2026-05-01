"""TransLive Mobile — FastAPI backend server."""

import asyncio
import json
import os
from pathlib import Path

import websockets
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

app = FastAPI()

# Allow all origins for mobile access
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

DEEPGRAM_API_KEY = os.environ.get("DEEPGRAM_API_KEY", "")
DEEPGRAM_URL = (
    "wss://api.deepgram.com/v1/listen"
    "?encoding=linear16&sample_rate=16000&model=nova-3"
    "&punctuate=true&smart_format=true"
)

STATIC_DIR = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/")
async def index():
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/health")
async def health():
    return JSONResponse({"status": "ok", "api_key_set": bool(DEEPGRAM_API_KEY)})


@app.websocket("/ws/transcribe")
async def transcribe_ws(client_ws: WebSocket):
    await client_ws.accept()

    if not DEEPGRAM_API_KEY:
        await client_ws.send_json({"error": "DEEPGRAM_API_KEY not set on server"})
        await client_ws.close()
        return

    # Read config
    try:
        config_msg = await asyncio.wait_for(client_ws.receive_text(), timeout=10)
        config = json.loads(config_msg)
    except Exception:
        config = {}

    source_lang = config.get("source", "auto")
    target_lang = config.get("target", "en")

    headers = {"Authorization": f"Token {DEEPGRAM_API_KEY}"}

    try:
        async with websockets.connect(
            DEEPGRAM_URL,
            additional_headers=headers,
            ping_interval=20,
            ping_timeout=10,
        ) as dg_ws:

            async def from_deepgram():
                from deep_translator import GoogleTranslator
                async for message in dg_ws:
                    try:
                        data = json.loads(message)
                        if data.get("type") in ("Metadata", "SpeechStarted", "UtteranceEnd"):
                            continue
                        text = (data.get("channel") or {}).get(
                            "alternatives", [{}])[0].get("transcript", "")
                        is_final = data.get("is_final", False)
                        if not text.strip():
                            continue
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

            async def from_client():
                while True:
                    try:
                        data = await client_ws.receive_bytes()
                        await dg_ws.send(data)
                    except (WebSocketDisconnect, Exception):
                        break

            await asyncio.gather(from_deepgram(), from_client(), return_exceptions=True)

    except Exception as e:
        try:
            await client_ws.send_json({"error": str(e)})
        except Exception:
            pass
