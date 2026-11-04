# Final demonstration script (~12 minutes)

**Before you start:** backend running (`bash scripts/run.sh` or `scripts\run.ps1`), dashboard open at <http://localhost:8000>, phone/emulator running the Flutter app (or use the *Request ambulance* tab). Set demand to **medium**, speed **3×**, priority **on**. Let it run 2–3 minutes so traffic builds up.

1. **The map (45 s).** Live map: real Nerul–Seawoods roads from OpenStreetMap, simulated cars (blue moving, orange stopped), simulated signals (zoom in: each approach coloured G/Y/R; *inferred* ones marked), hospitals, ambulance bases. Open **Map area** and show the presets / place search; optionally load *Navi Mumbai* with demand *high* and *Random emergencies 30/hour* to show city scale (thousands of vehicles, hundreds of signals).
2. **Booking (45 s).** On the phone, tap a pickup point in Nerul, choose a hospital, request. Show the booking appear in *Active journeys* with an assigned ambulance and ETA, and the dashed route on the map.
3. **Tracking (30 s).** Phone shows the ambulance moving, ETA counting down, "Live" link chip. The map follows the ambulance.
4. **Priority (1.5 min).** As the ambulance nears a junction, the event log shows *Priority requested … ETA x s, n vehicles ahead, lead y s*. Click the junction: the purple ring, the phase switching through amber and all-red, then green for the ambulance's approach. Point out the queue in front being flushed *before* the ambulance arrives.
5. **Release + recovery (1 min).** "Ambulance cleared n22 – priority released". The junction turns orange (recovery). On the *Intersections* page show the backlog chart (ΔQ vs baseline) and the green times the recovery engine applied, then "Recovery completed in N s".
6. **Second junction, arrival (30 s).** Repeat at the next junction; the phone shows *Arrived at hospital* with the number of green lights granted.
7. **Live video analytics (1.5 min).** *Video AI* tab: a phone on the window running an IP-camera app (or a clip you recorded at a Nerul junction) → YOLO boxes, track ids, the counting line, live queue / flow / congestion. Click **Attach** to junction n22 → on *Intersections* the approach shows 📷 *live camera*, and the next ambulance's priority uses the real queue. Show a batch-processed clip's annotated video and CSV.
8. **Camera → simulated signal (1.5 min).** *Video AI*: open the webcam (it shows every object – people, phone, cup – so the detector is visibly working; *Test detector* shows the model check). Attach it to a junction approach (*Simulated junction*). Show an ambulance clip on a phone to the webcam → red **AMBULANCE** box with the cues → on the *Signals* tab that junction goes amber → all-red → green for that approach; when the ambulance crosses the stop line it is released and the junction recovers. Also show *Intersections → Send test ambulance through* on any junction. (Optional: the LED signal under *Signals → advanced*.)
9. **Simulated cameras (30 s).** *Computer vision* tab: camera frame with detections (red = stopped, green = moving), estimates next to ground truth, then the accuracy numbers (precision/recall, queue MAE). Mention the frames are simulated.
10. **Evidence (1.5 min).** *Analytics* tab: the matched experiment (low/medium/high, 10 seeds): driving-time reduction with confidence intervals, signal delay, stops, the cost to other traffic, recovery time, and **0 conflicting greens**. Close with the limitations slide.

## Backup plan

* No internet → map tiles are missing but roads, vehicles and signals still draw (they come from the backend). Load the real map once beforehand – it is cached in `data/maps` and works offline afterwards.
* Phone cannot reach the laptop → use the *Request ambulance* tab in the browser (same API).
* Anything breaks mid-demo → *Reset run* on the Live page starts a fresh simulation in a second.
