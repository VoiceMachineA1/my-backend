"""
Nightly / Dentist Office Twilio IVR Backend (FastAPI + Twilio) — KEYPAD-ONLY v2 (Prompt rewrite)

This version is identical in logic to your KEYPAD-ONLY v2, but ALL spoken menus/submenus
are rewritten to consistently follow: “Press X to …” in the exact order of options.

REQUIRED ENV VARS (Render dashboard -> Environment)
- TWILIO_ACCOUNT_SID
- TWILIO_AUTH_TOKEN
- TWILIO_PHONE_NUMBER        (your Twilio number for SMS sending)
- OFFICE_MANAGER_NUMBER      (office manager / inbox number to receive summaries)

OPTIONAL
- ON_CALL_NUMBER             (for live transfer, if you want to enable later)
"""

import os
from typing import Dict, Tuple, Any, Optional

from fastapi import FastAPI, Request
from fastapi.responses import Response
from twilio.rest import Client
from twilio.base.exceptions import TwilioRestException
from twilio.twiml.voice_response import VoiceResponse, Gather

app = FastAPI()

# =========================
# ✅ UPDATE THESE CONSTANTS
# =========================

PRACTICE_NAME = "Luke's Office"  # UPDATE
OFFICE_HOURS_TEXT = "Our office hours are Monday through Friday, 8 A M to 5 P M."  # UPDATE

# Booking link (NexHealth or other)
SCHEDULING_LINK = "https://app.nexhealth.com/appt/sonoran-hills-dental"

# Directions link (Google Maps)
DIRECTIONS_LINK = "https://maps.google.com/?q=4909+E+Chandler+Blvd+Ste+501,+Phoenix,+AZ+85048"

# Optional emergency transfer (if you later want it)
ON_CALL_NUMBER = os.getenv("ON_CALL_NUMBER", "").strip()

MAX_VOICEMAIL_SECONDS = 180

# Office notification target (SMS)
OFFICE_MANAGER_NUMBER = os.getenv("OFFICE_MANAGER_NUMBER", "").strip()

# ============
# Call state
# ============
# V1: in-memory store keyed by CallSid. (OK for testing; may reset on deploy/restart.)
CALL_STATE: Dict[str, Dict[str, Any]] = {}


def _get_state(call_sid: str) -> Dict[str, Any]:
    if not call_sid:
        call_sid = "UNKNOWN_CALLSID"
    if call_sid not in CALL_STATE:
        CALL_STATE[call_sid] = {}
    return CALL_STATE[call_sid]


# =========================================================
# ✅ Twilio SMS helper (SAFE: never throws -> no 500 errors)
# =========================================================
def send_sms_safe(to_number: str, body: str) -> Tuple[bool, str]:
    try:
        sid = os.getenv("TWILIO_ACCOUNT_SID")
        token = os.getenv("TWILIO_AUTH_TOKEN")
        from_phone = os.getenv("TWILIO_PHONE_NUMBER")

        if not sid or not token or not from_phone:
            return False, "Missing env vars: TWILIO_ACCOUNT_SID / TWILIO_AUTH_TOKEN / TWILIO_PHONE_NUMBER"

        if not to_number:
            return False, "Missing destination number"

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


def notify_office_safe(message: str) -> Tuple[bool, str]:
    """
    Sends summary to office manager number (if configured).
    Never throws.
    """
    if not OFFICE_MANAGER_NUMBER:
        return False, "OFFICE_MANAGER_NUMBER not set"
    ok, err = send_sms_safe(OFFICE_MANAGER_NUMBER, message)
    return ok, err


def _digits_to_int(d: str) -> Optional[int]:
    try:
        return int(d)
    except Exception:
        return None


def _yesno_from_digit(d: str) -> Optional[bool]:
    # 1 = yes, 2 = no
    if d == "1":
        return True
    if d == "2":
        return False
    return None


