# ProxyForge

Desktop app that turns Magic: The Gathering card images into **true 1200 DPI
print-ready proxies** using AI upscaling on your own GPU — free, offline
after setup, no upload limits.

Unlike web-based proxy builders (which embed ~300 DPI images into a
"1200 DPI" PDF), this app reconstructs real detail with per-card AI model
selection, then builds guillotine-ready sheets with print-shop features.

## Features

- **Real AI upscaling to 2976×4160 px (1200 DPI at 63×88 mm)** on your GPU
  (Vulkan). Per-card automatic model choice: paper scans, digital renders
  and photorealistic art each get the model that suits them, with manual
  override per card.
- **Card sources**: card names, Scryfall links (any language), Gatherer
  links (classic and new format, foreign printings), decklist paste
  (`1 Card Name (SET) 123 [matte] *F*`), Archidekt deck URLs, local files
  (PNG/JPG/WEBP/AVIF/…), drag & drop. Double-faced cards fetch both faces.
- **Print sheets**: 3×3 at exactly 63×88 mm on A4/Letter, lossless or JPEG
  PDF, corner cut guides + margin ticks (customizable), edge bleed with
  color, page shift for thick-stock feeding, split into one file per page.
- **Duplex backs**: mirrored back pages for flip-on-long-edge printing,
  DFC backs paired automatically, user-supplied generic back, offset and
  bleed to survive duplex drift.
- **Printer calibration**: printable calibration sheet (9 color profiles),
  shadow-lift test sheet (rescues dark details crushed by inkjet + matte
  lamination), output sharpening. All applied at PDF time — PNG masters
  stay untouched.
- **Live preview** of the sheet while you tweak export options.
- Parallel processing, retry of failed cards, status filtering, in-app
  auto-update.

## Install (Windows)

1. Download the latest `.exe` from [Releases](../../releases).
2. Run it. On first launch it downloads the AI engine and models
   (~110 MB, one time) from their official sources and detects your GPU.
3. No compatible Vulkan GPU? The app still works with high-quality
   resizing instead of AI upscaling.

## Run from source

```
pip install -r requirements.txt
python main.py
```

## Licence

ProxyForge is **free to use** but it is **not open source**. The code is
published so anyone can read and audit what the app does — you are welcome
to study it, build it for your own use, and send bug reports or pull
requests. You may not redistribute it, publish a modified or rebranded
version, or sell it. See [LICENSE](LICENSE).

## Legal

- This is an unofficial Fan Content project, not affiliated with or
  endorsed by Wizards of the Coast. It ships no Wizards assets; card
  images are fetched at the user's request from public APIs.
- Card data and images courtesy of [Scryfall](https://scryfall.com);
  this app is not affiliated with Scryfall.
- AI engine: [Real-ESRGAN](https://github.com/xinntao/Real-ESRGAN)
  (BSD-3). Community models UltraSharp (Kim2091) and High Fidelity are
  fetched from the [Upscayl](https://github.com/upscayl/upscayl) project
  and carry non-commercial licenses.
- Intended for personal playtesting. You are responsible for how you use
  the output.

## Support

Free for the community. If ProxyForge saves you time, donations help keep
it maintained:

[![Donate](https://img.shields.io/badge/PayPal-Donate-gold?logo=paypal)](https://www.paypal.com/paypalme/warchazzz)
