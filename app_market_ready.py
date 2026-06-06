from __future__ import annotations

from pathlib import Path
import base64
from datetime import date, datetime
from io import BytesIO
import textwrap
from urllib.parse import quote_plus

import pandas as pd
from PIL import Image, ImageDraw, ImageFont
import plotly.graph_objects as go
import streamlit as st
from streamlit_gsheets import GSheetsConnection

from dataclasses import dataclass


@dataclass
class HouseholdInputs:
    household_size: str = "3-4"
    tenure_type: str = "Rented"
    bill_problem: str = "Not sure"
    hvac_type: str = "Heat pump"
    solar_status: str = "Not sure"
    monthly_bill_aud: float = 220.0
    electricity_price: float = 0.34
    shower_minutes_current: float = 10.0
    shower_minutes_target: float = 4.0
    showers_per_person_per_day: float = 1.0
    old_bulbs: int = 8
    hours_per_bulb_per_day: float = 3.0
    standby_devices: int = 8
    thermostat_degrees_improved: int = 2


def _people_multiplier(household_size: str) -> float:
    return {"1": 0.65, "2": 0.85, "3-4": 1.15, "5+": 1.45}.get(household_size, 1.0)


def estimate_all_savings(inputs: HouseholdInputs) -> dict:
    """Indicative New Zealand household energy-saving estimates.

    These calculations are deliberately conservative decision-support estimates for a
    public education prototype. They are not an energy audit, compliance calculation,
    MBIE H1 verification method, Healthy Homes compliance statement, or guaranteed bill forecast.
    """
    price = max(0.05, float(inputs.electricity_price or 0.34))
    people = _people_multiplier(inputs.household_size)
    annual_bill = max(0.0, float(inputs.monthly_bill_aud or 0) * 12)

    shower_gap = max(0.0, float(inputs.shower_minutes_current) - float(inputs.shower_minutes_target))
    # Approx. 0.33-0.55 kWh/minute for electric water heating depending on flow and temperature rise.
    shower_low = shower_gap * 0.30 * price * 365 * people * float(inputs.showers_per_person_per_day)
    shower_high = shower_gap * 0.50 * price * 365 * people * float(inputs.showers_per_person_per_day)

    bulbs = max(0, int(inputs.old_bulbs or 0))
    hours = max(0.0, float(inputs.hours_per_bulb_per_day or 0))
    # Old lamp ~50 W vs LED ~8-10 W, conservative 35-45 W saving.
    led_low = bulbs * hours * 365 * 0.035 * price
    led_high = bulbs * hours * 365 * 0.045 * price

    devices = max(0, int(inputs.standby_devices or 0))
    standby_low = devices * 0.003 * 24 * 365 * price
    standby_high = devices * 0.007 * 24 * 365 * price

    thermostat_deg = max(0, int(inputs.thermostat_degrees_improved or 0))
    heat_cool_factor = 0.10 if inputs.bill_problem in {"High winter bill", "High summer bill"} else 0.07
    thermostat_low = annual_bill * min(0.14, thermostat_deg * 0.025) * heat_cool_factor / 0.10 if annual_bill else 0
    thermostat_high = annual_bill * min(0.22, thermostat_deg * 0.04) * heat_cool_factor / 0.10 if annual_bill else 0

    curtains_low, curtains_high = (60.0, 95.0) if annual_bill > 0 else (0.0, 0.0)
    draught_low, draught_high = (80.0, 130.0) if annual_bill > 0 else (0.0, 0.0)
    insulation_low, insulation_high = (180.0, 330.0) if inputs.tenure_type == "Owned" else (0.0, 0.0)
    if inputs.tenure_type == "Rented":
        insulation_renter_low, insulation_renter_high = (0.0, 0.0)
    else:
        insulation_renter_low, insulation_renter_high = (0.0, 0.0)

    # Bound estimates so one behaviour does not produce unrealistic results against the entered annual bill.
    cap = annual_bill * 0.45 if annual_bill else 0
    def bounded(pair):
        low, high = pair
        if cap:
            high = min(high, cap)
            low = min(low, high)
        return (round(max(0, low)), round(max(0, high)))

    return {
        "shorter_showers": bounded((shower_low, shower_high)),
        "thermostat": bounded((thermostat_low, thermostat_high)),
        "leds": bounded((led_low, led_high)),
        "standby": bounded((standby_low, standby_high)),
        "curtains": bounded((curtains_low, curtains_high)),
        "draught_sealing": bounded((draught_low, draught_high)),
        "insulation_owner": bounded((insulation_low, insulation_high)),
        "insulation_renter": bounded((insulation_renter_low, insulation_renter_high)),
    }


def format_saving_range(value: tuple[float, float]) -> str:
    low, high = value
    if high <= 0:
        return "Not monetised in this prototype"
    return f"NZ${int(low):,}–NZ${int(high):,}/year"


ACTIONS = {
    "shorter_showers": {
        "priority": 1,
        "category": "Hot water",
        "title": "Run a shorter-shower challenge",
        "recommendation": "Set a realistic shower-time target and use a timer. Hot water is often one of the easiest household energy costs to reduce without building work.",
        "cost_level": "No cost",
        "impact_level": "High",
    },
    "thermostat": {
        "priority": 2,
        "category": "Heating and cooling",
        "title": "Use efficient heating and cooling set-points",
        "recommendation": "Heat occupied rooms to a healthy, moderate range and avoid overheating. For summer cooling, avoid very low set-points that make heat pumps work harder.",
        "cost_level": "No cost",
        "impact_level": "Medium–high",
    },
    "draught_sealing": {
        "priority": 3,
        "category": "Building shell",
        "title": "Seal obvious draughts",
        "recommendation": "Use renter-friendly door snakes, weather seals, and gap checks first. Owners can plan more complete airtightness improvements while maintaining controlled ventilation.",
        "cost_level": "Low cost",
        "impact_level": "Medium",
    },
    "curtains": {
        "priority": 4,
        "category": "Windows and comfort",
        "title": "Use curtains, blinds, and shading strategically",
        "recommendation": "Close curtains before it gets dark in winter, use fitted thermal curtains where possible, and manage summer sun to reduce overheating.",
        "cost_level": "No/low cost",
        "impact_level": "Medium",
    },
    "leds": {
        "priority": 5,
        "category": "Lighting",
        "title": "Replace frequently used old bulbs with LEDs",
        "recommendation": "Start with rooms used every day. This is a simple, low-risk upgrade with predictable electricity savings.",
        "cost_level": "Low cost",
        "impact_level": "Medium",
    },
    "standby": {
        "priority": 6,
        "category": "Appliances",
        "title": "Control standby loads",
        "recommendation": "Switch off unused devices at the wall or group them with a smart power board, especially entertainment and office equipment.",
        "cost_level": "No/low cost",
        "impact_level": "Low–medium",
    },
    "insulation_owner": {
        "priority": 7,
        "category": "Building Code / retrofit",
        "title": "Plan insulation and envelope improvements",
        "recommendation": "For owners, check ceiling, underfloor, wall, and window performance. Link any renovation decisions to NZ Building Code H1 energy-efficiency thinking.",
        "cost_level": "Medium/high cost",
        "impact_level": "High",
    },
    "insulation_renter": {
        "priority": 7,
        "category": "Healthy Homes / renting",
        "title": "Check rental insulation and Healthy Homes obligations",
        "recommendation": "For renters, document comfort, draughts, heating, ventilation, moisture, and insulation concerns and discuss them with the landlord/property manager using the Healthy Homes standards as the reference point.",
        "cost_level": "No cost to check",
        "impact_level": "Depends on landlord action",
    },
}


def generate_ranked_actions(selected_actions: list, tenure_type: str, bill_problem: str, solar_status: str) -> list:
    preferred = []
    if bill_problem == "High hot-water bill":
        preferred += ["shorter_showers"]
    if bill_problem in {"High winter bill", "High summer bill"}:
        preferred += ["thermostat", "draught_sealing", "curtains"]
    if tenure_type == "Rented":
        preferred += ["insulation_renter"]
    else:
        preferred += ["insulation_owner"]
    preferred += list(selected_actions or [])
    preferred += ["shorter_showers", "thermostat", "draught_sealing", "curtains", "leds", "standby", "insulation_owner" if tenure_type == "Owned" else "insulation_renter"]
    seen = []
    for key in preferred:
        if key in ACTIONS and key not in seen:
            seen.append(key)
    return sorted([(k, ACTIONS[k]) for k in seen], key=lambda item: item[1]["priority"])


def top_three_actions(ranked_actions: list) -> list:
    return ranked_actions[:3]


def score_label(score: int) -> str:
    score = int(score or 0)
    if score >= 96:
        return "Home Energy Master"
    if score >= 81:
        return "Energy Efficiency Champion"
    if score >= 61:
        return "Smart Saver"
    if score >= 36:
        return "Home Energy Explorer"
    return "Energy Leak Beginner"


APP_TITLE = "The Home-energy check-up (New Zealand)"
TAGLINE = "A practical home-energy check-up for New Zealand homes"
ROOT = Path(__file__).parent
LOGO_PATH = ROOT / "assets" / "company_logo.png"
APP_PUBLIC_URL = "https://your-streamlit-app-url-here"  # Replace with your deployed Streamlit URL for social sharing.


st.set_page_config(
    page_title=APP_TITLE,
    page_icon="🏡",
    layout="wide",
    initial_sidebar_state="expanded",
)

st.markdown(
    """
    <style>
    #MainMenu {visibility: hidden;}
    header {visibility: hidden;}
    footer {visibility: hidden;}
    .stDeployButton {display: none !important;}
    [data-testid="stToolbar"] {display: none !important;}
    [data-testid="stDecoration"] {display: none !important;}
    [data-testid="stStatusWidget"] {display: none !important;}
    [data-testid="stHeader"] {display: none !important;}
    [data-testid="stFooter"] {display: none !important;}
    </style>
    """,
    unsafe_allow_html=True,
)

CUSTOM_CSS = """
<style>
.block-container {
    padding-top: 1.4rem;
    padding-bottom: 3rem;
    max-width: 96vw;
    margin-left: auto;
    margin-right: auto;
}
.header-grid {
    display: grid;
    grid-template-columns: minmax(0, 1fr) 210px;
    gap: 1.35rem;
    align-items: stretch;
    margin: 0 auto 1rem auto;
}
.hero {
    padding: 2rem;
    border-radius: 24px;
    background: linear-gradient(135deg, #0F766E 0%, #0EA5E9 100%);
    color: white;
    min-height: 210px;
}
.hero h1 {font-size: 2.35rem; margin-bottom: 0.3rem; letter-spacing: -0.02em;}
.hero p {font-size: 1.05rem; opacity: 0.96;}
.logo-card {
    min-height: 210px;
    background: transparent;
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    padding: 0.5rem 0.25rem;
    text-align: center;
}
.logo-card img {
    width: 105px;
    max-width: 90%;
    height: auto;
    object-fit: contain;
    display: block;
}

.company-name {
    margin-top: 0.65rem;
    font-weight: 700;
    color: #0F172A;
    font-size: 0.95rem;
    line-height: 1.25;
}
.company-tagline {
    margin-top: 0.18rem;
    color: #475569;
    font-size: 0.78rem;
    line-height: 1.25;
}
.company-email {
    margin-top: 0.4rem;
    color: #0F766E;
    font-size: 0.80rem;
    font-weight: 600;
}

.sidebar-brand {
    margin-top: 2rem;
    padding-top: 1rem;
    border-top: 1px solid #E2E8F0;
    text-align: center;
}
.sidebar-brand img {
    width: 58px;
    max-width: 70%;
    height: auto;
    object-fit: contain;
    margin: 0 auto 0.45rem auto;
    display: block;
}
.sidebar-brand-title {
    font-size: 0.78rem;
    font-weight: 700;
    color: #0F172A;
    line-height: 1.2;
}
.sidebar-brand-text {
    font-size: 0.68rem;
    color: #64748B;
    line-height: 1.2;
    margin-top: 0.15rem;
}
.feedback-correct {
    padding: 0.9rem;
    border-radius: 14px;
    background:#ECFDF5;
    border:1px solid #A7F3D0;
    color:#065F46;
    margin-top: 0.75rem;
}
.feedback-wrong {
    padding: 0.9rem;
    border-radius: 14px;
    background:#FFFBEB;
    border:1px solid #FDE68A;
    color:#92400E;
    margin-top: 0.75rem;
}
.feedback-answer {
    margin-top: 0.35rem;
    font-size: 0.90rem;
    color: #334155;
}

.logo-placeholder {
    text-align: center;
    color: #64748B;
    font-size: 0.92rem;
}
.card {
    padding: 1.1rem; border-radius: 18px; background: white;
    border: 1px solid #E2E8F0; box-shadow: 0 1px 4px rgba(15, 23, 42, 0.06);
    min-height: 145px;
}
.compliance-strip {
    display: flex;
    align-items: center;
    gap: 0.65rem;
    padding: 0.85rem 1rem;
    border-radius: 16px;
    background: #F8FAFC;
    border: 1px solid #CBD5E1;
    color: #334155;
    margin: 0.25rem 0 1rem 0;
    font-size: 0.94rem;
}
.compliance-icon {
    width: 34px;
    height: 34px;
    min-width: 34px;
    border-radius: 10px;
    background: #0F766E;
    color: white;
    display: flex;
    align-items: center;
    justify-content: center;
    font-weight: 700;
}
.small-muted {color: #64748B; font-size: 0.92rem;}
.badge {display: inline-block; padding: 0.2rem 0.55rem; border-radius: 999px; background:#ECFDF5; color:#047857; font-size:0.82rem; font-weight:600;}
.warning-box {padding: 1rem; border-radius: 16px; background:#FFFBEB; border:1px solid #FDE68A; color:#92400E;}
.success-box {padding: 1rem; border-radius: 16px; background:#ECFDF5; border:1px solid #A7F3D0; color:#065F46;}

.certificate-card {
    position: relative;
    overflow: hidden;
    width: min(100%, 760px);
    aspect-ratio: 1 / 1;
    margin: 1.25rem auto 0 auto;
    padding: clamp(1.6rem, 4vw, 2.35rem);
    border-radius: 28px;
    background:
        radial-gradient(circle at top left, rgba(14,165,233,0.16), transparent 34%),
        radial-gradient(circle at bottom right, rgba(15,118,110,0.15), transparent 35%),
        linear-gradient(135deg, #FFFFFF 0%, #F8FAFC 100%);
    border: 1px solid #99F6E4;
    box-shadow: 0 18px 45px rgba(15, 23, 42, 0.12);
    text-align: center;
    display:flex;
    flex-direction:column;
    justify-content:center;
}
.certificate-card::before {
    content: "";
    position: absolute;
    inset: 18px;
    border: 2px solid #0F766E;
    border-radius: 22px;
    pointer-events: none;
}
.certificate-card::after {
    content: "Recognition";
    position: absolute;
    top: 42px;
    right: -60px;
    transform: rotate(45deg);
    background: #0F766E;
    color: #FFFFFF;
    padding: 0.42rem 4.6rem;
    font-size: 0.74rem;
    font-weight: 900;
    letter-spacing: 0.08em;
    text-transform: uppercase;
    box-shadow: 0 8px 18px rgba(15, 23, 42, 0.18);
    z-index: 2;
}
.certificate-kicker {font-size: 0.78rem; color:#0F766E; font-weight:800; letter-spacing:0.12em; text-transform:uppercase;}
.certificate-title {font-size: clamp(1.8rem, 4vw, 2.35rem); font-weight: 900; color: #0F172A; margin: 0.4rem 0 0.2rem 0; letter-spacing: -0.02em;}
.certificate-subtitle {font-size: 1rem; color: #475569; margin-bottom: 0.45rem;}
.certificate-company {font-size: 0.92rem; color:#0F766E; font-weight:800; margin-bottom:1.1rem;}
.certificate-name {font-size: clamp(1.65rem, 4vw, 2.15rem); font-weight: 900; color: #0F766E; margin: 0.75rem auto; padding-bottom:0.42rem; border-bottom: 2px solid #99F6E4; max-width: 560px;}
.certificate-small {font-size: 0.94rem; color: #334155; line-height: 1.55; max-width:620px; margin-left:auto; margin-right:auto;}
.certificate-meta {display:flex; justify-content:center; gap:0.65rem; flex-wrap:wrap; margin-top:1rem;}
.certificate-pill {padding:0.45rem 0.75rem; border-radius:999px; background:#ECFDF5; border:1px solid #A7F3D0; color:#065F46; font-weight:700; font-size:0.82rem;}
.certificate-footer {margin-top:1.15rem; font-size:0.82rem; color:#475569;}
.certificate-disclaimer {font-size:0.75rem; color:#64748B; margin-top:0.75rem;}
.share-row {display: flex; gap: 0.55rem; flex-wrap: wrap; margin-top: 0.75rem;}
.share-button {
    display: inline-flex; align-items: center; justify-content: center; min-width: 95px;
    padding: 0.55rem 0.75rem; border-radius: 999px; background: #0F172A;
    color: white !important; text-decoration: none !important; font-weight: 700; font-size: 0.86rem;
}
.share-button:hover { opacity: 0.88; }

@media (max-width: 760px) {
    .header-grid {grid-template-columns: 1fr;}
    .logo-card {min-height: 120px;}
    .logo-card img {width: 115px;}
}

.money-hero {
    padding: 1.6rem;
    border-radius: 24px;
    background: linear-gradient(135deg, #111827 0%, #0F766E 100%);
    color: white;
    margin: 1rem 0;
    box-shadow: 0 10px 30px rgba(15, 23, 42, 0.16);
}
.money-hero h2 {font-size: 2rem; margin: 0 0 0.5rem 0; letter-spacing: -0.02em;}
.money-hero p {font-size: 1rem; opacity: 0.95; margin-bottom: 0.4rem;}
.money-number {
    font-size: 2.35rem;
    font-weight: 950;
    line-height: 1.05;
    margin: 0.5rem 0;
    color: #064E3B !important;
    background: #FFFFFF !important;
    border: 2px solid #FBBF24 !important;
    border-radius: .85rem;
    padding: .18rem .6rem;
    display: inline-block;
    box-shadow: 0 6px 18px rgba(15,23,42,.18);
}
.money-sub {font-size: 0.88rem; opacity: 0.85;}
.money-card {
    padding: 1.15rem;
    border-radius: 18px;
    border: 1px solid #A7F3D0;
    background: #ECFDF5;
    min-height: 135px;
}
.money-card h4 {margin:0 0 0.35rem 0; color:#064E3B;}
.money-card .value {font-size:1.65rem; font-weight:900; color:#065F46; margin:0.2rem 0;}
.money-card p {color:#334155; font-size:0.92rem; margin-bottom:0;}
.unlock-card {
    padding: 1.05rem;
    border-radius: 18px;
    border: 1px solid #E2E8F0;
    background: #FFFFFF;
    box-shadow: 0 1px 5px rgba(15,23,42,0.06);
}
.unlock-badge {display:inline-block; padding:0.22rem 0.55rem; border-radius:999px; background:#F1F5F9; color:#334155; font-size:0.78rem; font-weight:700;}
.pathway-step {
    padding: 1rem;
    border-left: 5px solid #0F766E;
    background: #F8FAFC;
    border-radius: 14px;
    margin-bottom: 0.75rem;
}
.pathway-step h4 {margin: 0 0 0.25rem 0; color:#0F172A;}
.pathway-step p {margin: 0.2rem 0; color:#334155;}

</style>
"""
st.markdown(CUSTOM_CSS, unsafe_allow_html=True)


