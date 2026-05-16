// --- Star Rating (Trade Form) ---
document.addEventListener('DOMContentLoaded', function() {
  document.querySelectorAll('.trade-form').forEach(form => {
    const stars = form.querySelectorAll('input[name="trade_rating"]');
    if (!stars.length) return;
    const updateStars = () => {
      let val = 0;
      stars.forEach((radio, i) => {
        if (radio.checked) val = i + 1;
      });
      stars.forEach((radio, i) => {
        const star = radio.nextElementSibling;
        if (star) star.style.color = (i < val) ? 'gold' : '#ccc';
      });
    };
    stars.forEach(radio => {
      radio.addEventListener('change', updateStars);
      // Also allow clicking the star itself
      const star = radio.nextElementSibling;
      if (star) {
        star.addEventListener('click', () => {
          radio.checked = true;
          radio.dispatchEvent(new Event('change', { bubbles: true }));
        });
      }
    });
    updateStars();
  });
});
// Modal handlers
const modal = document.getElementById('modal');
document.getElementById('quick-add')?.addEventListener('click', e => {
  e.preventDefault(); modal.classList.remove('hidden');
});
document.getElementById('modal-close')?.addEventListener('click', () => modal.classList.add('hidden'));
modal?.addEventListener('click', e => { if (e.target === modal) modal.classList.add('hidden'); });

// Theme toggle
document.getElementById('toggle-theme')?.addEventListener('click', async e => {
  e.preventDefault();
  const html = document.documentElement;
  const newTheme = html.dataset.theme === 'dark' ? 'light' : 'dark';
  html.dataset.theme = newTheme;
  // persist via settings form submit (hacky simple method)
  const fd = new FormData();
  ['starting_capital','daily_loss_limit_pct','max_trades_per_day','max_consecutive_losses','monthly_goal']
    .forEach(k => fd.append(k, document.querySelector(`[name="${k}"]`)?.value || ''));
  fd.append('theme', newTheme);
  // Simple persist: navigate to settings page in background isn't great; use localStorage as cache
  localStorage.setItem('theme', newTheme);
  // Send to server
  await fetch('/settings', { method: 'POST', body: new URLSearchParams({ theme: newTheme,
    starting_capital: '', daily_loss_limit_pct:'', max_trades_per_day:'',
    max_consecutive_losses:'', monthly_goal:'' }) }).catch(()=>{});
  // Reload to pick up server-rendered theme
  location.reload();
});

// Keyboard shortcuts
let pendingG = false;
