# Raspberry Pi 5 Setup — Trump Endorsements Scraper Project
_Summary of setup conversation for future reference_

---

## Project Overview

Building a system that runs on a Raspberry Pi 5 (16GB RAM) to:
1. Scrape Trump statements (tweets, Truth Social posts, press conference transcripts, etc.)
2. Use a local LLM to detect when Trump endorses a specific company or financial asset
3. Send alerts when an endorsement is detected

The scraper side is being handled separately. This conversation focused on the LLM and hardware setup.

---

## Hardware

**Raspberry Pi 5 — 16GB RAM**

**Storage: Samsung PM9C1a 256GB M.2 2230 NVMe (PCIe Gen 4)**
- Purchased from AliExpress (~$55 CAD)
- DRAM-less, uses HMB (Host Memory Buffer)
- Rated 6,000/5,600 MB/s sequential — but bottlenecked by Pi 5's PCIe x1 lane
- Achieved **879 MB/s** real-world read speed on the Pi with Gen 3 enabled (see below)
- Used as boot drive (replaced USB/SD boot)

**M.2 HAT** required to connect NVMe to the Pi 5.

---

## Model Selection

**Chosen model: Qwen3-8B Q4_K_M**

Reasoning:
- Task is structured extraction (endorsement detection), not complex reasoning — doesn't need a huge model
- ~5.5GB RAM usage, leaves ~10GB free for OS and scraper processes
- ~3–4 tokens/sec on Pi 5 CPU — fast enough for a live feed
- Excellent structured/JSON output for reliable company name extraction
- Outperforms smaller models on edge cases (implicit endorsements, financial references)

### Model size reference for 16GB Pi 5:
| Model | RAM | Notes |
|---|---|---|
| Qwen3-4B Q4_K_M | ~2.7GB | Fast, good for simple extraction |
| Qwen3-8B Q4_K_M | ~5.5GB | **Current choice — sweet spot** |
| Qwen3-14B Q4_K_M | ~9GB | Fits in RAM, slower |
| Qwen3-30B-A3B Q4_K_M | ~18GB | Requires NVMe swap, batch only |

Note: MoE models (A3B, A4B) have low *active* params but still require all weights loaded in RAM.

---

## LLM Setup — Ollama + Qwen3-8B

### Install Ollama
```bash
curl -fsSL https://ollama.com/install.sh | sh
ollama pull qwen3:8b
ollama run qwen3:8b "Say hello"
```

### Test the API directly
```bash
curl http://localhost:11434/api/generate \
  -d '{
    "model": "qwen3:8b",
    "prompt": "Say hello in one word.",
    "stream": false,
    "think": false
  }'
```

### endorsement_detector.py
Located at: `Trump endorsements scraper/endorsement_detector.py`

Key design decisions:
- Uses Ollama REST API at `http://localhost:11434/api/generate`
- `"think": false` — disables Qwen3 chain-of-thought mode for faster inference
- `temperature: 0.1` — low temp for consistent JSON output
- Returns structured `EndorsementResult` dataclass
- `is_actionable()` filters by confidence (high/medium) before alerting
- Detects: explicit endorsements, implicit praise, financial asset references

Tested on 4 cases — all passed correctly on first run.

---

## NVMe Boot Setup

### Step 1 — Enable PCIe Gen 3 (do this first)
```bash
sudo nano /boot/firmware/config.txt
# Add at bottom:
dtparam=pciex1
dtparam=pciex1_gen=3
sudo reboot
```

Verify:
```bash
sudo dmesg | grep -i pcie
sudo hdparm -t /dev/nvme0n1   # Should show ~879 MB/s
```

### Step 2 — Clone SD/USB to NVMe using rpi-clone

**Important:** Use Jeff Geerling's fork of rpi-clone — the default version doesn't handle NVMe partition naming (`nvme0n1p1` vs `nvme0n11`).

```bash
sudo rm /usr/local/sbin/rpi-clone
sudo wget https://raw.githubusercontent.com/geerlingguy/rpi-clone/master/rpi-clone -O /usr/local/sbin/rpi-clone
sudo chmod +x /usr/local/sbin/rpi-clone
```

If rpi-clone fails with mount errors, wipe the NVMe first:
```bash
sudo wipefs -a /dev/nvme0n1
sudo rpi-clone nvme0n1
```

### Step 3 — Set boot order
```bash
sudo raspi-config nonint do_boot_order B2
sudo reboot
```

### Step 4 — Verify booted from NVMe
```bash
findmnt /
# Should show /dev/nvme0n1p2
```

---

## NVMe Notes

- Pi 5 PCIe is officially Gen 2 (~500 MB/s) but supports unofficial Gen 3 (~900 MB/s)
- Gen 3 is stable and widely used — enabled via `config.txt` as above
- Achieved 879 MB/s in testing
- NVMe swap is viable (~900 MB/s vs ~400 MB/s USB 3.0) but still CPU-bottlenecked
- Capacity verified implicitly — 12.6GB rsync completed successfully and OS boots

---

## Samsung NVMe Lineup (for reference)

| Model | NAND | Notes |
|---|---|---|
| PM9B1 | TLC | Older gen, pulled from Surface/laptops |
| PM9C1a | TLC | **Current drive** — newer 5nm controller, better efficiency |
| BM9C1 | QLC | Larger capacity but lower endurance/performance than PM9C1a |

All are DRAM-less (HMB). For a boot/swap drive, PM9C1a TLC is the best of the three.

---

## Current State

- Pi 5 boots from NVMe (PM9C1a 256GB) at PCIe Gen 3 speeds
- Ollama running with Qwen3-8B Q4_K_M
- `endorsement_detector.py` written and tested
- SSH working
- Ready to integrate with scraper pipeline
