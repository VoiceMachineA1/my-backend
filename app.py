"""
Nightly / Dentist Office Twilio IVR Backend (FastAPI + Twilio)

WHAT THIS DOES
- Twilio sends an HTTP POST to /twilio/voice when your Twilio phone number receives a call
- This app returns TwiML (XML) that speaks a menu and gathers one keypad digit
- Twilio POSTs the selected digit to /twilio/menu
- Options:
  1) Text appointment/scheduling link (NexHealth)
  2) Read office hours, then return to menu
  3) Text directions link (Google Maps)
  4) Record voicemail, then POST to /twilio/recording-complete
  0) Transfer call to on-call number

IMPORTANT SETUP (DO THIS OUTSIDE CODE)
1) requirements.txt must include:
   fastapi
   uvicorn
   twilio
   python-multipart

2) Render Environment Variables (Render dashboard -> Service -> Environment):
   TWILIO_ACCOUNT_SID    = ACxxxxxxxxxxxxxxxxxxxxxxxxxxxx
   TWILIO_AUTH_TOKEN     = xxxxxxxxxxxxxxxxxxxxxxxxxxxxx
   TWILIO_PHONE_NUMBER   = +1xxxxxxxxxx   (THIS IS YOUR TWILIO NUMBER that sends SMS)

3) Twilio Phone Number webhook:
   Twilio Console -> Phone Numbers -> (your number) -> Voice:
   "A CALL COMES IN" -> Webhook -> HTTP POST:
     https://<your-render-domain>/twilio/voice

WHY YOU WERE GETTING "APPLICATION ERROR"
- If your server returns HTTP 500 (crash), Twilio plays "Application error"
- This code is written to NEVER crash on SMS failures:
  it logs the error + speaks a fallback message instead.

NOTE ABOUT TWILIO TRIAL / TOLL-FREE
- Trial accounts often can only SMS verified destination numbers
- Toll-free numbers often require toll-free verification to reliably send SMS
"""

import os
from typing import Tuple

from fastapi import FastAPI, Request
from fastapi.responses import Response
from twilio.rest import Client
from twilio.base.exceptions import TwilioRestException
from twilio.twiml.voice_response import VoiceResponse, Gather

app = FastAPI()

# =========================
# ✅ UPDATE THESE CONSTANTS
# =========================

PRACTICE_NAME = "Luke's Office"  # UPDATE: Dentist/practice name

OFFICE_HOURS_TEXT = "Our office hours are Monday through Friday, 8 A M to 5 P M."  # UPDATE

# UPDATE: scheduling link (NexHealth or your existing booking page)
SCHEDULING_LINK = "https://app.nexhealth.com/appt/sonoran-hills-dental"

# UPDATE: directions link (short Google Maps query link is best)
DIRECTIONS_LINK = "https://maps.google.com/?q=4909+E+Chandler+Blvd+Ste+501,+Phoenix,+AZ+85048"

# UPDATE: real on-call phone to transfer emergencies to (NOT the Twilio number)
ON_CALL_NUMBER = "+18043109383"

# Optional: voicemail max length (seconds)
MAX_VOICEMAIL_SECONDS = 120


# =========================================================
# ✅ Twilio SMS helper (SAFE: never throws -> no 500 errors)
# =========================================================

