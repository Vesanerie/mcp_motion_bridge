# Estimator — environment setup

The estimator runs in a **separate Python environment** from Blender.
Blender's embedded Python cannot handle PyTorch + heavy ML dependencies.

## macOS Apple Silicon (M1/M2/M3/M4)

```bash
# Create dedicated venv with Python 3.11
python3.11 -m venv ~/hmr2_env
source ~/hmr2_env/bin/activate

# PyTorch with MPS support
pip install torch torchvision

# 4D-Humans (HMR2.0)
pip install hmr2

# Common dependencies
pip install opencv-python numpy scipy

# Required: enable fallback for MPS ops not yet implemented
export PYTORCH_ENABLE_MPS_FALLBACK=1
```

**RAM**: 16 GB unified minimum. 32 GB recommended for long videos.

## Linux / Windows (CUDA)

```bash
python3.11 -m venv ~/hmr2_env
source ~/hmr2_env/bin/activate  # or Scripts\activate on Windows

pip install torch torchvision --index-url https://download.pytorch.org/whl/cu121
pip install hmr2 opencv-python numpy scipy
```

## Usage

```bash
# Activate the env
source ~/hmr2_env/bin/activate
export PYTORCH_ENABLE_MPS_FALLBACK=1  # macOS only

# Run on a video
python run_4dhumans.py --video /path/to/video.mp4 --out motion.npz

# Lower batch size if RAM is tight
python run_4dhumans.py --video video.mp4 --out motion.npz --batch-size 8

# Force CPU (slow but guaranteed to work)
python run_4dhumans.py --video video.mp4 --out motion.npz --device cpu
```

## Output format

See `smpl_output.py` for the full .npz specification.

Key fields:
- `smpl_poses` (N, 72) — axis-angle rotations for 24 SMPL joints
- `smpl_betas` (10,) — body shape (constant across frames)
- `smpl_trans` (N, 3) — root translation per frame
- `fps` — source video frame rate

## Limitations on macOS

- **No global trajectory**: HMR2 outputs camera-relative poses. The
  character animates "in place" in Blender. This is fine for most use
  cases (dance, sport, combat). For global movement, Claude can
  reconstruct it from 2D hip positions in the video.

- **No TRAM/WHAM**: These require DROID-SLAM with CUDA kernels.
  Not portable to MPS. A future "cloud estimator" mode will allow
  running TRAM on a remote GPU.

## Cloud estimator (coming soon)

For cinematic camera movement or when TRAM quality is needed:
- Upload video to a cloud GPU endpoint (Runpod, Colab, Lambda)
- TRAM runs remotely, returns .npz
- Same output format, consumed identically by the addon
