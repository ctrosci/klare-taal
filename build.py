"""Build Klare Taal: parse curriculum.txt, synthesize audio with Microsoft Edge
neural voices (edge-tts), compress with ffmpeg, embed everything in index.html.

Usage:  python build.py            (re-uses cached audio; only new phrases are synthesized)
        python build.py --check    (parse + validate only, no audio)
"""
import asyncio, base64, hashlib, json, re, subprocess, sys
from pathlib import Path

import edge_tts

ROOT = Path(__file__).parent
CACHE = ROOT / "audio_cache"
VOICES = {"F": "nl-NL-FennaNeural", "M": "nl-NL-MaartenNeural", "C": "nl-NL-ColetteNeural"}
RATE = "-5%"          # slightly slower than conversational speed, still natural
BITRATE = "40k"
MALE = ("👨", "👦", "👴", "🧔", "♂")
FEMALE = ("👩", "👧", "👵", "♀")


def key_for(voice, text):
    return hashlib.sha1(f"{voice}|{RATE}|{text}".encode()).hexdigest()[:12]


def pick_voice(text, pic, idx):
    if text.lower().startswith("ik "):
        if any(m in pic for m in MALE):
            return "M"
        if any(f in pic for f in FEMALE):
            return "F"
    return "FMC"[idx % 3]


def parse():
    units, unit, lesson = [], None, None
    warnings = []
    for n, raw in enumerate((ROOT / "curriculum.txt").read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("//"):
            continue
        if line.startswith("## "):
            kind, title = [s.strip() for s in line[3:].split("|", 1)]
            lesson = {"kind": "dialogue" if kind == "D" else "lesson", "title": title,
                      "focus": [], "voices": ["F", "M"], "items": []}
            unit["lessons"].append(lesson)
        elif line.startswith("# "):
            title, icon = [s.strip() for s in line[2:].split("|", 1)]
            unit = {"title": title, "icon": icon, "lessons": []}
            units.append(unit)
        elif line.startswith("focus:"):
            lesson["focus"] = [w.strip() for w in line[6:].split(",") if w.strip()]
        elif line.startswith("voices:"):
            lesson["voices"] = [w.strip() for w in line[7:].split(",")]
        else:
            parts = [s.strip() for s in line.split("|")]
            text, pic = parts[0], parts[1] if len(parts) > 1 else ""
            item = {"t": text, "p": pic}
            if lesson["kind"] == "dialogue":
                m = re.match(r"^([AB]):\s*(.+)$", text)
                if not m:
                    warnings.append(f"line {n}: dialogue line needs A: or B:")
                    continue
                item["s"], item["t"] = m.group(1), m.group(2)
                item["v"] = lesson["voices"][0 if item["s"] == "A" else 1]
            else:
                item["v"] = parts[2] if len(parts) > 2 else pick_voice(text, pic, len(lesson["items"]) + n)
            lesson["items"].append(item)
    # validation
    for ui, u in enumerate(units, 1):
        for li, l in enumerate(u["lessons"], 1):
            l["id"] = f"u{ui}l{li}"
            pics = [i["p"] for i in l["items"] if l["kind"] == "lesson" or i["s"] == "A"]
            dupes = {p for p in pics if pics.count(p) > 1}
            if dupes:
                warnings.append(f"{l['id']} '{l['title']}': duplicate pictures {dupes}")
            if len(l["items"]) < 4:
                warnings.append(f"{l['id']} '{l['title']}': fewer than 4 items")
            for f in l["focus"]:
                if not any(re.search(rf"\b{re.escape(f)}\b", i["t"], re.I) for i in l["items"]):
                    warnings.append(f"{l['id']}: focus word '{f}' never used")
            for i in l["items"]:
                i["a"] = key_for(i["v"], i["t"])
    return units, warnings


async def synth_one(sem, voice, text, out):
    async with sem:
        for attempt in range(4):
            try:
                raw = out.with_suffix(".raw.mp3")
                await edge_tts.Communicate(text, VOICES[voice], rate=RATE).save(str(raw))
                subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(raw),
                                "-af", "silenceremove=start_periods=1:start_threshold=-55dB,"
                                       "areverse,silenceremove=start_periods=1:start_threshold=-55dB,"
                                       "areverse,adelay=60,apad=pad_dur=0.2",
                                "-ac", "1", "-ar", "24000", "-b:a", BITRATE, str(out)], check=True)
                raw.unlink()
                return
            except Exception as e:  # network hiccups: retry
                if attempt == 3:
                    print(f"  FAILED: {text!r}: {e}")
                await asyncio.sleep(1.5 * (attempt + 1))


async def synth_all(jobs):
    sem = asyncio.Semaphore(6)
    todo = [(v, t, CACHE / f"{k}.mp3") for k, (v, t) in jobs.items() if not (CACHE / f"{k}.mp3").exists()]
    print(f"{len(jobs)} phrases, {len(todo)} to synthesize")
    done = 0
    async def run(job):
        nonlocal done
        await synth_one(sem, *job)
        done += 1
        if done % 25 == 0 or done == len(todo):
            print(f"  {done}/{len(todo)}")
    await asyncio.gather(*(run(j) for j in todo))


def main():
    units, warnings = parse()
    for w in warnings:
        print("WARN", w)
    n_items = sum(len(l["items"]) for u in units for l in u["lessons"])
    print(f"{len(units)} units, {sum(len(u['lessons']) for u in units)} lessons, {n_items} items")
    if "--check" in sys.argv:
        return
    CACHE.mkdir(exist_ok=True)
    jobs = {i["a"]: (i["v"], i["t"]) for u in units for l in u["lessons"] for i in l["items"]}
    asyncio.run(synth_all(jobs))
    audio = {}
    for k in jobs:
        f = CACHE / f"{k}.mp3"
        if f.exists():
            audio[k] = base64.b64encode(f.read_bytes()).decode()
        else:
            print("missing audio for", jobs[k])
    html = (ROOT / "app.html").read_text(encoding="utf-8")
    html = html.replace("/*__DATA__*/null", json.dumps({"units": units}, ensure_ascii=False))
    html = html.replace("/*__AUDIO__*/null", json.dumps(audio))
    (ROOT / "index.html").write_text(html, encoding="utf-8")
    print(f"index.html written: {len(html.encode()) / 1e6:.1f} MB")


if __name__ == "__main__":
    main()
