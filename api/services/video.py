"""Video assembly — N narration images + branded outro card + WAV + captions → MP4.

Layout:
- Each script beat (hook / origin / payoff) carries 1–4 image prompts. A
  beat's duration is split evenly among its images.
- Audio: Piper-rendered WAV including the CTA outro line.
- Outro: a Pillow-rendered branded card (SUBSCRIBE button + bell hint) shown
  during the CTA's audio window — same card across every short for brand
  consistency.
- Captions: SENTENCE-LEVEL LIVE CAPTIONS (each beat's narration split into
  sentences, plus the CTA as a final sentence aligned with the outro card).
- Title: ALWAYS-ON. Big bold word top center for the entire video.

Duration math (when outro is enabled):
  per_word_s = total_audio_s / (story_words + cta_words)
  story_dur  = story_words * per_word_s
  cta_dur    = cta_words   * per_word_s
  → 3 beat segments fill [0, story_dur], outro fills [story_dur, total]
"""

from __future__ import annotations

import logging
import re
import subprocess
import tempfile
import wave
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from config import settings
from models import Script
from services.text_normalize import normalize_for_caption, normalize_inline

log = logging.getLogger("shorts-api.video")

OUTPUT_W = 1080
OUTPUT_H = 1920
FPS = 30

TITLE_FONT_SIZE = 140
TITLE_FONT_SIZE_MIN = 70
TITLE_MAX_WIDTH = 1000
CAPTION_FONT_SIZE = 60
CAPTION_MAX_TEXT_WIDTH = 900

FONT_CANDIDATES = [
    Path.home() / "n8n-shorts" / "assets" / "fonts" / "Inter-Bold.ttf",
    Path.home() / "n8n-shorts" / "assets" / "fonts" / "Inter-Black.ttf",
    Path("/System/Library/Fonts/Supplemental/Arial Bold.ttf"),
    Path("/Library/Fonts/Arial Bold.ttf"),
]


def _pick_font() -> Path:
    for p in FONT_CANDIDATES:
        if p.exists():
            return p
    raise RuntimeError("no usable font on this machine")


def _wav_duration_s(wav_path: Path) -> float:
    with wave.open(str(wav_path), "rb") as r:
        frames = r.getnframes()
        rate = r.getframerate()
        return frames / rate if rate else 0.0


def _compute_beat_durations(script: Script, total_s: float) -> list[float]:
    word_counts = [max(1, len(b.narration.split())) for b in script.beats]
    total_words = sum(word_counts)
    durs = [(wc / total_words) * total_s for wc in word_counts]
    durs[-1] = round(total_s - sum(durs[:-1]), 3)
    return [round(d, 3) for d in durs]


_SENT_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")


def _split_for_video(narration: str) -> list[tuple[str, list[str]]]:
    """Split narration into sentences. Returns (caption_text, voice_tokens) per
    sentence. Caption preserves digits/ordinals as written; voice_tokens are the
    fully-expanded spoken form (what Piper actually says) — their count gates
    proportional sentence durations, and the tokens themselves feed forced
    alignment so the word↔sentence mapping is exact by construction."""
    parts = _SENT_SPLIT_RE.split(narration.strip())
    out: list[tuple[str, list[str]]] = []
    for p in parts:
        if not p.strip():
            continue
        caption = normalize_for_caption(p.strip())
        tokens = normalize_inline(p.strip()).split() or [p.strip()]
        out.append((caption, tokens))
    return out


def _compute_sentence_timings(
    script: Script,
    beat_durations: list[float],
    lead_s: float = 0.0,
) -> list[tuple[str, float, float]]:
    out: list[tuple[str, float, float]] = []
    beat_start = 0.0
    for i, beat in enumerate(script.beats):
        sentences = _split_for_video(beat.narration)
        if not sentences:
            sentences = [(normalize_for_caption(beat.on_screen), beat.on_screen.split() or ["x"])]
        counts = [max(1, len(toks)) for _, toks in sentences]
        total_words = sum(counts)
        beat_dur = beat_durations[i]
        cursor = beat_start
        for j, ((caption, _toks), wc) in enumerate(zip(sentences, counts)):
            if j == len(sentences) - 1:
                end = beat_start + beat_dur
            else:
                end = cursor + beat_dur * (wc / total_words)
            shifted_start = max(0.0, cursor - lead_s)
            out.append((caption, round(shifted_start, 3), round(end, 3)))
            cursor = end
        beat_start += beat_dur
    return out


