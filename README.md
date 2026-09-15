# Peak Roasting — Mountain Top Roasters offline IR-5 app

This is a local-only roast logger for the Diedrich IR-5 and Phidget 1048 (serial 422272). It runs entirely on this Mac and does not require internet access.

The large **NEXT ACTION** banner is the primary operating guide. It gives advance notice, switches to **DO NOW** for time-critical manual actions, and plays a distinct sound. It cannot move the diverter, gas, drum door, or agitator.

## Start it

1. Close Artisan so it releases the Phidget.
2. Connect and power the USB hub and Phidget.
3. Double-click `run_roast_companion.command`.
4. Wait for the status pill to turn green.
5. Enter the bean name/origin, choose Light or Medium, optionally enable the roast microphone, then click **Start recording** before charge.
6. Click **Dump + save** when the beans leave the drum.

Roasts are permanently stored under `data/roasts/<timestamp>/` as both readable JSON and CSV. Daily diagnostic and acoustic-feature logs are under `data/logs/`.

## What is automatic

- CHARGE, turning point, dry end, and DROP use curve changes.
- FC start opens as a curve/reference **candidate**. A clustered-pop microphone signal or a manual button raises its confidence.
- FC end, SC start, and SC end stay visibly lower confidence unless confirmed acoustically or manually.
- The dump countdown uses the authoritative BT target and current RoR. It is advisory and does not control the roaster.
- Stage tones play locally in the browser. The microphone detector ignores a short window around those tones.
- Air-path prompts require operator confirmation: Cooling Bin during preheat/early roast, 50/50 near yellowing, Roasting Drum approaching first crack, then Cooling Bin before discharge. The buttons only log what the operator did; they do not actuate the roaster.

## IR-5 airflow checklist

1. **Preheat and early roast:** Through Cooling Bin. This position still allows limited drum airflow on the IR-series diverter.
2. **Yellowing (~270°F indicated BT):** 50/50.
3. **Approaching first crack:** Through Roasting Drum to carry chaff and smoke.
4. **About 15 seconds before anticipated dump:** Through Cooling Bin; turn the agitator on and put the flame at Pilot Only.
5. **After approximately one minute in the cooling bin:** Diedrich's guide suggests stopping the agitator and spreading the coffee so cooling air can pass through the bed, then restarting it to discharge once the beans reach room temperature.

Exact temperatures vary with probe placement. Follow the physical labels and operating manual for this machine, especially because its original control board has been bypassed.

## Reference curves

The library starts with the confirmed IR-5 curve and four public Artisan test fixtures. Import more `.alog` files in the UI and identify their source machine. Unlike-machine profiles contribute timing evidence only; same-machine profiles may contribute temperature evidence.

The operator's event buttons in imported curves are candidate annotations, not guaranteed truth. See [SOURCES.md](SOURCES.md).

## Current IR-5 calibration

The first recorded light batch dropped at about 385°F after 56 seconds of candidate development and tasted green/grassy. That setting is retained as a failed observation. To preserve a light endpoint, the next validation centers on 390°F and 75 seconds, with 392°F treated as a soft ceiling. It is intentionally marked untested until cupping feedback confirms or rejects it. Use the Cup Feedback panel after tasting each batch.

## Important

This software reads temperatures and gives advisory alerts only. It does not replace the roaster's safety controls, operating procedures, ventilation, or the operator.
