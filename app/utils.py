from app.config import DOCKER_CONDA, DOCKER_CONDA_ALT, MAC_CONDA
import subprocess
from tenacity import retry, stop_after_attempt, wait_fixed
import glob
import os

@retry(stop=stop_after_attempt(2), wait=wait_fixed(5))
def run_cmd(cmd, stdout=None):
    # Ensure system environment doesn't interfere with Conda tools
    env = os.environ.copy()
    
    # 1. Unset JAVA_HOME to avoid system Java overriding Conda's Java 21
    if "JAVA_HOME" in env:
        del env["JAVA_HOME"]
        
    # 2. Detect Conda Prefix (Dynamic for Mac/Linux/Docker)
    # Use environment variable if set, otherwise detect based on existence
    conda_prefix = os.getenv("CONDA_PREFIX")
    if not conda_prefix:
        if os.path.exists(DOCKER_CONDA):
            conda_prefix = DOCKER_CONDA
        elif os.path.exists(DOCKER_CONDA_ALT):
            conda_prefix = DOCKER_CONDA_ALT
        else:
            conda_prefix = MAC_CONDA
    
    # 3. DYNAMICALLY detect installed Perl version — do NOT hardcode 5.32
    perl_paths = [f"{conda_prefix}/lib/perl5/site_perl"]
    for version_dir in sorted(glob.glob(f"{conda_prefix}/lib/perl5/[0-9]*")):
        perl_paths.append(f"{version_dir}/site_perl")
    env["PERL5LIB"] = ":".join(perl_paths)
    
    # 4. Ensure Conda bin is prioritized in PATH
    env["PATH"] = f"{conda_prefix}/bin:" + env.get("PATH", "")
    
    try:
        subprocess.run(cmd, check=True, stdout=stdout, stderr=subprocess.PIPE, env=env)
    except subprocess.CalledProcessError as e:
        # Log stderr so we can see the actual tool error
        stderr_output = e.stderr.decode(errors="replace") if e.stderr else "N/A"
        print(f"❌ Command failed (exit {e.returncode}): {' '.join(str(c) for c in cmd)}")
        print(f"   stderr: {stderr_output}")
        raise