# ---------- Session state ----------
def initialise_state() -> None:
    defaults = {
        "stage": "welcome",
        "score": 0,
        "completed_hotspots": set(),
        "selected_actions": [],
        "bill_risk": 60,
        "comfort": 45,
        "last_feedback": {},
        "participant_id": "Anonymous Participant",
        "response_saved": False,
        "nav_history": [],
    }
    for key, value in defaults.items():
        if key not in st.session_state:
            st.session_state[key] = value


def reset_app() -> None:
    for key in list(st.session_state.keys()):
        del st.session_state[key]
    initialise_state()


initialise_state()


# ---------- Google Sheets response storage ----------
def get_next_participant_id(conn) -> str:
    """Generate a simple anonymous participant ID based on the next available sheet row."""
    try:
        existing_data = conn.read(ttl=0)
        if existing_data is None or existing_data.empty:
            return "Participant 1"
        return f"Participant {len(existing_data) + 1}"
    except Exception:
        return "Participant 1"


def save_response_to_gsheet(submission: dict) -> None:
    """Append one anonymous completed response to the connected Google Sheet."""
    conn = st.connection("gsheets", type=GSheetsConnection)
    existing_data = conn.read(ttl=0)
    new_row = pd.DataFrame([submission])

    if existing_data is None or existing_data.empty:
        updated_data = new_row
    else:
        updated_data = pd.concat([existing_data, new_row], ignore_index=True)

    conn.update(data=updated_data)


def build_anonymous_submission(participant_id: str, savings: dict, ranked_actions: list, result_text: str) -> dict:
    """Build the row saved to Google Sheets. No name, email, or contact details are collected."""
    total_low = int(sum(v[0] for v in savings.values() if isinstance(v, tuple)))
    total_high = int(sum(v[1] for v in savings.values() if isinstance(v, tuple)))
    action_titles = [action.get("title", key) for key, action in ranked_actions]

    return {
        "participant_id": participant_id,
        "submission_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "household_size": st.session_state.get("household_size", ""),
        "tenure_type": st.session_state.get("tenure_type", ""),
        "bill_problem": st.session_state.get("bill_problem", ""),
        "dwelling_type": st.session_state.get("dwelling_type", ""),
        "home_condition": st.session_state.get("home_condition", ""),
        "hvac_type": st.session_state.get("hvac_type", ""),
        "solar_status": st.session_state.get("solar_status", ""),
        "thermostat_answer": st.session_state.get("thermostat_answer", ""),
        "thermostat_correct": st.session_state.get("thermostat_correct", ""),
        "lighting_answer": st.session_state.get("leds_answer", ""),
        "lighting_correct": st.session_state.get("leds_correct", ""),
        "curtains_answer": st.session_state.get("curtains_answer", ""),
        "curtains_correct": st.session_state.get("curtains_correct", ""),
        "draught_answer": st.session_state.get("draught_answer", ""),
        "draught_correct": st.session_state.get("draught_correct", ""),
        "shower_answer": st.session_state.get("shower_answer", ""),
        "shower_correct": st.session_state.get("shower_correct", ""),
        "standby_answer": st.session_state.get("standby_answer", ""),
        "standby_correct": st.session_state.get("standby_correct", ""),
        "insulation_answer": st.session_state.get("insulation_answer", ""),
        "insulation_correct": st.session_state.get("insulation_correct", ""),
        "score": st.session_state.get("score", ""),
        "result_category": result_text,
        "estimated_annual_savings": f"NZ${total_low:,}–NZ${total_high:,}",
        "money_first_snapshot": str(st.session_state.get("money_snapshot", {})),
        "selected_actions": "; ".join(action_titles),
        "consent_given": True,
    }


# ---------- Data ----------
HOTSPOTS = {
    "thermostat": {
        "label": "Air-conditioner / heater remote",
        "room": "Living room",
        "question": "Which setting is usually more energy-smart for a New Zealand home?",
        "options": [
            "Heating at 25°C in winter",
            "Heating around 18–21°C in occupied rooms",
            "Cooling at 18°C in summer",
        ],
        "correct": "Heating around 18–21°C in occupied rooms",
        "points": 15,
        "action": "thermostat",
        "correct_feedback": "Correct. Moderate heat-pump and heater settings support comfort while reducing unnecessary energy use.",
        "wrong_feedback": "Not ideal. Extreme thermostat settings increase energy use. Aim for a healthy, moderate heating range around 18–21°C in occupied rooms and avoid very low cooling settings in summer.",
    },
    "leds": {
        "label": "Old light bulb",
        "room": "Living room / hallway",
        "question": "What should this household do first?",
        "options": ["Keep using old bulbs", "Replace frequently used bulbs with LEDs", "Use more lamps instead"],
        "correct": "Replace frequently used bulbs with LEDs",
        "points": 10,
        "action": "leds",
        "correct_feedback": "Correct. LEDs are a simple low-cost upgrade for reducing electricity use.",
        "wrong_feedback": "Not the best choice. Start by replacing old bulbs in frequently used areas with LEDs.",
    },
    "curtains": {
        "label": "Curtains and window",
        "room": "Living room",
        "question": "Which action helps reduce heating and cooling demand?",
        "options": [
            "Leave windows uncovered during hot afternoons",
            "Use curtains/blinds to block summer heat and reduce winter heat loss",
            "Open windows while heating",
        ],
        "correct": "Use curtains/blinds to block summer heat and reduce winter heat loss",
        "points": 15,
        "action": "curtains",
        "correct_feedback": "Correct. Good curtain and blind use helps reduce unwanted heat gain and heat loss.",
        "wrong_feedback": "Not ideal. Poor window covering can make heating and cooling systems work harder.",
    },
    "draught": {
        "label": "Draught gap",
        "room": "Door / window area",
        "question": "What is the best low-cost action?",
        "options": ["Ignore small gaps", "Seal obvious draughts with door snakes or weather seals", "Increase heating or cooling"],
        "correct": "Seal obvious draughts with door snakes or weather seals",
        "points": 15,
        "action": "draught_sealing",
        "correct_feedback": "Correct. Draught sealing helps reduce wasted heating and cooling.",
        "wrong_feedback": "Not ideal. Gaps can let conditioned air escape and make systems work harder.",
    },
    "shower": {
        "label": "Shower / hot water",
        "room": "Bathroom",
        "question": "What is one fast way to reduce hot-water energy use?",
        "options": ["Take shorter showers", "Use hotter water", "Leave hot water running before showering"],
        "correct": "Take shorter showers",
        "points": 15,
        "action": "shorter_showers",
        "correct_feedback": "Correct. Hot water is a major household energy use. Shorter showers reduce both water and energy costs.",
        "wrong_feedback": "Not ideal. Longer showers increase hot-water energy use and can increase bills.",
    },
    "standby": {
        "label": "Standby appliances",
        "room": "Living room / study",
        "question": "What should the household do?",
        "options": ["Leave everything on standby", "Turn off unused devices at the wall or use a smart power board", "Buy a second fridge"],
        "correct": "Turn off unused devices at the wall or use a smart power board",
        "points": 10,
        "action": "standby",
        "correct_feedback": "Correct. Some appliances keep using energy when not actively used.",
        "wrong_feedback": "Not ideal. Unused standby devices can still use energy.",
    },
    "insulation": {
        "label": "Ceiling / roof insulation",
        "room": "Building shell check",
        "question": "Which upgrade usually gives strong long-term comfort and bill benefits?",
        "options": ["Check/improve ceiling or roof insulation", "Paint the wall a darker colour", "Open windows during winter nights"],
        "correct": "Check/improve ceiling or roof insulation",
        "points": 20,
        "action": "insulation_owner",
        "correct_feedback": "Correct. Insulation helps reduce heat transfer and lowers heating and cooling demand.",
        "wrong_feedback": "Not ideal. Poor insulation can increase heating and cooling demand.",
    },
}


# ---------- Money-first engagement logic ----------
def _currency(value: float) -> str:
    return f"NZ${int(round(value)):,}"


def estimate_money_snapshot(
    monthly_bill: float,
    household_size: str,
    main_problem: str,
    dwelling_type: str = "Detached house",
    home_condition: str = "Average / not sure",
) -> dict:
    """Return a conservative money-first snapshot for the opening hook.

    These are engagement estimates only, not certified savings. The ranges are intentionally
    bounded so the app sounds market-ready without making irresponsible claims.
    Dwelling type and perceived building condition are used only as light calibration signals.
    """
    annual_bill = max(0.0, monthly_bill * 12)
    if annual_bill <= 0:
        waste_low, waste_high = 0.0, 0.0
    else:
        # Conservative avoidable-waste assumption for a first-pass household check-up.
        base_low, base_high = 0.08, 0.22
        if main_problem in {"High winter bill", "High summer bill"}:
            base_low, base_high = 0.10, 0.25
        elif main_problem == "High hot-water bill":
            base_low, base_high = 0.07, 0.20

        # Household size and dwelling/building condition slightly adjust the opening estimate.
        # This is not a compliance calculation; it simply stops the hook being too generic.
        if household_size in {"3-4", "5+"}:
            base_high += 0.03
        if dwelling_type in {"Large detached house", "Detached house"}:
            base_high += 0.02
        elif dwelling_type == "Small apartment / unit":
            base_high -= 0.02
        if home_condition == "Older or draughty":
            base_low += 0.02
            base_high += 0.05
        elif home_condition == "Newer / efficient":
            base_low -= 0.02
            base_high -= 0.04

        base_low = max(0.04, base_low)
        base_high = max(base_low + 0.04, min(base_high, 0.32))
        waste_low = annual_bill * base_low
        waste_high = annual_bill * base_high
    return {
        "annual_bill": annual_bill,
        "waste_low": waste_low,
        "waste_high": waste_high,
        "three_month_low": waste_low / 4,
        "three_month_high": waste_high / 4,
        "monthly_low": waste_low / 12,
        "monthly_high": waste_high / 12,
        "dwelling_type": dwelling_type,
        "home_condition": home_condition,
    }


def estimate_behaviour_saving_pool(savings: dict) -> tuple[float, float]:
    """Estimate the no/low-cost saving pool available to fund first upgrades."""
    behaviour_keys = ["shorter_showers", "thermostat", "standby", "curtains"]
    low = sum(float(savings.get(k, (0, 0))[0]) for k in behaviour_keys if isinstance(savings.get(k, None), tuple))
    high = sum(float(savings.get(k, (0, 0))[1]) for k in behaviour_keys if isinstance(savings.get(k, None), tuple))
    return low, high