def send_sms_safe(to_number: str, body: str) -> Tuple[bool, str]:
    """
    Sends SMS using Twilio REST API.
    Returns (ok, message). Never raises an exception.

    Most common failure causes:
    - Missing env vars in Render
    - Using a TWILIO_PHONE_NUMBER that is not owned by your Twilio account
    - Trial account: destination number not verified
    - Toll-free SMS restrictions / verification required
    """
    try:
        sid = os.getenv("TWILIO_ACCOUNT_SID")
        token = os.getenv("TWILIO_AUTH_TOKEN")
        from_phone = os.getenv("TWILIO_PHONE_NUMBER")

        if not sid or not token or not from_phone:
            return False, "Missing Render env vars: TWILIO_ACCOUNT_SID / TWILIO_AUTH_TOKEN / TWILIO_PHONE_NUMBER"

        client = Client(sid, token)
        client.messages.create(
            to=to_number,
            from_=from_phone,
            body=body,
        )
        return True, "Sent"
    except TwilioRestException as e:
        return False, f"TwilioRestException: {e}"
    except Exception as e:
        return False, f"Exception: {e}"


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
    Twilio should POST here when a call comes in.

    Returns TwiML:
    - speaks a menu
    - gathers 1 digit
    - posts to /twilio/menu
    """
    vr = VoiceResponse()

    gather = Gather(
        num_digits=1,
        action="/twilio/menu",   # relative path -> Twilio calls same host
        method="POST",
        timeout=7,
    )

    # UPDATE: wording as desired
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
    Handles the menu digit. Twilio posts form data including 'Digits'.
    """
    form = await request.form()
    digit = form.get("Digits", "")

    vr = VoiceResponse()

    if digit == "1":
        vr.say("Okay. We'll text you a link to request an appointment now.")
        vr.redirect("/twilio/send-scheduling-link", method="POST")

    elif digit == "2":
        vr.say(OFFICE_HOURS_TEXT + " If this is an emergency, press 0 to reach on call staff.")
        vr.redirect("/twilio/voice", method="POST")

    elif digit == "3":
        vr.say("Okay. We'll text you directions now.")
        vr.redirect("/twilio/send-directions-link", method="POST")

    elif digit == "4":
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
        vr.say("Connecting you now.")
        vr.dial(ON_CALL_NUMBER)

    else:
        vr.say("Sorry, that wasn't a valid selection.")
        vr.redirect("/twilio/voice", method="POST")

    return Response(content=str(vr), media_type="application/xml")


# =========================
# SMS endpoints
# =========================

@app.post("/twilio/send-scheduling-link")
async def send_scheduling_link(request: Request):
    """
    Texts the caller a scheduling link.
    IMPORTANT: This endpoint NEVER crashes if SMS fails.
    """
    form = await request.form()
    from_number = form.get("From")  # caller's phone number

    ok, err = send_sms_safe(
        from_number,
        f"Book an appointment with {PRACTICE_NAME}: {SCHEDULING_LINK}"
    )

    # This prints into Render logs so you can see the real Twilio error
    print("Scheduling SMS:", "OK" if ok else "FAILED", "|", err)

    vr = VoiceResponse()
    if ok:
        vr.say("Text sent. Thanks for calling. Goodbye.")
    else:
        # Fallback so Twilio does not play "application error"
        vr.say("We could not send a text right now. Please visit our website to book. Goodbye.")
    vr.hangup()

    return Response(content=str(vr), media_type="application/xml")


@app.post("/twilio/send-directions-link")
async def send_directions_link(request: Request):
    """
    Texts the caller a directions link.
    IMPORTANT: This endpoint NEVER crashes if SMS fails.
    """
    form = await request.form()
    from_number = form.get("From")

    ok, err = send_sms_safe(
        from_number,
        f"Directions to {PRACTICE_NAME}: {DIRECTIONS_LINK}"
    )

    print("Directions SMS:", "OK" if ok else "FAILED", "|", err)

    vr = VoiceResponse()
    if ok:
        vr.say("Directions text sent. Goodbye.")
    else:
        vr.say("We could not send directions by text right now. Goodbye.")
    vr.hangup()

    return Response(content=str(vr), media_type="application/xml")


# =========================
# Voicemail recording callback
# =========================

@app.post("/twilio/recording-complete")
async def recording_complete(request: Request):
    """
    Called by Twilio after voicemail recording ends.
    Twilio posts form data including:
      - RecordingUrl
      - From
    """
    form = await request.form()
    recording_url = form.get("RecordingUrl")
    from_number = form.get("From")

    # Log so you can see it in Render logs
    print("Voicemail received from:", from_number, "| RecordingUrl:", recording_url)

    # OPTIONAL: Notify on-call by SMS with recording link
    # ok, err = send_sms_safe(ON_CALL_NUMBER, f"New voicemail from {from_number}: {recording_url}.mp3")
    # print("Voicemail notify SMS:", "OK" if ok else "FAILED", "|", err)

    vr = VoiceResponse()
    vr.say("Thanks. Your message has been recorded. Goodbye.")
    vr.hangup()

    return Response(content=str(vr), media_type="application/xml")
