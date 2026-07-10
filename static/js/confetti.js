/**
 * confetti.js — tiny dependency-free celebration burst.
 *
 * spawnConfetti(x, y, count) drops `count` small colored particles at the
 * viewport coordinates (x, y); they fan out, fall under gravity, fade, and
 * clean themselves up. Used for small "nice!" moments (completing a task,
 * finishing a checklist). No canvas, no libraries — just short-lived DOM
 * nodes animated with requestAnimationFrame.
 */

const _COLORS = ['#7cc4ff', '#9d7cff', '#5be6b0', '#ffd166', '#ff7c9d', '#ff9d5b'];

export function spawnConfetti(x, y, count = 40) {
  if (typeof document === 'undefined') return;
  // Respect reduced-motion — skip the animation entirely.
  try {
    if (window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches) return;
  } catch (_) {}

  const layer = document.createElement('div');
  layer.style.cssText =
    'position:fixed;left:0;top:0;width:0;height:0;pointer-events:none;z-index:2147483647;';
  document.body.appendChild(layer);

  const particles = [];
  const n = Math.max(1, Math.min(200, count | 0));
  for (let i = 0; i < n; i++) {
    const el = document.createElement('div');
    const size = 5 + Math.random() * 5;
    const color = _COLORS[(Math.random() * _COLORS.length) | 0];
    const round = Math.random() < 0.5;
    el.style.cssText =
      `position:fixed;left:${x}px;top:${y}px;width:${size}px;height:${size}px;` +
      `background:${color};border-radius:${round ? '50%' : '2px'};` +
      'will-change:transform,opacity;';
    layer.appendChild(el);
    const angle = Math.random() * Math.PI * 2;
    const speed = 3 + Math.random() * 7;
    particles.push({
      el,
      vx: Math.cos(angle) * speed,
      vy: Math.sin(angle) * speed - (4 + Math.random() * 4),
      x: 0, y: 0,
      rot: Math.random() * 360,
      vrot: (Math.random() - 0.5) * 24,
      life: 1,
    });
  }

  const GRAVITY = 0.35;
  const DRAG = 0.98;
  let raf = 0;
  const start = performance.now();
  function step(now) {
    const elapsed = now - start;
    let alive = false;
    for (const p of particles) {
      if (p.life <= 0) continue;
      alive = true;
      p.vy += GRAVITY;
      p.vx *= DRAG;
      p.x += p.vx;
      p.y += p.vy;
      p.rot += p.vrot;
      p.life = Math.max(0, 1 - elapsed / 1100);
      p.el.style.transform = `translate(${p.x}px, ${p.y}px) rotate(${p.rot}deg)`;
      p.el.style.opacity = String(p.life);
    }
    if (alive && elapsed < 1200) {
      raf = requestAnimationFrame(step);
    } else {
      cancelAnimationFrame(raf);
      layer.remove();
    }
  }
  raf = requestAnimationFrame(step);
}

export default { spawnConfetti };
