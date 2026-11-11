import { useEffect, useState } from "react";
import { fetchBlob } from "./api";

/** Loads an engine file and gives back an object URL for <video> or <img>. */
export function useBlobUrl(path: string | null, version: unknown = 0) {
  const [url, setUrl] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    setUrl(null);
    setError(null);
    if (!path) return;
    let cancelled = false;
    let created: string | null = null;
    fetchBlob(path)
      .then((blob) => {
        if (cancelled) return;
        created = URL.createObjectURL(blob);
        setUrl(created);
      })
      .catch((err: Error) => {
        if (!cancelled) setError(err.message);
      });
    return () => {
      cancelled = true;
      if (created) URL.revokeObjectURL(created);
    };
  }, [path, version]);

  return { url, error };
}
