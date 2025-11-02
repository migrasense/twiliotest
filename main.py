from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.routing import APIRouter
import os, json, base64, asyncio, uuid, requests
from io import BytesIO
from deepgram import DeepgramClient
from deepgram import DeepgramClient, LiveOptions, LiveTranscriptionEvents, SpeakOptions


app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], allow_methods=["*"], allow_headers=["*"],
)
router = APIRouter()

# ======== CONFIG ========
DEEPGRAM_API_KEY = os.getenv("DEEPGRAM_API_KEY")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
PUBLIC_URL = os.getenv("PUBLIC_URL", "https://twiliotest-b4j9.onrender.com")
deepgram = DeepgramClient(DEEPGRAM_API_KEY)

@router.post("/twilio/voice")
async def twilio_voice():
    """
    Twilio webhook: greets caller, starts audio stream to Deepgram,
    and pauses to let the user respond.
    """
    # your deployed Render URL
    stream_url = "wss://twiliotest-b4j9.onrender.com/audio"

    # ✅ TwiML with stream opened *before* greeting
    twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <!-- Step 1: Start audio stream immediately -->
    <Start>
        <Stream url="{stream_url}"
                content-type="audio/ulaw"
                track="both_tracks">
            <Parameter name="caller" value="+18702735332"/>
            <Parameter name="receiver" value="+19094135795"/>
        </Stream>
    </Start>

    <!-- Step 2: Greeting plays while Deepgram is already connected -->
    <Say voice="Polly.Joanna">
        Hello! You’ve reached Servoice, your virtual assistant.
        How can I help you today?
    </Say>

    <!-- Step 3: Keep the line open for 45 seconds -->
    <Pause length="50"/>

    <!-- Step 4: Graceful end if no further input -->
    <Say>Thank you for calling. Goodbye!</Say>
    <Hangup/>
</Response>"""

    return Response(content=twiml, media_type="application/xml")


# ======== AI PIPELINE ========
async def generate_ai_reply(text: str) -> str:
    """Send transcript to Groq and return text reply."""
    headers = {"Authorization": f"Bearer {GROQ_API_KEY}"}
    payload = {
        "model": "mixtral-8x7b",  # or "llama3-8b"
        "messages": [{"role": "user", "content": text}],
        "temperature": 0.7,
    }
    r = requests.post(
        "https://api.groq.com/openai/v1/chat/completions",
        json=payload,
        headers=headers,
        timeout=30,
    )
    if r.status_code != 200:
        print("Groq error:", r.text)
        return "Sorry, I had trouble processing that."
    return r.json()["choices"][0]["message"]["content"]

async def synthesize_tts(text: str) -> bytes:
    """Convert text → mu-law audio bytes."""
    try:
        buffer = bytearray()
        options = SpeakOptions(model="aura-asteria-en", encoding="mulaw", sample_rate=8000)
        speak = deepgram.speak.v("1").stream({"text": text}, options)
        for chunk in speak:
            buffer.extend(chunk)
        return bytes(buffer)
    except Exception as e:
        print("TTS error:", e)
        return b""

# ======== WEBSOCKET HANDLER ========
@app.websocket("/audio")
async def audio_stream(websocket: WebSocket):
    """Handles live Twilio ↔ Deepgram ↔ Groq ↔ Deepgram TTS ↔ Twilio (continuous conversation)."""
    await websocket.accept()
    print("✅ WebSocket connected")

    dg_socket = deepgram.listen.live.v("1")

    # ---- Process Transcript ----
    async def process_transcript(transcript):
        transcript = transcript.strip()
        if not transcript:
            return
        print(f"🗣️ Heard: {transcript}")

        # Generate AI reply
        ai_reply = await generate_ai_reply(transcript)
        print(f"🤖 AI: {ai_reply}")

        # Stream TTS audio back to Twilio in small packets (real-time playback)
        try:
            options = SpeakOptions(model="aura-asteria-en", encoding="mulaw", sample_rate=8000)
            stream = deepgram.speak.v("1").stream({"text": ai_reply}, options)
            for chunk in stream:
                payload = base64.b64encode(chunk).decode("utf-8")
                await websocket.send_json({"event": "media", "media": {"payload": payload}})
                await asyncio.sleep(0.02)  # allow Twilio to buffer/play each small chunk

            # ⏸️ Add small pause before allowing next user input
            await asyncio.sleep(len(ai_reply.split()) * 0.05)

            print("🎧 Finished streaming reply audio to Twilio")
        except Exception as e:
            print("TTS streaming error:", e)

    # ---- Deepgram Transcript Event ----
    def on_transcript(_, result, **kwargs):
        try:
            alt = result.channel.alternatives[0]
            if alt.transcript and getattr(result, "is_final", False):
                # ✅ Schedule the coroutine correctly
                loop = asyncio.get_event_loop()
                if loop.is_running():
                    asyncio.create_task(process_transcript(alt.transcript))
                else:
                    loop.run_until_complete(process_transcript(alt.transcript))
        except Exception as e:
            print("Transcript error:", e)


    # ---- Deepgram Events ----
    dg_socket.on(LiveTranscriptionEvents.Open, lambda *_: print("🎧 Deepgram connected"))
    dg_socket.on(LiveTranscriptionEvents.Close, lambda *_: print("👋 Deepgram closed"))
    dg_socket.on(LiveTranscriptionEvents.Error, lambda *_: print("❌ Deepgram error"))
    dg_socket.on(LiveTranscriptionEvents.Transcript, on_transcript)

    # Start streaming to Deepgram
    dg_socket.start(LiveOptions(model="nova-2-general", encoding="mulaw", sample_rate=8000))

    try:
        while True:
            message = await websocket.receive()
            data = message.get("text") or message.get("bytes")

            if isinstance(data, str):
                event = json.loads(data)
                if event.get("event") == "media":
                    audio_payload = base64.b64decode(event["media"]["payload"])
                    dg_socket.send(audio_payload)
                elif event.get("event") == "stop":
                    print("🛑 Twilio stopped")
                    break
            elif isinstance(data, (bytes, bytearray)):
                dg_socket.send(data)

            await asyncio.sleep(0.01)

    except WebSocketDisconnect:
        print("❌ WebSocket disconnected")
    finally:
        dg_socket.finish()
        print("✅ Deepgram finished")




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
