# Using CompassCairn

Every example below is real output. `python3 compasscairn.py` is abbreviated to `compasscairn`
(the console script `install.sh` installs); `COMPASSCAIRN_PASSPHRASE` is set so nothing prompts.

## 1. Create the store once

```bash
$ compasscairn init
Initialized CompassCairn store at ./compasscairn.json (drift threshold 10.0°)
```

The passphrase (8 characters minimum) is asked for twice unless `COMPASSCAIRN_PASSPHRASE` is set.
Only a salted scrypt hash and its salt land in the file:

```json
"kdf": { "name": "scrypt", "n": 16384, "r": 8, "p": 1, "salt": "356b0b4b47b608bf040bb342414a92fa" },
"passphrase_hash": "f81919af57ee708c6ed2b48fc65db75f670360d9253751607525ae8fa5a78e7e"
```

`init` refuses to clobber an existing store: `CompassCairn store already initialized` (exit 4).

## 2. Record magnetic declination per location

```bash
$ compasscairn declination set Reykjavik -13.5
Declination for Reykjavik set to -13.5°

$ compasscairn declination set "Sable Point" 20.0
Declination for Sable Point set to 20.0°

$ compasscairn declination list
LOCATION      DECL
-----------  -----
Reykjavik    -13.5
Sable Point   20.0

2 location(s) on file
```

Lookups are case-insensitive and spelling-stable: the first spelling wins as the display name,
and re-setting the same location updates that row in place.

```bash
$ compasscairn declination get reykjavik
Reykjavik  -13.5

$ compasscairn declination set reykjavik -14.0
Declination for Reykjavik updated to -14.0°

$ compasscairn declination get atlantis
compasscairn: No declination stored for atlantis — set one with: compasscairn declination set atlantis <degrees>
$ echo $?
4
```

Out-of-range values write nothing:

```bash
$ compasscairn declination set Reykjavik 400
compasscairn: declination must be a number between -180 and 180 (got 400)
$ echo $?
2
```

## 3. Log waypoints

`--mag` is the bearing read off the compass, `--true` the true bearing you recorded. CompassCairn
applies the location's declination, wraps the result into `-180..180` and stores the delta.

```bash
$ compasscairn waypoint add "Whale Bay" --location Reykjavik --mag 170 --true -170
Saved waypoint 0001 "Whale Bay"
  location             Reykjavik
  magnetic_bearing     170.0
  true_bearing         -170.0
  declination_applied  -13.5
  corrected_bearing    156.5
  delta                -33.5
  status               FLAG (past the 10.0° drift threshold)
```

Wraparound in action — magnetic 170 with declination 20 is `-170`, not 190, so the delta is 0:

```bash
$ compasscairn waypoint add "Kelp Reef" --location "Sable Point" --mag 170 --true -170
Saved waypoint 0002 "Kelp Reef"
  location             Sable Point
  magnetic_bearing     170.0
  true_bearing         -170.0
  declination_applied  20.0
  corrected_bearing    -170.0
  delta                0.0
  status               ok (within the 10.0° drift threshold)
```

A location with no stored declination is not an error — it is saved with 0.0 applied and you get a
one-line notice:

```bash
$ compasscairn waypoint add "North Spur" --location "Gull Rock" --mag 10 --true 10
no declination on file for Gull Rock — applied 0.0
Saved waypoint 0003 "North Spur"
  location             Gull Rock
  magnetic_bearing     10.0
  true_bearing         10.0
  declination_applied  0.0
  corrected_bearing    10.0
  delta                0.0
  status               ok (within the 10.0° drift threshold)
```

Bad input names the field and records nothing:

```bash
$ compasscairn waypoint add "Bad" --location "Gull Rock" --mag 200 --true 10
compasscairn: magnetic_bearing must be a number between -180 and 180 (got 200)
$ echo $?
2
```

## 4. Read the log

```bash
$ compasscairn waypoint list
NAME        LOCATION       MAG    TRUE   DECL    CORR  DELTA  STATUS
----------  -----------  -----  ------  -----  ------  -----  ------
Whale Bay   Reykjavik    170.0  -170.0  -13.5   156.5  -33.5  FLAG
Kelp Reef   Sable Point  170.0  -170.0   20.0  -170.0    0.0  ok
North Spur  Gull Rock     10.0    10.0    0.0    10.0    0.0  ok

3 waypoint(s) logged — drift threshold 10.0°
```

Empty log:

```bash
$ compasscairn waypoint list
No waypoints logged yet — add one with: compasscairn waypoint add <name> --location <loc> --mag <deg> --true <deg>
```

## 5. Flag drift

```bash
$ compasscairn drift list
Flagged — 1 of 3 waypoint(s) past the 10.0° drift threshold (stored threshold)
NAME       LOCATION    CORR    TRUE  DELTA  STATUS
---------  ---------  -----  ------  -----  ------
Whale Bay  Reykjavik  156.5  -170.0  -33.5  FLAG
```

Nothing over the bar:

```bash
$ compasscairn drift list --threshold 50
No bearings past the 50.0° drift threshold
Raise the bar with: compasscairn config set threshold <degrees>
```

`--threshold` never persists. To change the stored value:

```bash
$ compasscairn config set threshold 5
Drift threshold set to 5.0° (was 10.0°)

$ compasscairn drift list
Flagged — 1 of 3 waypoint(s) past the 5.0° drift threshold (stored threshold)
NAME       LOCATION    CORR    TRUE  DELTA  STATUS
---------  ---------  -----  ------  -----  ------
Whale Bay  Reykjavik  156.5  -170.0  -33.5  FLAG
```

## 6. Scripting it

Everything is non-interactive when `COMPASSCAIRN_PASSPHRASE` is set, results go to stdout, errors
to stderr, and `--json` gives stable structured output:

```bash
$ COMPASSCAIRN_PASSPHRASE=... compasscairn --json drift list | jq -r '.flagged[].name'
Whale Bay
```

```json
{
  "threshold_degrees": 10.0,
  "threshold_source": "stored threshold",
  "stored_threshold_degrees": 10.0,
  "flagged": [ { "id": "0001", "name": "Whale Bay", "delta": -33.5, "...": "..." } ]
}
```

Exit codes: `0` success, `2` usage/validation, `3` passphrase rejected, `4` store or data error.
A rejected passphrase leaves the store byte-for-byte unchanged.

Use a second store for a different expedition:

```bash
compasscairn --store ./expedition-2026.json waypoint list
```
