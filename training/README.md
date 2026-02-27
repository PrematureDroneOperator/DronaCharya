# dronAcharya Target Model Training

This module provides a complete pipeline to train target detection for `dronAcharya`, from raw media to deployable weights.

## 1) Prerequisites

Use a machine with an NVIDIA GPU for training.

```bash
cd dronAcharya
python3 -m venv .venv
source .venv/bin/activate
pip install -U pip
pip install -r requirements.txt
```

## 2) Workspace Layout

```text
training/workspace/
├── raw/
│   ├── images/      # raw still images
│   └── videos/      # raw videos
├── prepared/
│   └── images/      # cleaned images for annotation
├── annotations/
│   ├── coco/        # COCO json exports from CVAT/Label Studio/etc
│   └── yolo_raw/    # YOLO txt labels (pre-split)
└── yolo_dataset/    # final split dataset with data.yaml
```

## 3) Put Raw Data

Copy data from flights:
- raw frame dumps from mapping runs (for example `data/maps/session_xxx/frames/*.jpg`)
- raw videos recorded during scouting flights

Place them under:
- `training/workspace/raw/images`
- `training/workspace/raw/videos`

## 4) Convert Raw Media to Prepared Images

This step:
- extracts video frames
- applies blur filtering
- deduplicates near-identical frames by SHA1 hash
- optionally resizes

```bash
python -m training.prepare_raw_media \
  --raw-images-dir training/workspace/raw/images \
  --raw-videos-dir training/workspace/raw/videos \
  --output-images-dir training/workspace/prepared/images \
  --manifest-path training/workspace/prepared/manifest.csv \
  --video-frame-step 8 \
  --min-sharpness 40 \
  --resize-width 1280 \
  --jpeg-quality 95
```

Tune:
- `--video-frame-step`: higher => fewer frames
- `--min-sharpness`: higher => stricter blur rejection

## 5) Annotate Targets

Annotate `training/workspace/prepared/images` in one of these ways:

1. Manual (recommended):
- Use CVAT/Label Studio/Roboflow
- Label class name exactly `target`
- Export as COCO JSON

2. Bootstrap + manual correction:
- Run pseudo-labeling with existing model
- Correct labels in an annotation tool

### 5A) If you have COCO export

Put COCO JSON at:
`training/workspace/annotations/coco/instances.json`

Convert to YOLO txt:

```bash
python -m training.coco_to_yolo \
  --coco-json training/workspace/annotations/coco/instances.json \
  --output-labels-dir training/workspace/annotations/yolo_raw \
  --class-names target \
  --write-empty-labels
```

### 5B) Optional pseudo-label bootstrap

```bash
python -m training.auto_annotate \
  --images-dir training/workspace/prepared/images \
  --model models/target_yolo.pt \
  --output-labels-dir training/workspace/annotations/yolo_raw \
  --conf 0.30 \
  --imgsz 640 \
  --device 0 \
  --class-id-filter 0 \
  --overwrite
```

Then manually fix labels before training.

## 6) Split Dataset into train/val/test

```bash
python -m training.split_yolo_dataset \
  --source-images-dir training/workspace/prepared/images \
  --source-labels-dir training/workspace/annotations/yolo_raw \
  --output-dataset-dir training/workspace/yolo_dataset \
  --train-ratio 0.8 \
  --val-ratio 0.2 \
  --test-ratio 0.0 \
  --class-names target \
  --copy-mode copy
```

This generates:
- `training/workspace/yolo_dataset/images/{train,val,test}`
- `training/workspace/yolo_dataset/labels/{train,val,test}`
- `training/workspace/yolo_dataset/data.yaml`

## 7) Validate Dataset Quality

```bash
python -m training.validate_yolo_dataset \
  --dataset-dir training/workspace/yolo_dataset \
  --num-classes 1 \
  --report-json training/workspace/validation_report.json \
  --fail-on-missing-label
```

Fix all reported errors before training.

## 8) Train YOLO

Recommended for first run:

```bash
python -m training.train_yolo \
  --data training/workspace/yolo_dataset/data.yaml \
  --model yolov8n.pt \
  --project training/runs \
  --name target-detector-v1 \
  --epochs 120 \
  --imgsz 640 \
  --batch 16 \
  --device 0 \
  --workers 4 \
  --patience 30 \
  --cache disk
```

Best weights output:
`training/runs/target-detector-v1/weights/best.pt`

## 9) Evaluate

```bash
python -m training.evaluate_yolo \
  --model training/runs/target-detector-v1/weights/best.pt \
  --data training/workspace/yolo_dataset/data.yaml \
  --split val \
  --imgsz 640 \
  --device 0
```

## 10) Export for Deployment

ONNX:

```bash
python -m training.export_model \
  --model training/runs/target-detector-v1/weights/best.pt \
  --format onnx \
  --imgsz 640 \
  --device 0 \
  --half
```

TensorRT engine:

```bash
python -m training.export_model \
  --model training/runs/target-detector-v1/weights/best.pt \
  --format engine \
  --imgsz 640 \
  --device 0 \
  --half \
  --workspace 2.0
```

## 11) Deploy into dronAcharya

Copy best weights into:
`models/target_yolo.pt`

Ensure [config/config.yaml](../config/config.yaml) has:

```yaml
vision:
  model_path: "models/target_yolo.pt"
  conf_threshold: 0.35
  target_class_name: "target"
  image_size: 640
```

## 12) Practical Data Collection Rules

- Capture across altitudes, sun angles, and backgrounds.
- Include hard negatives (round objects that are not targets).
- Keep at least 20% validation data from different flights than training.
- Avoid heavy class imbalance.
- Start with `yolov8n` for fast iteration, then test `yolov8s` if needed.
