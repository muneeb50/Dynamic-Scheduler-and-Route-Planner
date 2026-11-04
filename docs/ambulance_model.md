# Ambulance recognition in crowded photos and video: the trained model

## Why ambulances were missed

The detector (YOLO, trained on the COCO dataset) knows *car*, *truck*, *bus* and *motorcycle*. It has no
*ambulance* class and no *auto-rickshaw* class. In a crowded street it found the ambulance as a vehicle
("car 0.82") but nothing in the pipeline had learned what an ambulance looks like. The extra checks (an
open-vocabulary model, light bar, livery) were weak on Indian ambulances: the open-vocabulary YOLOE model
recognised only 6 of 73 ambulance photos in our labelled set.

## What the pipeline does now

```
photo / frame ─► YOLO11m, whole image (1280 px) + 2×2 tiles, fused ─► one box per vehicle
                                                                    │
                     for every vehicle box ◄────────────────────────┘
                        ├─ trained vehicle classifier ─► ambulance · fire/police · auto-rickshaw · bus · motorcycle · car
                        ├─ OCR: "AMBULANCE" (also mirrored, as on bonnets), "108", "EMERGENCY"
                        ├─ lit / flashing red-blue light bar
                        └─ white/yellow livery with markings
                                    │
                        logistic fusion (weights fitted on validation data) ─► AMBULANCE yes/no + reasons
```

| Part | What it adds |
|---|---|
| **YOLO11m + tiles** (photos, *accurate* mode) | YOLO11m is a larger COCO detector and finds far more vehicles in dense traffic than 11n. Tiles turn small or distant vehicles into big ones. Overlapping detections are fused into one box per vehicle, and boxes that wrap several vehicles are removed. |
| **Trained vehicle classifier** (`models/vehicle_cls.pt`, YOLO11n-cls) | Decides *what* each detected vehicle is. Trained by us on 7,500 real vehicle crops plus 3,600 synthetic hard negatives (below). Also relabels auto-rickshaws (COCO calls them car or motorcycle) and fire/police vehicles. |
| **OCR text cue** (RapidOCR, PP-OCRv3, offline) | Indian ambulances say AMBULANCE on the back and sides, and mirrored on the bonnet. Reading it is very specific. |
| **Fusion** (`models/ambulance_fusion.json`) | A logistic model over the cues, fitted on the validation split. The threshold was chosen for precision ≥ 0.9, because a false ambulance turns a signal green. In video the fused probability is re-centred on that threshold (`decision_score`) before temporal voting, so the verifier's fixed 0.5 / 0.25 cut-offs always mean "at the threshold". |
| Video | The same classifier and OCR run per tracked vehicle in a background thread (the camera keeps its frame rate), followed by the flashing-beacon check and temporal voting as before. |

## Training data (all public)

