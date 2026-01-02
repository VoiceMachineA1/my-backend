"""
Nightly / Dentist Office Twilio IVR Backend (FastAPI + Twilio)

✅ WHAT THIS DOES
- Twilio calls POST /twilio/voice when your Twilio phone number receives a call
- Your backend returns TwiML (XML) telling Twilio what to say/do
- Caller presses keys (DTMF). Twilio posts digits to /twilio/menu
- Option 1: Text scheduling link (NexHealth link)
- Option 3: Text directions link (Google Maps link)
- Option 4: Record voicemail and call /twilio/recording-complete when finished
- Option 0: Dial (transfer) to an on-call number

✅ WHAT YOU MUST UPDATE (search for "UPDATE THIS")
1) Update constants (practice name, hours, links, numbers)
2) requirements.txt must include: twilio
3) Render Environment Variables:
   - TWILIO_ACCOUNT_SID
   - TWILIO_AUTH_TOKEN
   - TWILIO_PHONE_NUMBER

✅ TWILIO CONFIG
Twilio Console → Phone Numbers → (your number) → Voice:
A CALL COMES IN → Webhook → POST:
  https://<your-render-domain>/twilio/voice
"""

import os
from fastapi import FastAPI, Request
from fastapi.responses import Response
from twilio.twiml.voice_response import VoiceResponse, Gather
from twilio.rest import Client

app = FastAPI()

# =========================
# ✅ UPDATE THIS SECTION
# =========================

PRACTICE_NAME = "Luke's Office"  # UPDATE THIS: dentist/practice name

OFFICE_HOURS_TEXT = (
    "Our office hours are Monday through Friday, 8 A M to 5 P M."
)  # UPDATE THIS: real hours

# UPDATE THIS: scheduling link (you said NexHealth)
SCHEDULING_LINK = "https://app.nexhealth.com/appt/sonoran-hills-dental"

# UPDATE THIS: directions link (keep it short; long URLs can be annoying)
DIRECTIONS_LINK = "https://maps.google.com/?q=4909+E+Chandler+Blvd+Ste+501,+Phoenix,+AZ+85048"

# UPDATE THIS: the real on-call number to transfer emergencies to
ON_CALL_NUMBER = "+18043109383"

# Optional: voicemail max length (seconds)
MAX_VOICEMAIL_SECONDS = 120


# =========================
# ✅ REQUIRED ENV VARS (Render)
# =========================
# In Render → Service → Environment add these three keys:
#   TWILIO_ACCOUNT_SID   = ACxxxxxxxxxxxxxxxxxxxxxxxxxxxx
#   TWILIO_AUTH_TOKEN    = xxxxxxxxxxxxxxxxxxxxxxxxxxxxx
#   TWILIO_PHONE_NUMBER  = +1xxxxxxxxxx
#
# NOTE: Do NOT hardcode secrets in code. Keep them only in Render.


def send_sms(to_number: str, body: str) -> None:
    """
    Sends an SMS using Twilio REST API.

    If env vars are missing, this will raise KeyError.
    """
    client = Client(
        os.environ["TWILIO_ACCOUNT_SID"],
        os.environ["TWILIO_AUTH_TOKEN"],
    )
    client.messages.create(
        to=to_number,
        from_=os.environ["TWILIO_PHONE_NUMBER"],
        body=body,
    )


# =========================
# Health endpoints
# =========================

@app.get("/")
def root():
    return {"message": f"{PRACTICE_NAME} backend is running."}

@app.get("/health")
def health():
    return {"ok": True}


# =========================
# Twilio Voice Webhooks
# =========================

@app.post("/twilio/voice")
async def twilio_voice(request: Request):
    """
    MAIN ENTRYPOINT FOR INCOMING CALLS

    Twilio phone number config must be set to:
      A CALL COMES IN → Webhook (POST) → https://<your-render-domain>/twilio/voice

    Returns TwiML that:
    - reads a menu
    - gathers ONE digit
    - posts that digit to /twilio/menu
    """
    vr = VoiceResponse()

    gather = Gather(
        num_digits=1,
        action="/twilio/menu",
        method="POST",
        timeout=7,
    )

    # UPDATE THIS SCRIPT if you want different wording
    gather.say(
        f"Thanks for calling {PRACTICE_NAME}. "
        "If you'd like to make an appointment, press 1. "
        "For office hours, press 2. "
        "For directions, press 3. "
        "To leave a message, press 4. "
        "To talk to the on call staff, press 0.",
        voice="alice",
    )
    vr.append(gather)

    # If no input, loop back
    vr.say("Sorry, I didn't get that.")
    vr.redirect("/twilio/voice", method="POST")

    return Response(content=str(vr), media_type="application/xml")