def _end_options(vr: VoiceResponse, intro: str = "") -> VoiceResponse:
    """
    Adds universal end prompt + navigation:
      - Press 1 => main menu (911 disclaimer + emergency gate)
      - Press 2 => business menu (appointments/billing/general)
    """
    if intro:
        vr.say(intro, voice="alice")

    gather = Gather(
        num_digits=1,
        action="/twilio/end-nav",
        method="POST",
        timeout=7,
    )
    gather.say(
        "You may hang up now. "
        "Press 1 to return to the main menu. "
        "Press 2 to return to scheduling, billing or insurance, and general practice information.",
        voice="alice",
    )
    vr.append(gather)
    vr.say("Goodbye.", voice="alice")
    vr.hangup()
    return vr


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
# STEP 1 + STEP 2: 911 disclaimer + Emergency gate
# =========================
@app.post("/twilio/voice")
async def twilio_voice(request: Request):
    """
    Entry point:
    1) 911 disclaimer
    2) Press 1 emergency, Press 2 business
    """
    vr = VoiceResponse()

    # 1) 911 disclaimer (always first)
    vr.say(
        "If this is a life threatening emergency, please hang up and dial 9 1 1.",
        voice="alice",
    )

    # 2) Emergency vs business gate (Press X to...)
    gather = Gather(
        num_digits=1,
        action="/twilio/gate",
        method="POST",
        timeout=7,
    )
    gather.say(
        "Press 1 if you are experiencing a dental emergency. "
        "Press 2 if you are calling about scheduling, billing or insurance, or general practice information.",
        voice="alice",
    )
    vr.append(gather)

    vr.say("Sorry, I didn't get that.", voice="alice")
    vr.redirect("/twilio/voice", method="POST")
    return Response(content=str(vr), media_type="application/xml")


@app.post("/twilio/gate")
async def twilio_gate(request: Request):
    form = await request.form()
    digit = form.get("Digits", "")
    call_sid = form.get("CallSid", "")
    from_number = form.get("From", "")

    st = _get_state(call_sid)
    st["from_number"] = from_number

    vr = VoiceResponse()

    if digit == "1":
        st["path"] = "emergency"
        vr.redirect("/twilio/emergency/pain", method="POST")
    elif digit == "2":
        st["path"] = "business"
        vr.redirect("/twilio/business/menu", method="POST")
    else:
        vr.say("Sorry, that wasn't a valid selection.", voice="alice")
        vr.redirect("/twilio/voice", method="POST")

    return Response(content=str(vr), media_type="application/xml")


# =========================
# Emergency workflow: 2.1 pain -> 2.2 symptoms -> 2.3 route/voicemail
# =========================
@app.post("/twilio/emergency/pain")
async def emergency_pain(request: Request):
    form = await request.form()
    call_sid = form.get("CallSid", "")
    from_number = form.get("From", "")

    st = _get_state(call_sid)
    st["from_number"] = from_number
    st["path"] = "emergency"

    vr = VoiceResponse()
    gather = Gather(num_digits=1, action="/twilio/emergency/pain-save", method="POST", timeout=7)
    gather.say(
        "Please enter your current pain level. "
        "Press a number from 1 to 9. "
        "Press 1 for the lowest pain. "
        "Press 9 for the highest pain.",
        voice="alice",
    )
    vr.append(gather)
    vr.say("Sorry, I didn't get that.", voice="alice")
    vr.redirect("/twilio/emergency/pain", method="POST")
    return Response(content=str(vr), media_type="application/xml")


@app.post("/twilio/emergency/pain-save")
async def emergency_pain_save(request: Request):
    form = await request.form()
    digit = form.get("Digits", "")
    call_sid = form.get("CallSid", "")
    from_number = form.get("From", "")

    st = _get_state(call_sid)
    st["from_number"] = from_number

    pain = _digits_to_int(digit)
    vr = VoiceResponse()

    if pain is None or pain < 1 or pain > 9:
        vr.say("Invalid entry. Please press a number from 1 to 9.", voice="alice")
        vr.redirect("/twilio/emergency/pain", method="POST")
        return Response(content=str(vr), media_type="application/xml")

    st["pain_level"] = pain
    vr.redirect("/twilio/emergency/symptoms", method="POST")
    return Response(content=str(vr), media_type="application/xml")


@app.post("/twilio/emergency/symptoms")
async def emergency_symptoms(request: Request):
    vr = VoiceResponse()
    gather = Gather(num_digits=1, action="/twilio/emergency/symptoms-save", method="POST", timeout=7)
    gather.say(
        "Press 1 if you are experiencing swelling, bleeding, or trauma to the dental region. "
        "Press 2 if you are not.",
        voice="alice",
    )
    vr.append(gather)
    vr.say("Sorry, I didn't get that.", voice="alice")
    vr.redirect("/twilio/emergency/symptoms", method="POST")
    return Response(content=str(vr), media_type="application/xml")


