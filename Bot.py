#!/usr/bin/env python3
"""
Resume Bot — Telegram bot powered by Claude (Anthropic).

Flow:
  1) User pastes a job description.
  2) Bot reads knowledge_bank.txt + prompts.py.
  3) Claude generates a tailored resume (JSON).
  4) Bot renders a 1-page PDF with WeasyPrint.
  5) Claude scores the resume vs JD (ATS).
  6) Bot sends: PDF + ATS score.

Files:
  bot.py             — this file
  knowledge_bank.txt — candidate background
  prompts.py         — all AI prompts
  .env               — TELEGRAM_TOKEN, ANTHROPIC_API_KEY
"""

import os
import re
import json
import logging
from pathlib import Path
from dotenv import load_dotenv
import anthropic
from telegram import Update
from telegram.ext import ApplicationBuilder, MessageHandler, filters, ContextTypes
from weasyprint import HTML

from prompts import (
    RESUME_SYSTEM_PROMPT,
    RESUME_USER_PROMPT,
    ATS_SYSTEM_PROMPT,
    ATS_USER_PROMPT,
)

# ── Bootstrap ─────────────────────────────────────────────────────────────────
load_dotenv()
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
)
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN    = os.environ["TELEGRAM_TOKEN"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
MODEL             = os.environ.get("CLAUDE_MODEL", "claude-sonnet-4-5")
OUTDIR            = Path(os.environ.get("OUTPUT_DIR", "out"))
OUTDIR.mkdir(parents=True, exist_ok=True)
KNOWLEDGE_BANK    = Path(os.environ.get("KNOWLEDGE_BANK", "knowledge_bank.txt"))

client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

KNOWLEDGE_BANK_CACHE: str | None = None


# ── Helpers ───────────────────────────────────────────────────────────────────

def load_knowledge_bank() -> str:
    global KNOWLEDGE_BANK_CACHE
    if KNOWLEDGE_BANK_CACHE:
        return KNOWLEDGE_BANK_CACHE
    if not KNOWLEDGE_BANK.exists():
        raise FileNotFoundError(
            f"knowledge_bank.txt not found at {KNOWLEDGE_BANK.resolve()}. "
            "Create it alongside bot.py before starting the bot."
        )
    KNOWLEDGE_BANK_CACHE = KNOWLEDGE_BANK.read_text(encoding="utf-8")
    return KNOWLEDGE_BANK_CACHE


def strip_fences(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    return text.strip()


def escape_html(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def extract_company(jd: str) -> str:
    m = re.search(
        r'(?:Company|Employer|Organization)\s*[:\-]\s*([A-Z][\w &.\-]{2,60})', jd
    )
    if m:
        return re.sub(r'[,.;]+$', '', m.group(1).strip())
    stopwords = {'least', 'all', 'this', 'our', 'the', 'a', 'an', 'your'}
    m = re.search(r'\bat\s+([A-Z][a-zA-Z]{2,}(?:\s+[A-Z][a-zA-Z]+){0,3})', jd)
    if m and m.group(1).strip().split()[0].lower() not in stopwords:
        return re.sub(r'[,.;]+$', '', m.group(1).strip())
    return "Company"


def safe_filename(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_\-]+", "", name.replace(" ", "-"))


def build_resume_text(data: dict) -> str:
    """Plain-text resume for ATS scoring (no HTML).
    
    NOTE: Update the static header line, company names, job titles, dates,
    and education details below to match YOUR knowledge_bank.txt.
    """
    bullets = data.get("work_experience", [])

    # ── UPDATE THESE VALUES TO MATCH YOUR KNOWLEDGE BANK ──────────────────────
    CANDIDATE_HEADER  = "Your Name | Your City, Province | 000-000-0000 | your@email.com"
    COMPANY_1         = "Company One — Job Title One (Month YYYY – Present)"
    COMPANY_2         = "Company Two — Job Title Two (Month YYYY – Month YYYY)"
    COMPANY_3         = "Company Three — Job Title Three (Month YYYY – Month YYYY)"
    EDUCATION_LINE    = "Your University — Your Degree (YYYY–YYYY)"
    CERTS_LINE        = "Certifications: Cert1, Cert2, Cert3"
    PROJECT_1_HEADING = data.get("project1_title", "Project One")
    PROJECT_2_HEADING = data.get("project2_title", "Project Two")
    # ──────────────────────────────────────────────────────────────────────────

    lines = [
        CANDIDATE_HEADER,
        "",
        "PROFESSIONAL SUMMARY",
        data.get("professional_summary", ""),
        "",
        "TECHNICAL SKILLS",
        data.get("technical_skills", ""),
        "",
        "PROFESSIONAL EXPERIENCE",
        COMPANY_1,
        *[f"- {b}" for b in bullets[0:4]],
        "",
        COMPANY_2,
        *[f"- {b}" for b in bullets[4:7]],
        "",
        COMPANY_3,
        *[f"- {b}" for b in bullets[7:10]],
        "",
        "PROJECTS",
        PROJECT_1_HEADING,
        *[f"- {b}" for b in data.get("project1_bullets", [])],
        PROJECT_2_HEADING,
        *[f"- {b}" for b in data.get("project2_bullets", [])],
        "",
        "EDUCATION & CERTIFICATIONS",
        EDUCATION_LINE,
        CERTS_LINE,
    ]
    return "\n".join(lines)


# ── Claude: Resume Generation ─────────────────────────────────────────────────

def claude_generate_resume(jd: str, knowledge_bank: str) -> dict:
    system = RESUME_SYSTEM_PROMPT.format(knowledge_bank=knowledge_bank)
    user   = RESUME_USER_PROMPT.format(jd=jd)

    response = client.messages.create(
        model=MODEL,
        max_tokens=2000,
        temperature=0.3,
        system=system,
        messages=[{"role": "user", "content": user}],
    )
    raw = strip_fences(response.content[0].text)

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        logger.error("Resume JSON parse failed:\n%s", raw)
        data = {}

    # ── Fallback bullets — REPLACE WITH YOUR OWN REAL BULLETS ─────────────────
    FILLER_BULLETS = [
        "Administered core infrastructure systems, maintaining high availability across all services.",
        "Automated routine maintenance tasks, reducing manual workload significantly.",
        "Configured network equipment to enforce segmentation and secure remote access.",
        "Managed virtualization infrastructure, optimizing resource allocation.",
        "Deployed endpoint management solutions, achieving compliance across all devices.",
    ]
    # ──────────────────────────────────────────────────────────────────────────

    # ── Default project bullets — REPLACE WITH YOUR OWN REAL PROJECTS ─────────
    DEFAULT_P1 = [
        "Describe what you built for Project 1 and the measurable outcome.",
        "Describe a second accomplishment or impact metric for Project 1.",
    ]
    DEFAULT_P2 = [
        "Describe what you built for Project 2 and the measurable outcome.",
        "Describe a second accomplishment or impact metric for Project 2.",
    ]
    # ──────────────────────────────────────────────────────────────────────────

    def coerce_list(val) -> list:
        if isinstance(val, list):
            flat = []
            for item in val:
                if isinstance(item, list):
                    flat.extend(str(x) for x in item)
                elif isinstance(item, dict):
                    flat.extend(str(v) for v in item.values() if isinstance(v, str))
                else:
                    flat.append(str(item))
            return flat
        if isinstance(val, dict):
            flat = []
            for v in val.values():
                if isinstance(v, list):
                    flat.extend(str(x) for x in v)
                elif isinstance(v, str):
                    flat.append(v)
            return flat
        return []

    # ── Add your company/role name patterns here to filter them out of bullets ─
    HEADER_PAT = re.compile(
        r'^(company one|company two|company three|your city|month yyyy)',
        re.IGNORECASE,
    )

    def sanitize(items):
        return [b for b in items if b.strip() and not HEADER_PAT.match(b.strip())]

    we = sanitize(coerce_list(data.get("work_experience", [])))
    while len(we) < 10:
        we.append(FILLER_BULLETS[len(we) % len(FILLER_BULLETS)])
    we = we[:10]

    p1b = coerce_list(data.get("project1_bullets", []))
    p2b = coerce_list(data.get("project2_bullets", []))
    while len(p1b) < 2:
        p1b.append(DEFAULT_P1[len(p1b)])
    while len(p2b) < 2:
        p2b.append(DEFAULT_P2[len(p2b)])

    return {
        "professional_summary": data.get(
            "professional_summary",
            "IT professional with X+ years managing [your key skills]. "
            "Delivered [a specific achievement with a metric]. "
            "Ready to bring proven expertise to your team.",
        ),
        "technical_skills": data.get(
            "technical_skills",
            "Skill1, Skill2, Skill3, Skill4, Skill5, Skill6, Skill7, Skill8, "
            "Skill9, Skill10, Skill11, Skill12, Skill13, Skill14, Skill15",
        ),
        "work_experience": we,
        "project1_title":   data.get("project1_title",   "Your Project One Title"),
        "project1_bullets": p1b[:2],
        "project2_title":   data.get("project2_title",   "Your Project Two Title"),
        "project2_bullets": p2b[:2],
        "target_company":   extract_company(jd),
    }


# ── Claude: ATS Scoring ───────────────────────────────────────────────────────

def claude_score_resume(jd: str, resume_text: str) -> dict:
    user = ATS_USER_PROMPT.format(jd=jd, resume_text=resume_text)

    response = client.messages.create(
        model=MODEL,
        max_tokens=1000,
        temperature=0.1,
        system=ATS_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user}],
    )
    raw = strip_fences(response.content[0].text)

    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        logger.error("ATS JSON parse failed:\n%s", raw)
        return {}


# ── PDF Renderer ──────────────────────────────────────────────────────────────

def render_pdf(data: dict, out_path: Path) -> None:
    summary    = escape_html(data["professional_summary"])
    tech       = escape_html(data["technical_skills"])
    bullets    = [escape_html(b) for b in data["work_experience"]]
    p1_title   = escape_html(data["project1_title"])
    p1_bullets = [escape_html(b) for b in data["project1_bullets"]]
    p2_title   = escape_html(data["project2_title"])
    p2_bullets = [escape_html(b) for b in data["project2_bullets"]]

    skill_items = [s.strip() for s in tech.split(",") if s.strip()]
    skills_html = "".join(f'<span class="skill-pill">{s}</span>' for s in skill_items)

    # ── UPDATE THESE STATIC VALUES TO MATCH YOUR INFO ─────────────────────────
    CANDIDATE_NAME    = "Your Full Name"
    CANDIDATE_CITY    = "Your City, Province"
    CANDIDATE_PHONE   = "000-000-0000"
    CANDIDATE_EMAIL   = "your@email.com"
    CANDIDATE_LINKEDIN = "linkedin.com/in/yourhandle"

    COMPANY_1_NAME    = "Company One"
    COMPANY_1_CITY    = "City, Province"
    COMPANY_1_TITLE   = "Your Job Title"
    COMPANY_1_DATES   = "Mon YYYY – Present"

    COMPANY_2_NAME    = "Company Two"
    COMPANY_2_CITY    = "City, Province"
    COMPANY_2_TITLE   = "Your Job Title"
    COMPANY_2_DATES   = "Mon YYYY – Mon YYYY"

    COMPANY_3_NAME    = "Company Three"
    COMPANY_3_CITY    = "City, Province"
    COMPANY_3_TITLE   = "Your Job Title"
    COMPANY_3_DATES   = "Mon YYYY – Mon YYYY"

    EDUCATION_SCHOOL  = "Your University / College"
    EDUCATION_CITY    = "City, Province"
    EDUCATION_DEGREE  = "Your Degree Name"
    EDUCATION_DATES   = "YYYY – YYYY"
    CERTIFICATIONS    = "Cert1, Cert2, Cert3, Cert4"
    # ──────────────────────────────────────────────────────────────────────────

    CSS = """
    <style>
      @page { size: Letter; margin: 5mm 8mm 5mm 8mm; }

      body {
        font-family: Georgia, "Times New Roman", serif;
        font-size: 10.5pt;
        color: #1a1a1a;
        line-height: 1.25;
      }

      .name {
        text-align: center;
        font-size: 24pt;
        font-weight: 700;
        margin: 0 0 1px 0;
        letter-spacing: 2px;
        text-transform: uppercase;
        font-family: Helvetica, Arial, sans-serif;
      }
      .contact {
        text-align: center;
        font-size: 9.5pt;
        margin-bottom: 5px;
        color: #333;
        font-family: Helvetica, Arial, sans-serif;
      }
      .contact a { color: #333; text-decoration: none; }

      .section { margin-bottom: 5px; }

      .section h2 {
        font-size: 10pt;
        font-weight: 700;
        text-transform: uppercase;
        letter-spacing: 1.2px;
        border-bottom: 1.5px solid #1a1a1a;
        margin: 0 0 3px 0;
        padding-bottom: 1px;
        font-family: Helvetica, Arial, sans-serif;
      }

      .summary-text { margin: 0; text-align: justify; font-size: 10.5pt; }

      .skills-wrap {
        display: flex;
        flex-wrap: wrap;
        gap: 3px 4px;
        margin: 0;
      }
      .skill-pill {
        display: inline-block;
        font-size: 8.5pt;
        font-family: Helvetica, Arial, sans-serif;
        background: #f0f0f0;
        border: 0.8px solid #c8c8c8;
        border-radius: 3px;
        padding: 1px 5px;
        color: #1a1a1a;
        white-space: nowrap;
      }

      .role-header {
        display: flex;
        justify-content: space-between;
        font-weight: 700;
        font-size: 10.5pt;
        margin-bottom: 0px;
        font-family: Helvetica, Arial, sans-serif;
      }
      .role-sub {
        display: flex;
        justify-content: space-between;
        font-style: italic;
        font-size: 10pt;
        margin-bottom: 0px;
        font-family: Helvetica, Arial, sans-serif;
        color: #333;
      }
      ul { margin: 1px 0 4px 14px; padding: 0; }
      li { margin-bottom: 0px; text-align: justify; font-size: 10.5pt; line-height: 1.22; }

      .project-title {
        font-weight: 700;
        font-size: 10.5pt;
        font-family: Helvetica, Arial, sans-serif;
        margin-bottom: 0px;
      }

      .edu-cert {
        font-style: italic;
        font-size: 10pt;
        font-family: Helvetica, Arial, sans-serif;
        color: #333;
      }
    </style>
    """

    r1 = bullets[0:4]
    r2 = bullets[4:7]
    r3 = bullets[7:10]

    def li(items):
        return "".join(f"<li>{b}</li>" for b in items)

    html = f"""<!doctype html>
<html>
<head><meta charset="utf-8"/>{CSS}</head>
<body>

  <div class="name">{escape_html(CANDIDATE_NAME)}</div>
  <div class="contact">
    {escape_html(CANDIDATE_CITY)} &nbsp;|&nbsp; {escape_html(CANDIDATE_PHONE)} &nbsp;|&nbsp;
    <a href="mailto:{escape_html(CANDIDATE_EMAIL)}">{escape_html(CANDIDATE_EMAIL)}</a> &nbsp;|&nbsp;
    <a href="https://{escape_html(CANDIDATE_LINKEDIN)}">{escape_html(CANDIDATE_LINKEDIN)}</a>
  </div>

  <div class="section">
    <h2>Professional Summary</h2>
    <p class="summary-text">{summary}</p>
  </div>

  <div class="section">
    <h2>Technical Skills</h2>
    <div class="skills-wrap">{skills_html}</div>
  </div>

  <div class="section">
    <h2>Professional Experience</h2>

    <div class="role-header"><span>{escape_html(COMPANY_1_NAME)}</span><span>{escape_html(COMPANY_1_CITY)}</span></div>
    <div class="role-sub"><span>{escape_html(COMPANY_1_TITLE)}</span><span>{escape_html(COMPANY_1_DATES)}</span></div>
    <ul>{li(r1)}</ul>

    <div class="role-header"><span>{escape_html(COMPANY_2_NAME)}</span><span>{escape_html(COMPANY_2_CITY)}</span></div>
    <div class="role-sub"><span>{escape_html(COMPANY_2_TITLE)}</span><span>{escape_html(COMPANY_2_DATES)}</span></div>
    <ul>{li(r2)}</ul>

    <div class="role-header"><span>{escape_html(COMPANY_3_NAME)}</span><span>{escape_html(COMPANY_3_CITY)}</span></div>
    <div class="role-sub"><span>{escape_html(COMPANY_3_TITLE)}</span><span>{escape_html(COMPANY_3_DATES)}</span></div>
    <ul>{li(r3)}</ul>
  </div>

  <div class="section">
    <h2>Projects</h2>
    <div class="project-title">{p1_title}</div>
    <ul>{li(p1_bullets)}</ul>
    <div class="project-title">{p2_title}</div>
    <ul>{li(p2_bullets)}</ul>
  </div>

  <div class="section">
    <h2>Education &amp; Certifications</h2>
    <div class="role-header"><span>{escape_html(EDUCATION_SCHOOL)}</span><span>{escape_html(EDUCATION_CITY)}</span></div>
    <div class="role-sub"><span>{escape_html(EDUCATION_DEGREE)}</span><span>{escape_html(EDUCATION_DATES)}</span></div>
    <div class="edu-cert">Certifications: {escape_html(CERTIFICATIONS)}</div>
  </div>

</body>
</html>"""

    HTML(string=html).write_pdf(out_path)


# ── ATS Report Formatter ──────────────────────────────────────────────────────

def format_ats_report(score: dict, company: str) -> str:
    if not score:
        return "ATS scoring unavailable for this submission."

    overall  = score.get("overall_score", "N/A")
    grade    = score.get("grade", "N/A")
    kw_pct   = score.get("keyword_match_percent", "N/A")
    matched  = score.get("matched_keywords", "N/A")
    total_kw = score.get("total_jd_keywords", "N/A")
    emoji    = {"Excellent": "🟢", "Good": "🔵", "Average": "🟡", "Poor": "🔴"}.get(grade, "⚪")

    return (
        f"*ATS Score — {company}*\n"
        f"{emoji} `{overall}/100` — {grade}\n\n"
        f"Keyword Match: `{matched}/{total_kw}` ({kw_pct}%)"
    )


# ── Telegram Handler ──────────────────────────────────────────────────────────

async def handle_jd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    jd = (update.message.text or "").strip()
    if not jd:
        return

    if len(jd) > 8000:
        await update.message.reply_text(
            "That JD is too long. Paste only the key sections "
            "(duties, requirements, preferred skills) — aim for under 8,000 characters."
        )
        return

    await update.message.reply_text("Analyzing the JD and building your resume — 20-30 seconds.")

    try:
        knowledge_bank = load_knowledge_bank()
    except FileNotFoundError as e:
        logger.error(str(e))
        await update.message.reply_text(f"Error: {e}")
        return

    try:
        data = claude_generate_resume(jd, knowledge_bank)
    except Exception:
        logger.exception("Claude resume generation error")
        await update.message.reply_text("Error generating resume. Check logs.")
        return

    company  = data.get("target_company", "Company")
    out_path = OUTDIR / f"Resume-{safe_filename(company)}.pdf"

    try:
        render_pdf(data, out_path)
    except Exception:
        logger.exception("PDF render error")
        await update.message.reply_text("Error generating PDF. Check logs.")
        return

    resume_text = build_resume_text(data)
    try:
        ats_score = claude_score_resume(jd, resume_text)
    except Exception:
        logger.exception("ATS scoring error")
        ats_score = {}

    with open(out_path, "rb") as f:
        await update.message.reply_document(
            f,
            filename=out_path.name,
            caption=f"Resume for *{company}*",
            parse_mode="Markdown",
        )

    await update.message.reply_text(
        format_ats_report(ats_score, company),
        parse_mode="Markdown",
    )


# ── Entry Point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_jd))
    logger.info("Resume Bot started — paste a JD to begin.")
    app.run_polling()