def _compute_image_segments(
    script: Script, beat_durations: list[float]
) -> list[tuple[int, float]]:
    """Map each image to a (global_index, duration) sub-segment.

    Images are numbered in flat beat order — matching Script.image_prompts and
    the PNG filenames written by the image stage — so the returned segments
    cover 0..N-1 exactly once. A beat's duration is split evenly among its
    images.
    """
    out: list[tuple[int, float]] = []
    global_idx = 0
    for i, beat in enumerate(script.beats):
        n = len(beat.images)
        if n == 0:
            raise ValueError(f"beat {i} has no images")
        seg_dur = beat_durations[i] / n
        for _ in range(n):
            out.append((global_idx, round(seg_dur, 3)))
            global_idx += 1
    return out


# ─── Rendering ──────────────────────────────────────────────────────────────


def _render_title_png(text: str, font_path: Path, out_path: Path) -> tuple[int, int]:
    pad_x, pad_y, shadow_off = 24, 16, 8
    target_text_w = TITLE_MAX_WIDTH - 2 * pad_x - shadow_off
    line_gap_factor = 1.05

    font = ImageFont.truetype(str(font_path), TITLE_FONT_SIZE)
    raw_tw = font.getbbox(text)[2] - font.getbbox(text)[0]
    if raw_tw <= target_text_w:
        size = TITLE_FONT_SIZE
        lines = [text]
    else:
        scaled = int(TITLE_FONT_SIZE * target_text_w / raw_tw)
        if scaled >= TITLE_FONT_SIZE_MIN:
            size = scaled
            font = ImageFont.truetype(str(font_path), size)
            lines = [text]
        else:
            size = TITLE_FONT_SIZE_MIN
            font = ImageFont.truetype(str(font_path), size)
            lines = _wrap_words(text.split(), font, target_text_w) or [text]

    bboxes = [font.getbbox(line) for line in lines]
    line_widths = [int(b[2] - b[0]) for b in bboxes]
    text_w = max(line_widths)
    line_h = int(size * line_gap_factor)
    text_h = line_h * len(lines)

    w = text_w + pad_x * 2 + shadow_off
    h = text_h + pad_y * 2 + shadow_off
    img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    for i, line in enumerate(lines):
        bbox = bboxes[i]
        x = (text_w - line_widths[i]) // 2 + pad_x - bbox[0]
        y = pad_y + i * line_h - bbox[1]
        d.text(
            (x + shadow_off, y + shadow_off),
            line, font=font, fill=(0, 0, 0, 200),
            stroke_width=6, stroke_fill=(0, 0, 0, 200),
        )
        d.text(
            (x, y),
            line, font=font, fill=(255, 255, 255, 255),
            stroke_width=6, stroke_fill=(0, 0, 0, 240),
        )
    img.save(out_path)
    return w, h


def _wrap_words(words: list[str], font: ImageFont.FreeTypeFont, max_width: int) -> list[str]:
    lines: list[str] = []
    current: list[str] = []
    for word in words:
        candidate = " ".join(current + [word])
        bbox = font.getbbox(candidate)
        width = bbox[2] - bbox[0]
        if width <= max_width or not current:
            current.append(word)
        else:
            lines.append(" ".join(current))
            current = [word]
    if current:
        lines.append(" ".join(current))
    return lines


def _render_sentence_png(text: str, font_path: Path, out_path: Path) -> tuple[int, int]:
    font = ImageFont.truetype(str(font_path), CAPTION_FONT_SIZE)
    pad_x, pad_y, radius = 32, 22, 26
    line_gap_factor = 1.18

    lines = _wrap_words(text.split(), font, CAPTION_MAX_TEXT_WIDTH)
    if not lines:
        lines = [text]

    line_h = int(CAPTION_FONT_SIZE * line_gap_factor)
    line_widths = [int(font.getbbox(line)[2] - font.getbbox(line)[0]) for line in lines]
    text_w = max(line_widths)
    text_h = line_h * len(lines)
    w = text_w + pad_x * 2
    h = text_h + pad_y * 2

    img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((0, 0, w - 1, h - 1), radius=radius, fill=(0, 0, 0, 145))
    for i, line in enumerate(lines):
        bbox = font.getbbox(line)
        lw = int(bbox[2] - bbox[0])
        x = (w - lw) // 2
        y = pad_y + i * line_h - bbox[1]
        d.text(
            (x, y),
            line, font=font, fill=(255, 255, 255, 255),
            stroke_width=4, stroke_fill=(0, 0, 0, 240),
        )
    img.save(out_path)
    return w, h


