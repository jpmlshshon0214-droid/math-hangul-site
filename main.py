from __future__ import annotations

import asyncio
import re
import tempfile
import time
import uuid
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import Response
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pypdf import PdfReader
from pyhwpxlib import HwpxBuilder

MAX_BYTES = 20 * 1024 * 1024
app = FastAPI(title="수식한글")
jobs: dict[str, dict] = {}
rate_history: dict[str, list[float]] = {}


def equation_script(latex: str) -> str:
    """Translate common OCR LaTeX commands to Hancom equation-editor syntax."""
    s = latex.strip().replace("\\left", "").replace("\\right", "")
    s = s.replace("\\displaystyle", "").replace("\\,", " ").replace("\\;", " ")
    # Convert the most common simple fractions and roots. Nested formulas remain
    # legible as text if they do not fit this deliberately small first pass.
    s = re.sub(r"\\frac\s*\{([^{}]*)\}\s*\{([^{}]*)\}", r"{ \1 } over { \2 }", s)
    s = re.sub(r"\\sqrt\s*\{([^{}]*)\}", r"sqrt { \1 }", s)
    tokens = {
        r"\\times": " times ", r"\\cdot": " cdot ", r"\\div": " div ",
        r"\\pm": " +- ", r"\\neq": " != ", r"\\leq": " <= ", r"\\geq": " >= ",
        r"\\infty": " inf ", r"\\pi": " pi ", r"\\theta": " theta ",
        r"\\alpha": " alpha ", r"\\beta": " beta ", r"\\gamma": " gamma ",
        r"\\Delta": " DELTA ", r"\\sum": " sum ", r"\\int": " int ",
        r"\\in": " in ", r"\\approx": " approx ", r"\\rightarrow": " -> ",
        r"\\log": " log ", r"\\sin": " sin ", r"\\cos": " cos ",
        r"\\tan": " tan ", r"\\lim": " lim ", r"\\cdots": " cdots ",
    }
    for source, target in tokens.items():
        s = re.sub(source, target, s)
    s = re.sub(r"\\(?:mathrm|text|mathbf|mathit)\s*\{([^{}]*)\}", r"\1", s)
    s = s.replace("\\{", "{").replace("\\}", "}").replace("\\%", "%")
    s = s.replace("\\_", "_")
    return re.sub(r"\s+", " ", s).strip()


def append_text_with_math(doc: HwpxBuilder, text: str) -> None:
    """Write prose and turn inline dollar-delimited formulas into equation objects."""
    matches = list(re.finditer(r"(?<!\\)\$([^$]+)\$|\\\((.+?)\\\)", text))
    if not matches:
        doc.add_paragraph(text.replace(r"\$", "$"))
        return
    cursor = 0
    for match in matches:
        if match.start() > cursor:
            doc.add_paragraph(text[cursor:match.start()].replace(r"\$", "$"))
        doc.add_equation(equation_script(match.group(1) or match.group(2)))
        cursor = match.end()
    if cursor < len(text):
        doc.add_paragraph(text[cursor:].replace(r"\$", "$"))


def mmd_to_hwpx(markdown: str, output_path: str) -> None:
    doc = HwpxBuilder(table_preset="academic")
    in_display_math = False
    display_math: list[str] = []
    for raw in markdown.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("$$") or line.startswith(r"\["):
            marker = "$$" if line.startswith("$$") else r"\["
            closer = "$$" if marker == "$$" else r"\]"
            if not in_display_math:
                in_display_math = True
                content = line[len(marker):].strip()
                if content.endswith(closer):
                    doc.add_equation(equation_script(content[:-len(closer)]))
                    in_display_math = False
                elif content:
                    display_math.append(content)
            else:
                content = line[:-len(closer)].strip() if line.endswith(closer) else line
                if content:
                    display_math.append(content)
                doc.add_equation(equation_script(" ".join(display_math)))
                display_math.clear()
                in_display_math = False
            continue
        if in_display_math:
            display_math.append(line)
            continue
        heading = re.match(r"^(#{1,4})\s+(.+)$", line)
        if heading:
            doc.add_heading(heading.group(2), level=len(heading.group(1)))
        elif line.startswith("|") and line.endswith("|"):
            cells = [c.strip() for c in line.strip("|").split("|")]
            if not all(re.fullmatch(r"[:\- ]+", c or " ") for c in cells):
                doc.add_table([cells])
        elif re.match(r"^[-*+]\s+", line):
            doc.add_bullet_list([re.sub(r"^[-*+]\s+", "", line)])
        elif re.match(r"^\d+[.)]\s+", line):
            doc.add_numbered_list([re.sub(r"^\d+[.)]\s+", "", line)])
        elif line in {"---", "___", "***"}:
            doc.add_line()
        else:
            # Remove Markdown image/link syntax while preserving readable labels.
            line = re.sub(r"!\[([^\]]*)\]\([^)]*\)", r"[그림: \1]", line)
            line = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", line)
            line = line.replace("**", "").replace("__", "").replace("`", "")
            append_text_with_math(doc, line)
    if display_math:
        doc.add_equation(equation_script(" ".join(display_math)))
    doc.save(output_path)