def months_to_fund(cost: float, monthly_low: float, monthly_high: float) -> str:
    """Translate a saving pool into a clear funding timeframe."""
    if cost <= 0:
        return "Immediate"
    if monthly_high <= 0:
        return "Not fundable from the current behaviour-saving estimate"
    fastest = max(1, int(-(-cost // max(monthly_high, 1))))
    slowest = max(fastest, int(-(-cost // max(monthly_low, 1)))) if monthly_low > 0 else None
    if slowest is None or slowest > 36:
        return f"From about {fastest}+ months, depending on consistency"
    if fastest == slowest:
        return f"About {fastest} month{'s' if fastest != 1 else ''}"
    return f"About {fastest}–{slowest} months"


def monthly_energy_seasonality(main_problem: str) -> list[float]:
    """Simple New Zealand monthly seasonality pattern for visual decision support.

    Values are normalised to average 1.0 so the user's reported monthly bill remains
    the annual anchor. Winter months are Jun-Aug; summer months are Dec-Feb.
    """
    if main_problem == "High winter bill":
        raw = [0.90, 0.88, 0.82, 0.78, 0.92, 1.28, 1.42, 1.32, 1.02, 0.86, 0.82, 0.88]
    elif main_problem == "High summer bill":
        raw = [1.38, 1.30, 1.05, 0.86, 0.78, 0.82, 0.86, 0.88, 0.92, 1.00, 1.12, 1.43]
    elif main_problem == "High hot-water bill":
        raw = [1.05, 1.02, 1.00, 0.98, 1.00, 1.08, 1.10, 1.08, 1.02, 0.98, 0.98, 1.01]
    else:
        raw = [1.15, 1.10, 0.98, 0.88, 0.90, 1.12, 1.22, 1.15, 0.95, 0.88, 0.92, 1.15]
    avg = sum(raw) / len(raw)
    return [v / avg for v in raw]


def build_energy_pathway_dataframe() -> pd.DataFrame:
    """Create the visual baseline/current/after-strategy annual cost pathway.

    The 'standard operating band' is a benchmark-style target: moderate thermostat use,
    efficient lighting, sensible hot-water behaviour, draught control, and reasonable
    envelope performance. It is aligned with the *intent* of efficient operation and
    NZ Building Code H1 energy-efficiency and Healthy Homes style thinking, but it is not a Building Code or Healthy Homes compliance output.
    """
    months = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
    monthly_bill = float(st.session_state.get("monthly_bill", 250.0))
    bill_problem = st.session_state.get("bill_problem", "Not sure")
    snap = st.session_state.get("money_snapshot") or estimate_money_snapshot(
        monthly_bill,
        st.session_state.get("household_size", "3-4"),
        bill_problem,
        st.session_state.get("dwelling_type", "Detached house"),
        st.session_state.get("home_condition", "Average / not sure"),
    )
    season = monthly_energy_seasonality(bill_problem)
    annual_bill = max(0.0, float(snap.get("annual_bill", monthly_bill * 12)))
    waste_low = max(0.0, float(snap.get("waste_low", annual_bill * 0.08)))
    waste_high = max(waste_low, float(snap.get("waste_high", annual_bill * 0.22)))

    current = [monthly_bill * m for m in season]
    # The optimal band is lower than the current line because this chart uses cost, not performance score.
    standard_upper = [(monthly_bill - waste_low / 12) * m for m in season]
    standard_lower = [(monthly_bill - waste_high / 12) * m for m in season]

    # Behaviour and low-cost upgrades improve progressively; the line moves toward the standard band.
    progress = [0.10, 0.16, 0.23, 0.31, 0.40, 0.50, 0.60, 0.69, 0.77, 0.84, 0.90, 0.94]
    after_strategy = [(monthly_bill - (waste_high / 12) * p) * m for p, m in zip(progress, season)]

    return pd.DataFrame({
        "Month": months,
        "Current estimated pathway": [round(v, 0) for v in current],
        "After applying strategies": [round(max(0, v), 0) for v in after_strategy],
        "Standard operating band - lower": [round(max(0, v), 0) for v in standard_lower],
        "Standard operating band - upper": [round(max(0, v), 0) for v in standard_upper],
    })


def energy_pathway_figure() -> go.Figure:
    df = build_energy_pathway_dataframe()
    fig = go.Figure()
    fig.add_trace(go.Scatter(
        x=df["Month"],
        y=df["Standard operating band - upper"],
        mode="lines",
        name="Standard operating band — upper",
        line=dict(width=0),
        hovertemplate="%{x}: NZ$%{y:.0f}<extra></extra>",
    ))
    fig.add_trace(go.Scatter(
        x=df["Month"],
        y=df["Standard operating band - lower"],
        mode="lines",
        name="Standard operating band",
        fill="tonexty",
        line=dict(width=0),
        hovertemplate="%{x}: NZ$%{y:.0f}<extra></extra>",
    ))
    fig.add_trace(go.Scatter(
        x=df["Month"],
        y=df["Current estimated pathway"],
        mode="lines+markers",
        name="Your current input",
        hovertemplate="%{x}: NZ$%{y:.0f}<extra></extra>",
    ))
    fig.add_trace(go.Scatter(
        x=df["Month"],
        y=df["After applying strategies"],
        mode="lines+markers",
        name="After applying strategies",
        hovertemplate="%{x}: NZ$%{y:.0f}<extra></extra>",
    ))
    fig.update_layout(
        height=385,
        margin=dict(l=20, r=20, t=45, b=20),
        yaxis_title="Estimated monthly energy cost (NZ$)",
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="left", x=0),
    )
    return fig


def current_household_inputs() -> HouseholdInputs:
    """Build the calculation input object from current session-state values."""
    return HouseholdInputs(
        household_size=st.session_state.get("household_size", "3-4"),
        tenure_type=st.session_state.get("tenure_type", "Rented"),
        bill_problem=st.session_state.get("bill_problem", "Not sure"),
        hvac_type=st.session_state.get("hvac_type", "Not sure"),
        solar_status=st.session_state.get("solar_status", "Not sure"),
        monthly_bill_aud=float(st.session_state.get("monthly_bill", 250.0)),
        electricity_price=float(st.session_state.get("electricity_price", 0.35)),
        shower_minutes_current=float(st.session_state.get("shower_current", 10)),
        shower_minutes_target=float(st.session_state.get("shower_target", 4)),
        showers_per_person_per_day=float(st.session_state.get("showers_per_person", 1.0)),
        old_bulbs=int(st.session_state.get("old_bulbs", 8)),
        hours_per_bulb_per_day=float(st.session_state.get("bulb_hours", 3.0)),
        standby_devices=int(st.session_state.get("standby_devices", 8)),
        thermostat_degrees_improved=int(st.session_state.get("thermostat_degrees", 2)),
    )


def current_ranked_actions() -> list:
    """Return ranked actions using the current completed inspection state."""
    return generate_ranked_actions(
        st.session_state.get("selected_actions", []),
        st.session_state.get("tenure_type", "Rented"),
        st.session_state.get("bill_problem", "Not sure"),
        st.session_state.get("solar_status", "Not sure"),
    )


def save_anonymous_progress_if_needed(savings: dict | None = None, ranked: list | None = None, result_text: str | None = None) -> bool:
    """Save the completed anonymous response once, at recommendation unlock.

    The app does not collect names, emails, phone numbers, or contact details. The save
    result is now visible so failed Google Sheets writes are not silently ignored.
    """
    if st.session_state.get("response_saved"):
        return True
    try:
        conn = st.connection("gsheets", type=GSheetsConnection)
        participant_id = get_next_participant_id(conn)
        if savings is None:
            savings = estimate_all_savings(current_household_inputs())
        if ranked is None:
            ranked = current_ranked_actions()
        if result_text is None:
            result_text = score_label(st.session_state.get("score", 0))
        submission = build_anonymous_submission(participant_id, savings, ranked, result_text)
        save_response_to_gsheet(submission)
        st.session_state["participant_id"] = participant_id
        st.session_state["response_saved"] = True
        st.session_state.pop("save_error", None)
        return True
    except Exception as exc:
        st.session_state["response_saved"] = False
        st.session_state["save_error"] = f"{type(exc).__name__}: {exc}"
        return False


def build_funding_pathway(inputs: HouseholdInputs, savings: dict) -> list[dict]:
    """Build a personalised save-to-upgrade pathway.

    The logic excludes irrelevant recommendations: for example, it does not ask users
    already at 4-minute showers to reduce shower time further.
    """
    behaviour_low, behaviour_high = estimate_behaviour_saving_pool(savings)
    monthly_low, monthly_high = behaviour_low / 12, behaviour_high / 12

    steps: list[dict] = []
    shower_gap = max(0, float(inputs.shower_minutes_current) - float(inputs.shower_minutes_target))
    if shower_gap > 0:
        steps.append({
            "stage": "Episode 1 — Recover money without spending",
            "title": "Run a 30-day hot-water challenge",
            "logic": f"Reduce average shower time from {inputs.shower_minutes_current:g} to {inputs.shower_minutes_target:g} minutes.",
            "saving": format_saving_range(savings.get("shorter_showers", (0, 0))),
            "cost": "NZ$0",
            "unlock": "Creates the first saving pool for small upgrades.",
        })
    else:
        steps.append({
            "stage": "Episode 1 — Recover money without spending",
            "title": "Skip shower reduction and target another behaviour",
            "logic": "Your shower target is already tight, so the pathway shifts to thermostat, standby, curtains, and appliance habits.",
            "saving": format_saving_range((max(0, behaviour_low - savings.get("shorter_showers", (0, 0))[0]), max(0, behaviour_high - savings.get("shorter_showers", (0, 0))[1]))),
            "cost": "NZ$0",
            "unlock": "Avoids giving you a generic recommendation that does not fit your behaviour.",
        })

    if inputs.old_bulbs > 0:
        led_cost = max(30, min(240, inputs.old_bulbs * 8))
        steps.append({
            "stage": "Episode 2 — Use saved money for a quick upgrade",
            "title": "Replace frequently used old bulbs with LEDs",
            "logic": f"You reported {inputs.old_bulbs} older bulbs. Start with the rooms used every day.",
            "saving": format_saving_range(savings.get("leds", (0, 0))),
            "cost": f"Approx. {_currency(led_cost)}",
            "unlock": months_to_fund(led_cost, monthly_low, monthly_high),
        })

    # Hot-water cylinder wrapping is presented as a conditional pathway because many New Zealand homes use different hot-water systems and safety/accessibility must be checked first.
    cylinder_wrap_cost = 200
    if inputs.bill_problem == "High hot-water bill" or shower_gap > 0:
        steps.append({
            "stage": "Episode 3 — Fund a hot-water efficiency action",
            "title": "Check whether a hot-water cylinder wrap is relevant",
            "logic": "Only use this step if the home has an accessible electric storage hot-water cylinder and local safety guidance allows wrapping.",
            "saving": "Indicative; depends on system type and existing insulation",
            "cost": f"Example budget: {_currency(cylinder_wrap_cost)}",
            "unlock": months_to_fund(cylinder_wrap_cost, monthly_low, monthly_high),
        })

    draught_budget = 250 if inputs.tenure_type == "Rented" else 750
    steps.append({
        "stage": "Episode 4 — Compound savings into building-shell action",
        "title": "Move from behaviour savings to draught control",
        "logic": "Start with door snakes and weather seals; owners can later consider more complete draught sealing.",
        "saving": format_saving_range(savings.get("draught_sealing", (0, 0))) if "draught_sealing" in savings else "Not fully monetised in this beta",
        "cost": f"Planning budget: {_currency(draught_budget)}",
        "unlock": months_to_fund(draught_budget, monthly_low, monthly_high),
    })

    if inputs.tenure_type == "Owned":
        insulation_budget = 1800
        steps.append({
            "stage": "Episode 5 — Long-term upgrade pathway",
            "title": "Prepare for insulation or larger envelope upgrades",
            "logic": "Use the accumulated saving history to decide whether a professional insulation check is worth it.",
            "saving": "Potentially high, but requires home-specific assessment",
            "cost": f"Planning budget: {_currency(insulation_budget)}+",
            "unlock": months_to_fund(insulation_budget, monthly_low, monthly_high),
        })
    else:
        steps.append({
            "stage": "Episode 5 — Renter pathway",
            "title": "Turn evidence into a landlord/property-manager conversation",
            "logic": "Document draughts, poor curtains, and comfort problems. Ask for realistic upgrades rather than paying for owner-controlled capital works yourself.",
            "saving": "Depends on landlord-approved changes",
            "cost": "NZ$0–low cost for documentation and temporary measures",
            "unlock": "Immediate renter-friendly pathway",
        })
    return steps


# ---------- UI helpers ----------
def _logo_html() -> str:
    """Return the company logo as constrained HTML so Streamlit does not enlarge it."""
    if LOGO_PATH.exists():
        suffix = LOGO_PATH.suffix.lower().replace(".", "") or "png"
        mime = "jpeg" if suffix in {"jpg", "jpeg"} else suffix
        encoded = base64.b64encode(LOGO_PATH.read_bytes()).decode("utf-8")
        return f'<img src="data:image/{mime};base64,{encoded}" alt="Company logo">'
    return '<div class="logo-placeholder"><strong>Your logo here</strong><br>Add your file as<br><code>assets/company_logo.png</code></div>'


def _sidebar_logo_html() -> str:
    """Return a compact logo block for the sidebar."""
    if LOGO_PATH.exists():
        suffix = LOGO_PATH.suffix.lower().replace(".", "") or "png"
        mime = "jpeg" if suffix in {"jpg", "jpeg"} else suffix
        encoded = base64.b64encode(LOGO_PATH.read_bytes()).decode("utf-8")
        logo = f'<img src="data:image/{mime};base64,{encoded}" alt="Company logo">'
    else:
        logo = '<div class="logo-placeholder">Logo</div>'
    return f"""
    <div class="sidebar-brand">
        {logo}
        <div class="sidebar-brand-title">Tech Innovation Experts</div>
        <div class="sidebar-brand-text">Providing technology-driven services across Oceania</div>
        <div class="sidebar-brand-text">support@tinx.co.nz</div>
    </div>
    """


def logo_header() -> None:
    st.markdown(
        f"""
        <div class="header-grid">
            <div class="hero">
                <span class="badge">Beta Version 1.0</span>
                <h1>{APP_TITLE}</h1>
                <p>{TAGLINE}</p>
                <p>Estimate avoidable energy waste, inspect the home, and build a staged saving pathway.</p>
            </div>
            <div class="logo-card">
                {_logo_html()}
                <div class="company-name">Tech Innovation Experts</div>
                <div class="company-tagline">Providing technology-driven services across Oceania</div>
                <div class="company-email">Email: support@tinx.co.nz</div>
            </div>
        </div>
        <div class="compliance-strip">
            <div class="compliance-icon">H1</div>
            <div><strong>NZ Building Code H1 / Healthy Homes aligned.</strong> Educational guidance only; this tool is not a certified energy assessment, NZ Building Code H1 assessment, or Healthy Homes compliance statement.</div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def sidebar_status() -> None:
    st.sidebar.title("Challenge status")
    st.sidebar.metric("Score", f"{st.session_state.score}/100")
    st.sidebar.progress(min(st.session_state.score, 100) / 100)
    st.sidebar.write(f"**Player type:** {player_title(st.session_state.score)}")
    st.sidebar.write(f"**Result:** {score_label(st.session_state.score)}")
    st.sidebar.write(f"Completed checks: {len(st.session_state.completed_hotspots)}/7")
    with st.sidebar:
        render_money_recovered_counter("identified")
        st.markdown("**Badges unlocked**")
        render_badge_wall()
        render_mission_map()
    if st.sidebar.button("Restart challenge"):
        reset_app()
        st.rerun()
    st.sidebar.markdown(_sidebar_logo_html(), unsafe_allow_html=True)


def next_stage(stage: str) -> None:
    """Move to another page and remember where the user came from."""
    current_stage = st.session_state.get("stage", "welcome")
    if "nav_history" not in st.session_state:
        st.session_state["nav_history"] = []
    if stage != current_stage:
        st.session_state["nav_history"].append(current_stage)
    st.session_state.stage = stage
    st.rerun()


def go_back() -> None:
    """Return to the previous page without clearing the user's entered values."""
    history = st.session_state.get("nav_history", [])
    if history:
        st.session_state.stage = history.pop()
        st.session_state["nav_history"] = history
    else:
        st.session_state.stage = "welcome"
    st.rerun()


def render_back_button() -> None:
    """Show a simple global back button on all non-welcome pages."""
    current_stage = st.session_state.get("stage", "welcome")
    if current_stage != "welcome":
        c1, c2 = st.columns([0.18, 0.82])
        with c1:
            if st.button("← Back", use_container_width=True, key=f"back_button_{current_stage}"):
                go_back()
        with c2:
            st.markdown("<div class='small-muted' style='padding-top:.45rem;'>Go back to review or change your previous choices.</div>", unsafe_allow_html=True)


def unlock_hotspot_for_edit(hotspot_key: str) -> None:
    """Allow users to change an already completed inspection answer.

    This removes the previous score/risk/comfort impact for that hotspot before the user
    chooses another answer, avoiding double scoring.
    """
    if hotspot_key not in st.session_state.completed_hotspots:
        return

    was_correct = bool(st.session_state.get(f"{hotspot_key}_correct", False))
    hotspot = HOTSPOTS[hotspot_key]

    if was_correct:
        st.session_state.score = max(0, int(st.session_state.score) - int(hotspot.get("points", 0)))
        st.session_state.bill_risk = min(100, int(st.session_state.bill_risk) + 7)
        st.session_state.comfort = max(0, int(st.session_state.comfort) - 5)
        action = hotspot.get("action")
        if hotspot_key == "insulation" and st.session_state.get("tenure_type") == "Rented":
            action = "insulation_renter"
        if action in st.session_state.selected_actions:
            st.session_state.selected_actions.remove(action)
    else:
        st.session_state.bill_risk = max(0, int(st.session_state.bill_risk) - 3)

    st.session_state.completed_hotspots.discard(hotspot_key)
    st.session_state.last_feedback.pop(hotspot_key, None)
    st.session_state.pop(f"{hotspot_key}_answer", None)
    st.session_state.pop(f"{hotspot_key}_correct", None)
    # Remove the Streamlit radio widget state so it can be selected again.
    st.session_state.pop(f"answer_{hotspot_key}", None)


def update_for_answer(hotspot_key: str, selected: str) -> None:
    hotspot = HOTSPOTS[hotspot_key]
    already_done = hotspot_key in st.session_state.completed_hotspots
    is_correct = selected == hotspot["correct"]
    feedback_text = hotspot["correct_feedback"] if is_correct else hotspot["wrong_feedback"]

    if is_correct:
        if not already_done:
            st.session_state.score += hotspot["points"]
            st.session_state.bill_risk = max(0, st.session_state.bill_risk - 7)
            st.session_state.comfort = min(100, st.session_state.comfort + 5)
            st.session_state.selected_actions.append(hotspot["action"])
            if hotspot_key == "insulation":
                if st.session_state.get("tenure_type") == "Rented":
                    st.session_state.selected_actions[-1] = "insulation_renter"
    else:
        if not already_done:
            st.session_state.bill_risk = min(100, st.session_state.bill_risk + 3)

    st.session_state.completed_hotspots.add(hotspot_key)
    st.session_state[f"{hotspot_key}_answer"] = selected
    st.session_state[f"{hotspot_key}_correct"] = is_correct
    st.session_state.last_feedback[hotspot_key] = {
        "is_correct": is_correct,
        "selected": selected,
        "correct": hotspot["correct"],
        "message": feedback_text,
    }


def gauge(value: int, title: str) -> go.Figure:
    fig = go.Figure(go.Indicator(
        mode="gauge+number",
        value=value,
        gauge={"axis": {"range": [0, 100]}, "bar": {"thickness": 0.25}},
        title={"text": title},
    ))
    fig.update_layout(height=240, margin=dict(l=20, r=20, t=50, b=10))
    return fig


def _load_font(size: int, bold: bool = False):
    candidates = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf",
    ]
    for candidate in candidates:
        if Path(candidate).exists():
            return ImageFont.truetype(candidate, size)
    return ImageFont.load_default()


def _draw_centered(draw: ImageDraw.ImageDraw, text: str, y: int, font, fill: str, width: int = 1200) -> int:
    bbox = draw.textbbox((0, 0), text, font=font)
    x = (width - (bbox[2] - bbox[0])) / 2
    draw.text((x, y), text, font=font, fill=fill)
    return y + (bbox[3] - bbox[1]) + 12


def _draw_wrapped_centered(draw: ImageDraw.ImageDraw, text: str, y: int, font, fill: str, width: int, wrap: int, line_gap: int = 7) -> int:
    for line in textwrap.wrap(text, width=wrap):
        bbox = draw.textbbox((0, 0), line, font=font)
        x = (width - (bbox[2] - bbox[0])) / 2
        draw.text((x, y), line, font=font, fill=fill)
        y += (bbox[3] - bbox[1]) + line_gap
    return y


def generate_certificate_png(name: str, score: int, result_label: str) -> bytes:
    name = name.strip() or "Home-energy Participant"
    today = date.today().strftime("%d %B %Y")
    width, height = 1400, 1400
    img = Image.new("RGB", (width, height), "#EEF2F7")
    draw = ImageDraw.Draw(img)

    # Main square certificate surface
    margin = 70
    draw.rounded_rectangle((margin, margin, width - margin, height - margin), radius=54, fill="#FFFFFF", outline="#0F766E", width=8)
    draw.rounded_rectangle((102, 102, width - 102, height - 102), radius=38, outline="#99F6E4", width=3)
    draw.rounded_rectangle((132, 132, width - 132, height - 132), radius=30, outline="#E2E8F0", width=2)

    # Decorative top band and corner shapes
    draw.rectangle((margin, margin, width - margin, 235), fill="#0F766E")
    draw.polygon([(margin, 235), (270, 235), (160, 345)], fill="#0EA5E9")
    draw.polygon([(width - margin, 235), (width - 270, 235), (width - 160, 345)], fill="#0EA5E9")

    # Visible right-side ribbon
    ribbon = [(width - 315, margin), (width - 70, margin), (width - 70, 315)]
    draw.polygon(ribbon, fill="#0F766E")
    draw.line((width - 315, margin, width - 70, 315), fill="#CCFBF1", width=3)
    # rotated ribbon text
    ribbon_layer = Image.new("RGBA", (430, 80), (0, 0, 0, 0))
    rd = ImageDraw.Draw(ribbon_layer)
    rd.text((20, 22), "RECOGNITION", font=_load_font(28, True), fill="#FFFFFF")
    ribbon_layer = ribbon_layer.rotate(-45, expand=True)
    img.paste(ribbon_layer, (width - 365, 78), ribbon_layer)

    # Logo on top band
    if LOGO_PATH.exists():
        try:
            logo = Image.open(LOGO_PATH).convert("RGBA")
            logo.thumbnail((240, 92))
            logo_x = int((width - logo.width) / 2)
            logo_y = 105 + int((92 - logo.height) / 2)
            img.paste(logo, (logo_x, logo_y), logo)
        except Exception:
            _draw_centered(draw, "Tech Innovation Experts", 130, _load_font(28, True), "#FFFFFF", width)
    else:
        _draw_centered(draw, "Tech Innovation Experts", 130, _load_font(28, True), "#FFFFFF", width)

    y = 315
    y = _draw_centered(draw, "BETA VERSION 1.0", y, _load_font(22, True), "#0F766E", width)
    y = _draw_centered(draw, "Certificate of Completion", y + 12, _load_font(60, True), "#0F172A", width)
    y = _draw_centered(draw, "The Home-energy check-up (New Zealand)", y + 8, _load_font(31, True), "#475569", width)
    y = _draw_centered(draw, "Tech Innovation Experts", y + 8, _load_font(26, True), "#0F766E", width)
    y = _draw_centered(draw, "This certificate recognises", y + 34, _load_font(28), "#475569", width)

    name_font = _load_font(62, True)
    name_lines = textwrap.wrap(name, width=28) or [name]
    for line in name_lines[:2]:
        y = _draw_centered(draw, line, y + 10, name_font, "#0F766E", width)
    draw.line((340, y + 6, width - 340, y + 6), fill="#99F6E4", width=5)
    y += 42

    body = "for completing the home-energy check-up and building a practical, prioritised energy action plan."
    y = _draw_wrapped_centered(draw, body, y, _load_font(30), "#334155", width, wrap=68, line_gap=11)
    y += 34

    badges = [f"Result: {result_label}", f"Score: {score}/100", f"Date: {today}"]
    badge_font = _load_font(25, True)
    badge_h = 62
    badge_widths = [draw.textbbox((0, 0), b, font=badge_font)[2] + 62 for b in badges]
    total_w = sum(badge_widths) + 24 * (len(badges) - 1)
    x = int((width - total_w) / 2)
    for b, bw in zip(badges, badge_widths):
        draw.rounded_rectangle((x, y, x + bw, y + badge_h), radius=31, fill="#ECFDF5", outline="#A7F3D0", width=2)
        bbox = draw.textbbox((0, 0), b, font=badge_font)
        draw.text((x + (bw - (bbox[2] - bbox[0])) / 2, y + 18), b, font=badge_font, fill="#065F46")
        x += bw + 24
    y += badge_h + 55

    # Recognition seal
    seal_cx, seal_cy, seal_r = width // 2, y + 68, 74
    draw.ellipse((seal_cx - seal_r, seal_cy - seal_r, seal_cx + seal_r, seal_cy + seal_r), fill="#0F766E", outline="#0EA5E9", width=6)
    _draw_centered(draw, "TIE", seal_cy - 42, _load_font(38, True), "#FFFFFF", width)
    _draw_centered(draw, "RECOGNISED", seal_cy + 2, _load_font(15, True), "#CCFBF1", width)

    footer_y = height - 205
    _draw_centered(draw, "Tech Innovation Experts", footer_y, _load_font(26, True), "#0F172A", width)
    _draw_centered(draw, "Providing technology-driven services across Oceania | support@tinx.co.nz", footer_y + 40, _load_font(22), "#475569", width)
    _draw_centered(draw, "Recognition certificate only. Not a certified energy assessment or accredited NZ Building Code H1 assessment or Healthy Homes compliance statement.", height - 105, _load_font(18), "#64748B", width)

    buffer = BytesIO()
    img.save(buffer, format="PNG", quality=95)
    return buffer.getvalue()

def welcome_screen() -> None:
    logo_header()
    col1, col2, col3 = st.columns(3)
    with col1:
        st.markdown("<div class='card'><h3>1. Profile</h3><p>Answer five simple household questions.</p></div>", unsafe_allow_html=True)
    with col2:
        st.markdown("<div class='card'><h3>2. Inspect</h3><p>Complete seven home-energy checks.</p></div>", unsafe_allow_html=True)
    with col3:
        st.markdown("<div class='card'><h3>3. Action plan</h3><p>Receive a prioritised plan with indicative savings.</p></div>", unsafe_allow_html=True)
    st.markdown("<br>", unsafe_allow_html=True)
    st.info("This public prototype gives educational guidance, not a certified home energy assessment. Dollar estimates are indicative and depend on tariff, climate, equipment, and behaviour.")
    if st.button("Show me my money leak", type="primary", use_container_width=True):
        next_stage("money")


def money_screen() -> None:
    logo_header()
    st.title("Start with the money")
    st.caption("The first step is not another generic energy tip. It is a quick estimate of how much avoidable energy waste may be leaving your pocket.")

    with st.form("money_snapshot_form"):
        c1, c2, c3 = st.columns(3)
        with c1:
            monthly_bill = st.number_input("Approximate monthly energy bill (NZ$)", min_value=0.0, max_value=3000.0, value=float(st.session_state.get("monthly_bill", 250.0)), step=10.0)
            dwelling_options = ["Small apartment / unit", "Townhouse / small house", "Detached house", "Large detached house"]
            dwelling_type = st.selectbox("What best describes the home?", dwelling_options, index=dwelling_options.index(st.session_state.get("dwelling_type", "Detached house")) if st.session_state.get("dwelling_type", "Detached house") in dwelling_options else 2)
        with c2:
            household_size = st.radio("People in the home", ["1", "2", "3-4", "5+"], horizontal=True, index=["1", "2", "3-4", "5+"].index(st.session_state.get("household_size", "3-4")) if st.session_state.get("household_size", "3-4") in ["1", "2", "3-4", "5+"] else 2)
            condition_options = ["Newer / efficient", "Average / not sure", "Older or draughty"]
            home_condition = st.radio("How does the home feel thermally?", condition_options, index=condition_options.index(st.session_state.get("home_condition", "Average / not sure")) if st.session_state.get("home_condition", "Average / not sure") in condition_options else 1)
        with c3:
            bill_problem = st.selectbox("Where does the bill hurt most?", ["High winter bill", "High summer bill", "High hot-water bill", "Not sure"], index=["High winter bill", "High summer bill", "High hot-water bill", "Not sure"].index(st.session_state.get("bill_problem", "Not sure")) if st.session_state.get("bill_problem", "Not sure") in ["High winter bill", "High summer bill", "High hot-water bill", "Not sure"] else 3)
            st.markdown("<div class='small-muted'>These five inputs are enough to create a stronger first estimate without turning the start into a long audit.</div>", unsafe_allow_html=True)
        submitted = st.form_submit_button("Estimate my saving opportunity", type="primary", use_container_width=True)

    if submitted or "money_snapshot" in st.session_state:
        if submitted:
            st.session_state["monthly_bill"] = monthly_bill
            st.session_state["household_size"] = household_size
            st.session_state["bill_problem"] = bill_problem
            st.session_state["dwelling_type"] = dwelling_type
            st.session_state["home_condition"] = home_condition
            st.session_state["money_snapshot"] = estimate_money_snapshot(monthly_bill, household_size, bill_problem, dwelling_type, home_condition)

        snap = st.session_state["money_snapshot"]
        st.markdown(
            f"""
            <div class='money-hero'>
                <h2>Your energy bill may contain avoidable waste</h2>
                <p>Based on this quick starting scenario, your estimated annual energy spend is:</p>
                <div class='money-number'>{_currency(snap['annual_bill'])}</div>
                <p>A realistic first-stage saving opportunity may be around <strong>{_currency(snap['waste_low'])}–{_currency(snap['waste_high'])} per year</strong>, depending on your habits, tariff, appliances, and building condition.</p>
                <div class='money-sub'>Indicative decision-support estimate only. This is not a certified energy assessment or a guaranteed saving.</div>
            </div>
            """,
            unsafe_allow_html=True,
        )
        c1, c2, c3 = st.columns(3)
        with c1:
            st.markdown(f"<div class='money-card'><h4>Possible monthly recovery</h4><div class='value'>{_currency(snap['monthly_low'])}–{_currency(snap['monthly_high'])}</div><p>Money that may be recovered through behaviour and low-cost actions.</p></div>", unsafe_allow_html=True)
        with c2:
            st.markdown(f"<div class='money-card'><h4>Possible 3-month target</h4><div class='value'>{_currency(snap['three_month_low'])}–{_currency(snap['three_month_high'])}</div><p>A short challenge target before larger upgrades.</p></div>", unsafe_allow_html=True)
        with c3:
            st.markdown(f"<div class='money-card'><h4>12-month saving pathway</h4><div class='value'>{_currency(snap['waste_low'])}–{_currency(snap['waste_high'])}</div><p>The pathway will rank actions and show what each saving can fund next.</p></div>", unsafe_allow_html=True)

        st.markdown("### Your annual energy-cost pathway")
        st.caption("This graph shows where you are now, where an efficient operating range may sit, and where your household could move after applying the recommended strategies. It is a benchmark-style cost pathway, not a NZ Building Code H1 assessment, Healthy Homes compliance statement, or guaranteed bill forecast.")
        st.plotly_chart(energy_pathway_figure(), use_container_width=True)
        st.markdown(
            f"""
            <div class='success-box'>
                <strong>What this means:</strong> your current pathway is estimated from the bill you entered. If you follow the personalised strategy pathway, the model estimates a possible annual saving of <strong>{_currency(snap['waste_low'])}–{_currency(snap['waste_high'])}</strong>.
            </div>
            """,
            unsafe_allow_html=True,
        )

        st.markdown("### Want to reduce this?")
        st.write("The next step checks whether your saving opportunity is coming from hot water, heating/cooling, lighting, standby devices, draughts, curtains, or insulation. Irrelevant recommendations will be skipped.")
        if st.button("Yes — build my personalised saving pathway", type="primary", use_container_width=True):
            next_stage("profile")


def profile_screen() -> None:
    st.title("Household profile")
    st.caption("A short profile helps personalise the energy action plan without making the tool feel like a survey.")
    with st.form("profile_form"):
        c1, c2 = st.columns(2)
        with c1:
            household_size_options = ["1", "2", "3-4", "5+"]
            household_size = st.radio("How many people live in the home?", household_size_options, horizontal=True, index=household_size_options.index(st.session_state.get("household_size", "3-4")) if st.session_state.get("household_size", "3-4") in household_size_options else 2)
            tenure_type = st.radio("Is the home rented or owned?", ["Rented", "Owned"], horizontal=True)
            bill_problem_options = ["High winter bill", "High summer bill", "High hot-water bill", "Not sure"]
            bill_problem = st.selectbox("What is the main bill problem?", bill_problem_options, index=bill_problem_options.index(st.session_state.get("bill_problem", "Not sure")) if st.session_state.get("bill_problem", "Not sure") in bill_problem_options else 3)
            dwelling_type = st.selectbox("Home type", ["Small apartment / unit", "Townhouse / small house", "Detached house", "Large detached house"], index=["Small apartment / unit", "Townhouse / small house", "Detached house", "Large detached house"].index(st.session_state.get("dwelling_type", "Detached house")) if st.session_state.get("dwelling_type", "Detached house") in ["Small apartment / unit", "Townhouse / small house", "Detached house", "Large detached house"] else 2)
        with c2:
            hvac_type = st.selectbox("What is the main heating/cooling system?", ["Heat pump", "Gas heater / fireplace", "Portable electric heater", "Ducted heat pump system", "Not sure"])
            solar_status = st.radio("Does the home have solar panels?", ["Yes", "No", "Not sure"], horizontal=True)
            monthly_bill = st.number_input("Approximate monthly energy bill (NZ$)", min_value=0.0, max_value=2000.0, value=float(st.session_state.get("monthly_bill", 250.0)), step=10.0)
            condition_options = ["Newer / efficient", "Average / not sure", "Older or draughty"]
            home_condition = st.radio("Thermal feel of the home", condition_options, index=condition_options.index(st.session_state.get("home_condition", "Average / not sure")) if st.session_state.get("home_condition", "Average / not sure") in condition_options else 1)
        st.markdown("#### Optional assumptions for indicative savings")
        c3, c4, c5 = st.columns(3)
        with c3:
            electricity_price = st.number_input("Electricity price (NZ$/kWh)", 0.05, 1.50, 0.35, 0.01)
            thermostat_degrees = st.slider("Thermostat improvement from current setting (°C)", 0, 6, 2)
        with c4:
            shower_current = st.slider("Current average shower time (minutes)", 2, 20, 10)
            shower_target = st.slider("Target shower time (minutes)", 2, 12, 4)
            showers_per_person = st.slider("Showers per person per day", 0.5, 2.0, 1.0, 0.1)
        with c5:
            old_bulbs = st.slider("Old halogen/incandescent bulbs used often", 0, 30, 8)
            bulb_hours = st.slider("Average hours per bulb per day", 0.5, 10.0, 3.0, 0.5)
            standby_devices = st.slider("Standby devices to manage", 0, 30, 8)
        submitted = st.form_submit_button("Save profile and inspect home", type="primary", use_container_width=True)
    if submitted:
        # If the user came back and changed profile assumptions, clear prior inspection answers.
        for hotspot_key in list(st.session_state.get("completed_hotspots", set())):
            unlock_hotspot_for_edit(hotspot_key)
        st.session_state.update({
            "household_size": household_size,
            "tenure_type": tenure_type,
            "bill_problem": bill_problem,
            "dwelling_type": dwelling_type,
            "home_condition": home_condition,
            "hvac_type": hvac_type,
            "solar_status": solar_status,
            "monthly_bill": monthly_bill,
            "electricity_price": electricity_price,
            "shower_current": shower_current,
            "shower_target": shower_target,
            "showers_per_person": showers_per_person,
            "old_bulbs": old_bulbs,
            "bulb_hours": bulb_hours,
            "standby_devices": standby_devices,
            "thermostat_degrees": thermostat_degrees,
            "money_snapshot": estimate_money_snapshot(monthly_bill, household_size, bill_problem, dwelling_type, home_condition),
        })
        next_stage("inspection")


def inspection_screen() -> None:
    episode_header(
        "Mission 2",
        "Hunt the seven money leaks",
        "Each check gives points and shows the likely saving logic. The selected inspection area stays open after every answer.",
        "⚡",
    )
    sidebar_status()
    render_unlock_message()

    snap = st.session_state.get("money_snapshot", {})
    if snap:
        render_payoff_strip(float(snap.get("waste_low", 0)), float(snap.get("waste_high", 0)), "target saving opportunity during this challenge")
    render_money_recovered_counter("identified so far")
    st.markdown("### Badges unlocked")
    render_badge_wall()

    selected_area = render_inspection_area_selector()
    area = INSPECTION_AREAS[selected_area]
    done, total = area_completion(selected_area)

    c1, c2 = st.columns([0.62, 0.38])
    with c1:
        st.markdown(f"### {area['label']} — {done}/{total} completed")
        st.caption(area["description"])
        for key in area["keys"]:
            render_hotspot(key)
    with c2:
        st.plotly_chart(gauge(st.session_state.bill_risk, "Bill risk"), use_container_width=True)
        st.plotly_chart(gauge(st.session_state.comfort, "Comfort readiness"), use_container_width=True)
        commercial_teaser_panel("Next episode unlock")

    if len(st.session_state.completed_hotspots) >= 7:
        st.info("When you unlock the recommendations, the completed responses are saved anonymously for tool improvement. No name, email address, phone number, or contact detail is collected or stored.")
        render_save_status()
        if st.button("Save responses and show my recommendations", type="primary", use_container_width=True):
            inputs = current_household_inputs()
            savings = estimate_all_savings(inputs)
            ranked = current_ranked_actions()
            result_text = score_label(st.session_state.get("score", 0))
            saved = save_anonymous_progress_if_needed(savings, ranked, result_text)
            if saved:
                next_stage("plan")
            else:
                st.rerun()
    else:
        st.info("Complete all seven checks to unlock the money roadmap.")


def render_hotspot(key: str) -> None:
    hotspot = HOTSPOTS[key]
    done = key in st.session_state.completed_hotspots
    feedback = st.session_state.last_feedback.get(key)
    with st.expander(("✅ " if done else "🔎 ") + hotspot["label"] + f" — {hotspot['room']}", expanded=True if feedback else not done):
        st.write(f"**Question:** {hotspot['question']}")
        render_hotspot_option_visuals(key)
        if done:
            if st.button("Change this answer", key=f"edit_{key}"):
                unlock_hotspot_for_edit(key)
                st.rerun()
            answer = st.radio("Choose one answer", hotspot["options"], key=f"answer_{key}", index=None, disabled=True)
        else:
            answer = st.radio("Choose one answer", hotspot["options"], key=f"answer_{key}", index=None)
            if st.button("Check answer", key=f"check_{key}", disabled=answer is None):
                update_for_answer(key, answer)
                st.rerun()
        if feedback:
            if feedback["is_correct"]:
                st.success(
                    "**Correct answer**\n\n"
                    f"**Your selection:** {feedback['selected']}\n\n"
                    f"{feedback['message']}"
                )
            else:
                st.error(
                    "**Not the best answer**\n\n"
                    f"**Your selection:** {feedback['selected']}\n\n"
                    f"**Better choice:** {feedback['correct']}\n\n"
                    f"{feedback['message']}"
                )
        elif done:
            st.caption("This hotspot has already been scored.")


def plan_screen() -> None:
    st.title("Your Energy Action Plan")
    sidebar_status()

    inputs = current_household_inputs()
    savings = estimate_all_savings(inputs)
    ranked = current_ranked_actions()
    top_actions = top_three_actions(ranked)

    result_text = score_label(st.session_state.score)
    completion_date = date.today().strftime("%d %B %Y")
    total_low = int(sum(v[0] for v in savings.values()))
    total_high = int(sum(v[1] for v in savings.values()))
    st.session_state["result_category"] = result_text
    st.session_state["estimated_annual_savings"] = f"NZ${total_low:,}–NZ${total_high:,}"
    certificate_name = st.session_state.get("certificate_display_name", "") or st.session_state.get("participant_id", "Anonymous Participant")

    st.markdown(f"<div class='success-box'><h3>{result_text}</h3><p>Score: {st.session_state.score}/100. Fix energy waste first, then consider bigger upgrades.</p></div>", unsafe_allow_html=True)

    st.markdown("### Top three actions")
    cols = st.columns(3)
    for idx, (key, action) in enumerate(top_actions):
        with cols[idx]:
            saving_text = format_saving_range(savings[key]) if key in savings else "Impact depends on the home"
            st.markdown(
                f"""
                <div class='card'>
                    <span class='badge'>{action['category']}</span>
                    <h4>{action['title']}</h4>
                    <p>{action['recommendation']}</p>
                    <p><strong>Indicative saving:</strong> {saving_text}</p>
                </div>
                """,
                unsafe_allow_html=True,
            )

    st.markdown("### Full recommendation list")
    rows = []
    for key, action in ranked:
        rows.append({
            "Priority": action["priority"],
            "Action": action["title"],
            "Category": action["category"],
            "Cost": action["cost_level"],
            "Impact": action["impact_level"],
            "Indicative annual saving": format_saving_range(savings[key]) if key in savings else "Not monetised in prototype",
        })
    st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)

    st.markdown("### Savings chart")
    monetised_rows = [
        {"Action": key.replace("_", " ").title(), "Low": val[0], "High": val[1]}
        for key, val in savings.items() if val[1] > 0
    ]
    if monetised_rows:
        chart_df = pd.DataFrame(monetised_rows)
        fig = go.Figure()
        fig.add_trace(go.Bar(x=chart_df["Action"], y=chart_df["Low"], name="Low estimate"))
        fig.add_trace(go.Bar(x=chart_df["Action"], y=chart_df["High"], name="High estimate"))
        fig.update_layout(yaxis_title="Indicative annual saving (NZ$)", barmode="group", height=420)
        st.plotly_chart(fig, use_container_width=True)

    st.markdown("### Your save-to-upgrade pathway")
    behaviour_low, behaviour_high = estimate_behaviour_saving_pool(savings)
    st.markdown(
        f"""
        <div class='unlock-card'>
            <span class='unlock-badge'>Personalised funding loop</span>
            <h4>Use behaviour savings to fund the next energy upgrade</h4>
            <p>Your estimated no/low-cost behaviour-saving pool is approximately <strong>{_currency(behaviour_low)}–{_currency(behaviour_high)} per year</strong>. The pathway below shows how those savings could fund the next action instead of asking you to spend everything upfront.</p>
        </div>
        """,
        unsafe_allow_html=True,
    )
    for step in build_funding_pathway(inputs, savings):
        st.markdown(
            f"""
            <div class='pathway-step'>
                <h4>{step['stage']}: {step['title']}</h4>
                <p><strong>Logic:</strong> {step['logic']}</p>
                <p><strong>Estimated saving:</strong> {step['saving']}</p>
                <p><strong>Indicative cost:</strong> {step['cost']}</p>
                <p><strong>When it may be unlocked:</strong> {step['unlock']}</p>
            </div>
            """,
            unsafe_allow_html=True,
        )
    st.caption("This pathway is deliberately conservative. It should motivate action without pretending to be a certified bill forecast.")

    csv = pd.DataFrame(rows).to_csv(index=False).encode("utf-8")
    st.download_button("Download action plan as CSV", csv, "energy_action_plan.csv", "text/csv", use_container_width=True)

    st.markdown("### Completion recognition")
    st.info("Optional: if you would like a certificate, type your name below. This name is used only on the on-screen certificate and is not saved to Google Sheets. You can then take a screenshot or print the page.")
    certificate_input = st.text_input(
        "Name to display on certificate only",
        value=st.session_state.get("certificate_display_name", ""),
        placeholder="Type your name here for the certificate",
        key="certificate_display_name_input",
    )
    st.session_state["certificate_display_name"] = certificate_input.strip()
    certificate_name = st.session_state.get("certificate_display_name", "") or st.session_state.get("participant_id", "Anonymous Participant")

    st.markdown(
        f"""
        <div class='certificate-card'>
            <div class='certificate-kicker'>Beta Version 1.0</div>
            <div class='certificate-title'>Certificate of Completion</div>
            <div class='certificate-subtitle'>The Home-energy check-up (New Zealand)</div>
            <div class='certificate-company'>Tech Innovation Experts</div>
            <div class='certificate-small'>This certificate recognises</div>
            <div class='certificate-name'>{certificate_name}</div>
            <div class='certificate-small'>for completing the home-energy check-up and building a practical, prioritised energy action plan.</div>
            <div class='certificate-meta'>
                <div class='certificate-pill'>Result: {result_text}</div>
                <div class='certificate-pill'>Score: {st.session_state.score}/100</div>
                <div class='certificate-pill'>Date: {completion_date}</div>
            </div>
            <div class='certificate-footer'>Tech Innovation Experts | Providing technology-driven services across Oceania | support@tinx.co.nz</div>
            <div class='certificate-disclaimer'>Recognition certificate only. Not a certified energy assessment or accredited NZ Building Code H1 assessment or Healthy Homes compliance statement.</div>
        </div>
        """,
        unsafe_allow_html=True,
    )
    st.caption("This recognition certificate is shown on-screen only. Screenshot or print the page if you want to keep a copy.")


# ---------- Commercial engagement layer: challenge, visuals, and saving logic ----------
RECOMMENDATION_VISUALS = {
    "thermostat": {
        "icon": "🌡️",
        "image": "https://images.unsplash.com/photo-1556912173-3bb406ef7e77?auto=format&fit=crop&w=900&q=80",
        "caption": "Heating discipline: keep occupied rooms in a healthy, moderate range and avoid unnecessary overheating."
    },
    "leds": {
        "icon": "💡",
        "image": "https://images.unsplash.com/photo-1507473885765-e6ed057f782c?auto=format&fit=crop&w=900&q=80",
        "caption": "LED lighting: low-cost upgrade with quick payback potential."
    },
    "curtains": {
        "icon": "🪟",
        "image": "https://images.unsplash.com/photo-1618221195710-dd6b41faaea6?auto=format&fit=crop&w=900&q=80",
        "caption": "Curtains and blinds: reduce summer heat gain and winter heat loss."
    },
    "draught": {
        "icon": "🚪",
        "image": "https://images.unsplash.com/photo-1505693416388-ac5ce068fe85?auto=format&fit=crop&w=900&q=80",
        "caption": "Draught control: stop paid heating/cooling escaping through gaps."
    },
    "shower": {
        "icon": "🚿",
        "image": "https://images.unsplash.com/photo-1584622650111-993a426fbf0a?auto=format&fit=crop&w=900&q=80",
        "caption": "Hot water behaviour: shorter showers can recover money quickly."
    },
    "standby": {
        "icon": "🔌",
        "image": "https://images.unsplash.com/photo-1621905252507-b35492cc74b4?auto=format&fit=crop&w=900&q=80",
        "caption": "Standby control: stop small loads quietly draining your bill."
    },
    "insulation": {
        "icon": "🏠",
        "image": "https://images.unsplash.com/photo-1560518883-ce09059eeffa?auto=format&fit=crop&w=900&q=80",
        "caption": "Building shell: insulation and envelope upgrades improve long-term performance."
    },
    "cylinder_wrap": {
        "icon": "♨️",
        "image": "https://images.unsplash.com/photo-1621905251918-48416bd8575a?auto=format&fit=crop&w=900&q=80",
        "caption": "Hot-water cylinder wrapping: relevant only for suitable accessible storage systems."
    },
}

ACTION_KEY_TO_VISUAL = {
    "thermostat": "thermostat",
    "leds": "leds",
    "curtains": "curtains",
    "draught_sealing": "draught",
    "shorter_showers": "shower",
    "standby": "standby",
    "insulation_owner": "insulation",
    "insulation_renter": "insulation",
}


def saving_fee_monthly() -> float:
    return float(st.session_state.get("saving_fee", 19.0))


def annual_saving_cost() -> float:
    return saving_fee_monthly() * 12


def roi_summary(low_saving: float, high_saving: float, monthly_fee: float | None = None) -> dict:
    """Compare indicative savings with a possible saving fee.

    This is not a payment system. It is a commercial value framing tool: if the user pays
    X per month, the app shows whether the estimated annual saving opportunity is larger.
    """
    if monthly_fee is None:
        monthly_fee = saving_fee_monthly()
    annual_fee = monthly_fee * 12
    net_low = low_saving - annual_fee
    net_high = high_saving - annual_fee
    multiple_low = (low_saving / annual_fee) if annual_fee > 0 else 0
    multiple_high = (high_saving / annual_fee) if annual_fee > 0 else 0
    return {
        "monthly_fee": monthly_fee,
        "annual_fee": annual_fee,
        "net_low": net_low,
        "net_high": net_high,
        "multiple_low": multiple_low,
        "multiple_high": multiple_high,
    }


def value_bar(label: str, value: str, note: str, icon: str = "💰") -> None:
    st.markdown(
        f"""
        <div class='value-card'>
            <div class='value-icon'>{icon}</div>
            <div>
                <div class='value-label'>{label}</div>
                <div class='value-number'>{value}</div>
                <div class='value-note'>{note}</div>
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def episode_header(episode: str, title: str, subtitle: str, icon: str = "💸") -> None:
    st.markdown(
        f"""
        <div class='episode-hero'>
            <div class='episode-token'>{icon}</div>
            <div>
                <div class='episode-label'>{episode}</div>
                <h2>{title}</h2>
                <p>{subtitle}</p>
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def render_payoff_strip(low_saving: float, high_saving: float, context: str = "estimated saving opportunity") -> None:
    """Show the money value without saving framing."""
    st.markdown(
        f"""
        <div class='payoff-strip'>
            <div><strong>Saving target</strong><br><span>{context}</span></div>
            <div><strong>{_currency(low_saving)}–{_currency(high_saving)}</strong><br><span>possible annual recovery</span></div>
            <div><strong>{_currency(low_saving / 12)}–{_currency(high_saving / 12)}</strong><br><span>possible monthly recovery</span></div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def _local_visual_path(visual_key: str) -> Path:
    return ROOT / "assets" / "recommendations" / f"{visual_key}.jpg"


def render_recommendation_visual(visual_key: str, height: int = 190) -> None:
    """Render recommendation image with fixed aspect ratio.

    Local files in assets/recommendations/ are preferred. If they are missing, the app
    falls back to the provided online image URL. All visuals are forced into the same
    16:9 frame so the recommendation cards stay aligned.
    """
    data = RECOMMENDATION_VISUALS.get(visual_key, RECOMMENDATION_VISUALS["thermostat"])
    local_path = _local_visual_path(visual_key)
    try:
        if local_path.exists():
            suffix = local_path.suffix.lower().replace(".", "") or "jpg"
            mime = "jpeg" if suffix in {"jpg", "jpeg"} else suffix
            img_src = f"data:image/{mime};base64,{base64.b64encode(local_path.read_bytes()).decode('utf-8')}"
        else:
            img_src = data["image"]
        st.markdown(
            f"""
            <div class='photo-frame'>
                <img class='reco-img' src="{img_src}" alt="{data['caption']}">
            </div>
            <div class='reco-caption'>{data['icon']} {data['caption']}</div>
            """,
            unsafe_allow_html=True,
        )
    except Exception:
        st.markdown(
            f"""
            <div class='visual-fallback'>
                <div class='visual-icon'>{data['icon']}</div>
                <div>{data['caption']}</div>
            </div>
            """,
            unsafe_allow_html=True,
        )


def saving_range_for_hotspot(key: str) -> tuple[float, float]:
    try:
        savings = estimate_all_savings(current_household_inputs())
    except Exception:
        return (0.0, 0.0)
    action_key = HOTSPOTS[key].get("action", "")
    if key == "insulation" and st.session_state.get("tenure_type") == "Rented":
        action_key = "insulation_renter"
    return savings.get(action_key, (0.0, 0.0)) if isinstance(savings.get(action_key, None), tuple) else (0.0, 0.0)


def commercial_teaser_panel(title: str = "Next advisor step") -> None:
    snap = st.session_state.get("money_snapshot", {})
    low = float(snap.get("waste_low", 0))
    high = float(snap.get("waste_high", 0))
    st.markdown(
        f"""
        <div class='premium-card'>
            <span class='premium-badge'>Next unlock</span>
            <h4>{title}</h4>
            <p>The next stage keeps the user moving: track one action, update the saving estimate, then unlock the next upgrade.</p>
            <p><strong>Current target:</strong> {_currency(low)}–{_currency(high)} possible annual recovery.</p>
        </div>
        """,
        unsafe_allow_html=True,
    )


# Extend CSS after the original CSS block.
st.markdown("""
<style>
.episode-hero {
    display:flex; gap:1rem; align-items:center;
    padding:1.25rem 1.35rem; border-radius:24px;
    background:linear-gradient(135deg,#0F172A 0%,#0F766E 100%);
    color:white; margin:1rem 0 1.15rem 0;
    box-shadow:0 12px 30px rgba(15,23,42,.18);
}
.episode-token {
    width:58px; height:58px; min-width:58px; border-radius:18px;
    display:flex; align-items:center; justify-content:center;
    background:rgba(255,255,255,.15); font-size:1.85rem;
}
.episode-label {font-size:.82rem; letter-spacing:.12em; text-transform:uppercase; font-weight:900; opacity:.82;}
.episode-hero h2 {margin:.15rem 0 .25rem 0; font-size:1.75rem; letter-spacing:-.02em;}
.episode-hero p {margin:0; opacity:.93;}
.value-card {
    display:flex; gap:.75rem; align-items:flex-start;
    padding:1rem; border-radius:18px; background:#FFFFFF;
    border:1px solid #A7F3D0; box-shadow:0 1px 5px rgba(15,23,42,.06);
    min-height:128px;
}
.value-icon {
    width:42px; height:42px; min-width:42px; border-radius:14px;
    background:#ECFDF5; display:flex; align-items:center; justify-content:center; font-size:1.35rem;
}
.value-label {font-size:.82rem; font-weight:800; color:#064E3B; text-transform:uppercase; letter-spacing:.05em;}
.value-number {font-size:1.55rem; font-weight:950; color:#0F172A; margin:.12rem 0;}
.value-note {font-size:.86rem; color:#475569; line-height:1.35;}
.payoff-strip {
    display:grid; grid-template-columns:1fr 1fr 1fr; gap:.85rem;
    padding:1rem; border-radius:18px; background:#FFFBEB; border:1px solid #FDE68A;
    margin:.85rem 0 1rem 0;
}
.payoff-strip strong {color:#78350F; font-size:1rem;}
.payoff-strip span {color:#92400E; font-size:.86rem;}
.premium-card {
    padding:1.1rem; border-radius:20px; background:linear-gradient(135deg,#111827 0%,#312E81 100%);
    color:white; box-shadow:0 12px 30px rgba(15,23,42,.16); margin:.75rem 0;
}
.premium-card h4 {margin:.4rem 0 .25rem 0; font-size:1.18rem;}
.premium-card p {margin:.35rem 0; opacity:.94;}
.premium-badge {display:inline-block; padding:.22rem .6rem; border-radius:999px; background:rgba(255,255,255,.16); font-weight:900; font-size:.76rem;}
.photo-frame img {border-radius:18px;}
.visual-fallback {
    min-height:165px; border-radius:18px; background:#F8FAFC; border:1px dashed #CBD5E1;
    display:flex; flex-direction:column; align-items:center; justify-content:center; text-align:center; color:#334155; padding:1rem;
}
.visual-icon {font-size:2.4rem; margin-bottom:.4rem;}
.series-card {
    padding:1rem; border-radius:18px; background:#F8FAFC; border:1px solid #E2E8F0; margin:.65rem 0;
}
.series-card h4 {margin:0 0 .35rem 0;}
.series-meta {display:flex; gap:.5rem; flex-wrap:wrap; margin-top:.6rem;}
.series-pill {padding:.32rem .55rem; border-radius:999px; background:#ECFDF5; color:#065F46; font-size:.78rem; font-weight:800;}
.hotspot-money {
    padding:.75rem; border-radius:14px; background:#F8FAFC; border:1px solid #CBD5E1; color:#334155; margin:.55rem 0;
}
.photo-frame {
    width:100%;
    aspect-ratio: 16 / 9;
    overflow:hidden;
    border-radius:18px;
    border:1px solid #E2E8F0;
    background:#F8FAFC;
    box-shadow:0 1px 6px rgba(15,23,42,.08);
}
.reco-img {
    width:100%;
    height:100%;
    object-fit:cover;
    display:block;
}
.reco-caption {
    color:#64748B;
    font-size:.80rem;
    line-height:1.45;
    margin:.45rem 0 .15rem 0;
}
@media (max-width: 760px) {
    .payoff-strip {grid-template-columns:1fr;}
    .episode-hero {align-items:flex-start;}
}
.money-highlight {
    display:inline-block;
    padding:.12rem .38rem;
    border-radius:.55rem;
    background:#FEF3C7;
    color:#78350F;
    border:1px solid #F59E0B;
    font-weight:950;
    box-shadow:0 1px 0 rgba(120,53,15,.08);
}
.money-line {
    padding:.85rem 1rem;
    border-radius:18px;
    background:linear-gradient(135deg,#FFFBEB 0%,#ECFDF5 100%);
    border:1px solid #FBBF24;
    color:#78350F;
    font-weight:800;
    margin:.65rem 0;
}
.money-line strong {
    color:#064E3B;
    background:#FEF3C7;
    border:1px solid #F59E0B;
    border-radius:.55rem;
    padding:.08rem .32rem;
}
.money-card, .value-card, .payoff-strip, .unlock-card, .hotspot-money {
    box-shadow:0 6px 18px rgba(15,23,42,.09);
}
.value-number, .payoff-strip strong {
    background:#FEF3C7;
    border:1px solid #F59E0B;
    border-radius:.7rem;
    padding:.12rem .45rem;
    display:inline-block;
}
.money-hero .money-number {
    color:#064E3B !important;
    background:#FFFFFF !important;
    border:2px solid #FBBF24 !important;
}
.visual-choice-grid {
    display:grid;
    grid-template-columns:repeat(3,minmax(0,1fr));
    gap:.55rem;
    margin:.55rem 0 0 0;
    width:100%;
    max-width:100%;
    box-sizing:border-box;
}
.visual-choice-card {
    min-height:96px;
    border-radius:15px;
    border:1px solid #CBD5E1;
    background:#FFFFFF;
    padding:.62rem .5rem;
    text-align:center;
    box-shadow:0 1px 5px rgba(15,23,42,.06);
    display:flex;
    flex-direction:column;
    align-items:center;
    justify-content:center;
    overflow:hidden;
    box-sizing:border-box;
    min-width:0;
}
.visual-choice-icon {font-size:1.85rem; line-height:1; margin-bottom:.3rem;}
.visual-choice-title {
    font-weight:900;
    color:#0F172A;
    font-size:.82rem;
    line-height:1.14;
    max-width:100%;
    word-break:normal;
    overflow-wrap:normal;
    hyphens:none;
}
.visual-choice-note {
    color:#64748B;
    font-size:.68rem;
    line-height:1.18;
    margin-top:.20rem;
    max-width:100%;
    word-break:normal;
    overflow-wrap:normal;
    hyphens:none;
}
.visual-strip {
    padding:.9rem;
    border-radius:18px;
    background:#F8FAFC;
    border:1px solid #E2E8F0;
    margin:.55rem 0 1rem 0;
    width:100%;
}
.visual-strip-title {font-weight:900; color:#0F172A; margin-bottom:.35rem;}
.hotspot-visual-strip {
    overflow:hidden;
}
.hotspot-visual-strip .visual-choice-grid {
    grid-template-columns:repeat(3,minmax(0,1fr));
}
.hotspot-visual-strip .visual-choice-card {
    min-height:98px;
}
@media (max-width: 900px) {.visual-choice-grid {grid-template-columns:repeat(3,minmax(0,1fr));}}
@media (max-width: 680px) {.visual-choice-grid {grid-template-columns:1fr;}}

.gamified-status-card {padding:1rem; border-radius:18px; background:#FFFFFF; border:1px solid #A7F3D0; box-shadow:0 6px 18px rgba(15,23,42,.08); margin:.75rem 0;}
.gamified-status-card h4 {margin:.1rem 0 .35rem 0; color:#0F172A;}
.gamified-status-card p {margin:.15rem 0; color:#475569;}
.mission-map {padding:.85rem; border-radius:16px; background:#F8FAFC; border:1px solid #CBD5E1; margin:.75rem 0;}
.mission-title {font-weight:900; color:#0F172A; margin-bottom:.45rem;}
.mission-row {display:flex; gap:.5rem; align-items:center; padding:.35rem 0; color:#334155; border-top:1px solid #E2E8F0;}
.mission-row:first-of-type {border-top:none;}
.badge-wall {display:flex; flex-wrap:wrap; gap:.5rem; padding:.65rem; border-radius:16px; background:#F8FAFC; border:1px solid #E2E8F0; margin:.7rem 0;}
.badge-wall.empty {color:#64748B; font-size:.9rem;}
.earned-badge {display:flex; gap:.35rem; align-items:center; padding:.45rem .65rem; border-radius:999px; background:#ECFDF5; border:1px solid #A7F3D0; color:#065F46; font-size:.82rem;}
.recovery-counter {display:flex; justify-content:space-between; gap:1rem; align-items:center; padding:1rem; border-radius:20px; background:linear-gradient(135deg,#FEF3C7 0%,#ECFDF5 100%); border:1px solid #F59E0B; margin:.75rem 0;}
.counter-label {font-size:.82rem; font-weight:900; color:#78350F; text-transform:uppercase; letter-spacing:.06em;}
.counter-value {font-size:1.7rem; font-weight:950; color:#064E3B;}
.counter-note {font-size:.86rem; color:#475569; max-width:330px;}
.roadmap-card {padding:1.25rem; border-radius:24px; background:linear-gradient(135deg,#0F172A 0%,#0F766E 100%); color:white; box-shadow:0 14px 34px rgba(15,23,42,.18); margin:1rem 0;}
.roadmap-card h3 {margin:.25rem 0 .8rem 0; font-size:1.6rem;}
.roadmap-kicker {display:inline-block; padding:.22rem .6rem; border-radius:999px; background:rgba(255,255,255,.16); font-weight:900; font-size:.76rem;}
.roadmap-grid {display:grid; grid-template-columns:repeat(4,minmax(0,1fr)); gap:.7rem; margin:.8rem 0;}
.roadmap-grid div {padding:.75rem; border-radius:16px; background:rgba(255,255,255,.12);}
.roadmap-grid strong {color:#CCFBF1;}
.challenge-card {display:flex; gap:.9rem; align-items:flex-start; padding:1.1rem; border-radius:22px; background:#FFFBEB; border:1px solid #FBBF24; margin:.8rem 0 1rem 0; color:#78350F;}
.challenge-icon {width:48px; height:48px; min-width:48px; border-radius:16px; display:flex; align-items:center; justify-content:center; background:#FEF3C7; font-size:1.55rem;}
.challenge-card h4 {margin:.1rem 0 .35rem 0; color:#78350F;}
.challenge-card p {margin:.25rem 0;}
@media (max-width: 900px) {.roadmap-grid {grid-template-columns:1fr 1fr;} .recovery-counter {align-items:flex-start; flex-direction:column;}}
@media (max-width: 600px) {.roadmap-grid {grid-template-columns:1fr;}}
</style>
""", unsafe_allow_html=True)




st.markdown("""
<style>
.inspection-area-grid {
    display:grid;
    grid-template-columns:repeat(3,minmax(0,1fr));
    gap:.75rem;
    margin:.7rem 0 1rem 0;
}
.inspection-area-card {
    padding:1rem;
    border-radius:18px;
    background:#FFFFFF;
    border:1px solid #CBD5E1;
    box-shadow:0 4px 14px rgba(15,23,42,.07);
}
.inspection-area-card.active {
    border:2px solid #2563EB;
    background:#EFF6FF;
}
.inspection-area-card.completed {
    border:2px solid #16A34A;
    background:#ECFDF5;
}
.inspection-area-card.completed h4::after {
    content:"  ✓";
    color:#16A34A;
    font-weight:900;
}
.inspection-area-card.completed.active {
    border:2px solid #0F766E;
    background:#DCFCE7;
}
.inspection-area-card h4 {margin:.05rem 0 .3rem 0; color:#0F172A;}
.inspection-area-card p {margin:0; color:#475569; font-size:.88rem;}
.save-status-box {
    padding:1rem;
    border-radius:18px;
    border:1px solid #CBD5E1;
    background:#F8FAFC;
    margin:.8rem 0;
}
.save-status-ok {
    padding:1rem;
    border-radius:18px;
    border:1px solid #A7F3D0;
    background:#ECFDF5;
    color:#065F46;
    margin:.8rem 0;
}
.save-status-error {
    padding:1rem;
    border-radius:18px;
    border:1px solid #FCA5A5;
    background:#FEF2F2;
    color:#991B1B;
    margin:.8rem 0;
    white-space:pre-wrap;
}
@media (max-width: 760px) {.inspection-area-grid {grid-template-columns:1fr;}}
</style>
""", unsafe_allow_html=True)

def _visual_choice_card(icon: str, title: str, note: str = "") -> str:
    return f"""
    <div class='visual-choice-card'>
        <div class='visual-choice-icon'>{icon}</div>
        <div class='visual-choice-title'>{title}</div>
        <div class='visual-choice-note'>{note}</div>
    </div>
    """


def render_visual_options(title: str, cards: list[tuple[str, str, str]], css_class: str = "") -> None:
    """Render compact picture-style option cards beside ordinary Streamlit inputs."""
    html_cards = "".join(_visual_choice_card(icon, heading, note) for icon, heading, note in cards)
    extra_class = f" {css_class}" if css_class else ""
    st.markdown(
        f"""
        <div class='visual-strip{extra_class}'>
            <div class='visual-strip-title'>{title}</div>
            <div class='visual-choice-grid'>{html_cards}</div>
        </div>
        """,
        unsafe_allow_html=True,
    )


DWELLING_VISUAL_CARDS = [
    ("🏢", "Apartment / unit", "Small footprint"),
    ("🏘️", "Townhouse", "Attached or compact"),
    ("🏠", "Detached house", "Typical standalone home"),
    ("🏡", "Large detached", "More shell area"),
]

THERMAL_VISUAL_CARDS = [
    ("✨", "Newer / efficient", "Fewer obvious leaks"),
    ("❔", "Average / unsure", "Needs quick checks"),
    ("🌬️", "Older / draughty", "Likely money leak"),
]

HVAC_VISUAL_CARDS = [
    ("❄️", "Heat pump", "Heating and cooling"),
    ("🔥", "Gas heater / fireplace", "Winter comfort"),
    ("♨️", "Portable electric", "High running risk"),
    ("🌬️", "Ducted heat pump system", "Whole-home airflow"),
]

SOLAR_VISUAL_CARDS = [
    ("☀️", "Solar panels", "Daytime generation"),
    ("🔋", "Battery-ready", "Future storage option"),
    ("⚡", "Grid only", "Bill depends on tariff"),
]

CHALLENGE_VISUAL_CARDS = [
    ("🚿", "Shower time", "Hot-water cost"),
    ("💡", "Old bulbs", "LED upgrade"),
    ("🔌", "Standby devices", "Hidden load"),
    ("🌡️", "Thermostat", "Healthy set-points"),
]

HOTSPOT_OPTION_VISUALS = {
    "thermostat": [
        ("🥶", "18°C cooling", "High load"),
        ("✅", "25–27°C", "Healthy heating range"),
        ("🥵", "26°C heating", "High load"),
    ],
    "leds": [
        ("💡", "LED", "Efficient choice"),
        ("🔥", "Halogen", "Wastes more heat"),
        ("🕯️", "Incandescent", "Old high-use bulb"),
    ],
    "curtains": [
        ("🪟", "Open glass", "Heat gain/loss"),
        ("🧵", "Curtains/blinds", "Protects comfort"),
        ("☀️", "Summer shading", "Reduces overheating"),
    ],
    "standby": [
        ("🔌", "Wall switch", "Stop standby load"),
        ("🧠", "Smart board", "Manage devices"),
        ("📺", "Always-on", "Hidden money leak"),
    ],
    "shower": [
        ("🚿", "Short shower", "Lower hot-water use"),
        ("⏱️", "Timer", "Simple behaviour cue"),
        ("♨️", "Hot water", "Major energy use"),
    ],
    "draught": [
        ("🚪", "Door snake", "Low-cost seal"),
        ("🪟", "Weather strip", "Seal gaps"),
        ("🌬️", "Draught", "Paid air escaping"),
    ],
    "insulation": [
        ("🏠", "Ceiling", "Insulation check"),
        ("🌡️", "Heat transfer", "Lower demand"),
        ("🛠️", "Owner upgrade", "Needs assessment"),
    ],
}


def render_hotspot_option_visuals(key: str) -> None:
    cards = HOTSPOT_OPTION_VISUALS.get(key)
    if cards:
        render_visual_options("Visual guide", cards, css_class="hotspot-visual-strip")


def money_sentence(text: str) -> None:
    """Render a single highlighted sentence when the content is about bills, cost, or savings."""
    st.markdown(f"<div class='money-line'>{text}</div>", unsafe_allow_html=True)


# ---------- Gamified engagement layer ----------
def player_title(score: int) -> str:
    """Translate the numeric score into a friendly progress identity."""
    score = int(score or 0)
    if score >= 96:
        return "Home Energy Master"
    if score >= 81:
        return "Energy Efficiency Champion"
    if score >= 61:
        return "Smart Saver"
    if score >= 36:
        return "Home Energy Explorer"
    return "Energy Leak Beginner"


BADGE_LIBRARY = {
    "thermostat": ("🌡️", "Thermostat Controller"),
    "leds": ("💡", "LED Upgrader"),
    "curtains": ("🪟", "Curtain Strategist"),
    "draught": ("🚪", "Draught Defender"),
    "shower": ("🚿", "Hot Water Saver"),
    "standby": ("🔌", "Standby Slayer"),
    "insulation": ("🏠", "Insulation Planner"),
}


def earned_badges() -> list[tuple[str, str]]:
    badges: list[tuple[str, str]] = []
    for key, badge in BADGE_LIBRARY.items():
        if st.session_state.get(f"{key}_correct") is True:
            badges.append(badge)
    return badges


def render_badge_wall() -> None:
    badges = earned_badges()
    if not badges:
        st.markdown("<div class='badge-wall empty'>No badges unlocked yet. Correct moves unlock badges.</div>", unsafe_allow_html=True)
        return
    html = "".join(f"<div class='earned-badge'><span>{icon}</span><strong>{label}</strong></div>" for icon, label in badges)
    st.markdown(f"<div class='badge-wall'>{html}</div>", unsafe_allow_html=True)


def money_recovered_identified() -> tuple[float, float]:
    try:
        savings = estimate_all_savings(current_household_inputs())
    except Exception:
        return (0.0, 0.0)
    low = high = 0.0
    for key in st.session_state.get("completed_hotspots", set()):
        if st.session_state.get(f"{key}_correct") is not True:
            continue
        action_key = HOTSPOTS[key].get("action", "")
        if key == "insulation" and st.session_state.get("tenure_type") == "Rented":
            action_key = "insulation_renter"
        val = savings.get(action_key)
        if isinstance(val, tuple):
            low += float(val[0])
            high += float(val[1])
    return low, high


def render_money_recovered_counter(context: str = "identified so far") -> None:
    low, high = money_recovered_identified()
    st.markdown(
        f"""
        <div class='recovery-counter'>
            <div>
                <div class='counter-label'>Money recovery {context}</div>
                <div class='counter-value'>{_currency(low)}–{_currency(high)} / year</div>
            </div>
            <div class='counter-note'>This increases when users choose evidence-based energy-saving moves.</div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def render_mission_map() -> None:
    stage = st.session_state.get("stage", "welcome")
    completed = len(st.session_state.get("completed_hotspots", set()))
    mission_rows = [
        ("Money Scan", "✅" if stage in {"profile", "inspection", "plan"} or "money_snapshot" in st.session_state else "🔒"),
        ("Challenge Setup", "✅" if stage in {"inspection", "plan"} else ("▶️" if stage == "profile" else "🔒")),
        (f"Leak Hunt {completed}/7", "✅" if completed >= 7 else ("▶️" if stage == "inspection" else "🔒")),
        ("Roadmap", "✅" if stage == "plan" else "🔒"),
    ]
    html = "".join(f"<div class='mission-row'><span>{icon}</span><strong>{label}</strong></div>" for label, icon in mission_rows)
    st.markdown(f"<div class='mission-map'><div class='mission-title'>Mission map</div>{html}</div>", unsafe_allow_html=True)


def render_unlock_message() -> None:
    completed = len(st.session_state.get("completed_hotspots", set()))
    if completed == 3 and not st.session_state.get("unlock_3_seen"):
        st.session_state["unlock_3_seen"] = True
        st.success("First saving clue unlocked. You have enough evidence to see where early money recovery may start.")
    elif completed == 5 and not st.session_state.get("unlock_5_seen"):
        st.session_state["unlock_5_seen"] = True
        st.success("Top money-leak category unlocked. Finish the final checks to reveal the full roadmap.")
    elif completed == 7 and not st.session_state.get("unlock_7_seen"):
        st.session_state["unlock_7_seen"] = True
        st.success("Full saving roadmap unlocked. Your completed answers can now be converted into a prioritised action plan.")


def first_challenge_options(ranked: list, savings: dict) -> list[str]:
    options = []
    for key, action in ranked:
        title = action.get("title", key.replace("_", " " ).title())
        saving = format_saving_range(savings[key]) if key in savings else "impact depends on the home"
        options.append(f"{title} — {saving}")
    return options[:6]


def render_roadmap_card(result_text: str, total_low: float, total_high: float, first_action: str) -> None:
    st.markdown(
        f"""
        <div class='roadmap-card'>
            <span class='roadmap-kicker'>Personal roadmap card</span>
            <h3>Your Home Energy Roadmap</h3>
            <div class='roadmap-grid'>
                <div><strong>Player type</strong><br>{player_title(st.session_state.score)}</div>
                <div><strong>Result</strong><br>{result_text}</div>
                <div><strong>First action</strong><br>{first_action}</div>
                <div><strong>Estimated annual saving</strong><br>{_currency(total_low)}–{_currency(total_high)}</div>
            </div>
            <p>The next practical step is not to read everything again. It is to run one small action consistently for 30 days.</p>
        </div>
        """,
        unsafe_allow_html=True,
    )


def render_30_day_challenge(ranked: list, savings: dict) -> str:
    options = first_challenge_options(ranked, savings)
    if not options:
        options = ["Track one energy-saving habit for 30 days"]
    default = st.session_state.get("first_30_day_action", options[0])
    if default not in options:
        default = options[0]
    chosen = st.selectbox("Choose your first 30-day challenge", options, index=options.index(default), key="first_30_day_action")
    st.markdown(
        f"""
        <div class='challenge-card'>
            <div class='challenge-icon'>🎯</div>
            <div>
                <h4>Your 30-day challenge has started</h4>
                <p><strong>{chosen}</strong></p>
                <p>Do this first, then come back to update the assumptions and unlock the next upgrade decision. Behaviour first; capital upgrades second.</p>
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )
    return chosen




# ---------- Persistent inspection navigation and save diagnostics ----------
INSPECTION_AREAS = {
    "living": {
        "label": "💡 Living room",
        "description": "Thermostat, lighting, curtains, and standby appliances",
        "keys": ["thermostat", "leds", "curtains", "standby"],
    },
    "bathroom": {
        "label": "🚿 Bathroom",
        "description": "Hot-water and shower behaviour",
        "keys": ["shower"],
    },
    "shell": {
        "label": "🏠 Building shell",
        "description": "Draughts, doors, windows, roof, and insulation",
        "keys": ["draught", "insulation"],
    },
}


def area_completion(area_key: str) -> tuple[int, int]:
    keys = INSPECTION_AREAS[area_key]["keys"]
    done = sum(1 for key in keys if key in st.session_state.get("completed_hotspots", set()))
    return done, len(keys)


def render_inspection_area_selector() -> str:
    if "inspection_area" not in st.session_state:
        st.session_state["inspection_area"] = "living"

    st.markdown("### Choose inspection area")
    st.caption("Use these large buttons instead of tabs. The selected area stays open after every answer, so users do not get pushed back to the first section.")

    cols = st.columns(3)
    for col, area_key in zip(cols, INSPECTION_AREAS.keys()):
        area = INSPECTION_AREAS[area_key]
        done, total = area_completion(area_key)
        active = st.session_state.get("inspection_area") == area_key
        completed = done == total
        card_classes = ["inspection-area-card"]
        if completed:
            card_classes.append("completed")
        if active:
            card_classes.append("active")
        card_class = " ".join(card_classes)
        status_text = "Completed — go to the next area" if completed else ("Current area" if active else "Open this area")
        with col:
            st.markdown(
                f"""
                <div class='{card_class}'>
                    <h4>{area['label']}</h4>
                    <p>{area['description']}</p>
                    <p><strong>{done}/{total} completed</strong></p>
                    <p><em>{status_text}</em></p>
                </div>
                """,
                unsafe_allow_html=True,
            )
            if st.button(f"Open {area['label']}", key=f"open_area_{area_key}", use_container_width=True):
                st.session_state["inspection_area"] = area_key
                st.rerun()
    return st.session_state.get("inspection_area", "living")


def render_save_status() -> None:
    if st.session_state.get("response_saved"):
        st.markdown(
            f"<div class='save-status-ok'><strong>Saved to Google Sheets.</strong><br>Participant ID: {st.session_state.get('participant_id', 'not available')}</div>",
            unsafe_allow_html=True,
        )
    elif st.session_state.get("save_error"):
        st.markdown(
            f"<div class='save-status-error'><strong>Google Sheets save failed.</strong><br>{st.session_state.get('save_error')}</div>",
            unsafe_allow_html=True,
        )
        st.info("The roadmap is not opened until saving succeeds. Check Streamlit Secrets, Google Sheet sharing permission, enabled APIs, and the spreadsheet URL.")
    else:
        st.markdown(
            "<div class='save-status-box'><strong>Save status:</strong> not saved yet. Completed responses will be saved when you unlock the roadmap.</div>",
            unsafe_allow_html=True,
        )


# ---------- Overridden market-ready screens ----------
def welcome_screen() -> None:
    logo_header()
    episode_header(
        "Mission intro",
        "Can you recover the money leaking from your home energy bill?",
        "This home energy challenge helps users find hidden bill waste, unlock evidence-based actions, and commit to one practical 30-day saving move.",
        "💸",
    )
    c1, c2, c3 = st.columns(3)
    with c1:
        value_bar("Mission 1", "Money leak scan", "Estimate annual cost and avoidable waste before asking for technical details.", "💰")
    with c2:
        value_bar("Mission 2", "Seven-leak hunt", "Complete short checks to reveal where the bill is leaking.", "⚡")
    with c3:
        value_bar("Mission 3", "30-day challenge", "Choose one action and turn the roadmap into a practical commitment.", "🚀")

    st.markdown("<br>", unsafe_allow_html=True)
    st.caption("Indicative decision-support estimates only. Results depend on tariff, climate, equipment, and behaviour.")
    render_mission_map()
    if st.button("Start my home energy challenge", type="primary", use_container_width=True):
        next_stage("money")


def money_screen() -> None:
    logo_header()
    episode_header(
        "Mission 1",
        "Energy Money Leak Scan",
        "Answer a few money-first questions. The app will compare your current bill pathway with a strategy pathway.",
        "💰",
    )

    with st.form("money_snapshot_form"):
        c1, c2, c3 = st.columns(3)
        with c1:
            monthly_bill = st.number_input("Approximate monthly energy bill (NZ$)", min_value=0.0, max_value=3000.0, value=float(st.session_state.get("monthly_bill", 250.0)), step=10.0)
            dwelling_options = ["Small apartment / unit", "Townhouse / small house", "Detached house", "Large detached house"]
            dwelling_type = st.selectbox("What best describes the home?", dwelling_options, index=dwelling_options.index(st.session_state.get("dwelling_type", "Detached house")) if st.session_state.get("dwelling_type", "Detached house") in dwelling_options else 2)
        with c2:
            household_size = st.radio("People in the home", ["1", "2", "3-4", "5+"], horizontal=True, index=["1", "2", "3-4", "5+"].index(st.session_state.get("household_size", "3-4")) if st.session_state.get("household_size", "3-4") in ["1", "2", "3-4", "5+"] else 2)
            condition_options = ["Newer / efficient", "Average / not sure", "Older or draughty"]
            home_condition = st.radio("How does the home feel thermally?", condition_options, index=condition_options.index(st.session_state.get("home_condition", "Average / not sure")) if st.session_state.get("home_condition", "Average / not sure") in condition_options else 1)
        with c3:
            bill_problem = st.selectbox("Where does the bill hurt most?", ["High winter bill", "High summer bill", "High hot-water bill", "Not sure"], index=["High winter bill", "High summer bill", "High hot-water bill", "Not sure"].index(st.session_state.get("bill_problem", "Not sure")) if st.session_state.get("bill_problem", "Not sure") in ["High winter bill", "High summer bill", "High hot-water bill", "Not sure"] else 3)
            st.markdown("<div class='small-muted'>This is deliberately short. The goal is to show money first, then unlock the deeper inspection.</div>", unsafe_allow_html=True)
        submitted = st.form_submit_button("Reveal my saving opportunity", type="primary", use_container_width=True)

    if submitted or "money_snapshot" in st.session_state:
        if submitted:
            st.session_state["monthly_bill"] = monthly_bill
            st.session_state["household_size"] = household_size
            st.session_state["bill_problem"] = bill_problem
            st.session_state["dwelling_type"] = dwelling_type
            st.session_state["home_condition"] = home_condition
            st.session_state["money_snapshot"] = estimate_money_snapshot(monthly_bill, household_size, bill_problem, dwelling_type, home_condition)

        snap = st.session_state["money_snapshot"]
        st.markdown(
            f"""
            <div class='money-hero'>
                <h2>Your bill may contain recoverable energy money</h2>
                <p>Based on your starting scenario, your estimated annual energy spend is:</p>
                <div class='money-number'>{_currency(snap['annual_bill'])}</div>
                <p>First-stage recoverable opportunity: <strong>{_currency(snap['waste_low'])}–{_currency(snap['waste_high'])} per year</strong>.</p>
                <p>The next steps show which actions can reduce the leak first and what those savings may fund next.</p>
                <div class='money-sub'>Indicative decision-support estimate only. No saving is guaranteed.</div>
            </div>
            """,
            unsafe_allow_html=True,
        )
        c1, c2, c3 = st.columns(3)
        with c1:
            value_bar("Current annual spend", _currency(snap["annual_bill"]), "This is your starting money baseline.", "💳")
        with c2:
            value_bar("Possible annual recovery", f"{_currency(snap['waste_low'])}–{_currency(snap['waste_high'])}", "This is the saving prize the challenge is trying to unlock.", "💰")
        with c3:
            value_bar("Possible monthly recovery", f"{_currency(snap['monthly_low'])}–{_currency(snap['monthly_high'])}", "The short-term money target for the first challenge.", "📈")

        render_payoff_strip(snap["waste_low"], snap["waste_high"], "first-stage recoverable opportunity")
        money_sentence(f"Money focus: the app now highlights the recoverable target of <strong>{_currency(snap['waste_low'])}–{_currency(snap['waste_high'])} per year</strong> before asking users to inspect the house.")

        st.markdown("### Your annual energy-cost pathway")
        st.caption("The current line is based on the bill you entered. The strategy line shows how the bill could move if the recommended behaviour and low-cost actions are followed. The band is a benchmark-style efficient operating range, not a compliance result.")
        st.plotly_chart(energy_pathway_figure(), use_container_width=True)

        st.markdown("### What the next episodes unlock")
        c4, c5, c6 = st.columns(3)
        with c4:
            st.markdown("<div class='series-card'><h4>Episode 2 — The inspection</h4><p>Find which household habits or features are costing money.</p><div class='series-meta'><span class='series-pill'>7 checks</span><span class='series-pill'>score points</span></div></div>", unsafe_allow_html=True)
        with c5:
            st.markdown("<div class='series-card'><h4>Episode 3 — The action plan</h4><p>Rank actions by saving potential, cost level, and fit to your household.</p><div class='series-meta'><span class='series-pill'>money first</span><span class='series-pill'>personalised</span></div></div>", unsafe_allow_html=True)
        with c6:
            st.markdown("<div class='series-card'><h4>Episode 4 — Upgrade roadmap</h4><p>Use recovered money to fund the next efficiency move.</p><div class='series-meta'><span class='series-pill'>payback</span><span class='series-pill'>next unlock</span></div></div>", unsafe_allow_html=True)

        if st.button("Continue to household details", type="primary", use_container_width=True):
            next_stage("profile")


def profile_screen() -> None:
    episode_header(
        "Mission 2 setup",
        "Set the rules of your saving challenge",
        "These assumptions personalise the money calculation and stop the advisor from giving generic recommendations.",
        "🎯",
    )
    snap = st.session_state.get("money_snapshot", {})
    if snap:
        render_payoff_strip(float(snap.get("waste_low", 0)), float(snap.get("waste_high", 0)), "available target before detailed inspection")

    with st.form("profile_form"):
        c1, c2 = st.columns(2)
        with c1:
            household_size_options = ["1", "2", "3-4", "5+"]
            household_size = st.radio("How many people live in the home?", household_size_options, horizontal=True, index=household_size_options.index(st.session_state.get("household_size", "3-4")) if st.session_state.get("household_size", "3-4") in household_size_options else 2)
            tenure_type = st.radio("Is the home rented or owned?", ["Rented", "Owned"], horizontal=True)
            bill_problem_options = ["High winter bill", "High summer bill", "High hot-water bill", "Not sure"]
            bill_problem = st.selectbox("Main bill pressure", bill_problem_options, index=bill_problem_options.index(st.session_state.get("bill_problem", "Not sure")) if st.session_state.get("bill_problem", "Not sure") in bill_problem_options else 3)
            dwelling_type = st.selectbox("Home type", ["Small apartment / unit", "Townhouse / small house", "Detached house", "Large detached house"], index=["Small apartment / unit", "Townhouse / small house", "Detached house", "Large detached house"].index(st.session_state.get("dwelling_type", "Detached house")) if st.session_state.get("dwelling_type", "Detached house") in ["Small apartment / unit", "Townhouse / small house", "Detached house", "Large detached house"] else 2)
        with c2:
            hvac_type = st.selectbox("Main heating/cooling system", ["Heat pump", "Gas heater / fireplace", "Portable electric heater", "Ducted heat pump system", "Not sure"])
            solar_status = st.radio("Does the home have solar panels?", ["Yes", "No", "Not sure"], horizontal=True)
            monthly_bill = st.number_input("Approximate monthly energy bill (NZ$)", min_value=0.0, max_value=3000.0, value=float(st.session_state.get("monthly_bill", 250.0)), step=10.0)
            condition_options = ["Newer / efficient", "Average / not sure", "Older or draughty"]
            home_condition = st.radio("Thermal feel of the home", condition_options, index=condition_options.index(st.session_state.get("home_condition", "Average / not sure")) if st.session_state.get("home_condition", "Average / not sure") in condition_options else 1)
        st.markdown("#### Challenge assumptions")
        c3, c4, c5 = st.columns(3)
        with c3:
            electricity_price = st.number_input("Electricity price (NZ$/kWh)", 0.05, 1.50, float(st.session_state.get("electricity_price", 0.35)), 0.01)
            thermostat_degrees = st.slider("Thermostat improvement from current setting (°C)", 0, 6, int(st.session_state.get("thermostat_degrees", 2)))
        with c4:
            shower_current = st.slider("Current average shower time (minutes)", 2, 20, int(st.session_state.get("shower_current", 10)))
            shower_target = st.slider("Target shower time (minutes)", 2, 12, int(st.session_state.get("shower_target", 4)))
            showers_per_person = st.slider("Showers per person per day", 0.5, 2.0, float(st.session_state.get("showers_per_person", 1.0)), 0.1)
        with c5:
            old_bulbs = st.slider("Old halogen/incandescent bulbs used often", 0, 30, int(st.session_state.get("old_bulbs", 8)))
            bulb_hours = st.slider("Average hours per bulb per day", 0.5, 10.0, float(st.session_state.get("bulb_hours", 3.0)), 0.5)
            standby_devices = st.slider("Standby devices to manage", 0, 30, int(st.session_state.get("standby_devices", 8)))
        submitted = st.form_submit_button("Continue to home inspection", type="primary", use_container_width=True)

    if submitted:
        # If the user came back and changed profile assumptions, clear prior inspection answers.
        for hotspot_key in list(st.session_state.get("completed_hotspots", set())):
            unlock_hotspot_for_edit(hotspot_key)
        st.session_state.update({
            "household_size": household_size,
            "tenure_type": tenure_type,
            "bill_problem": bill_problem,
            "dwelling_type": dwelling_type,
            "home_condition": home_condition,
            "hvac_type": hvac_type,
            "solar_status": solar_status,
            "monthly_bill": monthly_bill,
                        "electricity_price": electricity_price,
            "shower_current": shower_current,
            "shower_target": shower_target,
            "showers_per_person": showers_per_person,
            "old_bulbs": old_bulbs,
            "bulb_hours": bulb_hours,
            "standby_devices": standby_devices,
            "thermostat_degrees": thermostat_degrees,
            "money_snapshot": estimate_money_snapshot(monthly_bill, household_size, bill_problem, dwelling_type, home_condition),
        })
        next_stage("inspection")


def inspection_screen() -> None:
    episode_header(
        "Mission 2",
        "Hunt the seven money leaks",
        "Each check gives points and shows the likely saving logic. The selected inspection area stays open after every answer.",
        "⚡",
    )
    sidebar_status()
    render_unlock_message()

    snap = st.session_state.get("money_snapshot", {})
    if snap:
        render_payoff_strip(float(snap.get("waste_low", 0)), float(snap.get("waste_high", 0)), "target saving opportunity during this challenge")
    render_money_recovered_counter("identified so far")
    st.markdown("### Badges unlocked")
    render_badge_wall()

    selected_area = render_inspection_area_selector()
    area = INSPECTION_AREAS[selected_area]
    done, total = area_completion(selected_area)

    c1, c2 = st.columns([0.62, 0.38])
    with c1:
        st.markdown(f"### {area['label']} — {done}/{total} completed")
        st.caption(area["description"])
        for key in area["keys"]:
            render_hotspot(key)
    with c2:
        st.plotly_chart(gauge(st.session_state.bill_risk, "Bill risk"), use_container_width=True)
        st.plotly_chart(gauge(st.session_state.comfort, "Comfort readiness"), use_container_width=True)
        commercial_teaser_panel("Next episode unlock")

    if len(st.session_state.completed_hotspots) >= 7:
        st.info("When you unlock the recommendations, the completed responses are saved anonymously for tool improvement. No name, email address, phone number, or contact detail is collected or stored.")
        render_save_status()
        if st.button("Save responses and show my recommendations", type="primary", use_container_width=True):
            inputs = current_household_inputs()
            savings = estimate_all_savings(inputs)
            ranked = current_ranked_actions()
            result_text = score_label(st.session_state.get("score", 0))
            saved = save_anonymous_progress_if_needed(savings, ranked, result_text)
            if saved:
                next_stage("plan")
            else:
                st.rerun()
    else:
        st.info("Complete all seven checks to unlock the money roadmap.")


def render_hotspot(key: str) -> None:
    hotspot = HOTSPOTS[key]
    done = key in st.session_state.completed_hotspots
    feedback = st.session_state.last_feedback.get(key)
    visual_key = ACTION_KEY_TO_VISUAL.get(hotspot.get("action", ""), key)
    visual_data = RECOMMENDATION_VISUALS.get(visual_key, {})
    low, high = saving_range_for_hotspot(key)
    icon = visual_data.get("icon", "⚡")
    with st.expander(("✅ " if done else f"{icon} ") + hotspot["label"] + f" — {hotspot['room']}", expanded=True if feedback else not done):
        st.write(f"**Money-leak check:** {hotspot['question']}")
        render_hotspot_option_visuals(key)
        left, right = st.columns([0.62, 0.38])
        with left:
            st.markdown(
                f"""
                <div class='hotspot-money'>
                    <strong>Potential saving lens:</strong> {format_saving_range((low, high)) if high > 0 else "Impact depends on your home and behaviour."}<br>
                    <strong>Points available:</strong> {hotspot['points']} energy-saving points
                </div>
                """,
                unsafe_allow_html=True,
            )
            if done:
                if st.button("Change this answer", key=f"edit_{key}"):
                    unlock_hotspot_for_edit(key)
                    st.rerun()
                answer = st.radio("Choose the best move", hotspot["options"], key=f"answer_{key}", index=None, disabled=True)
            else:
                answer = st.radio("Choose the best move", hotspot["options"], key=f"answer_{key}", index=None)
                if st.button("Lock in this move", key=f"check_{key}", disabled=answer is None):
                    update_for_answer(key, answer)
                    st.rerun()
        with right:
            render_recommendation_visual(visual_key)

        if feedback:
            if feedback["is_correct"]:
                st.success(
                    "**Good move — this reduces the leak**\n\n"
                    f"**Your selection:** {feedback['selected']}\n\n"
                    f"{feedback['message']}"
                )
            else:
                st.error(
                    "**Money still leaking**\n\n"
                    f"**Your selection:** {feedback['selected']}\n\n"
                    f"**Better move:** {feedback['correct']}\n\n"
                    f"{feedback['message']}"
                )
        elif done:
            st.caption("This hotspot has already been scored.")


def plan_screen() -> None:
    episode_header(
        "Mission 3",
        "Your money-first energy roadmap",
        "The goal is simple: recover avoidable bill waste, compare it with the advisor cost, and use savings to fund the next upgrade.",
        "🚀",
    )
    sidebar_status()

    inputs = current_household_inputs()
    savings = estimate_all_savings(inputs)
    ranked = current_ranked_actions()
    top_actions = top_three_actions(ranked)

    result_text = score_label(st.session_state.score)
    completion_date = date.today().strftime("%d %B %Y")
    total_low = int(sum(v[0] for v in savings.values() if isinstance(v, tuple)))
    total_high = int(sum(v[1] for v in savings.values() if isinstance(v, tuple)))
    st.session_state["result_category"] = result_text
    st.session_state["estimated_annual_savings"] = f"NZ${total_low:,}–NZ${total_high:,}"

    c1, c2, c3 = st.columns(3)
    with c1:
        value_bar("Your score", f"{st.session_state.score}/100", f"{result_text}. The higher the score, the fewer obvious leaks remain.", "🏆")
    with c2:
        value_bar("Annual saving estimate", f"{_currency(total_low)}–{_currency(total_high)}", "Combined indicative opportunity from monetised actions.", "💰")
    with c3:
        value_bar("Possible monthly recovery", f"{_currency(total_low / 12)}–{_currency(total_high / 12)}", "A practical monthly target if actions are followed consistently.", "📈")

    render_payoff_strip(total_low, total_high, "combined monetised action estimate")
    money_sentence(f"Roadmap value: the combined monetised actions are highlighted as <strong>{_currency(total_low)}–{_currency(total_high)} per year</strong>, so the user sees the money reason for acting before reading the detailed list.")

    st.markdown("### Choose your first 30-day challenge")
    first_action = render_30_day_challenge(ranked, savings)
    render_roadmap_card(result_text, total_low, total_high, first_action)

    st.markdown("### Badges earned")
    render_badge_wall()

    st.markdown("### Top three money moves")
    cols = st.columns(3)
    for idx, (key, action) in enumerate(top_actions):
        with cols[idx]:
            visual_key = ACTION_KEY_TO_VISUAL.get(key, key)
            render_recommendation_visual(visual_key)
            saving_text = format_saving_range(savings[key]) if key in savings else "Impact depends on the home"
            st.markdown(
                f"""
                <div class='card'>
                    <span class='badge'>{action['category']}</span>
                    <h4>{action['title']}</h4>
                    <p>{action['recommendation']}</p>
                    <p><strong>Indicative saving:</strong> {saving_text}</p>
                </div>
                """,
                unsafe_allow_html=True,
            )

    st.markdown("### Next step preview — the saving loop")
    commercial_teaser_panel("Saving loop")
    st.markdown(
        """
        <div class='series-card'>
            <h4>Ongoing roadmap concept</h4>
            <p>The first result gives a starting roadmap. The next loop would keep the user moving: monthly check-ins, revised savings, next upgrade unlocks, and a running payback summary.</p>
            <div class='series-meta'><span class='series-pill'>monthly challenge</span><span class='series-pill'>upgrade unlocks</span><span class='series-pill'>payback tracking</span></div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    st.markdown("### Full recommendation list")
    rows = []
    for key, action in ranked:
        rows.append({
            "Priority": action["priority"],
            "Action": action["title"],
            "Category": action["category"],
            "Cost": action["cost_level"],
            "Impact": action["impact_level"],
            "Indicative annual saving": format_saving_range(savings[key]) if key in savings else "Not monetised in prototype",
        })
    st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)

    st.markdown("### Savings chart")
    monetised_rows = [
        {"Action": key.replace("_", " ").title(), "Low": val[0], "High": val[1]}
        for key, val in savings.items() if isinstance(val, tuple) and val[1] > 0
    ]
    if monetised_rows:
        chart_df = pd.DataFrame(monetised_rows)
        fig = go.Figure()
        fig.add_trace(go.Bar(x=chart_df["Action"], y=chart_df["Low"], name="Low estimate"))
        fig.add_trace(go.Bar(x=chart_df["Action"], y=chart_df["High"], name="High estimate"))
        fig.update_layout(yaxis_title="Indicative annual saving (NZ$)", barmode="group", height=420)
        st.plotly_chart(fig, use_container_width=True)

    st.markdown("### Your save-to-upgrade pathway")
    behaviour_low, behaviour_high = estimate_behaviour_saving_pool(savings)
    st.markdown(
        f"""
        <div class='unlock-card'>
            <span class='unlock-badge'>Personalised funding loop</span>
            <h4>Use behaviour savings to fund the next energy upgrade</h4>
            <p>Your estimated no/low-cost behaviour-saving pool is approximately <strong>{_currency(behaviour_low)}–{_currency(behaviour_high)} per year</strong>. The pathway below shows how those savings could fund the next action instead of asking you to spend everything upfront.</p>
        </div>
        """,
        unsafe_allow_html=True,
    )
    for step in build_funding_pathway(inputs, savings):
        st.markdown(
            f"""
            <div class='pathway-step'>
                <h4>{step['stage']}: {step['title']}</h4>
                <p><strong>Logic:</strong> {step['logic']}</p>
                <p><strong>Estimated saving:</strong> {step['saving']}</p>
                <p><strong>Indicative cost:</strong> {step['cost']}</p>
                <p><strong>When it may be unlocked:</strong> {step['unlock']}</p>
            </div>
            """,
            unsafe_allow_html=True,
        )
        if "hot-water cylinder" in step["title"].lower() or "cylinder" in step["logic"].lower():
            render_recommendation_visual("cylinder_wrap")
        elif "draught" in step["title"].lower() or "door" in step["logic"].lower():
            render_recommendation_visual("draught")
        elif "insulation" in step["title"].lower():
            render_recommendation_visual("insulation")

    csv = pd.DataFrame(rows).to_csv(index=False).encode("utf-8")
    st.download_button("Download action plan as CSV", csv, "energy_action_plan.csv", "text/csv", use_container_width=True)

    st.markdown("### Completion recognition")
    st.info("Optional: if you would like a certificate, type your name below. This name is used only on the on-screen certificate and is not saved to Google Sheets.")
    certificate_input = st.text_input(
        "Name to display on certificate only",
        value=st.session_state.get("certificate_display_name", ""),
        placeholder="Type your name here for the certificate",
        key="certificate_display_name_input",
    )
    st.session_state["certificate_display_name"] = certificate_input.strip()
    certificate_name = st.session_state.get("certificate_display_name", "") or st.session_state.get("participant_id", "Anonymous Participant")

    st.markdown(
        f"""
        <div class='certificate-card'>
            <div class='certificate-kicker'>Beta Version 1.0</div>
            <div class='certificate-title'>Certificate of Completion</div>
            <div class='certificate-subtitle'>The Home-energy check-up (New Zealand)</div>
            <div class='certificate-company'>Tech Innovation Experts</div>
            <div class='certificate-small'>This certificate recognises</div>
            <div class='certificate-name'>{certificate_name}</div>
            <div class='certificate-small'>for completing the home-energy challenge, building a practical energy-saving roadmap, and selecting a first 30-day action.</div>
            <div class='certificate-meta'>
                <div class='certificate-pill'>Result: {result_text}</div>
                <div class='certificate-pill'>Score: {st.session_state.score}/100</div>
                <div class='certificate-pill'>Date: {completion_date}</div>
                <div class='certificate-pill'>30-day challenge selected</div>
            </div>
            <div class='certificate-footer'>Tech Innovation Experts Ltd. | Providing technology-driven services across Oceania | support@tinx.co.nz</div>
            <div class='certificate-disclaimer'>Recognition certificate only. Not a certified energy assessment or accredited NZ Building Code H1 assessment or Healthy Homes compliance statement.</div>
        </div>
        """,
        unsafe_allow_html=True,
    )




# ---------- Market-ready layer: public-safe saving, report, confidence, privacy, local visuals ----------
APP_VERSION = "Beta Version 1.1"
APP_LAST_UPDATED = "June 2026"
APP_REGION = "New Zealand"

st.markdown("""
<style>
.market-value-strip {
    display:grid; grid-template-columns:repeat(3,minmax(0,1fr)); gap:.85rem; margin:1rem 0;
}
.market-value-card {
    padding:1.05rem; border-radius:20px; background:#FFFFFF; border:1px solid #CBD5E1;
    box-shadow:0 6px 18px rgba(15,23,42,.08); min-height:135px;
}
.market-value-card strong {display:block; font-size:1.1rem; color:#0F172A; margin:.25rem 0;}
.market-value-card span {font-size:2rem; display:block; margin-bottom:.2rem;}
.market-note {
    padding:1rem; border-radius:18px; background:#F8FAFC; border:1px solid #CBD5E1; color:#334155; margin:.85rem 0;
}
.market-footer {
    margin-top:2rem; padding:1rem; border-radius:18px; background:#F8FAFC; border:1px solid #E2E8F0;
    color:#475569; font-size:.84rem; line-height:1.45;
}
.privacy-card {
    padding:1rem; border-radius:18px; background:#FFFFFF; border:1px solid #E2E8F0; color:#334155; margin:.65rem 0;
}
.brand-action-card {
    min-height:190px; border-radius:20px; background:linear-gradient(135deg,#F8FAFC 0%,#ECFDF5 100%);
    border:1px solid #CBD5E1; display:flex; flex-direction:column; align-items:center; justify-content:center;
    text-align:center; padding:1rem; box-shadow:0 6px 18px rgba(15,23,42,.08);
}
.brand-action-icon {font-size:3rem; margin-bottom:.5rem;}
.brand-action-title {font-weight:900; color:#0F172A; font-size:1.05rem;}
.brand-action-caption {color:#475569; font-size:.83rem; line-height:1.35; margin-top:.35rem;}
.owner-renter-grid {display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:.85rem; margin:1rem 0;}
.owner-renter-card {padding:1rem; border-radius:20px; background:#FFFFFF; border:1px solid #CBD5E1; box-shadow:0 6px 18px rgba(15,23,42,.07);}
.owner-renter-card h4 {margin:.1rem 0 .5rem 0; color:#0F172A;}
.owner-renter-card ul {margin:.25rem 0 .1rem 1.1rem; padding:0; color:#334155;}
.commitment-confirmed {padding:1rem; border-radius:18px; background:#ECFDF5; border:1px solid #16A34A; color:#065F46; margin:.85rem 0; font-weight:700;}
.public-save-warning {padding:1rem; border-radius:18px; background:#FFFBEB; border:1px solid #F59E0B; color:#78350F; margin:.85rem 0;}
@media (max-width: 800px) {.market-value-strip, .owner-renter-grid {grid-template-columns:1fr;}}
</style>
""", unsafe_allow_html=True)


def confidence_for_action(action_key: str) -> str:
    confidence = {
        "leds": "High",
        "standby": "Medium",
        "shorter_showers": "Medium",
        "thermostat": "Medium",
        "curtains": "Medium",
        "draught_sealing": "Medium",
        "insulation_owner": "Low without home-specific assessment",
        "insulation_renter": "Depends on landlord/property-manager action",
    }
    return confidence.get(action_key, "Home-specific")


def render_market_footer() -> None:
    with st.expander("Privacy and data note"):
        st.markdown(
            """
            <div class='privacy-card'>
            <strong>What is collected:</strong> anonymous tool responses, selected household assumptions, score, recommendation category, and estimated saving range.<br>
            <strong>What is not collected:</strong> name, email address, phone number, physical address, or contact details. The optional certificate name is displayed on screen only and is not saved to Google Sheets.<br>
            <strong>Why it is collected:</strong> to improve the tool, check usability, and understand common household energy-learning patterns.<br>
            <strong>Storage:</strong> responses are intended to be saved to a private Google Sheet controlled by the tool owner.
            </div>
            """,
            unsafe_allow_html=True,
        )
    st.markdown(
        f"""
        <div class='market-footer'>
            <strong>{APP_TITLE}</strong> | {APP_VERSION} | Region: {APP_REGION} | Last updated: {APP_LAST_UPDATED}.<br>
            Educational decision-support only. This tool is not a certified energy audit, NZ Building Code H1 assessment, Healthy Homes compliance statement, or guaranteed bill forecast.
        </div>
        """,
        unsafe_allow_html=True,
    )


def render_recommendation_visual(visual_key: str, height: int = 190) -> None:
    """Use consistent local/branded visual cards instead of external stock-image dependencies."""
    data = RECOMMENDATION_VISUALS.get(visual_key, RECOMMENDATION_VISUALS.get("thermostat", {}))
    icon = data.get("icon", "⚡")
    caption = data.get("caption", "Practical household energy action.")
    title = visual_key.replace("_", " ").title()
    st.markdown(
        f"""
        <div class='brand-action-card'>
            <div class='brand-action-icon'>{icon}</div>
            <div class='brand-action-title'>{title}</div>
            <div class='brand-action-caption'>{caption}</div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def render_save_status() -> None:
    debug_mode = bool(st.session_state.get("admin_debug_mode", False))
    if st.session_state.get("response_saved"):
        st.markdown(
            f"<div class='save-status-ok'><strong>Saved to Google Sheets.</strong><br>Participant ID: {st.session_state.get('participant_id', 'not available')}</div>",
            unsafe_allow_html=True,
        )
    elif st.session_state.get("save_error"):
        st.markdown(
            "<div class='public-save-warning'><strong>Responses could not be saved because of a Google Sheets connection issue.</strong><br>You can still view the recommendations. The tool owner should check Streamlit Secrets, service-account access, APIs, and spreadsheet URL.</div>",
            unsafe_allow_html=True,
        )
        if debug_mode:
            st.markdown(
                f"<div class='save-status-error'><strong>Admin debug error:</strong><br>{st.session_state.get('save_error')}</div>",
                unsafe_allow_html=True,
            )
    else:
        st.markdown(
            "<div class='save-status-box'><strong>Save status:</strong> not saved yet. Completed responses will be saved when you unlock the roadmap.</div>",
            unsafe_allow_html=True,
        )


def sidebar_status() -> None:
    st.sidebar.title("Challenge status")
    st.sidebar.metric("Score", f"{st.session_state.score}/100")
    st.sidebar.progress(min(st.session_state.score, 100) / 100)
    st.sidebar.write(f"**Player type:** {player_title(st.session_state.score)}")
    st.sidebar.write(f"**Result:** {score_label(st.session_state.score)}")
    st.sidebar.write(f"Completed checks: {len(st.session_state.completed_hotspots)}/7")
    with st.sidebar:
        render_money_recovered_counter("identified")
        st.markdown("**Badges unlocked**")
        render_badge_wall()
        render_mission_map()
        with st.expander("Admin options"):
            st.session_state["admin_debug_mode"] = st.checkbox(
                "Show technical save errors",
                value=bool(st.session_state.get("admin_debug_mode", False)),
                help="Keep this off for public users. Turn on only when debugging Google Sheets or secrets.",
            )
    if st.sidebar.button("Restart challenge"):
        reset_app()
        st.rerun()
    st.sidebar.markdown(_sidebar_logo_html(), unsafe_allow_html=True)


def welcome_screen() -> None:
    logo_header()
    episode_header(
        "Mission intro",
        "Find where your home may be leaking energy money",
        "Receive a practical New Zealand-specific action roadmap in under five minutes.",
        "💸",
    )
    st.markdown(
        """
        <div class='market-value-strip'>
            <div class='market-value-card'><span>💰</span><strong>Estimate avoidable bill waste</strong>Start with a simple money-leak scan rather than generic tips.</div>
            <div class='market-value-card'><span>⚡</span><strong>Identify seven household leaks</strong>Check heating, lighting, hot water, standby, draughts, curtains, and insulation.</div>
            <div class='market-value-card'><span>🎯</span><strong>Build a 30-day action plan</strong>Choose one realistic behaviour or low-cost action to start immediately.</div>
        </div>
        <div class='market-note'>
            <strong>Product promise:</strong> this is a fast educational decision-support tool for New Zealand households. It does not replace a professional assessment, but it helps users understand where to start.
        </div>
        """,
        unsafe_allow_html=True,
    )
    render_mission_map()
    st.caption(f"{APP_VERSION} | {APP_REGION} | Educational decision-support only.")
    if st.button("Start my home energy challenge", type="primary", use_container_width=True):
        next_stage("money")


def inspection_screen() -> None:
    episode_header(
        "Mission 2",
        "Hunt the seven money leaks",
        "Each check gives points and shows the likely saving logic. The selected inspection area stays open after every answer.",
        "⚡",
    )
    sidebar_status()
    render_unlock_message()

    snap = st.session_state.get("money_snapshot", {})
    if snap:
        render_payoff_strip(float(snap.get("waste_low", 0)), float(snap.get("waste_high", 0)), "target saving opportunity during this challenge")
    render_money_recovered_counter("identified so far")
    st.markdown("### Badges unlocked")
    render_badge_wall()

    selected_area = render_inspection_area_selector()
    area = INSPECTION_AREAS[selected_area]
    done, total = area_completion(selected_area)

    c1, c2 = st.columns([0.62, 0.38])
    with c1:
        st.markdown(f"### {area['label']} — {done}/{total} completed")
        st.caption(area["description"])
        for key in area["keys"]:
            render_hotspot(key)
    with c2:
        st.plotly_chart(gauge(st.session_state.bill_risk, "Bill risk"), use_container_width=True)
        st.plotly_chart(gauge(st.session_state.comfort, "Comfort readiness"), use_container_width=True)
        commercial_teaser_panel("Next episode unlock")

    if len(st.session_state.completed_hotspots) >= 7:
        st.info("When you unlock the recommendations, the completed responses are saved anonymously for tool improvement. No name, email address, phone number, or contact detail is collected or stored.")
        render_save_status()
        if st.button("Show my recommendations", type="primary", use_container_width=True):
            inputs = current_household_inputs()
            savings = estimate_all_savings(inputs)
            ranked = current_ranked_actions()
            result_text = score_label(st.session_state.get("score", 0))
            saved = save_anonymous_progress_if_needed(savings, ranked, result_text)
            if not saved:
                st.session_state["save_warning_for_plan"] = True
            next_stage("plan")
    else:
        st.info("Complete all seven checks to unlock the money roadmap.")


def _report_markdown(result_text: str, total_low: int, total_high: int, first_action: str, rows: list[dict]) -> str:
    lines = [
        f"# {APP_TITLE} — Home Energy Roadmap",
        "",
        f"Version: {APP_VERSION}",
        f"Region: {APP_REGION}",
        f"Date: {date.today().strftime('%d %B %Y')}",
        "",
        f"Score: {st.session_state.score}/100",
        f"Result: {result_text}",
        f"Estimated annual saving range: NZ${total_low:,}–NZ${total_high:,}",
        f"First 30-day challenge: {first_action}",
        "",
        "## Recommended actions",
    ]
    for row in rows:
        lines.append(f"- Priority {row['Priority']}: {row['Action']} ({row['Category']}) — {row['Indicative annual saving']}; confidence: {row.get('Confidence', 'Home-specific')}.")
    lines += [
        "",
        "## Disclaimer",
        "Educational decision-support only. This is not a certified energy audit, NZ Building Code H1 assessment, Healthy Homes compliance statement, or guaranteed bill forecast.",
    ]
    return "\n".join(lines)


def render_owner_renter_guidance(tenure_type: str) -> None:
    st.markdown("### Renter and owner pathway")
    st.markdown(
        """
        <div class='owner-renter-grid'>
            <div class='owner-renter-card'>
                <h4>🏘️ If you rent</h4>
                <ul>
                    <li>Start with behaviour, curtain use, standby control, and short-shower habits.</li>
                    <li>Document draughts, poor heating, moisture, ventilation, and insulation concerns.</li>
                    <li>Use Healthy Homes obligations as a reference point when discussing issues with the landlord or property manager.</li>
                </ul>
            </div>
            <div class='owner-renter-card'>
                <h4>🏡 If you own</h4>
                <ul>
                    <li>Start with no-cost and low-cost behaviour changes.</li>
                    <li>Then consider draught sealing, insulation, curtains, efficient lighting, and heating system improvements.</li>
                    <li>Link renovation decisions to the intent of NZ Building Code H1 energy-efficiency thinking.</li>
                </ul>
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )
    if tenure_type == "Rented":
        st.info("Your selected pathway is renter-focused, so the roadmap prioritises actions you can control and issues you can document.")
    else:
        st.info("Your selected pathway is owner-focused, so the roadmap includes larger envelope and retrofit planning options after behaviour-first actions.")


def plan_screen() -> None:
    episode_header(
        "Mission 3",
        "Your market-ready home energy roadmap",
        "The goal is simple: recover avoidable bill waste, choose one 30-day action, and use the result to prioritise future upgrades.",
        "🚀",
    )
    sidebar_status()

    if st.session_state.get("save_warning_for_plan") and not st.session_state.get("response_saved"):
        render_save_status()

    inputs = current_household_inputs()
    savings = estimate_all_savings(inputs)
    ranked = current_ranked_actions()
    top_actions = top_three_actions(ranked)

    result_text = score_label(st.session_state.score)
    completion_date = date.today().strftime("%d %B %Y")
    total_low = int(sum(v[0] for v in savings.values() if isinstance(v, tuple)))
    total_high = int(sum(v[1] for v in savings.values() if isinstance(v, tuple)))
    st.session_state["result_category"] = result_text
    st.session_state["estimated_annual_savings"] = f"NZ${total_low:,}–NZ${total_high:,}"

    c1, c2, c3 = st.columns(3)
    with c1:
        value_bar("Your score", f"{st.session_state.score}/100", f"{result_text}. The higher the score, the fewer obvious leaks remain.", "🏆")
    with c2:
        value_bar("Annual saving estimate", f"{_currency(total_low)}–{_currency(total_high)}", "Combined indicative opportunity from monetised actions.", "💰")
    with c3:
        value_bar("Possible monthly recovery", f"{_currency(total_low / 12)}–{_currency(total_high / 12)}", "A practical monthly target if actions are followed consistently.", "📈")

    render_payoff_strip(total_low, total_high, "combined monetised action estimate")

    st.markdown("### Choose and confirm your first 30-day challenge")
    first_action = render_30_day_challenge(ranked, savings)
    if st.button("I commit to this 30-day challenge", type="primary", use_container_width=True):
        st.session_state["challenge_committed"] = True
    if st.session_state.get("challenge_committed"):
        st.markdown(
            f"<div class='commitment-confirmed'>Challenge confirmed: {first_action}<br>Suggested first check-in: 7 days from today.</div>",
            unsafe_allow_html=True,
        )
    render_roadmap_card(result_text, total_low, total_high, first_action)

    render_owner_renter_guidance(st.session_state.get("tenure_type", "Rented"))

    st.markdown("### Top three money moves")
    cols = st.columns(3)
    for idx, (key, action) in enumerate(top_actions):
        with cols[idx]:
            visual_key = ACTION_KEY_TO_VISUAL.get(key, key)
            render_recommendation_visual(visual_key)
            saving_text = format_saving_range(savings[key]) if key in savings else "Impact depends on the home"
            st.markdown(
                f"""
                <div class='card'>
                    <span class='badge'>{action['category']}</span>
                    <h4>{action['title']}</h4>
                    <p>{action['recommendation']}</p>
                    <p><strong>Indicative saving:</strong> {saving_text}</p>
                    <p><strong>Confidence:</strong> {confidence_for_action(key)}</p>
                </div>
                """,
                unsafe_allow_html=True,
            )

    st.markdown("### Full recommendation list with confidence level")
    rows = []
    for key, action in ranked:
        rows.append({
            "Priority": action["priority"],
            "Action": action["title"],
            "Category": action["category"],
            "Cost": action["cost_level"],
            "Impact": action["impact_level"],
            "Indicative annual saving": format_saving_range(savings[key]) if key in savings else "Not monetised in prototype",
            "Confidence": confidence_for_action(key),
        })
    st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)

    st.markdown("### Savings chart")
    monetised_rows = [
        {"Action": key.replace("_", " ").title(), "Low": val[0], "High": val[1]}
        for key, val in savings.items() if isinstance(val, tuple) and val[1] > 0
    ]
    if monetised_rows:
        chart_df = pd.DataFrame(monetised_rows)
        fig = go.Figure()
        fig.add_trace(go.Bar(x=chart_df["Action"], y=chart_df["Low"], name="Low estimate"))
        fig.add_trace(go.Bar(x=chart_df["Action"], y=chart_df["High"], name="High estimate"))
        fig.update_layout(yaxis_title="Indicative annual saving (NZ$)", barmode="group", height=420)
        st.plotly_chart(fig, use_container_width=True)

    st.markdown("### Your save-to-upgrade pathway")
    behaviour_low, behaviour_high = estimate_behaviour_saving_pool(savings)
    st.markdown(
        f"""
        <div class='unlock-card'>
            <span class='unlock-badge'>Personalised funding loop</span>
            <h4>Use behaviour savings to fund the next energy upgrade</h4>
            <p>Your estimated no/low-cost behaviour-saving pool is approximately <strong>{_currency(behaviour_low)}–{_currency(behaviour_high)} per year</strong>. The pathway below shows how those savings could fund the next action instead of asking you to spend everything upfront.</p>
        </div>
        """,
        unsafe_allow_html=True,
    )
    for step in build_funding_pathway(inputs, savings):
        st.markdown(
            f"""
            <div class='pathway-step'>
                <h4>{step['stage']}: {step['title']}</h4>
                <p><strong>Logic:</strong> {step['logic']}</p>
                <p><strong>Estimated saving:</strong> {step['saving']}</p>
                <p><strong>Indicative cost:</strong> {step['cost']}</p>
                <p><strong>When it may be unlocked:</strong> {step['unlock']}</p>
            </div>
            """,
            unsafe_allow_html=True,
        )

    report_text = _report_markdown(result_text, total_low, total_high, first_action, rows)
    st.download_button("Download screenshot-friendly roadmap report", report_text.encode("utf-8"), "nz_home_energy_roadmap.md", "text/markdown", use_container_width=True)
    csv = pd.DataFrame(rows).to_csv(index=False).encode("utf-8")
    st.download_button("Download action plan as CSV", csv, "energy_action_plan.csv", "text/csv", use_container_width=True)

    st.markdown("### Completion recognition")
    st.info("Optional: if you would like a certificate, type your name below. This name is used only on the on-screen certificate and is not saved to Google Sheets.")
    certificate_input = st.text_input(
        "Name to display on certificate only",
        value=st.session_state.get("certificate_display_name", ""),
        placeholder="Type your name here for the certificate",
        key="certificate_display_name_input",
    )
    st.session_state["certificate_display_name"] = certificate_input.strip()
    certificate_name = st.session_state.get("certificate_display_name", "") or st.session_state.get("participant_id", "Anonymous Participant")

    st.markdown(
        f"""
        <div class='certificate-card'>
            <div class='certificate-kicker'>{APP_VERSION}</div>
            <div class='certificate-title'>Certificate of Completion</div>
            <div class='certificate-subtitle'>The Home-energy check-up (New Zealand)</div>
            <div class='certificate-company'>Tech Innovation Experts</div>
            <div class='certificate-small'>This certificate recognises</div>
            <div class='certificate-name'>{certificate_name}</div>
            <div class='certificate-small'>for completing the home-energy challenge, building a practical energy-saving roadmap, and selecting a first 30-day action.</div>
            <div class='certificate-meta'>
                <div class='certificate-pill'>Result: {result_text}</div>
                <div class='certificate-pill'>Score: {st.session_state.score}/100</div>
                <div class='certificate-pill'>Date: {completion_date}</div>
                <div class='certificate-pill'>30-day challenge selected</div>
            </div>
            <div class='certificate-footer'>Tech Innovation Experts Ltd. | Providing technology-driven services across Oceania | support@tinx.co.nz</div>
            <div class='certificate-disclaimer'>Recognition certificate only. Not a certified energy assessment or accredited NZ Building Code H1 assessment or Healthy Homes compliance statement.</div>
        </div>
        """,
        unsafe_allow_html=True,
    )


# Render simple navigation and run the tool.
render_back_button()

if st.session_state.stage == "welcome":
    welcome_screen()
elif st.session_state.stage == "money":
    money_screen()
elif st.session_state.stage == "profile":
    profile_screen()
elif st.session_state.stage == "inspection":
    inspection_screen()
elif st.session_state.stage == "plan":
    plan_screen()
else:
    st.session_state.stage = "welcome"
    welcome_screen()

render_market_footer()
