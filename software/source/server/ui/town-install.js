/* The operator reviews an immutable preview before requesting a write. */
(() => {
  'use strict';
  const $ = id => document.getElementById('town-install-' + id);
  const esc = value => String(value ?? '').replace(/[&<>"']/g, c =>
    ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const section = (label, rows) => `<h3>${esc(label)} (${rows.length})</h3>${
    rows.length ? '<ul>' + rows.map(row => `<li>${esc(row)}</li>`).join('') + '</ul>' : '<p>None.</p>'}`;
  let snapshot, busy = false;

  function controls() {
    $('preview').disabled = busy;
    $('confirm').hidden = !snapshot?.confirmation;
    $('restore').hidden = snapshot?.receipt?.rollback_allowed !== true;
    $('apply').disabled = busy || !$('reviewed').checked || !snapshot?.confirmation;
    $('rollback').disabled = busy || !$('restore-reviewed').checked || !snapshot?.receipt?.rollback_allowed;
  }

  function render(view) {
    const receipt = view.receipt;
    $('evidence').innerHTML = `<dl class="ws-facts">
      <div><dt>Catalog revision</dt><dd>${esc(view.catalog_revision)}</dd></div>
      <div><dt>Source-world snapshot</dt><dd class="ws-diagnostics">${esc(view.world_snapshot_id)}</dd></div>
      <div><dt>Reachable spare cells after placement</dt><dd>${esc(view.capacity.remaining_cells)}</dd></div>
      <div><dt>Unresolved catalog aliases</dt><dd>${esc(view.alias_conflicts)}</dd></div>
      </dl>${section('Installation blockers', view.blocks)}
      ${section('Verified links', view.verified_links.map(row => `${row.name} (${row.character}) → ${row.resident}`))}
      ${section('Proposed NPC residents', view.placements.map(row => `${row.name} (${row.character_id}) · cell ${row.cell.join(', ')}`))}
      ${section('Unplaced residents', view.unplaced.map(row => `${row.name} (${row.character_id})`))}
      ${section('Unresolved residents kept unchanged', view.unresolved.map(row => `${row.name} (${row.id})`))}
      ${section('Ownership conflicts', view.conflicts.map(row => `${row.resident} → ${row.character} · ${row.reason}`))}
      ${view.invalid_cells.length ? section('Invalid source cells',
        view.invalid_cells.map(row => `Cell ${row.cell.join(', ')} · ${row.reason}`)) : ''}
      ${receipt ? `<h3>Installation receipt</h3><dl class="ws-facts ws-diagnostics">
        <div><dt>Batch</dt><dd>${esc(receipt.batch_id)}</dd></div>
        <div><dt>Pre-install backup</dt><dd>${esc(receipt.backup_id)}</dd></div>
        <div><dt>Batch state</dt><dd>${esc(receipt.state.replaceAll('_',' '))}</dd></div></dl>
        <p>${esc(receipt.rollback_block || 'Rollback is available while the saved world and tracked history remain unchanged.')}</p>` : ''}`;
  }

  window.FieldTownInstall = {
    init(call, refreshTown) {
      async function run(method, reference) {
        if (busy) return;
        const reading = method === 'get_town_install_preview';
        busy = true;
        snapshot = null;
        $('reviewed').checked = $('restore-reviewed').checked = false;
        $('evidence').replaceChildren();
        controls();
        $('status').textContent = reading ? 'Reading the saved world and catalog…' :
          method === 'confirm_town_installation' ? 'Verifying the preview, creating a backup and installing…' :
            'Checking and restoring the installation batch…';
        try {
          const result = reading ? await call(method) : await call(method, reference);
          if (result?.ok !== true) {
            $('status').textContent = result?.reason || 'The operation was not confirmed. Refresh before retrying.';
          } else if (reading) {
            snapshot = result;
            render(result);
            $('status').textContent = 'Preview ready. Town has not changed.';
          } else {
            $('status').textContent = result.status === 'installed' ?
              `Installed ${result.created} NPC residents. Batch ${result.batch_id}, backup ${result.backup_id}. Preview again to review rollback.` :
              result.status === 'rolled_back' ? 'Installation rolled back. Preview again to review the current state.' :
                'This batch was already processed. Preview again to check its receipt.';
            refreshTown();
          }
        } catch (_) {
          $('status').textContent = 'The operation could not be confirmed. Preview again to check for a receipt.';
        } finally {
          busy = false;
          controls();
        }
      }
      $('preview').onclick = () => run('get_town_install_preview');
      $('reviewed').onchange = $('restore-reviewed').onchange = controls;
      $('apply').onclick = () => {
        if (!$('apply').disabled) run('confirm_town_installation', snapshot.confirmation);
      };
      $('rollback').onclick = () => {
        if (!$('rollback').disabled) run('rollback_town_installation', snapshot.receipt.batch_id);
      };
    }
  };
})();