def extract_pdf_text(data: bytes) -> str:
    """Extract the existing text layer; no paid OCR or external upload is used."""
    import io
    reader = PdfReader(io.BytesIO(data))
    if len(reader.pages) > 100:
        raise ValueError("PDF는 100페이지 이하로 선택해 주세요.")
    pages = [(page.extract_text() or "").strip() for page in reader.pages]
    text = "\n\n".join(f"## {i + 1}쪽\n{page}" for i, page in enumerate(pages) if page)
    if not text.strip():
        raise ValueError("PDF에서 선택 가능한 글자를 찾지 못했어요. 스캔 이미지 PDF는 무료 버전에서 지원하지 않습니다.")
    return text


@app.get("/api/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/api/convert")
async def convert(request: Request, file: UploadFile = File(...)) -> JSONResponse:
    now = time.time()
    client_ip = request.client.host if request and request.client else "unknown"
    recent = [stamp for stamp in rate_history.get(client_ip, []) if now - stamp < 3600]
    if len(recent) >= 5:
        raise HTTPException(429, "한 시간에 변환할 수 있는 횟수를 넘었어요. 나중에 다시 시도해 주세요.")
    filename = Path(file.filename or "document.pdf").name
    if not filename.lower().endswith(".pdf"):
        raise HTTPException(400, "PDF 파일만 올릴 수 있어요.")
    data = await file.read(MAX_BYTES + 1)
    await file.close()
    if len(data) > MAX_BYTES:
        raise HTTPException(413, "파일 크기는 20MB 이하로 선택해 주세요.")
    if not data.startswith(b"%PDF-"):
        raise HTTPException(400, "정상적인 PDF 파일인지 확인해 주세요.")
    recent.append(now)
    rate_history[client_ip] = recent
    job_id = uuid.uuid4().hex
    jobs[job_id] = {"status": "queued", "created": now, "filename": filename, "result": None, "error": None}
    asyncio.create_task(process_pdf(job_id, data, filename))
    # Drop old in-memory results and rate buckets during normal traffic.
    for old_id, old_job in list(jobs.items()):
        if now - old_job["created"] > 1800:
            jobs.pop(old_id, None)
    return JSONResponse({"job_id": job_id, "status": "queued"}, status_code=202)


async def process_pdf(job_id: str, data: bytes, filename: str) -> None:
    try:
        jobs[job_id]["status"] = "extracting"
        markdown = await asyncio.to_thread(extract_pdf_text, data)
    except Exception as exc:
        jobs[job_id]["status"] = "error"
        jobs[job_id]["error"] = str(exc) if isinstance(exc, ValueError) else "PDF 글자를 읽지 못했어요. 파일이 손상되었는지 확인해 주세요."
        return

    if jobs[job_id]["status"] == "error":
        return
    jobs[job_id]["status"] = "building"
    try:
        with tempfile.TemporaryDirectory() as temp_dir:
            output = Path(temp_dir) / "converted.hwpx"
            await asyncio.to_thread(mmd_to_hwpx, markdown, str(output))
            contents = output.read_bytes()
        jobs[job_id]["result"] = contents
        jobs[job_id]["status"] = "completed"
    except Exception:
        jobs[job_id]["status"] = "error"
        jobs[job_id]["error"] = "인식 결과를 한글 문서로 만들지 못했어요."


@app.get("/api/jobs/{job_id}")
async def job_status(job_id: str) -> dict[str, str]:
    job = jobs.get(job_id)
    if not job:
        raise HTTPException(404, "변환 작업을 찾을 수 없어요.")
    answer = {"status": job["status"]}
    if job["error"]:
        answer["error"] = job["error"]
    return answer


@app.get("/api/jobs/{job_id}/file")
async def job_file(job_id: str) -> Response:
    job = jobs.get(job_id)
    if not job or job["status"] != "completed" or not job["result"]:
        raise HTTPException(404, "변환 파일이 아직 준비되지 않았어요.")
    stem = re.sub(r"[^\w가-힣.-]", "_", Path(job["filename"]).stem)[:100] or "document"
    result = job["result"]
    jobs.pop(job_id, None)
    return Response(
        content=result,
        media_type="application/vnd.hancom.hwpx",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{stem}.hwpx"},
    )


app.mount("/", StaticFiles(directory=Path(__file__).parent, html=True), name="static")
