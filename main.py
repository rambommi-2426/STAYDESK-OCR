"""
StayDesk OCR Microservice
-------------------------
FastAPI service for Indian ID card OCR.
Uses OpenBharatOCR (PaddleOCR + EasyOCR) for Aadhaar, DL, PAN.
Also reads Aadhaar QR code for instant extraction.

Deploy on Railway.app (free) — one endpoint, returns JSON.
"""

import os
import io
import json
import re
import tempfile
import traceback
from typing import Optional

from fastapi import FastAPI, File, UploadFile, Form, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from PIL import Image

# ── App setup ────────────────────────────────────────────────────────────────
app = FastAPI(title="StayDesk OCR Service", version="1.0.0")

# Allow requests from Netlify (and localhost for testing)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Restrict to your Netlify URL in production
    allow_methods=["POST", "GET"],
    allow_headers=["*"],
)

# ── Lazy-load heavy OCR engines ───────────────────────────────────────────────
_openbharat_loaded = False
_obocr = None

def get_obocr():
    global _openbharat_loaded, _obocr
    if not _openbharat_loaded:
        try:
            import openbharatocr
            _obocr = openbharatocr
        except ImportError:
            _obocr = None
        _openbharat_loaded = True
    return _obocr


# ── Aadhaar QR code reader ───────────────────────────────────────────────────
def read_aadhaar_qr(image: Image.Image) -> Optional[dict]:
    """
    Reads the QR code on back of Aadhaar card.
    UIDAI encodes all details as XML inside the QR.
    Returns parsed dict or None if no QR found.
    """
    try:
        from pyzbar.pyzbar import decode as pyzbar_decode
        import xml.etree.ElementTree as ET

        qr_data = pyzbar_decode(image)
        for code in qr_data:
            raw = code.data.decode("utf-8", errors="ignore")
            # Try XML parse (older Aadhaar QR format)
            try:
                root = ET.fromstring(raw)
                uid_data = root.find("UidData") or root
                poi = uid_data.find("Poi") or {}
                poa = uid_data.find("Poa") or {}

                def g(el, attr): 
                    return el.get(attr, "") if hasattr(el, "get") else ""

                name    = g(poi, "name") or g(uid_data, "name")
                dob     = g(poi, "dob")
                gender  = g(poi, "gender")
                phone   = g(poi, "phone") or g(uid_data, "mobile")

                # Build address from Poa element
                addr_parts = [
                    g(poa, "house"), g(poa, "street"), g(poa, "lm"),
                    g(poa, "loc"), g(poa, "vtc"), g(poa, "subdist"),
                    g(poa, "dist"), g(poa, "state"), g(poa, "pc"),
                ]
                address = ", ".join(p for p in addr_parts if p)

                if name:
                    return {
                        "source":   "Aadhaar QR",
                        "name":     name,
                        "dob":      dob,
                        "gender":   "Male" if gender=="M" else "Female" if gender=="F" else gender,
                        "address":  address,
                        "phone":    phone,
                        "idType":   "Aadhaar",
                        "idNumber": "",  # Aadhaar number not in QR for privacy
                        "fatherName": "",
                    }
            except ET.ParseError:
                # Try JSON format (newer Aadhaar)
                try:
                    data = json.loads(raw)
                    return {
                        "source":   "Aadhaar QR",
                        "name":     data.get("name",""),
                        "dob":      data.get("dob",""),
                        "gender":   data.get("gender",""),
                        "address":  data.get("address",""),
                        "phone":    data.get("mobile",""),
                        "idType":   "Aadhaar",
                        "idNumber": "",
                        "fatherName": data.get("father",""),
                    }
                except json.JSONDecodeError:
                    pass
    except Exception:
        pass
    return None


# ── OpenBharatOCR extraction ─────────────────────────────────────────────────
def extract_with_openbharat(image_path: str, doc_type: str, side: str = "front") -> dict:
    """
    Use OpenBharatOCR to extract fields from Indian ID documents.
    doc_type: aadhaar | driving_licence | pan | auto
    side: front | back
    """
    obocr = get_obocr()
    if not obocr:
        raise RuntimeError("OpenBharatOCR not available")

    result = {}

    if doc_type == "aadhaar" or doc_type == "auto":
        if side == "front":
            try:
                result = obocr.aadhaar_front(image_path)
            except Exception:
                pass
        elif side == "back":
            try:
                result = obocr.aadhaar_back(image_path)
            except Exception:
                pass

    if not result and (doc_type == "driving_licence" or doc_type == "auto"):
        try:
            result = obocr.driving_licence(image_path)
        except Exception:
            pass

    if not result and (doc_type == "pan" or doc_type == "auto"):
        try:
            result = obocr.pan(image_path)
        except Exception:
            pass

    return result or {}