| Source | Content | Licence |
|---|---|---|
| [HariPrashanna-07/Ambulance_dataset](https://github.com/HariPrashanna-07/Ambulance_dataset) | 5,300 Indian traffic scenes with boxes for ambulance, auto, bike, bus, car (Roboflow export, plus augmented copies) | Public domain |
| [shivamsingh96/Emergency-vs-Non-Emergency-Vehicle-Classification](https://github.com/shivamsingh96/Emergency-vs-Non-Emergency-Vehicle-Classification) | Analytics Vidhya JanataHack set: 1,646 vehicle photos, 681 emergency. We split the emergency photos by hand into 73 ambulances and 608 fire/police (`train/data/av_emergency_ambulance_labels.json`). | Hackathon dataset |
| [Jomeezius/yolov5_ambulance_detection](https://github.com/Jomeezius/yolov5_ambulance_detection) | British ambulances with boxes | CC BY 4.0 |
| [Vatsalsamarth/Ambulance-Detection](https://github.com/Vatsalsamarth/Ambulance-Detection) | Frames of Indian ambulances | CC BY 4.0 |
| [Abhi-899/YOLOV4-Custom-Object-Detection](https://github.com/Abhi-899/YOLOV4-Custom-Object-Detection) | Open Images ambulance and car boxes | CC BY 2.0 (Open Images) |

`computer_vision/train/build_ambulance_dataset.py` turns the boxes into vehicle crops (6 classes). It also
de-duplicates across sources and splits with a perceptual hash: a validation or test image that looks like
a training image is moved to training, so the test numbers come from vehicles the model never saw.

| split | ambulance | fire/police | auto-rickshaw | bus | motorcycle | car |
|---|---|---|---|---|---|---|
| train | 1,323 | 503 | 492 | 795 | 1,180 | 3,220 |
| validation | 138 | 61 | 137 | 203 | 379 | 822 |
| test | 103 | 44 | 40 | 83 | 140 | 499 |

Training: `train_vehicle_classifier.py`, YOLO11n-cls (ImageNet-pretrained), 224 px, AdamW with cosine LR,
RandAugment, colour jitter, flips, random erasing, early stopping on validation accuracy (35 epochs, best
validation top-1 0.944).

**Hard negatives (second stage).** The first model had learned a shortcut, "white body + red band = ambulance".
Painting a red stripe on ordinary test cars turned 6% of them into ambulances, and in a crowded Mumbai photo a
white taxi with a red bumper reflector was called an ambulance. `add_hard_negatives.py` adds 3,649 synthetic
training crops: cars, buses, bikes, autos and fire/police vehicles with random red, blue and red-blue stripes,
plus copies of every class (ambulances included) with drawn detection boxes and stock-photo watermarks. The model
was then fine-tuned on this for 10 more epochs. Validation and test data are untouched.

**Occlusion-aware crops.** In a jam, vehicles overlap. Before a vehicle is classified, the parts of its crop
covered by a vehicle in front of it (the box that reaches lower in the image) are greyed out, so a car is not
judged by the ambulance overlapping it.

**Scene-relative light bar (photos).** A still photo cannot show flashing, so the photo light-bar cue looks for
bright red/blue in the top band of each vehicle. In rain or a jam, the brake lights of the vehicle ahead put red into
almost every box. With three or more vehicles in a photo, the scene's median light-bar score is therefore subtracted,
so only a vehicle that stands out from its neighbours gets light-bar credit.

Finally `fit_ambulance_fusion.py` fits the fusion weights on the validation split.

## Results

All numbers are on data the model never saw. Near-duplicates of training images are removed from validation and test.

**Full traffic scenes** (`python -m computer_vision.evaluation.photo_eval`, 200 test photos from the Indian traffic
dataset, 62 of them with ambulances; `results/photo_eval.json`):

| | before (YOLO11n + YOLOE + light bar/livery) | now |
|---|---|---|
| vehicles found (161 labelled, IoU ≥ 0.5) | 69.6% | **93.8%** |
| ambulances recognised (73 labelled) | 26.0% | **82.2%** |
| reported ambulances that are labelled ambulances | 36.4% | **62.5%** |
| … counting ambulances whose label is only the siren | 38.2% | **77.1%** |
| photos without any ambulance where one was reported | 19 of 103 (18.4%) | **8 of 103 (7.8%)** |
| time per photo (2 CPU cores) | 0.6 s | 5.2 s |

The dataset does not label every ambulance. By eye, 4 of the 8 "false alarm" photos contain a real, unlabelled
ambulance, so the true false-alarm count is 4 of 103 (a white SUV, a small white van, a small truck, the back of a
white bus). The precision numbers are lower bounds.

**Single vehicles** (`fit_ambulance_fusion.py`, 909 test crops, 103 ambulances; `results/ambulance_classifier_eval.json`):

| cue | precision | recall | false alarms on other vehicles |
|---|---|---|---|
| previous cues: light bar + livery | 0.21 | 0.15 | 7.1% |
| previous open-vocabulary model (YOLOE) on the crop | – | 0.00 | 0% |
| 'AMBULANCE' text only (OCR) | 0.97 | 0.37 | 0.1% |
| trained classifier only | 0.95 | 0.84 | 0.6% |
| **fusion (used by the app)** | **0.95** | **0.85** | **0.6%** |

Vehicle-type accuracy of the classifier: **93.0%** (car 95%, bus 93%, motorcycle 93%, fire/police 91%,
auto-rickshaw 90%, ambulance 84%).

Shortcut check: painting a red or blue stripe on 806 non-ambulance test vehicles made 28 of them "ambulance" with
the first model and **3** with the final one.

**The crowded Mumbai photo that prompted this work** (a Maruti Eeco ambulance in rain, among taxis, an
auto-rickshaw and buses). Before: the ambulance was "car 0.82" and not recognised. Now: **AMBULANCE 1.00**
(classifier 1.00 + the word AMBULANCE read on the back), 11 vehicles found, the auto-rickshaw labelled
correctly, no other vehicle flagged.

## Re-training with your own photos (recommended before a public demo)

1. Collect photos or video frames of ambulances on your roads, such as Navi Mumbai 108 vans, Force Travellers or Eecos. Include night and rain.
2. Label them with any YOLO-format tool (CVAT, Label Studio, Roboflow, makesense.ai), or simply put vehicle crops into `datasets/vehicle_crops/train/ambulance/`.
3. Run `build_ambulance_dataset.py` (or add the crops), `add_hard_negatives.py`, then `train_vehicle_classifier.py` and `fit_ambulance_fusion.py`. A free Colab GPU trains it in a few minutes (2 CPU cores: about 5 hours).
4. Run `python -m computer_vision.evaluation.photo_eval` and quote the new numbers.

## Limits

- The classifier sees one vehicle at a time. A heavily hidden ambulance (only the roof visible) is hard; OCR and the beacon cue help.
- There are fewer Indian ambulance photos at night or in heavy rain than daytime ones.
- Vans that look like ambulances (white Eeco or Traveller with red stripes) can still confuse the classifier if no text is visible.
- The OCR model reads Latin letters, so Hindi or Marathi "रुग्णवाहिका" is not read.
- White SUVs, small white vans and the backs of white buses are the remaining false alarms in photos. In video, temporal voting (3 consecutive updates) filters most of them. For the demo, upload the original photo, not a screenshot of an earlier analysis with boxes drawn on it.
- A photo takes about 5 s on 2 CPU cores in *accurate* mode; *fast* mode (one pass) is ~1 s.
