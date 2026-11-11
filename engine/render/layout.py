"""ffmpeg filter graphs that turn a landscape stream into a 1080x1920 clip.

Two layouts:
- "blur": the whole frame, full width, centred over a blurred copy of itself.
- "split": the streamer's facecam on top, the gameplay (centre-cropped) below. Used when
  the user has marked where the facecam is.
"""

from __future__ import annotations

OUT_W, OUT_H = 1080, 1920
_MIN_CAM_H, _MAX_CAM_H = 420, 860


def _even(value: float) -> int:
    return max(2, int(round(value / 2)) * 2)


def valid_facecam(layout: dict | None) -> dict | None:
    """The facecam rectangle as fractions of the frame, if the stored layout is usable."""
    cam = (layout or {}).get("facecam")
    if not isinstance(cam, dict):
        return None
    try:
        x, y, w, h = (float(cam[key]) for key in ("x", "y", "w", "h"))
    except (KeyError, TypeError, ValueError):
        return None
    if not (0 <= x < 1 and 0 <= y < 1 and 0.05 <= w <= 1 and 0.05 <= h <= 1):
        return None
    w, h = min(w, 1 - x), min(h, 1 - y)
    if w < 0.05 or h < 0.05:
        return None
    return {"x": x, "y": y, "w": w, "h": h}


def build_filter(src_w: int, src_h: int, layout: dict | None, captions_file: str | None) -> str:
    """A filter_complex string reading [0:v] and producing [v]."""
    cam = valid_facecam(layout)
    if cam is None:
        fg_h = min(OUT_H, _even(OUT_W * src_h / src_w))
        graph = (
            "[0:v]split=2[bg][fg];"
            # Blur a small copy: far cheaper than blurring at full size, same look.
            f"[bg]scale=270:480:force_original_aspect_ratio=increase,crop=270:480,"
            f"boxblur=8:2,scale={OUT_W}:{OUT_H},setsar=1[bgb];"
            f"[fg]scale={OUT_W}:{fg_h},setsar=1[fgs];"
            "[bgb][fgs]overlay=(W-w)/2:(H-h)/2[base]"
        )
    else:
        cw, ch = _even(cam["w"] * src_w), _even(cam["h"] * src_h)
        cx, cy = int(cam["x"] * src_w), int(cam["y"] * src_h)
        cw, ch = min(cw, _even(src_w - cx - 1)), min(ch, _even(src_h - cy - 1))
        cam_h = min(_MAX_CAM_H, max(_MIN_CAM_H, _even(OUT_W * ch / cw)))
        game_h = OUT_H - cam_h
        graph = (
            "[0:v]split=2[a][b];"
            f"[a]crop={cw}:{ch}:{cx}:{cy},"
            f"scale={OUT_W}:{cam_h}:force_original_aspect_ratio=increase,"
            f"crop={OUT_W}:{cam_h},setsar=1[cam];"
            f"[b]scale=-2:{game_h},crop={OUT_W}:{game_h},setsar=1[game];"
            "[cam][game]vstack=inputs=2[base]"
        )
    if captions_file:
        return graph + f";[base]ass={captions_file}[v]"
    return graph + ";[base]null[v]"
