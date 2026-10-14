/* Admin navigation only. Content, authentication and saves keep their handlers. */
(function () {
  'use strict';

  document.addEventListener('DOMContentLoaded', function () {
    var root = document.querySelector('#admin-form .admin-workspace');
    if (!root) return;
    var nav = root.querySelector('.atelier-nav');
    var buttons = Array.from(nav.querySelectorAll('[data-atelier]'));
    var screens = Array.from(root.querySelectorAll('.atelier-view'));

    function revealNavigationItem(button) {
      if (nav.scrollWidth <= nav.clientWidth) return;
      var item = button.getBoundingClientRect();
      var bounds = nav.getBoundingClientRect();
      if (item.left < bounds.left) nav.scrollLeft -= bounds.left - item.left + 8;
      else if (item.right > bounds.right) nav.scrollLeft += item.right - bounds.right + 8;
    }

    function open(key) {
      var screen = screens.find(function (item) { return item.id === 'atelier-' + key; });
      var selected = buttons.find(function (button) { return button.dataset.atelier === key; });
      if (!screen || !selected) return;

      screens.forEach(function (item) { item.hidden = item !== screen; });
      buttons.forEach(function (button) {
        if (button === selected) button.setAttribute('aria-current', 'page');
        else button.removeAttribute('aria-current');
      });
      revealNavigationItem(selected);

      var heading = screen.querySelector('.atelier-heading h2');
      if (heading) {
        heading.tabIndex = -1;
        heading.focus({ preventScroll: true });
        var bounds = heading.getBoundingClientRect();
        var top = document.querySelector('header').getBoundingClientRect().bottom;
        if (getComputedStyle(nav).display === 'flex') top = nav.getBoundingClientRect().bottom;
        if (bounds.top < top || bounds.bottom > innerHeight) heading.scrollIntoView({ block: 'start' });
      }

      // Keep the existing lazy loading and unsaved-JSON protections.
      if (key === 'proba' && window.loadProbaSimOnce) window.loadProbaSimOnce();
      if (key === 'qcm' && window.contentAdmin && !window.contentAdmin.hasDraft() && !window.qcmSaving) window.loadQcmEditor();
      if (key === 'ankimie' && window.loadAnkimieStats) window.loadAnkimieStats();
    }

    buttons.forEach(function (button) {
      button.addEventListener('click', function () { open(button.dataset.atelier); });
    });
    root.querySelectorAll('[data-admin-open]').forEach(function (button) {
      button.addEventListener('click', function () { open(button.dataset.adminOpen);
        var screen = root.querySelector('#atelier-' + button.dataset.adminOpen);
        if (button.dataset.adminPane) {
          var tab = screen.querySelector('[data-subtab="' + button.dataset.adminPane + '"]');
          if (tab) tab.click();
        }
        if (button.dataset.adminSetting) {
          var section = screen.querySelector('[data-setting-section="' + button.dataset.adminSetting + '"]');
          if (section) {
            section.open = true;
            section.querySelector('summary').focus({ preventScroll: true });
            section.scrollIntoView({ block: 'start' });
          }
        }
      });
    });

    root.querySelectorAll('.subtab-bar').forEach(function (bar) {
      var screen = bar.closest('.atelier-view');
      var tabs = Array.from(bar.querySelectorAll('[data-subtab]'));
      function select(tab) {
        tabs.forEach(function (item) {
          item.setAttribute('aria-selected', item === tab ? 'true' : 'false');
          item.tabIndex = item === tab ? 0 : -1;
        });
        screen.querySelectorAll(':scope > .subtab-panel').forEach(function (panel) {
          panel.hidden = panel.dataset.pane !== tab.dataset.subtab;
        });
      }
      tabs.forEach(function (tab) {
        tab.tabIndex = tab.getAttribute('aria-selected') === 'true' ? 0 : -1;
        tab.addEventListener('click', function () { select(tab); });
      });
      bar.addEventListener('keydown', function (event) {
        var index = tabs.indexOf(event.target), next;
        if (index < 0 || event.altKey || event.ctrlKey || event.metaKey) return;
        if (event.key === 'ArrowRight') next = (index + 1) % tabs.length;
        else if (event.key === 'ArrowLeft') next = (index + tabs.length - 1) % tabs.length;
        else if (event.key === 'Home') next = 0;
        else if (event.key === 'End') next = tabs.length - 1;
        else return;
        event.preventDefault(); event.stopPropagation();
        select(tabs[next]); tabs[next].focus();
      });
    });

    nav.addEventListener('keydown', function (event) {
      var index = buttons.indexOf(event.target);
      if (index < 0 || event.altKey || event.ctrlKey || event.metaKey) return;
      var next;
      if (event.key === 'Home') next = 0;
      else if (event.key === 'End') next = buttons.length - 1;
      else if (event.key === 'ArrowDown' || event.key === 'ArrowRight') next = (index + 1) % buttons.length;
      else if (event.key === 'ArrowUp' || event.key === 'ArrowLeft') next = (index + buttons.length - 1) % buttons.length;
      else return;
      event.preventDefault();
      // The legacy file picker also listens for arrow keys on document.
      event.stopPropagation();
      buttons[next].focus();
      revealNavigationItem(buttons[next]);
    });
  });
})();