# ── Fallback: smart regex parser ─────────────────────────────────────────────
def parse_with_regex(text: str, side: str = "front") -> dict:
    """Fallback regex parser for when OpenBharatOCR is unavailable."""
    lines = [l.strip() for l in text.split("\n") if l.strip()]
    t = text

    # Aadhaar number — 12 digits
    aadh = re.sub(r"\s","", t)
    aadh_m = re.search(r"\d{12}", aadh)
    aadhaar_no = (aadh_m.group().replace(r"(\d{4})(\d{4})(\d{4})", r"\1 \2 \3")
                  if aadh_m else "")
    if aadhaar_no:
        aadhaar_no = f"{aadhaar_no[:4]} {aadhaar_no[4:8]} {aadhaar_no[8:]}"

    # DL number
    dl_m = (re.search(r"[A-Z]{2}[\s-]?\d{2}[\s-]?\d{4}[\s-]?\d{7}", t, re.I)
            or re.search(r"[A-Z]{2}\d{13}", t, re.I))
    dl_no = dl_m.group().replace(" ","").upper() if dl_m else ""

    # PAN
    pan_m = re.search(r"[A-Z]{5}\d{4}[A-Z]", t)
    pan_no = pan_m.group() if pan_m else ""

    # ID type
    id_type, id_number = "", ""
    if aadhaar_no:     id_type, id_number = "Aadhaar", aadhaar_no
    elif dl_no:        id_type, id_number = "Driving Licence", dl_no
    elif pan_no:       id_type, id_number = "PAN", pan_no

    # DOB
    dob_m = (re.search(r"(?:DOB|D\.O\.B|Date of Birth)[:\s]*(\d{1,2}[\/\-]\d{1,2}[\/\-]\d{2,4})", t, re.I)
             or re.search(r"\b(\d{2}[\/\-]\d{2}[\/\-]\d{4})\b", t))
    dob = dob_m.group(1) if dob_m else ""

    # Gender
    gender = ""
    if re.search(r"\b(MALE|Male)\b", t): gender = "Male"
    elif re.search(r"\b(FEMALE|Female)\b", t): gender = "Female"

    # Name
    name = ""
    nm = re.search(r"(?:Name|नाम)[:\s]+([A-Z][a-zA-Z\s]{3,40})", t)
    if nm:
        name = nm.group(1).strip()
    else:
        for line in lines:
            if (re.match(r"^[A-Z][A-Z\s]{4,35}$", line)
                and not re.search(r"INDIA|GOVERNMENT|UIDAI|AUTHORITY|DRIVING|LICENCE|AADHAAR|MALE|FEMALE", line, re.I)
                and 2 <= len(line.split()) <= 5):
                name = line
                break

    # Phone
    ph_m = (re.search(r"(?:Mobile|Phone|Mob)[:\s]*([6-9]\d{9})", t, re.I)
            or re.search(r"\b([6-9]\d{9})\b", t))
    phone = ph_m.group(1) if ph_m else ""

    # Address — from back side or after label
    address = ""
    if side == "back":
        addr_m = re.search(r"(?:Address|पता|S\/O|D\/O|W\/O|C\/O)[:\s]+(.+)", t, re.I | re.DOTALL)
        if addr_m:
            address = addr_m.group(1).replace("\n"," ").strip()[:250]
        else:
            addr_lines = [l for l in lines[-5:] if not re.match(r"^\d{4}\s\d{4}\s\d{4}$", l) and len(l) > 5]
            if len(addr_lines) >= 2:
                address = ", ".join(addr_lines)[:250]

    # Father/Guardian
    father_m = re.search(r"(?:S\/O|D\/O|W\/O|Father|Husband)[:\s]+([A-Za-z\s]{3,40})", t, re.I)
    father = father_m.group(1).strip() if father_m else ""

    return {
        "name": name, "dob": dob, "gender": gender,
        "idType": id_type, "idNumber": id_number,
        "address": address, "phone": phone, "fatherName": father,
    }


def normalise(raw: dict, side: str) -> dict:
    """Normalise OpenBharatOCR output to our standard field names."""
    return {
        "name":       raw.get("name","")       or raw.get("Name",""),
        "dob":        raw.get("dob","")        or raw.get("DOB","")  or raw.get("date_of_birth",""),
        "gender":     raw.get("gender","")     or raw.get("Gender",""),
        "idType":     raw.get("id_type","")    or ("Aadhaar" if "aadhaar" in str(raw).lower() else
                                                    "Driving Licence" if "dl" in str(raw).lower() else
                                                    "PAN" if "pan" in str(raw).lower() else ""),
        "idNumber":   raw.get("aadhaar_number","") or raw.get("dl_number","") or raw.get("pan_number","") or raw.get("id_number",""),
        "address":    raw.get("address","")    if side=="back" else "",
        "phone":      raw.get("phone","")      or raw.get("mobile",""),
        "fatherName": raw.get("father","")     or raw.get("father_name",""),
    }


