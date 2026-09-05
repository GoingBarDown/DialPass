"""Inspect a recorded call WAV against Tier 1.

Slides the same analysis window the live classifier uses over a recording and
prints, per window: the current `HeuristicClassifier` verdict and the raw
features. Give `--segments` (ground truth) to also get per-label feature
distributions — that's what you retune the thresholds from.

    uv run python scripts/analyze_audio.py recordings/CAxxxx.wav \
        --segments "0:7=RINGBACK,7:38=MENU_SPEAKING,38:55=MENU_AWAITING_INPUT,55:180=HOLD_MUSIC"
"""

from __future__ import annotations

import argparse
import statistics
import wave

import numpy as np

from dialpass.agent.classifier import Frame, HeuristicClassifier, acoustic_features
from dialpass.config import get_settings

WINDOW_MS = None  # filled from settings
HOP_MS = None


def _read_wav(path: str) -> tuple[np.ndarray, int]:
    with wave.open(path, "rb") as w:
        sr = w.getframerate()
        n = w.getnframes()
        raw = w.readframes(n)
    return np.frombuffer(raw, dtype=np.int16), sr


def _parse_segments(spec: str) -> list[tuple[float, float, str]]:
    out = []
    for part in spec.split(","):
        span, label = part.split("=")
        a, b = span.split(":")
        out.append((float(a), float(b), label.strip()))
    return out


def _label_at(t: float, segments: list[tuple[float, float, str]]) -> str | None:
    for a, b, label in segments:
        if a <= t < b:
            return label
    return None


def main(argv: list[str] | None = None) -> None:
    settings = get_settings()
    window_ms = settings.classify_window_ms
    hop_ms = settings.classifier_interval_ms

    ap = argparse.ArgumentParser()
    ap.add_argument("wav")
    ap.add_argument("--segments", default="", help="a:b=LABEL,... in seconds")
    ap.add_argument("--quiet", action="store_true", help="skip the per-window dump")
    args = ap.parse_args(argv)

    pcm, sr = _read_wav(args.wav)
    clf = HeuristicClassifier()
    win = int(sr * window_ms / 1000)
    hop = int(sr * hop_ms / 1000)
    segments = _parse_segments(args.segments) if args.segments else []

    by_label: dict[str, dict[str, list[float]]] = {}
    confusion: dict[tuple[str, str], int] = {}

    if not args.quiet:
        print(
            f"{'t':>7}  {'truth':<20} {'predicted':<22} {'rms':>8} {'zcr':>6} "
            f"{'flat':>7} {'ring':>6}"
        )

    for start in range(0, max(1, pcm.size - win + 1), hop):
        seg = pcm[start : start + win]
        t = start / sr
        f = acoustic_features(seg, sr)
        pred = clf.classify(Frame(pcm=seg, sample_rate=sr, t_start=t))
        truth = _label_at(t, segments)

        if not args.quiet:
            print(
                f"{t:7.2f}  {truth or '-':<20} "
                f"{pred.label.value + f' ({pred.confidence:.2f})':<22} "
                f"{f['rms']:8.4f} {f['zcr']:6.2f} {f['flatness']:7.3f} "
                f"{f['tone_460_ratio']:6.3f}"
            )

        if truth:
            d = by_label.setdefault(truth, {k: [] for k in f})
            for k, v in f.items():
                d[k].append(v)
            confusion[(truth, pred.label.value)] = confusion.get((truth, pred.label.value), 0) + 1

    if by_label:
        print("\n=== feature distributions per ground-truth label (min / median / max) ===")
        for label, feats in by_label.items():
            print(f"\n{label}  (n={len(next(iter(feats.values())))})")
            for k, vals in feats.items():
                print(f"  {k:16} {min(vals):8.4f} {statistics.median(vals):8.4f} {max(vals):8.4f}")
        print("\n=== confusion (truth -> predicted : count) ===")
        for (t_label, p_label), c in sorted(confusion.items()):
            hit = t_label == p_label or p_label in t_label or t_label in p_label
            mark = "" if hit else "  <-- miss"
            print(f"  {t_label:20} -> {p_label:22} {c}{mark}")


if __name__ == "__main__":
    main()
