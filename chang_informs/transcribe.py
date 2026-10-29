#!/usr/bin/env python3
"""音频转文字（faster-whisper），输出带时间戳的 .txt + .json。

用法:
    python chang_informs/transcribe.py <audio> -o <out_prefix> [-m medium] [-d cpu] [-c int8]

示例:
    python chang_informs/transcribe.py audio/xxx.m4a -o transcripts/xxx -m small
"""
import argparse
import json
import time

from faster_whisper import WhisperModel


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("audio")
    ap.add_argument("-o", "--out", required=True, help="输出前缀，会写 <out>.txt / <out>.json")
    ap.add_argument("-m", "--model", default="small")
    ap.add_argument("-d", "--device", default="cpu")
    ap.add_argument("-c", "--compute", default="int8")
    ap.add_argument("-l", "--lang", default="zh")
    args = ap.parse_args()

    t0 = time.time()
    print(f"[load] model={args.model} device={args.device} compute={args.compute}", flush=True)
    model = WhisperModel(args.model, device=args.device, compute_type=args.compute)
    print(f"[load] done in {time.time() - t0:.1f}s", flush=True)

    t1 = time.time()
    segments, info = model.transcribe(
        args.audio,
        language=args.lang,
        vad_filter=True,
        beam_size=5,
        condition_on_previous_text=False,
    )
    segs = []
    for s in segments:
        text = s.text.strip()
        segs.append({"start": round(s.start, 2), "end": round(s.end, 2), "text": text})
        print(f"[{s.start:7.2f}-{s.end:7.2f}] {text}", flush=True)

    with open(args.out + ".json", "w", encoding="utf-8") as f:
        json.dump(segs, f, ensure_ascii=False, indent=2)
    with open(args.out + ".txt", "w", encoding="utf-8") as f:
        for s in segs:
            f.write(f"[{s['start']:7.2f}-{s['end']:7.2f}] {s['text']}\n")

    print(f"\nDONE {len(segs)} segs, lang={info.language}, "
          f"audio={info.duration:.1f}s, cost={time.time() - t1:.1f}s", flush=True)


if __name__ == "__main__":
    main()
