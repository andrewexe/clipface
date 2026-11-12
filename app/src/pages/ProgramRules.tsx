import { useState } from "react";
import { api, type Streamer } from "../api";

const lines = (text: string) =>
  text
    .split(/[\n,]/)
    .map((item) => item.trim())
    .filter(Boolean);

/** A clipping program's rules: applied to every title check and description. */
export function ProgramRulesEditor({
  streamer,
  onClose,
  onSaved,
}: {
  streamer: Streamer;
  onClose: () => void;
  onSaved: () => void;
}) {
  const rules = streamer.program_rules;
  const [tags, setTags] = useState(rules.required_tags.join("\n"));
  const [credit, setCredit] = useState(rules.credit_format);
  const [banned, setBanned] = useState(rules.banned.join("\n"));
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const save = async () => {
    setBusy(true);
    setError(null);
    try {
      await api.updateStreamer(streamer.id, {
        program_rules: {
          required_tags: lines(tags),
          credit_format: credit.trim(),
          banned: lines(banned),
        },
      });
      onSaved();
    } catch (err) {
      setError((err as Error).message);
      setBusy(false);
    }
  };

  return (
    <div className="modal-backdrop" role="dialog" aria-modal="true" aria-label="Program rules">
      <div className="modal">
        <h2>Clipping rules for {streamer.display_name ?? streamer.username}</h2>
        <p className="muted">
          From the streamer's clipping program. These are applied to every clip of theirs
          automatically.
        </p>
        <div className="field">
          <label htmlFor="rules-tags">Required hashtags</label>
          <textarea
            id="rules-tags"
            rows={2}
            value={tags}
            placeholder="One per line, for example #ShroudClips"
            onChange={(event) => setTags(event.target.value)}
          />
          <span className="muted small">Added to the description when the caption lacks them.</span>
        </div>
        <div className="field">
          <label htmlFor="rules-credit">Credit line</label>
          <input
            id="rules-credit"
            value={credit}
            maxLength={200}
            placeholder="Clip from {name}: {url}"
            onChange={(event) => setCredit(event.target.value)}
          />
          <span className="muted small">
            Use {"{name}"} and {"{url}"}. The streamer's name and channel link are always
            included, even if you leave them out.
          </span>
        </div>
        <div className="field">
          <label htmlFor="rules-banned">Not allowed</label>
          <textarea
            id="rules-banned"
            rows={2}
            value={banned}
            placeholder="Words or topics, one per line"
            onChange={(event) => setBanned(event.target.value)}
          />
          <span className="muted small">
            A clip that mentions any of these is held for you instead of publishing.
          </span>
        </div>
        {error && (
          <p className="error" role="alert">
            {error}
          </p>
        )}
        <div className="detail-actions">
          <button className="btn btn-primary" disabled={busy} onClick={() => void save()}>
            Save rules
          </button>
          <button className="btn" disabled={busy} onClick={onClose}>
            Cancel
          </button>
        </div>
      </div>
    </div>
  );
}
