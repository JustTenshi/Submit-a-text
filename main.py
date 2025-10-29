from fastapi import FastAPI, Request, Form
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from dotenv import load_dotenv
from db import fetch_all, fetch_one, execute, execute_returning
import os
import re
import requests
from starlette.middleware.sessions import SessionMiddleware


# Load environment variables
load_dotenv()

# Initialize FastAPI
app = FastAPI()
templates = Jinja2Templates(directory="templates")

app.add_middleware(SessionMiddleware, secret_key=os.getenv("SESSION_SECRET", "supersecretkey"))


# Environment variables
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "AngelSecure!")

TELNYX_API_KEY = os.getenv("TELNYX_API_KEY")
TELNYX_MESSAGING_PROFILE_ID = os.getenv("TELNYX_MESSAGING_PROFILE_ID")
TELNYX_FROM_NUMBER = os.getenv("TELNYX_FROM_NUMBER")


# ======================
# Utility Functions
# ======================

def normalize_phone(raw: str) -> str:
    """Normalize raw phone input into +1XXXXXXXXXX format."""
    digits = re.sub(r"\D", "", raw or "")
    if len(digits) == 10:
        return "+1" + digits
    elif len(digits) == 11 and digits.startswith("1"):
        return "+" + digits
    return "+" + digits


def send_sms_via_telnyx(to_number: str, message: str):
    """Send an SMS using the Telnyx Messaging API v2."""
    url = "https://api.telnyx.com/v2/messages"
    headers = {
        "Authorization": f"Bearer {TELNYX_API_KEY}",
        "Content-Type": "application/json"
    }
    data = {
    "from": TELNYX_FROM_NUMBER,
    "to": to_number,
    "text": message,
    "messaging_profile_id": TELNYX_MESSAGING_PROFILE_ID,
    "type": "SMS",
    "use_profile_webhooks": True
    }


    try:
        response = requests.post(url, json=data, headers=headers)
        response.raise_for_status()
        print("✅ Telnyx message sent successfully:", response.json())
        return response.json()
    except requests.exceptions.RequestException as e:
        print("❌ Telnyx send failed:", e)
        return {"data": {"id": "FAILED"}}


# ======================
# Root & Admin Pages
# ======================

@app.get("/", response_class=HTMLResponse)
async def login_page(request: Request):
    return templates.TemplateResponse("login.html", {"request": request})

ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "admin")

@app.post("/login")
async def login(request: Request, username: str = Form(...), password: str = Form(...)):
    if username == ADMIN_USERNAME and password == ADMIN_PASSWORD:
        request.session["logged_in"] = True
        return RedirectResponse(url="/admin", status_code=303)
    else:
        return HTMLResponse("<h3>Invalid credentials</h3>", status_code=401)






@app.get("/admin", response_class=HTMLResponse)
async def admin_home(request: Request):
    if not request.session.get("logged_in"):
        return RedirectResponse(url="/", status_code=303)


    sales_rows = fetch_all("""
        SELECT id, phone, office, plan_type, created_on
        FROM sales
        ORDER BY created_on DESC
        LIMIT 50;
    """)

    return templates.TemplateResponse(
        "admin_home.html",
        {"request": request, "sales": sales_rows}
    )


@app.get("/admin/sale/{sale_id}", response_class=HTMLResponse)
async def admin_lead(request: Request, sale_id: int):
    if not request.session.get("logged_in"):
        return RedirectResponse(url="/", status_code=303)


    sale = fetch_one("SELECT * FROM sales WHERE id=%s;", (sale_id,))
    if not sale:
        return HTMLResponse("<h3>Sale not found</h3>", status_code=404)

    return templates.TemplateResponse(
        "admin_lead.html",
        {"request": request, "lead": sale}
    )


# ======================
# API: Submit a Sale
# ======================

@app.post("/api/new-sale")
async def new_sale(request: Request):
    """
    Submit-a-Sale POST endpoint.
    """
    body = await request.json()
    raw_phone = body.get("phone")
    clean_phone = normalize_phone(raw_phone)

    existing = fetch_one(
        "SELECT id, opted_out FROM sales WHERE phone = %s;", (clean_phone,)
    )

    # Skip if opted out
    if existing and existing["opted_out"]:
        return JSONResponse({
            "ok": True,
            "skipped_send": True,
            "reason": "opted_out"
        })

    # Insert or update
    if existing:
        sale_id = existing["id"]
        execute(
            """
            UPDATE sales
            SET
                external_sale_id = %s,
                agent_name = %s,
                office = %s,
                source = %s,
                health_id = %s,
                plan_type = %s
            WHERE id = %s;
            """,
            (
                body.get("saleId"),
                body.get("agent"),
                body.get("office"),
                body.get("source"),
                body.get("healthId"),
                body.get("planType"),
                sale_id
            )
        )
    else:
        row = execute_returning(
            """
            INSERT INTO sales
                (external_sale_id, phone, agent_name, office, source, health_id, plan_type)
            VALUES
                (%s, %s, %s, %s, %s, %s, %s)
            RETURNING id;
            """,
            (
                body.get("saleId"),
                clean_phone,
                body.get("agent"),
                body.get("office"),
                body.get("source"),
                body.get("healthId"),
                body.get("planType")
            )
        )
        sale_id = row["id"]

    # Send first SMS
    text_msg = (
        "Thank you for enrolling! We're here to help with your coverage. "
        "Reply STOP to opt out."
    )

    outbound_resp = send_sms_via_telnyx(clean_phone, text_msg)
    provider_sid = outbound_resp.get("data", {}).get("id", "FAILED")

    execute(
        """
        INSERT INTO outbound_messages (sale_id, body, provider, provider_sid)
        VALUES (%s, %s, %s, %s);
        """,
        (sale_id, text_msg, "telnyx", provider_sid)
    )

    return JSONResponse({
        "ok": True,
        "sale_id": sale_id,
        "sent_to": clean_phone,
        "provider_sid": provider_sid,
        "skipped_send": False
    })


# ======================
# API: Inbound Webhook
# ======================

@app.post("/api/inbound-sms")
async def inbound_sms(request: Request):
    """
    Telnyx webhook handler for inbound messages.
    """
    payload = await request.json()
    data = payload.get("data", {}).get("payload", {})

    from_phone = data.get("from", {}).get("phone_number")
    body = data.get("text", "")

    clean_from = normalize_phone(from_phone)

    sale_row = fetch_one("SELECT id FROM sales WHERE phone = %s;", (clean_from,))
    sale_id = sale_row["id"] if sale_row else None

    execute(
        "INSERT INTO inbound_messages (sale_id, from_phone, body) VALUES (%s, %s, %s);",
        (sale_id, clean_from, body)
    )

    if body.strip().upper() == "STOP":
        execute("UPDATE sales SET opted_out = TRUE WHERE phone = %s;", (clean_from,))

    return {"ok": True}


@app.get("/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse(url="/", status_code=303)

