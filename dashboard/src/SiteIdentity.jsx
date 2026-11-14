import { useId, useRef, useState } from 'react';
import Icon from './Icon.jsx';

export function Brand({ href = '/', tagline = false }) {
  return <a className="brand" href={href} aria-label={tagline ? 'rinkside. THE FORECAST DESK' : 'rinkside.'}><span className="brand-mark" aria-hidden="true"><Icon name="rink" size={27} /></span><span>rinkside<span className="brand-dot">.</span>{tagline && <small>THE FORECAST DESK</small>}</span></a>;
}

export function SiteFooter() {
  return <footer className="site-footer"><span><img src="/rink.svg" width="20" height="20" alt="" /> rinkside. <span>A clearer view of the ice.</span></span><span>Created by Louis Polly</span></footer>;
}

export function Help({ label, children }) {
  const id = useId();
  const trigger = useRef(null);
  const [open, setOpen] = useState(false);
  const [position, setPosition] = useState({});
  const show = () => { const rect = trigger.current.getBoundingClientRect(); setPosition({ left: Math.max(8, Math.min(rect.left, window.innerWidth - Math.min(260, window.innerWidth * .65) - 8)), top: Math.min(rect.bottom, window.innerHeight - 160) }); setOpen(true); };
  return <span className="help" onMouseEnter={show} onMouseLeave={e => { if (!e.currentTarget.contains(document.activeElement)) setOpen(false); }} onBlur={e => { if (!e.currentTarget.contains(e.relatedTarget)) setOpen(false); }} onKeyDown={e => { if (e.key === 'Escape') { setOpen(false); e.stopPropagation(); } }}>
    <button ref={trigger} type="button" className="help-trigger" aria-label={`About ${label}`} aria-describedby={open ? id : undefined} aria-expanded={open} onFocus={show} onClick={show}>?</button>
    <span id={id} role="tooltip" className="help-content" style={position} hidden={!open}>{children}</span>
  </span>;
}