@app.post("/twilio/emergency/symptoms-save")
async def emergency_symptoms_save(request: Request):
    form = await request.form()
    digit = form.get("Digits", "")
    call_sid = form.get("CallSid", "")
    from_number = form.get("From", "")

    st = _get_state(call_sid)
    st["from_number"] = from_number

    yn = _yesno_from_digit(digit)
    vr = VoiceResponse()

    if yn is None:
        vr.say("Invalid entry. Press 1 for yes. Press 2 for no.", voice="alice")
        vr.redirect("/twilio/emergency/symptoms", method="POST")
        return Response(content=str(vr), media_type="application/xml")

    st["symptoms_flag"] = yn
    vr.redirect("/twilio/emergency/route", method="POST")
    return Response(content=str(vr), media_type="application/xml")


@app.post("/twilio/emergency/route")
async def emergency_route(request: Request):
    """
    2.3: Press 1 notify team with answers OR press 2 record voicemail
    """
    vr = VoiceResponse()
    gather = Gather(num_digits=1, action="/twilio/emergency/route-handle", method="POST", timeout=7)
    gather.say(
        "Press 1 to send your answers to the first available team member. "
        "Press 2 to record a voicemail to further explain your symptoms.",
        voice="alice",
    )
    vr.append(gather)
    vr.say("Sorry, I didn't get that.", voice="alice")
    vr.redirect("/twilio/emergency/route", method="POST")
    return Response(content=str(vr), media_type="application/xml")


@app.post("/twilio/emergency/route-handle")
async def emergency_route_handle(request: Request):
    form = await request.form()
    digit = form.get("Digits", "")
    call_sid = form.get("CallSid", "")
    from_number = form.get("From", "")

    st = _get_state(call_sid)
    st["from_number"] = from_number
    st["path"] = "emergency"

    vr = VoiceResponse()

    pain = st.get("pain_level")
    symptoms_flag = st.get("symptoms_flag")
    symptoms_text = "Yes" if symptoms_flag is True else "No" if symptoms_flag is False else "Unknown"

    if digit == "1":
        # Notify team with collected answers (no voicemail)
        msg = (
            f"{PRACTICE_NAME} — After-hours EMERGENCY intake\n"
            f"Caller: {from_number}\n"
            f"Pain (1-9): {pain}\n"
            f"Swelling/Bleeding/Trauma: {symptoms_text}\n"
            f"Action: Please call back ASAP."
        )
        ok, err = notify_office_safe(msg)
        print("Emergency notify:", "OK" if ok else "FAILED", "|", err)

        _end_options(
            vr,
            intro="Your emergency intake information has been sent to the first available team member.",
        )
        return Response(content=str(vr), media_type="application/xml")

    if digit == "2":
        # Record voicemail; pass intent and context in query params
        vr.say(
            "Please leave a voicemail after the tone. "
            "Please include your name and your callback number. "
            "Please do not include sensitive medical details. "
            "When you are finished, you may hang up.",
            voice="alice",
        )
        vr.record(
            action="/twilio/recording-complete?intent=emergency",
            method="POST",
            max_length=MAX_VOICEMAIL_SECONDS,
            play_beep=True,
        )
        return Response(content=str(vr), media_type="application/xml")

    vr.say("Sorry, that wasn't a valid selection.", voice="alice")
    vr.redirect("/twilio/emergency/route", method="POST")
    return Response(content=str(vr), media_type="application/xml")


# =========================
# Business workflow (Step 3): appointments / billing / general
# =========================
@app.post("/twilio/business/menu")
async def business_menu(request: Request):
    """
    Step 3: Press 1 appointments, 2 billing, 3 general info
    """
    vr = VoiceResponse()
    gather = Gather(num_digits=1, action="/twilio/business/menu-handle", method="POST", timeout=7)
    gather.say(
        "Press 1 for appointments. "
        "Press 2 for billing or insurance. "
        "Press 3 for general practice information.",
        voice="alice",
    )
    vr.append(gather)
    vr.say("Sorry, I didn't get that.", voice="alice")
    vr.redirect("/twilio/business/menu", method="POST")
    return Response(content=str(vr), media_type="application/xml")


@app.post("/twilio/business/menu-handle")
async def business_menu_handle(request: Request):
    form = await request.form()
    digit = form.get("Digits", "")
    call_sid = form.get("CallSid", "")
    from_number = form.get("From", "")

    st = _get_state(call_sid)
    st["from_number"] = from_number
    st["path"] = "business"

    vr = VoiceResponse()
    if digit == "1":
        vr.redirect("/twilio/business/appointments", method="POST")
    elif digit == "2":
        vr.redirect("/twilio/business/billing-voicemail", method="POST")
    elif digit == "3":
        vr.redirect("/twilio/business/general-info", method="POST")
    else:
        vr.say("Sorry, that wasn't a valid selection.", voice="alice")
        vr.redirect("/twilio/business/menu", method="POST")

    return Response(content=str(vr), media_type="application/xml")


