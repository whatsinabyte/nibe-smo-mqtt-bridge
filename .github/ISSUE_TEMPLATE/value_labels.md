---
name: Value labels
about: Report what your controller shows for a value the bridge labels as not confirmed
title: "[Value labels] Point "
labels: help wanted
assignees: ''

---

Some value labels in the bridge were inferred, or are only partly known, because the project could only check them on the hardware it has. What your controller shows for a value tells us what it means. No programming needed: just compare the two.

## The point

| Item | Value |
|---|---|
| Point number | e.g. `56150` (shown in the Entity Manager card) |
| Title | e.g. `All sub units operating prio` |

## What you see

For each value you can observe, one row. Read both at the same moment.

| What Home Assistant shows | What the controller display or myUplink shows at that moment |
|---|---|
| e.g. `3` | e.g. `Hot water` |
| | |

If Home Assistant shows a label (not a number) that is wrong, write the label and what it should be.

## Your installation

| Item | Value |
|---|---|
| Controller model | e.g. `SMO S40`, `VVM S320` |
| Heat pump and accessories | e.g. `S2125-12`, `ACS 45` |
| Firmware version | e.g. `4.13.12` (menu 3.7 on the controller) |
| Controller language | e.g. `English` |

Pull requests are welcome too, but an issue with the table above is all that's needed.
