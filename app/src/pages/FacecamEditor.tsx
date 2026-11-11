import { useRef, useState, type PointerEvent } from "react";
import { api, type Facecam, type Streamer } from "../api";
import { useBlobUrl } from "../useBlobUrl";

const MIN = 0.05;

/** Drag a box over a recent frame of the stream to mark where the facecam is. */
export function FacecamEditor({
  streamer,
  onClose,
  onSaved,
}: {
  streamer: Streamer;
  onClose: () => void;
  onSaved: () => void;
}) {
  const { url, error: frameError } = useBlobUrl(`/streamers/${streamer.id}/frame`);
  const [box, setBox] = useState<Facecam | null>(streamer.facecam);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const stage = useRef<HTMLDivElement>(null);
  const anchor = useRef<{ x: number; y: number } | null>(null);

  const point = (event: PointerEvent) => {
    const rect = stage.current!.getBoundingClientRect();
    return {
      x: Math.min(1, Math.max(0, (event.clientX - rect.left) / rect.width)),
      y: Math.min(1, Math.max(0, (event.clientY - rect.top) / rect.height)),
    };
  };

  const down = (event: PointerEvent) => {
    event.currentTarget.setPointerCapture(event.pointerId);
    anchor.current = point(event);
    setBox(null);
  };
  const move = (event: PointerEvent) => {
    if (!anchor.current) return;
    const p = point(event);
    const a = anchor.current;
    setBox({
      x: Math.min(a.x, p.x),
      y: Math.min(a.y, p.y),
      w: Math.abs(p.x - a.x),
      h: Math.abs(p.y - a.y),
    });
  };
  const up = () => {
    anchor.current = null;
    setBox((current) => (current && current.w >= MIN && current.h >= MIN ? current : null));
  };

  const save = async (facecam: Facecam | null) => {
    setBusy(true);
    setError(null);
    try {
      await api.updateStreamer(streamer.id, { layout: { facecam } });
      onSaved();
    } catch (err) {
      setError((err as Error).message);
      setBusy(false);
    }
  };

  return (
    <div className="modal-backdrop" role="dialog" aria-modal="true" aria-label="Facecam layout">
      <div className="modal">
        <h2>Facecam for {streamer.display_name ?? streamer.username}</h2>
        <p className="muted">
          Drag a box around the streamer's camera. Clips will show it on top with the gameplay
          below. With no box, clips show the whole frame over a blurred background.
        </p>
        {frameError ? (
          <div className="banner banner-warn">{frameError}</div>
        ) : !url ? (
          <p className="muted">Loading a frame…</p>
        ) : (
          <div
            className="crop-stage"
            ref={stage}
            onPointerDown={down}
            onPointerMove={move}
            onPointerUp={up}
          >
            <img src={url} alt="A recent frame of the stream" draggable={false} />
            {box && (
              <div
                className="crop-box"
                data-testid="crop-box"
                style={{
                  left: `${box.x * 100}%`,
                  top: `${box.y * 100}%`,
                  width: `${box.w * 100}%`,
                  height: `${box.h * 100}%`,
                }}
              />
            )}
          </div>
        )}
        {error && (
          <p className="error" role="alert">
            {error}
          </p>
        )}
        <div className="detail-actions">
          <button
            className="btn btn-primary"
            disabled={busy || !box || !!frameError}
            onClick={() => void save(box)}
          >
            Save facecam
          </button>
          <button className="btn" disabled={busy} onClick={() => void save(null)}>
            No facecam
          </button>
          <button className="btn" disabled={busy} onClick={onClose}>
            Cancel
          </button>
        </div>
      </div>
    </div>
  );
}