# ── Health check ─────────────────────────────────────────────────────────────
@app.get("/")
def health():
    obocr = get_obocr()
    return {
        "status": "ok",
        "service": "StayDesk OCR",
        "openbharatocr": "available" if obocr else "unavailable (using fallback)",
    }


# ── Main OCR endpoint ─────────────────────────────────────────────────────────
@app.post("/ocr/id")
async def ocr_id(
    front: UploadFile = File(...),
    back:  UploadFile = File(None),
    doc_type: str = Form("auto"),     # aadhaar | driving_licence | pan | auto
):
    """
    POST /ocr/id
    Fields:
      front    — image file (required) — front of ID card
      back     — image file (optional) — back of ID card (has address)
      doc_type — hint for document type (default: auto-detect)

    Returns JSON:
      { name, dob, gender, idType, idNumber, address, phone, fatherName, source }
    """
    try:
        # ── Read front image ──
        front_bytes = await front.read()
        front_img   = Image.open(io.BytesIO(front_bytes)).convert("RGB")

        # ── Try Aadhaar QR first (instant, 100% accurate) ──
        qr_result = read_aadhaar_qr(front_img)
        if not qr_result and back:
            back_bytes = await back.read()
            back_img   = Image.open(io.BytesIO(back_bytes)).convert("RGB")
            qr_result  = read_aadhaar_qr(back_img)

        if qr_result:
            return JSONResponse({"ok": True, "source": "Aadhaar QR", **qr_result})

        # ── Save images to temp files for OpenBharatOCR ──
        with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as f_front:
            front_img.save(f_front.name, "JPEG", quality=92)
            front_path = f_front.name

        back_path = None
        if back:
            back_bytes = back_bytes if 'back_bytes' in dir() else await back.read()
            back_img   = Image.open(io.BytesIO(back_bytes)).convert("RGB")
            with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as f_back:
                back_img.save(f_back.name, "JPEG", quality=92)
                back_path = f_back.name

        # ── Try OpenBharatOCR ──
        front_data, back_data = {}, {}
        source = "OpenBharatOCR"

        try:
            front_raw  = extract_with_openbharat(front_path, doc_type, "front")
            front_data = normalise(front_raw, "front")
            if back_path:
                back_raw  = extract_with_openbharat(back_path, doc_type, "back")
                back_data = normalise(back_raw, "back")
        except RuntimeError:
            # OpenBharatOCR not available — use Tesseract fallback
            source = "Tesseract (fallback)"
            import pytesseract
            front_text = pytesseract.image_to_string(front_img, lang="eng")
            front_data = parse_with_regex(front_text, "front")
            if back_path:
                back_text  = pytesseract.image_to_string(back_img, lang="eng")
                back_data  = parse_with_regex(back_text, "back")

        # ── Merge: front wins for identity fields, back wins for address ──
        merged = {
            "name":       front_data.get("name")       or back_data.get("name",""),
            "dob":        front_data.get("dob")        or back_data.get("dob",""),
            "gender":     front_data.get("gender")     or back_data.get("gender",""),
            "idType":     front_data.get("idType")     or back_data.get("idType",""),
            "idNumber":   front_data.get("idNumber")   or back_data.get("idNumber",""),
            "phone":      front_data.get("phone")      or back_data.get("phone",""),
            "fatherName": front_data.get("fatherName") or back_data.get("fatherName",""),
            "address":    back_data.get("address")     or front_data.get("address",""),
            "source":     source,
        }

        # Cleanup temp files
        os.unlink(front_path)
        if back_path:
            os.unlink(back_path)

        return JSONResponse({"ok": True, **merged})

    except Exception as e:
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))


# ── Aadhaar QR-only endpoint ──────────────────────────────────────────────────
@app.post("/ocr/qr")
async def ocr_qr(image: UploadFile = File(...)):
    """
    POST /ocr/qr
    Reads Aadhaar QR code only — fastest option, use for back of Aadhaar.
    """
    try:
        img_bytes = await image.read()
        img       = Image.open(io.BytesIO(img_bytes)).convert("RGB")
        result    = read_aadhaar_qr(img)
        if result:
            return JSONResponse({"ok": True, **result})
        return JSONResponse({"ok": False, "message": "No QR code found in image"})
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
