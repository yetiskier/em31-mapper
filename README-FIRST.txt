EM31 Mapper 0.5.1 for Windows 10/11 (64-bit) - no Python or other installs needed
==================================================================================

INSTALL ON THE MESA2
  1. Right-click EM31Mapper.zip > Extract All..., set the destination to C:\ and
     click Extract. You get C:\EM31Mapper. Keep everything in the folder together.
     To UPDATE: close the program, delete C:\EM31Mapper, extract the new zip the same way.
     (Settings and survey data are kept; they live outside this folder.)
  2. Double-click EM31Mapper.exe. (Right-click it > Send to > Desktop to make a shortcut.)
  3. The program is signed by Aesir Consulting LLC. While the signature is new, Windows
     SmartScreen may still say "Windows protected your PC": check that it names
     Aesir Consulting LLC as the publisher, then choose More info > Run anyway.

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