# ---- Appointments submenu (1.1)
@app.post("/twilio/business/appointments")
async def business_appointments(request: Request):
    vr = VoiceResponse()
    gather = Gather(num_digits=1, action="/twilio/business/appointments-handle", method="POST", timeout=7)
    gather.say(
        "Press 1 to request a callback at the earliest operating hours. "
        "Press 2 to receive a scheduling link by text message. "
        "Press 3 to leave a voicemail about scheduling.",
        voice="alice",
    )
    vr.append(gather)
    vr.say("Sorry, I didn't get that.", voice="alice")
    vr.redirect("/twilio/business/appointments", method="POST")
    return Response(content=str(vr), media_type="application/xml")


@app.post("/twilio/business/appointments-handle")
async def business_appointments_handle(request: Request):
    form = await request.form()
    digit = form.get("Digits", "")
    call_sid = form.get("CallSid", "")
    from_number = form.get("From", "")

    st = _get_state(call_sid)
    st["from_number"] = from_number
    st["intent"] = "appointment"

    vr = VoiceResponse()

    if digit == "1":
        # Callback request
        msg = (
            f"{PRACTICE_NAME} — After-hours APPOINTMENT callback request\n"
            f"Caller: {from_number}\n"
            f"Action: Please contact at earliest operating hours."
        )
        ok, err = notify_office_safe(msg)
        print("Appt callback notify:", "OK" if ok else "FAILED", "|", err)

        _end_options(
            vr,
            intro="Your callback request has been sent. We will contact you at the earliest operating hours.",
        )
        return Response(content=str(vr), media_type="application/xml")

    if digit == "2":
        # Send scheduling link to caller
        vr.say("A scheduling link will be sent by text message now.", voice="alice")

        ok, err = send_sms_safe(
            from_number,
            f"Book an appointment with {PRACTICE_NAME}: {SCHEDULING_LINK}",
        )
        print("Scheduling link SMS to caller:", "OK" if ok else "FAILED", "|", err)

        # Also notify office (optional)
        msg = (
            f"{PRACTICE_NAME} — After-hours APPOINTMENT link sent\n"
            f"Caller: {from_number}\n"
            f"Action: Scheduling link was sent."
        )
        ok2, err2 = notify_office_safe(msg)
        print("Appt link notify office:", "OK" if ok2 else "FAILED", "|", err2)

        if ok:
            _end_options(vr, intro="Text sent.")
        else:
            _end_options(vr, intro="We could not send a text right now. Please call back during business hours.")
        return Response(content=str(vr), media_type="application/xml")

    if digit == "3":
        # Voicemail for appointment
        vr.say(
            "Please leave a voicemail about scheduling. "
            "Include your name and your callback number. "
            "Please do not include sensitive medical details. "
            "When you are finished, you may hang up.",
            voice="alice",
        )
        vr.record(
            action="/twilio/recording-complete?intent=appointment",
            method="POST",
            max_length=MAX_VOICEMAIL_SECONDS,
            play_beep=True,
        )
        return Response(content=str(vr), media_type="application/xml")

    vr.say("Sorry, that wasn't a valid selection.", voice="alice")
    vr.redirect("/twilio/business/appointments", method="POST")
    return Response(content=str(vr), media_type="application/xml")


# ---- Billing voicemail (1.2)
@app.post("/twilio/business/billing-voicemail")
async def business_billing_voicemail(request: Request):
    vr = VoiceResponse()
    vr.say(
        "Please leave a voicemail about billing or insurance. "
        "Include your name and your callback number. "
        "Please do not include sensitive medical details. "
        "When you are finished, you may hang up.",
        voice="alice",
    )
    vr.record(
        action="/twilio/recording-complete?intent=billing",
        method="POST",
        max_length=MAX_VOICEMAIL_SECONDS,
        play_beep=True,
    )
    return Response(content=str(vr), media_type="application/xml")


# ---- General info (1.3) + directions vs voicemail
@app.post("/twilio/business/general-info")
async def business_general_info(request: Request):
    vr = VoiceResponse()
    vr.say(OFFICE_HOURS_TEXT, voice="alice")

    gather = Gather(num_digits=1, action="/twilio/business/general-handle", method="POST", timeout=7)
    gather.say(
        "Press 1 to receive directions by text message. "
        "Press 2 to leave a voicemail with a general question.",
        voice="alice",
    )
    vr.append(gather)

    vr.say("Sorry, I didn't get that.", voice="alice")
    vr.redirect("/twilio/business/general-info", method="POST")
    return Response(content=str(vr), media_type="application/xml")