@app.post("/twilio/menu")
async def twilio_menu(request: Request):
    """
    HANDLES THE MENU DIGIT THE CALLER PRESSES

    Twilio will POST a form field 'Digits'
    """
    form = await request.form()
    digit = form.get("Digits", "")

    vr = VoiceResponse()

    if digit == "1":
        # Text scheduling link
        vr.say("Okay. We'll text you a link to request an appointment now.")
        vr.redirect("/twilio/send-scheduling-link", method="POST")

    elif digit == "2":
        # Office hours
        vr.say(OFFICE_HOURS_TEXT + " If this is an emergency, press 0 to reach on call staff.")
        vr.redirect("/twilio/voice", method="POST")

    elif digit == "3":
        # Text directions link
        vr.say("Okay. We'll text you directions now.")
        vr.redirect("/twilio/send-directions-link", method="POST")

    elif digit == "4":
        # Voicemail recording
        vr.say(
            "Please leave your name and callback number after the tone. "
            "Please do not include sensitive medical details. "
            "When you're done, you can hang up."
        )
        vr.record(
            action="/twilio/recording-complete",
            method="POST",
            max_length=MAX_VOICEMAIL_SECONDS,
            play_beep=True,
        )

    elif digit == "0":
        # Transfer call to on-call staff
        vr.say("Connecting you now.")
        vr.dial(ON_CALL_NUMBER)

    else:
        vr.say("Sorry, that wasn't a valid selection.")
        vr.redirect("/twilio/voice", method="POST")

    return Response(content=str(vr), media_type="application/xml")


# =========================
# SMS helper endpoints (YOUR SERVER PATHS)
# =========================

@app.post("/twilio/send-scheduling-link")
async def send_scheduling_link(request: Request):
    """
    Sends the caller a scheduling link by SMS.

    UPDATE THIS:
    - SCHEDULING_LINK (top of file)
    """
    form = await request.form()
    from_number = form.get("From")  # caller's phone number

    send_sms(
        from_number,
        f"Book an appointment with {PRACTICE_NAME}: {SCHEDULING_LINK}"
    )

    vr = VoiceResponse()
    vr.say("Text sent. Thanks for calling. Goodbye.")
    vr.hangup()
    return Response(content=str(vr), media_type="application/xml")


@app.post("/twilio/send-directions-link")
async def send_directions_link(request: Request):
    """
    Sends the caller a directions link by SMS.

    UPDATE THIS:
    - DIRECTIONS_LINK (top of file)
    """
    form = await request.form()
    from_number = form.get("From")

    send_sms(
        from_number,
        f"Directions to {PRACTICE_NAME}: {DIRECTIONS_LINK}"
    )

    vr = VoiceResponse()
    vr.say("Directions text sent. Goodbye.")
    vr.hangup()
    return Response(content=str(vr), media_type="application/xml")


# =========================
# Voicemail recording callback
# =========================

@app.post("/twilio/recording-complete")
async def recording_complete(request: Request):
    """
    Called by Twilio after the caller finishes recording a voicemail.

    Twilio will include:
    - RecordingUrl (a URL to the audio)
    - From (caller number)

    Next steps (optional):
    - Notify staff with the recording URL
    - Store in DB
    """
    form = await request.form()
    recording_url = form.get("RecordingUrl")
    from_number = form.get("From")

    # OPTIONAL UPDATE:
    # If you want to text yourself the recording link, uncomment the line below.
    # send_sms(ON_CALL_NUMBER, f"New voicemail from {from_number}: {recording_url}.mp3")

    vr = VoiceResponse()
    vr.say("Thanks. Your message has been recorded. Goodbye.")
    vr.hangup()
    return Response(content=str(vr), media_type="application/xml")
