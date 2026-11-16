EM31 Mapper 0.5.0 for Windows 10/11 (64-bit) - no Python or other installs needed
==================================================================================

UNZIPPING (the app comes as two zips because of a download size limit)
  Right-click EM31Mapper-Windows-part1-of-2.zip > Extract All..., then do the same with
  part2-of-2.zip using the SAME destination folder (C:\ is a good choice). Both contain an
  "EM31Mapper" folder; they merge into one. To UPDATE, extract both over the old folder.

INSTALL ON THE MESA2
  1. Copy this whole "EM31Mapper" folder to the Mesa2 (USB stick is easiest),
     e.g. to C:\EM31Mapper. Keep everything in the folder together.
  2. Double-click EM31Mapper.exe. (Right-click it > Send to > Desktop to make a shortcut.)
  3. If Windows SmartScreen says "Windows protected your PC": More info > Run anyway.
     (It's an unsigned app you built, not a Microsoft-store one.)

CHECK IT WORKS (optional, 15 seconds)
  Make a second shortcut and add  --selftest  after the .exe in its Target box,
  or run in Command Prompt:   EM31Mapper.exe --selftest
  It runs the built-in simulator end to end and shows PASS/FAIL for each part.
  Report is saved to Documents\EM31Mapper\selftest.txt

TRY WITHOUT INSTRUMENTS
  EM31Mapper.exe --simulate      (or Settings > Ports > Simulate instruments)

FIELD SETUP
  Settings > Ports: pick the EM31 COM port (9600 baud) and the GPS COM port.
  Survey data, raw captures and logs go to Documents\EM31Mapper.
  If anything goes wrong, Documents\EM31Mapper\error_log.txt has the details.
