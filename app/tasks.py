import shutil
import traceback
from pathlib import Path
from .worker import celery
from .config import RUNS_DIR, REFERENCE, SNPEFF_DB
from .utils import run_cmd
from .visualization import generate_visualization


@celery.task(bind=True)
def process_fasta(self, file_path: str):
    try:
        run_id = self.request.id
        run_dir = RUNS_DIR / run_id
        run_dir.mkdir(parents=True, exist_ok=True)

        fasta_path = Path(file_path)
        local_fasta = run_dir / fasta_path.name
        shutil.copy(fasta_path, local_fasta)

        # ===== SNIPPY =====
        snippy_out = run_dir / "snippy"
        run_cmd([
            "snippy",
            "--force",
            "--ref", str(REFERENCE),
            "--ctgs", str(local_fasta),
            "--outdir", str(snippy_out)
        ])

        snps_vcf = snippy_out / "snps.vcf"

        if not snps_vcf.exists():
            raise Exception("Snippy failed: VCF not generated")

        # ===== SNPEFF =====
        annotated_vcf = run_dir / "annotated.vcf"

        with open(annotated_vcf, "w") as out:
            run_cmd([
                "snpEff",
                "-v",
                SNPEFF_DB,
                str(snps_vcf)
            ], stdout=out)

        # ===== VISUALIZATION =====
        try:
            generate_visualization(run_id)
            viz_generated = True
        except Exception as e:
            print(f"Warning: Visualization generation failed: {e}")
            viz_generated = False

        # ===== CLEANUP =====
        # We only want to keep the final results. 
        # Move them out of the snippy folder to the run_dir, then delete the rest.
        final_snps = run_dir / "snps.vcf"
        shutil.move(str(snps_vcf), str(final_snps))
        
        # Remove the bulky snippy directory and the local fasta
        shutil.rmtree(snippy_out)
        if local_fasta.exists():
            local_fasta.unlink()
        
        # Also cleanup the upload file to save space
        if fasta_path.exists():
            fasta_path.unlink()

        return {
            "status": "completed",
            "run_id": run_id,
            "snps_vcf": str(final_snps),
            "annotated_vcf": str(annotated_vcf),
            "visualization": f"/visualization/{run_id}" if viz_generated else None
        }

    except Exception as e:
        error_msg = f"{str(e)}\n{traceback.format_exc()}"
        print(f"❌ Task failed: {error_msg}")
        return {"status": "failed", "error": str(e), "traceback": error_msg}


@celery.task
def cleanup_old_runs(hours: int = 12):
    """Delete run directories and orphaned upload files older than the specified hours."""
    import time
    now = time.time()
    cutoff = now - (hours * 3600)
    
    print(f"Starting cleanup of runs and uploads older than {hours} hours...")
    
    deleted_runs = 0
    deleted_uploads = 0
    
    if not RUNS_DIR.exists():
        return {"status": "skipped", "reason": "RUNS_DIR does not exist"}

    for item_path in RUNS_DIR.iterdir():
        if item_path.is_dir() and item_path.name == "uploads":
            # Clean up old upload files
            for upload_file in item_path.iterdir():
                if upload_file.is_file():
                    try:
                        if upload_file.stat().st_mtime < cutoff:
                            print(f"Removing orphaned upload file: {upload_file}")
                            upload_file.unlink()
                            deleted_uploads += 1
                    except Exception as e:
                        print(f"Error deleting {upload_file}: {e}")
        elif item_path.is_dir():
            # Clean up old run directories
            try:
                # Use the modification time of the directory
                mtime = item_path.stat().st_mtime
                if mtime < cutoff:
                    print(f"Removing old run directory: {item_path}")
                    shutil.rmtree(item_path)
                    deleted_runs += 1
            except Exception as e:
                print(f"Error deleting {item_path}: {e}")

    return {
        "status": "completed", 
        "deleted_runs_count": deleted_runs,
        "deleted_uploads_count": deleted_uploads
    }
