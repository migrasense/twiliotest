from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.routing import APIRouter
import os, json, base64, asyncio, uuid, requests
from io import BytesIO
from deepgram import DeepgramClient, SpeakOptions, LiveOptions
from deepgram.core import EventType

app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], allow_methods=["*"], allow_headers=["*"],
)
router = APIRouter()

# ======== CONFIG ========
DEEPGRAM_API_KEY = os.getenv("DEEPGRAM_API_KEY")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
PUBLIC_URL = os.getenv("PUBLIC_URL", "https://your-render-app.onrender.com")
deepgram = DeepgramClient(DEEPGRAM_API_KEY)

# ======== TWILIO ENTRY POINT ========
@router.post("/twilio/voice")
async def twilio_voice():
    """TwiML that tells Twilio to stream audio to /audio."""
    stream_url = f"wss://{PUBLIC_URL.replace('https://','').replace('http://','')}/audio"
    twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
    <Response>
        <Say voice="Polly.Joanna">Hello! You are connected to the AI assistant.</Say>
        <Start>
            <Stream url="{stream_url}">
                <Parameter name="caller" value="+18702735332"/>
                <Parameter name="receiver" value="+19094135795"/>
            </Stream>
        </Start>
    </Response>"""
    return Response(content=twiml, media_type="application/xml")

# ======== AI PIPELINE ========
async def generate_ai_reply(text: str) -> str:
    """Send transcript to Groq and get a reply."""
    headers = {"Authorization": f"Bearer {GROQ_API_KEY}"}
    payload = {
        "model": "mixtral-8x7b",
        "messages": [{"role": "user", "content": text}],
        "temperature": 0.7,
    }
    r = requests.post(
        "https://api.groq.com/openai/v1/chat/completions",
        json=payload, headers=headers, timeout=30
    )
    if r.status_code != 200:
        print("Groq error:", r.text)
        return "Sorry, I encountered an error."
    return r.json()["choices"][0]["message"]["content"]

async def synthesize_tts(text: str) -> bytes:
    """Convert AI reply → mu-law 8kHz bytes for Twilio."""
    try:
        buffer = BytesIO()
        options = SpeakOptions(
            model="aura-asteria-en",
            encoding="mulaw",
            sample_rate=8000
        )
        # v3 SDK streaming interface
        deepgram.speak.v("1").stream(buffer, {"text": text}, options)
        buffer.seek(0)
        return buffer.read()
    except Exception as e:
        print("TTS error:", e)
        return b""

# ======== WEBSOCKET HANDLER ========
@router.websocket("/audio")
async def audio_stream(websocket: WebSocket):
    await websocket.accept()
    print("✅ WebSocket connected")

    dg_socket = deepgram.listen.live.v("1")

    async def handle_transcript(transcript: str):
        print(f"🗣️ Heard: {transcript}")
        await process_transcript(transcript, websocket)

    def on_transcript(event, result, **kwargs):
        transcript = result["channel"]["alternatives"][0]["transcript"]
        if transcript.strip():
            asyncio.run_coroutine_threadsafe(
                handle_transcript(transcript),
                asyncio.get_event_loop()
            )

    def on_error(event, error, **kwargs):
        print(f"❌ Deepgram error: {error}")

    dg_socket.on(EventType.TRANSCRIPT_RECEIVED, on_transcript)
    dg_socket.on(EventType.ERROR, on_error)

    dg_socket.start(
        LiveOptions(
            model="nova-2-general",
            encoding="mulaw",
            sample_rate=8000,
            channels=1,
            punctuate=True,
            interim_results=False
        )
    )
    print("🎧 Deepgram listening...")

    try:
        while True:
            msg = await websocket.receive()
            data = msg.get("text") or msg.get("bytes")
            if isinstance(data, str):
                event = json.loads(data)
                if event.get("event") == "media":
                    audio_payload = base64.b64decode(event["media"]["payload"])
                    dg_socket.send(audio_payload)
                elif event.get("event") == "stop":
                    print("🛑 Twilio stream stopped")
                    break
            elif isinstance(data, (bytes, bytearray)):
                dg_socket.send(data)
    except WebSocketDisconnect:
        print("❌ WebSocket disconnected")
    finally:
        dg_socket.finish()
        print("✅ Deepgram socket closed")

# ======== CORE LOGIC ========
async def process_transcript(text: str, websocket: WebSocket):
    """Transcript → Groq → TTS → return to Twilio."""
    try:
        ai_reply = await generate_ai_reply(text)
        print(f"🤖 AI: {ai_reply}")
        audio_bytes = await synthesize_tts(ai_reply)
        if audio_bytes:
            payload = base64.b64encode(audio_bytes).decode("utf-8")
            await websocket.send_json({
                "event": "media",
                "media": {"payload": payload}
            })
            print("🎧 Sent synthesized audio to Twilio")
    except Exception as e:
        print("Error in process_transcript:", e)

# ======== REGISTER ROUTES ========
app.include_router(router)