def _draw_bell(d: ImageDraw.ImageDraw, cx: int, cy: int, size: int, fill) -> None:
    """Simple bell glyph drawn with primitives. cx,cy is the visual center."""
    # Bell body — arch shape via pieslice + rectangle
    half = size // 2
    arch_top = cy - half
    arch_bottom = cy + size // 4
    # arch
    d.pieslice(
        (cx - half, arch_top, cx + half, arch_top + size),
        start=180, end=360, fill=fill,
    )
    d.rectangle(
        (cx - half, arch_top + half, cx + half, arch_bottom),
        fill=fill,
    )
    # rim (slightly wider)
    rim_w = int(size * 0.62)
    rim_h = int(size * 0.10)
    d.rounded_rectangle(
        (cx - rim_w, arch_bottom, cx + rim_w, arch_bottom + rim_h),
        radius=rim_h // 2, fill=fill,
    )
    # clapper
    d.ellipse(
        (cx - rim_h // 2, arch_bottom + rim_h + 2,
         cx + rim_h // 2, arch_bottom + rim_h * 3 + 2),
        fill=fill,
    )
    # top dot (handle)
    d.ellipse(
        (cx - rim_h // 2, arch_top - rim_h, cx + rim_h // 2, arch_top),
        fill=fill,
    )


def _render_outro_png(font_path: Path, out_path: Path) -> None:
    """Branded end-card shown during the CTA audio.

    Design echoes the CTA narration ("Hey!… like, share, subscribe… bell…
    Thanks for the support"): a warm closing header, a big SUBSCRIBE pill,
    three smaller LIKE / SHARE / BELL pills, and a 'never miss an upload'
    subtitle. Same across every short for brand consistency. 1080×1920.
    """
    W, H = OUTPUT_W, OUTPUT_H
    img = Image.new("RGB", (W, H), (40, 30, 25))
    d = ImageDraw.Draw(img)

    # Vertical earth-tone gradient
    for y in range(H):
        t = y / H
        r = int(56 + (32 - 56) * t)
        g = int(44 + (24 - 44) * t)
        b = int(36 + (20 - 36) * t)
        d.line([(0, y), (W, y)], fill=(r, g, b))

    # Header
    h_font = ImageFont.truetype(str(font_path), 100)
    header = "Thanks for watching!"
    bb = h_font.getbbox(header)
    hw = int(bb[2] - bb[0])
    d.text(
        ((W - hw) / 2 - bb[0], int(H * 0.10) - bb[1]),
        header, font=h_font, fill=(255, 245, 220),
        stroke_width=4, stroke_fill=(0, 0, 0),
    )

    # Big SUBSCRIBE pill — primary action
    btn_w, btn_h = 820, 210
    btn_x = (W - btn_w) // 2
    btn_y = int(H * 0.22)
    d.rounded_rectangle(
        (btn_x, btn_y, btn_x + btn_w, btn_y + btn_h),
        radius=56, fill=(204, 0, 0),
    )
    s_font = ImageFont.truetype(str(font_path), 102)
    s = "SUBSCRIBE"
    bb = s_font.getbbox(s)
    sw, sh = int(bb[2] - bb[0]), int(bb[3] - bb[1])
    d.text(
        (btn_x + (btn_w - sw) // 2 - bb[0],
         btn_y + (btn_h - sh) // 2 - bb[1]),
        s, font=s_font, fill=(255, 255, 255),
    )

    # Three smaller action pills: LIKE / SHARE / BELL (bell pill has icon)
    pills_y = int(H * 0.43)
    pill_h = 130
    pill_gap = 36
    pill_font = ImageFont.truetype(str(font_path), 60)

    pill_defs = [
        ("LIKE",  (52, 122, 200), False),  # blue
        ("SHARE", (60, 160, 100), False),  # green
        ("BELL",  (200, 150, 50), True),   # amber + bell glyph
    ]

    # Pre-measure widths
    pill_widths: list[int] = []
    for lbl, _c, has_icon in pill_defs:
        bb = pill_font.getbbox(lbl)
        text_w = int(bb[2] - bb[0])
        extra = 80 if has_icon else 0
        pill_widths.append(text_w + 60 + extra)
    total_w = sum(pill_widths) + pill_gap * (len(pill_defs) - 1)
    x_cursor = (W - total_w) // 2

    for (lbl, color, has_icon), pw in zip(pill_defs, pill_widths):
        d.rounded_rectangle(
            (x_cursor, pills_y, x_cursor + pw, pills_y + pill_h),
            radius=40, fill=color, outline=(255, 255, 255), width=4,
        )
        bb = pill_font.getbbox(lbl)
        text_w = int(bb[2] - bb[0])
        text_h = int(bb[3] - bb[1])
        content_w = text_w + (60 if has_icon else 0)  # icon size = 60 here
        inner_x = x_cursor + (pw - content_w) // 2
        if has_icon:
            _draw_bell(d, inner_x + 30, pills_y + pill_h // 2, 56, (255, 255, 255))
            text_x = inner_x + 60 - bb[0] + 8
        else:
            text_x = inner_x - bb[0]
        d.text(
            (text_x, pills_y + (pill_h - text_h) // 2 - bb[1]),
            lbl, font=pill_font, fill=(255, 255, 255),
            stroke_width=2, stroke_fill=(0, 0, 0),
        )
        x_cursor += pw + pill_gap

    # No bottom tagline — the long live-caption CTA wraps to ~6 lines and
    # would overlap. SUBSCRIBE + the three pills carry the visual CTA;
    # the caption carries the spoken version.

    img.save(out_path)


# ─── Assembly ───────────────────────────────────────────────────────────────


def assemble_video(
    script: Script,
    image_paths: list[Path],
    audio_path: Path,
    output_path: Path,
    cta: str | None = None,
) -> dict:
    n_images = len(script.image_prompts)
    if len(image_paths) != n_images:
        raise ValueError(
            f"got {len(image_paths)} image paths, expected {n_images}"
        )
    for p in image_paths + [audio_path]:
        if not p.exists():
            raise FileNotFoundError(p)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    font_path = _pick_font()
    total_s = _wav_duration_s(audio_path)

    # Split total audio into story vs CTA by relative word counts.
    # Then absorb `outro_pause_s` into the story side so the outro card
    # appears slightly later than pure-proportional would put it — this
    # gives viewers a beat to process the last narration line before the
    # SUBSCRIBE card hits.
    cta_text = (settings.outro_cta if cta is None else cta).strip()
    cta_words = len(cta_text.split()) if cta_text else 0
    story_words = sum(len(b.narration.split()) for b in script.beats)
    total_words = max(1, story_words + cta_words)
    cta_dur_raw = total_s * (cta_words / total_words) if cta_text else 0.0
    cta_dur = round(max(0.0, cta_dur_raw - settings.outro_pause_s), 3)
    story_dur = round(total_s - cta_dur, 3)

    beat_durs = _compute_beat_durations(script, story_dur)
    sent_timings = _compute_sentence_timings(
        script, beat_durs, lead_s=settings.caption_lead_s
    )
    if cta_text:
        sent_timings.append(
            (normalize_for_caption(cta_text), round(story_dur, 3), round(total_s, 3))
        )

    segments = _compute_image_segments(script, beat_durs)  # narration segments only
    log.info(
        "story=%.2f cta=%.2f beats=%s sentences=%d segments=%d",
        story_dur, cta_dur, beat_durs, len(sent_timings), len(segments),
    )

    with tempfile.TemporaryDirectory(prefix="shorts-asm-") as tmp:
        tmp_dir = Path(tmp)
        title_png = tmp_dir / "title.png"
        title_w, _ = _render_title_png(script.title_text, font_path, title_png)

        # Render outro card (when CTA enabled)
        outro_png = tmp_dir / "outro.png" if cta_text else None
        if outro_png is not None:
            _render_outro_png(font_path, outro_png)

        # Sentence PNGs
        sent_pngs: list[Path] = []
        sent_sizes: list[tuple[int, int]] = []
        for i, (text, _start, _end) in enumerate(sent_timings):
            p = tmp_dir / f"sent_{i:02d}.png"
            sw, sh = _render_sentence_png(text, font_path, p)
            sent_pngs.append(p)
            sent_sizes.append((sw, sh))

        title_x = (OUTPUT_W - title_w) // 2
        title_y = int(OUTPUT_H * 0.06)
        cap_bottom_offset = int(OUTPUT_H * 0.16)
        kb_strength = 0.0008

        def kb_filter() -> str:
            return (
                f"scale=-1:{OUTPUT_H * 2}:flags=lanczos,"
                f"zoompan=z='min(zoom+{kb_strength},1.15)':d=1:"
                f"x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':s={OUTPUT_W}x{OUTPUT_H}:fps={FPS},"
                f"setsar=1"
            )

        def static_filter() -> str:
            # Outro card already 1080×1920 — just pass through at FPS, no zoom.
            return f"scale={OUTPUT_W}:{OUTPUT_H}:flags=lanczos,setsar=1,fps={FPS}"

        # Input layout:
        #   0 .. N-1                  : narration images
        #   N (if outro)              : outro.png
        #   N (or N+1 if outro)       : audio
        #   next                      : title.png
        #   next                      : sentence PNGs
        N = n_images
        has_outro = outro_png is not None
        outro_input_idx = N if has_outro else -1
        audio_input = N + (1 if has_outro else 0)
        title_input = audio_input + 1
        sent_input_start = title_input + 1

        # Story segment durations (per image input). Segments are numbered in
        # flat beat order and cover every image exactly once, so img_dur is
        # dense over 0..N-1; guard that invariant rather than risk a KeyError.
        img_dur: dict[int, float] = {idx: dur for idx, dur in segments}
        if len(img_dur) != N:
            raise ValueError(
                f"image/segment mismatch: {N} images but {len(img_dur)} segments"
            )

        chains: list[str] = []
        seg_labels: list[str] = []

        # Narration segments: each img_idx scaled+kb
        for seg_pos, (img_idx, _seg_dur) in enumerate(segments):
            label = f"seg{seg_pos}"
            chains.append(f"[{img_idx}:v]{kb_filter()}[{label}]")
            seg_labels.append(label)

        # Outro segment: outro PNG → static_filter, appended last
        if has_outro:
            label = f"seg{len(seg_labels)}"
            chains.append(f"[{outro_input_idx}:v]{static_filter()}[{label}]")
            seg_labels.append(label)

        # Concat
        concat_inputs = "".join(f"[{lbl}]" for lbl in seg_labels)
        chains.append(f"{concat_inputs}concat=n={len(seg_labels)}:v=1:a=0[concatv]")

        # Title overlay — visible only during narration. Hides during the
        # outro card so the SUBSCRIBE frame reads cleanly.
        title_end = story_dur if cta_text else total_s
        chains.append(
            f"[concatv][{title_input}:v]overlay=x={title_x}:y={title_y}:"
            f"enable='between(t,0,{title_end:.3f})'[withtitle]"
        )

        # Sentence overlays
        current_label = "withtitle"
        for i, ((sw, sh), (_text, start, end)) in enumerate(zip(sent_sizes, sent_timings)):
            x = (OUTPUT_W - sw) // 2
            y = OUTPUT_H - cap_bottom_offset - sh
            next_label = "outv" if i == len(sent_timings) - 1 else f"c{i}"
            chains.append(
                f"[{current_label}][{sent_input_start + i}:v]"
                f"overlay=x={x}:y={y}:enable='between(t,{start:.3f},{end:.3f})'"
                f"[{next_label}]"
            )
            current_label = next_label

        filter_complex = ";".join(chains)

        # Build ffmpeg command
        cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error"]
        for i in range(N):
            cmd += [
                "-loop", "1",
                "-framerate", str(FPS),
                "-t", f"{img_dur[i]}",
                "-i", str(image_paths[i]),
            ]
        if has_outro:
            cmd += [
                "-loop", "1",
                "-framerate", str(FPS),
                "-t", f"{cta_dur}",
                "-i", str(outro_png),
            ]
        cmd += ["-i", str(audio_path), "-i", str(title_png)]
        for p in sent_pngs:
            cmd += ["-i", str(p)]
        cmd += [
            "-filter_complex", filter_complex,
            "-map", "[outv]",
            "-map", f"{audio_input}:a",
            "-c:v", "libx264",
            "-preset", "medium",
            "-crf", "20",
            "-pix_fmt", "yuv420p",
            "-r", str(FPS),
            "-c:a", "aac",
            "-b:a", "192k",
            "-movflags", "+faststart",
            "-shortest",
            str(output_path),
        ]

        log.info(
            "ffmpeg run | narration_images=%d outro=%s sentences=%d segments=%d args=%d",
            N, has_outro, len(sent_pngs), len(seg_labels), len(cmd),
        )
        proc = subprocess.run(cmd, capture_output=True, text=True)
        if proc.returncode != 0:
            log.error("ffmpeg stderr: %s", proc.stderr[-4000:])
            raise RuntimeError(f"ffmpeg failed: {proc.stderr[-500:]}")

    size = output_path.stat().st_size
    log.info("assembled → %s (%.1f MB, %.2fs)", output_path, size / 1e6, total_s)
    return {
        "video_path": str(output_path),
        "duration_s": round(total_s, 2),
        "size_bytes": size,
        "width": OUTPUT_W,
        "height": OUTPUT_H,
        "fps": FPS,
        "beat_durations_s": beat_durs,
        "sentence_count": len(sent_timings),
        "image_count": N,
        "segment_count": len(seg_labels),
        "story_duration_s": story_dur,
        "outro_duration_s": cta_dur,
        "font": str(font_path),
    }