@app.post("/twilio/business/general-handle")
async def business_general_handle(request: Request):
    form = await request.form()
    digit = form.get("Digits", "")
    call_sid = form.get("CallSid", "")
    from_number = form.get("From", "")

    st = _get_state(call_sid)
    st["from_number"] = from_number
    st["intent"] = "general"

    vr = VoiceResponse()

    if digit == "1":
        vr.say("Directions will be sent by text message now.", voice="alice")
        ok, err = send_sms_safe(
            from_number,
            f"Directions to {PRACTICE_NAME}: {DIRECTIONS_LINK}",
        )
        print("Directions SMS to caller:", "OK" if ok else "FAILED", "|", err)

        msg = (
            f"{PRACTICE_NAME} — After-hours DIRECTIONS requested\n"
            f"Caller: {from_number}\n"
            f"Action: Directions link sent."
        )
        ok2, err2 = notify_office_safe(msg)
        print("Directions notify office:", "OK" if ok2 else "FAILED", "|", err2)

        if ok:
            _end_options(vr, intro="Text sent.")
        else:
            _end_options(vr, intro="We could not send directions by text right now.")
        return Response(content=str(vr), media_type="application/xml")

    if digit == "2":
        vr.say(
            "Please leave a voicemail with your general question. "
            "Include your name and your callback number. "
            "Please do not include sensitive medical details. "
            "When you are finished, you may hang up.",
            voice="alice",
        )
        vr.record(
            action="/twilio/recording-complete?intent=general",
            method="POST",
            max_length=MAX_VOICEMAIL_SECONDS,
            play_beep=True,
        )
        return Response(content=str(vr), media_type="application/xml")

    vr.say("Sorry, that wasn't a valid selection.", voice="alice")
    vr.redirect("/twilio/business/general-info", method="POST")
    return Response(content=str(vr), media_type="application/xml")


# =========================
# Universal end navigation
# =========================
@app.post("/twilio/end-nav")
async def end_nav(request: Request):
    form = await request.form()
    digit = form.get("Digits", "")

    vr = VoiceResponse()
    if digit == "1":
        vr.redirect("/twilio/voice", method="POST")
    elif digit == "2":
        vr.redirect("/twilio/business/menu", method="POST")
    else:
        vr.say("Goodbye.", voice="alice")
        vr.hangup()

    return Response(content=str(vr), media_type="application/xml")


# =========================
# Voicemail callback
# =========================
@app.post("/twilio/recording-complete")
async def recording_complete(request: Request):
    """
    Called by Twilio after voicemail recording ends.
    Twilio posts form data including:
      - RecordingUrl
      - From
      - CallSid
    We also pass intent in query string: ?intent=billing|general|appointment|emergency
    """
    form = await request.form()
    recording_url = form.get("RecordingUrl")
    from_number = form.get("From", "")
    call_sid = form.get("CallSid", "")

    intent = request.query_params.get("intent", "unknown").strip().lower()

    st = _get_state(call_sid)
    st["from_number"] = from_number
    st["recording_url"] = recording_url
    st["intent"] = intent

    pain = st.get("pain_level")
    symptoms_flag = st.get("symptoms_flag")
    symptoms_text = "Yes" if symptoms_flag is True else "No" if symptoms_flag is False else "Unknown"

    # Twilio RecordingUrl is usually accessible with .mp3
    rec_link = f"{recording_url}.mp3" if recording_url else "(no recording url)"

    # Build office notification
    header = f"{PRACTICE_NAME} — After-hours VOICEMAIL"
    if intent != "unknown":
        header += f" ({intent.upper()})"

    lines = [
        header,
        f"Caller: {from_number}",
        f"Voicemail: {rec_link}",
    ]

    # Include emergency triage info if present
    if intent == "emergency" or st.get("path") == "emergency":
        lines.insert(2, f"Pain (1-9): {pain}")
        lines.insert(3, f"Swelling/Bleeding/Trauma: {symptoms_text}")

    msg = "\n".join(lines)
    ok, err = notify_office_safe(msg)
    print("Voicemail notify office:", "OK" if ok else "FAILED", "|", err)

    vr = VoiceResponse()
    _end_options(vr, intro="Your message has been recorded.")
    return Response(content=str(vr), media_type="application/xml")
