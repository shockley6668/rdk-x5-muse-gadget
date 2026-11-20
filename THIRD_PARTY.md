# Third-party code and assets

- Muse UI, avatar, simulator adapters and vendored Linux SDK: [Meta Muse Gadget SDK](https://github.com/facebookincubator/muse-gadget-sdk/tree/74a5e2d), Apache-2.0. Original file notices are preserved. RDK adaptations modify the original UI/simulator; the Python bridge and launcher are community additions.
- Voice reply matching uses Meta's [MuseTurn at 08e5980](https://github.com/facebookincubator/muse-gadget-sdk/blob/08e59803b7606a0ba585238ad7e701a058072dfa/linux/src/musegadget/muse_turn.py) under Apache-2.0.
- GNU Unifont 16.0.04 CJK bitmap subset, by Roman Czyborra, Paul Hardy and contributors: SIL Open Font License 1.1, or GPL-2.0-or-later with the font embedding exception. Distributed here under OFL 1.1. See `components/muse/fonts/README.md` and `LICENSES/Unifont-COPYING.txt`.
- LVGL 9.5.0 (MIT), SDL2 (zlib): fetched at build time or provided by the system. See `simulator/THIRD_PARTY.md`.
- Python dependencies are installed separately; their respective licenses apply.

- SPI initialization generator and legacy overlay sources: Shukun Huang's
  [Cathay-Launch-UI, revision 623032d](https://github.com/shockley6668/Cathay-Launch-UI/tree/623032d56395b018387b8f262ac0693e2ca28e2d),
  carried over at the author's request. The display guide documents corrections
  to pin numbering and the derived Waveshare pogo profile. Kernel module binaries
  and Linux driver source are not redistributed here; the quick installer fetches
  the pinned GPL module downloads listed in `hardware/display/drivers.json`.
- Vendor hardware geometry embedded in the CAD: D-Robotics RDK X5 and GS130WI,
  and Waveshare 3.5inch RPi LCD (F). Vendor geometry/marks retain their respective
  ownership and are not relicensed by this repository's code license. Reference sources: [GS130WI](https://archive.d-robotics.cc/downloads/hardware/accessories/gs130wi/RDK%20Stereo%20Camera%20GS130WI%20Community.STEP),
  [RDK X5](https://archive.d-robotics.cc/downloads/hardware/rdk_x5/),
  [Waveshare LCD F](https://www.waveshare.com/3.5inch-rpi-lcd-f.htm).
- The Fusion timeline retains a retired PAM8302 reference from
  [Adafruit CAD Parts](https://github.com/adafruit/Adafruit_CAD_Parts/tree/main/2130%20PAM8302).
  It is removed from the current assembly and print set; the source geometry
  retains its original ownership.
