"""
StayDesk OCR Microservice — Lightweight version
Uses EasyOCR + Tesseract (no PaddleOCR — keeps image under 2GB)
"""

import os, io, re, json, traceback
from typing import Optional

from fastapi import FastAPI, File, UploadFile, Form, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from PIL import Image

app = FastAPI(title="StayDesk OCR", version="2.0.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

_reader = None
def get_reader():
    global _reader
    if _reader is None:
        import easyocr
        _reader = easyocr.Reader(["en","hi"], gpu=False)
    return _reader


def read_qr(img: Image.Image) -> Optional[dict]:
    try:
        from pyzbar.pyzbar import decode
        import xml.etree.ElementTree as ET
        for code in decode(img):
            raw = code.data.decode("utf-8", errors="ignore")
            try:
                root = ET.fromstring(raw)
                uid  = root.find("UidData") or root
                poi  = uid.find("Poi") or {}
                poa  = uid.find("Poa") or {}
                g    = lambda el,a: el.get(a,"") if hasattr(el,"get") else ""
                name = g(poi,"name") or g(uid,"name")
                if not name: continue
                addr = ", ".join(filter(None,[g(poa,"house"),g(poa,"street"),
                    g(poa,"loc"),g(poa,"dist"),g(poa,"state"),g(poa,"pc")]))
                return {"source":"Aadhaar QR","name":name,"dob":g(poi,"dob"),
                    "gender":"Male" if g(poi,"gender")=="M" else "Female" if g(poi,"gender")=="F" else "",
                    "address":addr,"phone":g(poi,"phone"),"idType":"Aadhaar","idNumber":"","fatherName":""}
            except ET.ParseError:
                try:
                    d = json.loads(raw)
                    if d.get("name"):
                        return {"source":"Aadhaar QR","name":d.get("name",""),"dob":d.get("dob",""),
                            "gender":d.get("gender",""),"address":d.get("address",""),
                            "phone":d.get("mobile",""),"idType":"Aadhaar","idNumber":"","fatherName":d.get("father","")}
                except: pass
    except: pass
    return None


def parse(text: str, side: str = "front") -> dict:
    t = text
    lines = [l.strip() for l in t.split("\n") if l.strip()]

    aadh = re.sub(r"\s","",t)
    am   = re.search(r"\d{12}", aadh)
    aadhaar = f"{am.group()[:4]} {am.group()[4:8]} {am.group()[8:]}" if am else ""
    dm = re.search(r"[A-Z]{2}[\s-]?\d{2}[\s-]?\d{4}[\s-]?\d{7}",t,re.I) or re.search(r"[A-Z]{2}\d{13}",t,re.I)
    dl = re.sub(r"\s","",dm.group()).upper() if dm else ""
    pm = re.search(r"[A-Z]{5}\d{4}[A-Z]", t)
    pan = pm.group() if pm else ""

    id_type = "Aadhaar" if aadhaar else "Driving Licence" if dl else "PAN" if pan else ""
    id_num  = aadhaar or dl or pan

    dob = ""
    dobm = re.search(r"(?:DOB|D\.O\.B|Date of Birth)[:\s]*(\d{1,2}[\/\-]\d{1,2}[\/\-]\d{2,4})",t,re.I) \
        or re.search(r"\b(\d{2}[\/\-]\d{2}[\/\-]\d{4})\b",t)
    if dobm:
        p = re.split(r"[\/\-]", dobm.group(1))
        if len(p)==3 and len(p[2])==4 and int(p[0])<=31:
            dob = f"{p[0].zfill(2)}/{p[1].zfill(2)}/{p[2]}"

    gender = "Male" if re.search(r"\b(MALE|Male)\b",t) else "Female" if re.search(r"\b(FEMALE|Female)\b",t) else ""

    name = ""
    nm = re.search(r"(?:Name|नाम)[:\s]+([A-Z][a-zA-Z\s]{3,40})",t)
    if nm: name = nm.group(1).strip()
    else:
        for line in lines:
            if (re.match(r"^[A-Z][A-Z\s]{4,35}$",line)
                and not re.search(r"INDIA|GOVT|GOVERNMENT|UIDAI|AUTHORITY|DRIVING|LICENCE|AADHAAR|MALE|FEMALE",line,re.I)
                and 2 <= len(line.split()) <= 5):
                name = line; break

    phone = ""
    phm = re.search(r"\b([6-9]\d{9})\b",t)
    if phm: phone = phm.group(1)

    address = ""
    if side == "back":
        adm = re.search(r"(?:Address|पता|S\/O|D\/O|W\/O|C\/O)[:\s]+(.+)",t,re.I|re.DOTALL)
        if adm: address = adm.group(1).replace("\n"," ").strip()[:250]
        else:
            al = [l for l in lines[-6:] if not re.match(r"^\d{4}\s\d{4}\s\d{4}$",l) and len(l)>5]
            if len(al)>=2: address = ", ".join(al)[:250]

    father = ""
    fm = re.search(r"(?:S\/O|D\/O|W\/O|Father|Husband)[:\s]+([A-Za-z\s]{3,40})",t,re.I)
    if fm: father = fm.group(1).strip()

    return {"name":name,"dob":dob,"gender":gender,"idType":id_type,"idNumber":id_num,
            "address":address,"phone":phone,"fatherName":father}


def run_ocr(img: Image.Image) -> str:
    try:
        reader = get_reader()
        results = reader.readtext(img, detail=0, paragraph=True)
        text = "\n".join(results)
        if len(text.strip()) > 20:
            return text
    except: pass
    try:
        import pytesseract
        return pytesseract.image_to_string(img, lang="eng")
    except: return ""


@app.get("/")
def health():
    return {"status":"ok","service":"StayDesk OCR","version":"2.0"}


@app.post("/ocr/id")
async def ocr_id(
    front: UploadFile = File(...),
    back:  UploadFile = File(None),
    doc_type: str = Form("auto"),
):
    try:
        front_bytes = await front.read()
        front_img   = Image.open(io.BytesIO(front_bytes)).convert("RGB")

        # Try QR first — instant and 100% accurate
        qr = read_qr(front_img)
        if not qr and back:
            back_bytes = await back.read()
            back_img   = Image.open(io.BytesIO(back_bytes)).convert("RGB")
            qr = read_qr(back_img)
        if qr:
            return JSONResponse({"ok":True, **qr})

        # OCR front
        front_text  = run_ocr(front_img)
        front_data  = parse(front_text, "front")

        # OCR back
        back_data = {}
        if back:
            if 'back_img' not in dir():
                back_bytes = await back.read()
                back_img   = Image.open(io.BytesIO(back_bytes)).convert("RGB")
            back_text  = run_ocr(back_img)
            back_data  = parse(back_text, "back")

        merged = {
            "name":       front_data.get("name")       or back_data.get("name",""),
            "dob":        front_data.get("dob")        or back_data.get("dob",""),
            "gender":     front_data.get("gender")     or back_data.get("gender",""),
            "idType":     front_data.get("idType")     or back_data.get("idType",""),
            "idNumber":   front_data.get("idNumber")   or back_data.get("idNumber",""),
            "phone":      front_data.get("phone")      or back_data.get("phone",""),
            "fatherName": front_data.get("fatherName") or back_data.get("fatherName",""),
            "address":    back_data.get("address")     or front_data.get("address",""),
            "source":     "EasyOCR",
        }
        return JSONResponse({"ok":True, **merged})

    except Exception as e:
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/ocr/qr")
async def ocr_qr(image: UploadFile = File(...)):
    try:
        img = Image.open(io.BytesIO(await image.read())).convert("RGB")
        result = read_qr(img)
        if result:
            return JSONResponse({"ok":True, **result})
        return JSONResponse({"ok":False,"message":"No QR code found"})
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
