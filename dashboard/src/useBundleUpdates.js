import { useEffect, useState } from 'react';
import { fetchJSON } from './data.js';

// Poll our saved API, never NHL. Hidden tabs pause work; returning checks now.
export function useBundleUpdates(hash, onChange) {
  useEffect(() => {
    if (!hash) return;
    const controller = new AbortController();
    let busy = false;
    async function check() {
      if (document.hidden || busy) return;
      busy = true;
      try {
        const metadata = await fetchJSON('/api/metadata', controller.signal);
        if (!controller.signal.aborted && metadata.bundle_sha256 !== hash) onChange();
      } catch { /* Existing data remains usable; explicit refresh handles outages. */ }
      finally { busy = false; }
    }
    const timer = setInterval(check, 60000);
    document.addEventListener('visibilitychange', check);
    return () => { clearInterval(timer); controller.abort(); document.removeEventListener('visibilitychange', check); };
    // hash is the version being watched; onChange only increments a revision.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [hash]);
}

export function useMinuteTick() {
  const [tick, setTick] = useState(0);
  useEffect(() => {
    const update = () => { if (!document.hidden) setTick(t => t + 1); };
    const timer = setInterval(update, 60000);
    document.addEventListener('visibilitychange', update);
    return () => { clearInterval(timer); document.removeEventListener('visibilitychange', update); };
  }, []);
  return tick;
}
