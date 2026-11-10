import { useEffect, useState } from 'react';

// Each resource clears on a new key and ignores responses from an old filter.
export function useResource(key, load) {
  const [state, setState] = useState({ key: null, data: null, error: null, loading: false });
  useEffect(() => {
    if (!key) return;
    const controller = new AbortController();
    let active = true;
    setState({ key, data: null, error: null, loading: true });
    load(controller.signal).then(data => {
      if (active) setState({ key, data, error: null, loading: false });
    }).catch(error => {
      if (active && error.name !== 'AbortError') setState({ key, data: null, error: error.message, loading: false });
    });
    return () => { active = false; controller.abort(); };
    // The key describes every input; loader identity intentionally isn't a fetch trigger.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key]);
  return state.key === key ? state : { key, data: null, error: null, loading: Boolean(key) };
}
