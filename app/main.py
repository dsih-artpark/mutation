from fastapi import FastAPI, UploadFile, File, HTTPException
from fastapi.responses import FileResponse, HTMLResponse
import uuid
import shutil
from pathlib import Path

from .tasks import process_fasta
from .config import RUNS_DIR
from .visualization import generate_visualization

app = FastAPI(title="LSDV Variant Pipeline")

UPLOAD_DIR = RUNS_DIR / "uploads"
UPLOAD_DIR.mkdir(exist_ok=True, parents=True)

@app.get("/")
def read_root():
    return {"message": "LSDV Variant Pipeline is running"}


@app.get("/health")
def health_check():
    return {"status": "ok"}


@app.post("/upload")
async def upload_file(file: UploadFile = File(..., description="Select a FASTA file")):
    file_id = str(uuid.uuid4())
    save_path = UPLOAD_DIR / f"{file_id}_{file.filename}"

    with open(save_path, "wb") as f:
        shutil.copyfileobj(file.file, f)

    task = process_fasta.delay(str(save_path))
    
    return {
        "job_id": task.id,
        "filename": file.filename,
        "status": "queued"
    }


@app.get("/status/{job_id}")
def get_status(job_id: str):
    from .worker import celery
    result = celery.AsyncResult(job_id)

    status_data = {
        "job_id": job_id,
        "status": result.status,
        "result": result.result
    }

    if result.status == "SUCCESS" and result.result:
        run_id = result.result.get("run_id")
        if run_id:
            run_dir = RUNS_DIR / run_id
            snps_vcf = run_dir / "snps.vcf"
            annotated_vcf = run_dir / "annotated.vcf"

            if snps_vcf.exists():
                with open(snps_vcf, "r") as f:
                    status_data["snps_vcf_data"] = f.read()
            
            if annotated_vcf.exists():
                with open(annotated_vcf, "r") as f:
                    status_data["annotated_vcf_data"] = f.read()

    return status_data


@app.get("/download/{run_id}/{file_name}")
def download_file(run_id: str, file_name: str):
    if file_name not in ["snps.vcf", "annotated.vcf", "visualization.html"]:
        raise HTTPException(status_code=400, detail="Invalid file name")
    
    file_path = RUNS_DIR / run_id / file_name
    if not file_path.exists():
        raise HTTPException(status_code=404, detail="File not found")
    
    return FileResponse(file_path, filename=f"{run_id}_{file_name}")


@app.get("/visualization/{run_id}")
def view_visualization(run_id: str):
    run_dir = RUNS_DIR / run_id
    viz_path = run_dir / "visualization.html"

    # If it doesn't exist, try to generate it
    if not viz_path.exists():
        try:
            generate_visualization(run_id)
        except FileNotFoundError:
            raise HTTPException(status_code=404, detail="Run not found or annotated VCF missing")
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Error generating visualization: {str(e)}")

    if not viz_path.exists():
        raise HTTPException(status_code=404, detail="Visualization could not be generated")

    with open(viz_path, "r") as f:
        return HTMLResponse(f.read())
    
