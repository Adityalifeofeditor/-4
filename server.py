from fastapi import FastAPI, File, UploadFile
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
import shutil
import os
import uuid

# -----------------------------
# Setup
# -----------------------------
app = FastAPI()

UPLOAD_FOLDER = "uploads"
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

# Serve uploaded PDFs as static files
app.mount("/uploads", StaticFiles(directory=UPLOAD_FOLDER), name="uploads")


# -----------------------------
# Upload Endpoint
# -----------------------------
@app.post("/upload")
async def upload_pdf(file: UploadFile = File(...)):
    # Check file type
    if file.content_type != "application/pdf":
        return JSONResponse(status_code=400, content={"error": "Only PDFs allowed."})

    # Limit file size (~25MB)
    contents = await file.read()
    MAX_BACKEND_SIZE = 25 * 1024 * 1024
    if len(contents) > MAX_BACKEND_SIZE:
        return JSONResponse(status_code=413, content={"error": "File too large."})

    # Save file with unique name
    file_id = str(uuid.uuid4())
    file_path = os.path.join(UPLOAD_FOLDER, f"{file_id}.pdf")
    with open(file_path, "wb") as f:
        f.write(contents)

    # Return a public viewer URL
    # On Render, your domain would be like: https://yourapp.onrender.com/uploads/<file_id>.pdf
    viewer_url = f"https://yourapp.onrender.com/uploads/{file_id}.pdf"

    return {"viewer_url": viewer_url}
