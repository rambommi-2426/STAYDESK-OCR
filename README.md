# StayDesk OCR Microservice

Python FastAPI service for Indian ID card OCR. Deployed on Railway.app (free).

## What it does

POST a photo of Aadhaar / Driving Licence / PAN →  
Returns: name, DOB, gender, address, ID number, phone

## OCR Priority (best to fallback)

1. **Aadhaar QR code** — instant, 100% accurate, reads UIDAI digital data
2. **OpenBharatOCR** — PaddleOCR + EasyOCR, India-specific
3. **Tesseract** — fallback if above unavailable

## Deploy on Railway (free — 500 hrs/month)

1. Go to railway.app → New Project → Deploy from GitHub
2. Push this folder to a GitHub repo
3. Railway auto-detects Dockerfile and builds
4. Copy the generated URL (e.g. https://staydesk-ocr.up.railway.app)
5. Paste that URL into StayDesk settings

## API Endpoints

### GET /
Health check — shows which OCR engines are available

### POST /ocr/id
Scan front + back of ID card

```
curl -X POST https://your-service.up.railway.app/ocr/id \
  -F "front=@aadhaar_front.jpg" \
  -F "back=@aadhaar_back.jpg" \
  -F "doc_type=aadhaar"
```

Response:
```json
{
  "ok": true,
  "source": "OpenBharatOCR",
  "name": "RAM KUMAR",
  "dob": "01/01/1985",
  "gender": "Male",
  "idType": "Aadhaar",
  "idNumber": "1234 5678 9012",
  "address": "12, Gandhi Nagar, Coimbatore, Tamil Nadu 641035",
  "phone": "9876543210",
  "fatherName": "SURESH KUMAR"
}
```

### POST /ocr/qr
Scan only the QR code on back of Aadhaar (fastest)

```
curl -X POST https://your-service.up.railway.app/ocr/qr \
  -F "image=@aadhaar_back.jpg"
```

## Local testing

```bash
pip install -r requirements.txt
uvicorn main:app --reload
# Open http://localhost:8000
```
